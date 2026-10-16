"""原生协议(**mini 版**)—— ``clickhouse-client`` 与 ``clickhouse-server`` 之间的那根线。

真实 ClickHouse 的客户端/服务端协议在 ``src/Client/`` 与
``src/Server/TCPHandler.cpp``,是**自定义的二进制协议**(不是 HTTP、不是 gRPC)。
理解它只有一个入口,就是这句话:

    协议 = **一串带类型标签的消息**;每条消息的载荷要么是定长字段,
           要么是 ``Native`` 格式的 Block。

.. code-block:: text

   一次 clickhouse-client 会话(简化后的消息序列):

     client ──── Hello ──────────────►  server      ① 握手:报身份/版本/协议号
     client ◄─── Hello ───────────────  server      ② 回敬:确认协议可用
     client ──── Query ──────────────►  server      ③ 发查询(query_id + 文本)
     client ──── Data (可选) ─────────►  server      ④ INSERT 时:数据块跟着来
     client ◄─── Data ────────────────  server      ⑤ SELECT 时:结果块(可能很多条)
     client ◄─── Progress ────────────  server      ⑥ "我已经读了 N 行/N 字节"
     client ◄─── Progress ────────────  server
     client ◄─── EndOfStream ─────────  server      ⑦ 这条查询结束了
     client ──── Ping ────────────────►  server      ⑧ 心跳
     client ◄─── Pong ────────────────  server

**为什么 ``Progress`` 是独立的一条消息,而不是塞进 ``Data`` 里?**
因为"进度"与"数据"是**两个独立的变化频率**:100 万行的结果可能只有 8 个 Data
块,但 Progress 可以每 10 万行就发一次 —— 客户端据此画进度条、判断卡不卡。
把它耦合进 Data,进度就只能在块边界上更新,粒度太粗。

**本模块的帧格式(逐字节)**

真实协议的消息**没有长度前缀**:读的人靠"已经知道的消息类型"来决定每个字段
有多长。这在一条纯字节流(TCP)上其实很麻烦 —— 你必须精确地按消息类型解析,
一个字段解析错了,后面全乱。

mini-clickhouse 为了好读、好调,给每条消息**加了一个长度前缀**:

.. code-block:: text

   一条消息(帧):
   ┌──────────────────────────────────────────────────────────┐
   │ varint  packet_type     消息类型(见 PacketType)          │
   │ varint  payload_length  载荷字节数(**本实现的简化**)     │
   │ bytes   payload         载荷(内容取决于 packet_type)     │
   └──────────────────────────────────────────────────────────┘

   例:一条 Progress(read_rows=100, 其余为 0)的完整字节
   ┌────┬────┬────┬───────────────────────────┐
   │ 07 │ 04 │ 64 00 00 00 ...               │  ← 4 个 varint 字段
   └────┴────┴───────────────────────────────┘
    类型 长度=4  100,0,0,0(varint 各占 1 字节)

.. warning::

   **这里有哪些"简化",如实列出来**(免得你以为它能和真实服务端对话):

   1. **多了 payload 长度前缀**。真实协议没有它;我们加它是为了让
      "从 TCP 字节流里切出一条消息"变成一件平凡的事。代价是**字节布局
      与真实 ClickHouse 不兼容**(第 3 条同样如此)。
   2. **没有 revision 协商**。真实握手会交换 DBMS revision(一个很大的
      整数)并据此启用/禁用几百个特性;我们只交换一个 ``protocol_version``
      常量,不据此改变任何行为。
   3. **没有压缩**。真实协议可以按块启用 LZ4/ZSTD(``compression`` 字段);
      我们把它当成一个"客户端想不想压缩"的**标记**,但从不真的压。
   4. **``ClientInfo`` 被压成一个字符串**。真实的 ``ClientInfo`` 有 ~20 个
      字段(客户端名、版本、TCP 端口、进程 ID、是否分布式查询的初始节点、
      接口类型、OS 用户、客户端主机名、配额 key……)。我们只带
      ``"名字/版本"``一段文本,够日志用,不参与任何逻辑。
   5. **``Native`` 块本身也是 mini 版**(见 :mod:`minich.formats.binary_formats`
      的警告:没有 BlockInfo、没有 custom-serialization 标志字节、每列多一个
      行数 varint)。所以**我们生成的数据包真实服务端读不了,反之亦然**。

   换句话说:本模块复刻的是协议的**结构与消息流**(它能教会你"客户端和
   服务端在聊什么"),而不是它的**字节级兼容性**。第 18 章会拿真实的
   ``TCPHandler`` 消息序列和这里逐条对照。
"""

from __future__ import annotations

from dataclasses import dataclass, field as _dc_field
from typing import Any, Dict, List, Optional, Tuple

from .block import Block
from .errors import ErrorCode
from .formats import get_input_format, get_output_format
from .serialization import VarUIntReader, write_var_uint

__all__ = [
    "PacketType",
    "write_packet",
    "read_packet",
    "Hello",
    "Query",
    "ExceptionPacket",
    "Progress",
    "EndOfStream",
    "DataPacket",
    "BlockStreamWriter",
    "BlockStreamReader",
    "encode_blocks",
    "decode_blocks",
    "parse_message",
]


# ══════════════════════════════════════════════════════════════════════════
# 消息类型号
# ══════════════════════════════════════════════════════════════════════════


class PacketType:
    """消息类型号 —— 与真实 ClickHouse 的 ``Protocol::Client::`` / ``Server::`` 同义。

    .. code-block:: text

       方向    类型号   名字             载荷
       ──────────────────────────────────────────────────────────────────
       双向      0     Hello            客户端/服务端的身份与版本
       客户端→   1     Query            查询文本 + query_id + settings
       双向      2     Data             Native 格式的 Block
       客户端→   3     Cancel           取消当前查询
       客户端→   4     Ping             心跳
       服务端→   5     Pong             心跳应答
       服务端→   6     Exception        带错误码/名字的错误
       服务端→   7     Progress         已读行数/字节数
       服务端→   8     EndOfStream      本次查询(或本次流)结束
    """

    #: 握手消息 —— 两端都会发一条
    HELLO = 0
    #: 客户端 → 服务端:发起一条查询
    QUERY = 1
    #: 双向:一个 Native 格式的 Block(参数是数据本身)
    DATA = 2
    #: 客户端 → 服务端:请求取消当前查询
    CANCEL = 3
    #: 客户端 → 服务端:心跳
    PING = 4
    #: 服务端 → 客户端:心跳应答
    PONG = 5
    #: 服务端 → 客户端:错误(带 ErrorCodes 的编号与名字)
    EXCEPTION = 6
    #: 服务端 → 客户端:进度
    PROGRESS = 7
    #: 服务端 → 客户端:流结束
    END_OF_STREAM = 8


# ══════════════════════════════════════════════════════════════════════════
# 帧读写
# ══════════════════════════════════════════════════════════════════════════


def write_packet(out: bytearray, packet_type: int, payload: bytes = b"") -> None:
    """把一条消息**追加**写进 ``out``(``bytearray``)。

    .. code-block:: text

       out += [varint 类型][varint 载荷长度][载荷字节]

    ``out`` 是 ``bytearray`` 而不是"有 write() 的对象",因为这一层是**字节**层
    (与 :mod:`minich.formats` 的文本格式刻意不同:文本格式写的是 ``str``)。
    """
    write_var_uint(out, packet_type)
    write_var_uint(out, len(payload))
    out += payload


def read_packet(buf: Any) -> Tuple[int, bytes, int]:
    """从 ``buf`` 里读**一条**消息,返回 ``(packet_type, payload, 新位置)``。

    ``buf`` 可以是 ``bytes`` / ``bytearray`` / ``memoryview``,也可以是一个
    已经带游标的 :class:`minich.serialization.VarUIntReader`(从套接字读时
    用后者更自然)。返回的第三项是"读完这条消息之后游标到了哪里" ——
    调用方靠它继续读下一条,或判断"还有没有下一条"。

    .. note::

       ``read_packet`` 假设 ``buf`` 的**起点正好是某条消息的开头**。
       它不认识"半条消息" —— 从字节流里凑齐一条完整消息是
       :func:`minich.client` 里 ``_recv_packet`` 的职责(先读类型号与长度,
       再精确读 ``length`` 个字节)。
    """
    if isinstance(buf, VarUIntReader):
        reader = buf
    else:
        reader = VarUIntReader(bytes(buf))
    packet_type = reader.read_var_uint()
    length = reader.read_var_uint()
    payload = reader.read(length)
    return packet_type, payload, reader.pos


# ── 载荷里的小工具:二进制字符串 ────────────────────────────────────────


def _write_string(out: bytearray, text: str) -> None:
    """写一个 ``String`` —— ``[varint 长度][UTF-8 字节]``(与 Native 里一致)。"""
    raw = text.encode("utf-8", "surrogateescape")
    write_var_uint(out, len(raw))
    out += raw


def _read_string(reader: VarUIntReader) -> str:
    """读一个 ``String``。"""
    length = reader.read_var_uint()
    return reader.read(length).decode("utf-8", "surrogateescape")


# ══════════════════════════════════════════════════════════════════════════
# 握手与查询:两个"客户端发起"的消息
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Hello:
    """``Hello`` —— 会话的第一条消息(两端都发)。

    真实 ``Hello`` 的字段顺序(``TCPHandler::receiveHello``)::

        String   client_name          "ClickHouse client" / "ClickHouse server"
        varint   version_major        25
        varint   version_minor        3
        varint   protocol_version     54429(握手协商用的大整数)
        String   default_database     "default"
        String   user                 "default"
        String   password             ""

    .. code-block:: text

       Hello 载荷(逐字段):
       ┌───────────────────┬─────────┬─────────┬──────────────┬──────────┬────────┬──────────┐
       │ String client_name│ varint  │ varint  │ varint proto │ String   │ String │ String   │
       │ "ClickHouse client"│ major  │ minor   │ version      │ db       │ user   │ password │
       └───────────────────┴─────────┴─────────┴──────────────┴──────────┴────────┴──────────┘

    真实服务端在收到 Hello 之后,会**用自己的 revision 去查表**决定"哪些特性可用",
    并用客户端传的 user/password 做认证。mini-clickhouse 不做 revision 协商
    (第 2 条简化),``password`` 也只是一个被原样接收、不做校验的字段 ——
    真正的鉴权在 :mod:`minich.access`(第 23 章)。
    """

    client_name: str = "mini-clickhouse client"
    client_version_major: int = 0
    client_version_minor: int = 1
    protocol_version: int = 1
    default_database: str = "default"
    user: str = "default"
    password: str = ""

    #: 这条消息的类型号 —— 消息类都有一个,便于统一分派
    packet_type = PacketType.HELLO

    def serialize(self) -> bytes:
        """把 Hello 编成载荷字节(不含帧头)。"""
        out = bytearray()
        _write_string(out, self.client_name)
        write_var_uint(out, self.client_version_major)
        write_var_uint(out, self.client_version_minor)
        write_var_uint(out, self.protocol_version)
        _write_string(out, self.default_database)
        _write_string(out, self.user)
        _write_string(out, self.password)
        return bytes(out)

    @classmethod
    def parse(cls, payload: bytes) -> "Hello":
        reader = VarUIntReader(payload)
        return cls(
            client_name=_read_string(reader),
            client_version_major=reader.read_var_uint(),
            client_version_minor=reader.read_var_uint(),
            protocol_version=reader.read_var_uint(),
            default_database=_read_string(reader),
            user=_read_string(reader),
            password=_read_string(reader),
        )


@dataclass
class Query:
    """``Query`` —— 客户端发起一条查询。

    真实 ``Query`` 的字段:``query_id`` + 一整棵结构化的 ``ClientInfo`` +
    **settings(序列化成"名字/值/flag"三元组的一长串)** + ``stage`` +
    ``compression`` + 查询文本。mini-clickhouse 的布局:

    .. code-block:: text

       Query 载荷:
       ┌────────────────┬──────────────┬───────────────┬────────────┬──────────────┬──────────────┐
       │ String query_id│ String text  │ varint stage  │ varint     │ varint N     │ N × (String  │
       │                │ (整条 SQL)   │ (见下)        │ compression│ (settings数) │ name, String │
       │                │              │               │            │              │ value)       │
       └────────────────┴──────────────┴───────────────┴────────────┴──────────────┴──────────────┘
       另有一个 String client_info 在最前面(见下,被压成一段描述文本)

    * ``stage`` —— 查询的**阶段**。真实枚举 ``QueryProcessingStage`` 区分
      ``FetchColumns`` / ``WithMergeableState`` / ``Complete`` 等,分布式查询时
      协调节点会让分片表"只做到中间状态就返回"。我们有这个字段,但服务端
      只在本地跑完整条查询(不用它)。
    * ``compression`` —— 客户端希望的压缩方式(0 = 不压)。服务端原样忽略。
    * ``settings`` —— 会话级设置里**这条查询要覆盖**的那些。真实协议里
      它是一大段"`varint 名字长度 + 名字 + flag字节 + 值`",我们简化成
      简单的名字/值字符串对。
    """

    query_id: str = ""
    client_info: str = ""
    settings: Dict[str, Any] = _dc_field(default_factory=dict)
    stage: int = 2          # 2 = Complete,真实枚举里"我要最终结果"
    compression: int = 0    # 0 = None
    query_text: str = ""

    packet_type = PacketType.QUERY

    def serialize(self) -> bytes:
        out = bytearray()
        _write_string(out, self.query_id)
        _write_string(out, self.client_info)
        write_var_uint(out, self.stage)
        write_var_uint(out, self.compression)
        write_var_uint(out, len(self.settings))
        for name, value in self.settings.items():
            _write_string(out, str(name))
            _write_string(out, "" if value is None else str(value))
        _write_string(out, self.query_text)
        return bytes(out)

    @classmethod
    def parse(cls, payload: bytes) -> "Query":
        reader = VarUIntReader(payload)
        query_id = _read_string(reader)
        client_info = _read_string(reader)
        stage = reader.read_var_uint()
        compression = reader.read_var_uint()
        count = reader.read_var_uint()
        settings: Dict[str, Any] = {}
        for _ in range(count):
            name = _read_string(reader)
            settings[name] = _read_string(reader)
        query_text = _read_string(reader)
        return cls(query_id=query_id, client_info=client_info, settings=settings,
                   stage=stage, compression=compression, query_text=query_text)


# ══════════════════════════════════════════════════════════════════════════
# 服务端发起的消息
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class ExceptionPacket:
    """``Exception`` —— 服务端把错误透传给客户端。

    真实 ClickHouse 的 ``Exception`` 载荷::

        Int32    code         错误编号(ErrorCodes 里的值,见 minich.errors)
        String   name         "DB::Exception"
        String   message      人类可读的说明
        String   stack_trace  栈(默认空,要 --stacktrace/配置才带)
        UInt8    has_nested   1 表示后面还跟着一个"嵌套异常"(异常链)

    .. code-block:: text

       ┌─────────────┬───────────────┬─────────────┬──────────────┬───────────┐
       │ String name │ String message│ String stack│ varint code  │ UInt8     │
       │ "DB::…"     │ "Table …"     │  (可空)     │ (错误编号)   │ has_nest  │
       └─────────────┴───────────────┴─────────────┴──────────────┴───────────┘
       (真实协议里 code 是定长 Int32 且在最前;本实现把 code 放最后并用 varint —— 见"简化"第 1 条精神)

    **为什么错误要单独一种消息,而不用一个"错误字段"?**
    因为错误可能在任何时刻出现(握手后、第一条 Data 前、两块 Data 之间),
    客户端必须能在**不牺牲流式**的前提下随时处理它 —— 独立消息类型是最自然
    的做法。而且错误是"带编号"的(见 :mod:`minich.errors`),客户端可以据
    ``code`` 决定重试与否,这正是 :meth:`minich.client.Client.execute` 抛出的
    :class:`minich.errors.MiniChError` 里那个 ``code`` 的来源。
    """

    code: int = ErrorCode.LOGICAL_ERROR_IN_MINICH
    name: str = "DB::Exception"
    message: str = ""
    stack_trace: str = ""
    has_nested: int = 0

    packet_type = PacketType.EXCEPTION

    def serialize(self) -> bytes:
        out = bytearray()
        _write_string(out, self.name)
        _write_string(out, self.message)
        _write_string(out, self.stack_trace)
        write_var_uint(out, self.code)
        out.append(1 if self.has_nested else 0)
        return bytes(out)

    @classmethod
    def parse(cls, payload: bytes) -> "ExceptionPacket":
        reader = VarUIntReader(payload)
        name = _read_string(reader)
        message = _read_string(reader)
        stack_trace = _read_string(reader)
        code = reader.read_var_uint()
        has_nested = reader.read(1)[0]
        return cls(code=code, name=name, message=message,
                   stack_trace=stack_trace, has_nested=has_nested)

    def to_error(self, query_id: Optional[str] = None) -> Any:
        """还原成一个 :class:`minich.errors.MiniChError`(客户端收到错误时用)。"""
        from .errors import MiniChError

        return MiniChError(self.message, code=self.code, query_id=query_id)


@dataclass
class Progress:
    """``Progress`` —— 服务端汇报"我已经干了多少"。

    载荷是**四个 varint**::

        varint  read_rows             已读行数
        varint  read_bytes            已读字节数
        varint  total_rows_to_read    预计总行数(0 表示未知)
        varint  elapsed_ns            已耗时(纳秒)

    .. code-block:: text

       ┌───────────┬─────────────┬───────────────────┬─────────────┐
       │ varint    │ varint      │ varint            │ varint      │
       │ read_rows │ read_bytes  │ total_rows_to_read│ elapsed_ns  │
       └───────────┴─────────────┴───────────────────┴─────────────┘

    .. note::

       真实 ``Progress`` 的字段比这多得多(还有 ``written_rows``、
       ``written_bytes``、``total_bytes_to_read``、以及 SQL/计划/网络三类
       单独的 elapsed)。我们只留最核心的四个,因为它们正好构成
       "进度条需要的信息":分子(read_rows)、分母(total_rows_to_read)、
       以及"还要多久"(elapsed_ns)。
    """

    read_rows: int = 0
    read_bytes: int = 0
    total_rows_to_read: int = 0
    elapsed_ns: int = 0

    packet_type = PacketType.PROGRESS

    def serialize(self) -> bytes:
        out = bytearray()
        write_var_uint(out, self.read_rows)
        write_var_uint(out, self.read_bytes)
        write_var_uint(out, self.total_rows_to_read)
        write_var_uint(out, self.elapsed_ns)
        return bytes(out)

    @classmethod
    def parse(cls, payload: bytes) -> "Progress":
        reader = VarUIntReader(payload)
        return cls(
            read_rows=reader.read_var_uint(),
            read_bytes=reader.read_var_uint(),
            total_rows_to_read=reader.read_var_uint(),
            elapsed_ns=reader.read_var_uint(),
        )


@dataclass
class EndOfStream:
    """``EndOfStream`` —— 本次查询(或本次数据流)结束。

    载荷为空。它的作用是给客户端一个**明确的终止信号**:
    客户端读到它才知道"不会再有 Data 了",从而结束 ``for`` 循环。
    没有它,客户端只能靠"服务端把连接关了"来判断结束 —— 那既不可靠
    (连接可能因为别的原因关),也让"一条连接上跑多条查询"变得没法做。

    .. note::

       真实协议里同一个词还有一层意思:客户端 ``INSERT`` 发完数据后,
       也会发一个 ``EndOfStream`` 告诉服务端"数据发完了,你去提交吧"。
       本实现复用了这个约定 —— 见 :func:`minich.client.Client.insert`。
    """

    packet_type = PacketType.END_OF_STREAM

    def serialize(self) -> bytes:
        return b""

    @classmethod
    def parse(cls, payload: bytes) -> "EndOfStream":
        return cls()


@dataclass
class DataPacket:
    """``Data`` —— 一个 ``Native`` 格式的 Block(本身没有额外字段)。

    载荷**就是** :class:`minich.formats.binary_formats.NativeOutputFormat`
    写出来的字节。所以"``Data`` 消息"与"``Native`` 块"是同义词:
    服务端把要发的 Block 用 Native 序列化,塞进一条 DATA 帧即可。

    这个类只是给 "DATA 帧" 一个能被 :func:`parse_message` 返回的对象,
    本身不持有解析逻辑(解析交给 :class:`BlockStreamReader`)。
    """

    payload: bytes = b""

    packet_type = PacketType.DATA

    def serialize(self) -> bytes:
        return self.payload

    @classmethod
    def parse(cls, payload: bytes) -> "DataPacket":
        return cls(payload=payload)


#: ``packet_type`` → 消息类。``Data`` 也登记在内(它有自己的对象)。
_MESSAGE_CLASSES = {
    PacketType.HELLO: Hello,
    PacketType.QUERY: Query,
    PacketType.DATA: DataPacket,
    PacketType.EXCEPTION: ExceptionPacket,
    PacketType.PROGRESS: Progress,
    PacketType.END_OF_STREAM: EndOfStream,
}


def parse_message(packet_type: int, payload: bytes) -> Any:
    """按类型号把载荷解成对应的消息对象(未知类型返回 ``payload`` 原文)。

    ``PING`` / ``PONG`` / ``CANCEL`` 没有载荷,也不带对象 —— 调用方一般直接
    比较类型号就够了,不需要一个空类。
    """
    cls = _MESSAGE_CLASSES.get(packet_type)
    if cls is None:
        return payload
    return cls.parse(payload)


# ══════════════════════════════════════════════════════════════════════════
# Block 流:Data 帧的读写
# ══════════════════════════════════════════════════════════════════════════


def encode_blocks(blocks: Any) -> bytes:
    """把一串 ``Block`` 编成一段 ``Native`` 字节(供 DATA 帧使用)。

    用 :func:`minich.formats.get_output_format` 拿到的 ``Native`` 输出器
    **每个块调用一次 write()**,最后写一次 ``write_suffix()`` 收尾
    (那正是 Native 的 ``0x00 0x00`` 结束标记)。所以这段字节是
    "自定界 + 自描述"的:接收方不需要额外信息就能知道有几个块、每个块几行。
    """
    fmt = get_output_format("Native")
    out = bytearray()
    blocks = list(blocks)
    header = blocks[0].clone_empty() if blocks else Block()
    fmt.write_prefix(header, out)
    for block in blocks:
        fmt.write(block, out)
    fmt.write_suffix(header, out)
    return bytes(out)


def decode_blocks(payload: bytes, sample_block: Optional[Block] = None) -> List[Block]:
    """把一段 ``Native`` 字节解成 ``Block`` 列表(:func:`encode_blocks` 的逆)。

    ``sample_block`` 给了就会做"名字与类型必须逐位相同"的校验(见
    :class:`minich.formats.binary_formats.NativeInputFormat`)。
    """
    fmt = get_input_format("Native")
    return list(fmt.read(payload, sample_block if sample_block is not None else Block()))


class BlockStreamWriter:
    """把 ``Block`` 序列**流式**写成一串 ``Data`` 帧。

    .. code-block:: text

       writer = BlockStreamWriter(bytearray())
       for block in blocks:            # ← 每来一个块就发一条,不必等全部算完
           writer.write_block(block)   #   发出 [DATA 帧][该块的 Native 字节]
       writer.finish()                 # 发出 [END_OF_STREAM 帧]

    这就是 ClickHouse "边算边发"在协议层的落地:**块一旦产出就能发包**。
    ``write_block`` 只把当前块的 Native 字节写出去,不缓存前面的块 ——
    所以服务端的内存峰值与结果集大小**无关**(只要它自己在产生块时也是流式的)。
    """

    def __init__(self, out: bytearray) -> None:
        self.out = out
        self._wrote = 0

    def write_block(self, block: Block) -> None:
        """发一个 Data 帧。"""
        write_packet(self.out, PacketType.DATA, encode_blocks([block]))
        self._wrote += 1

    def write_progress(self, progress: "Progress") -> None:
        """发一个 Progress 帧(混在 Data 之间,客户端按类型号分派)。"""
        write_packet(self.out, PacketType.PROGRESS, progress.serialize())

    def write_exception(self, exc: "ExceptionPacket") -> None:
        write_packet(self.out, PacketType.EXCEPTION, exc.serialize())

    def finish(self) -> None:
        """发 EndOfStream —— 告诉接收方"没有更多 Data 了"。"""
        write_packet(self.out, PacketType.END_OF_STREAM, b"")

    @property
    def blocks_written(self) -> int:
        return self._wrote


class BlockStreamReader:
    """从一串 ``Data`` 帧(可能夹着 Progress/Exception)里读出 ``Block``。

    .. code-block:: text

       reader = BlockStreamReader(frames)
       for block in reader.blocks():
           ...                     # 按到达顺序拿到块
       # 读到 END_OF_STREAM 就停;读到 EXCEPTION 会抛 MiniChError

    ``blocks()`` 会**跳过** ``Progress`` 帧(并把它记进 ``progress``),
    遇到 ``Exception`` 帧就抛出 —— 这样调用方拿到的永远是"干净的块序列",
    不必在循环里处理协议杂项。
    """

    def __init__(self, data: bytes) -> None:
        self._reader = VarUIntReader(data)
        #: 最近一次收到的 Progress(多个 Data 块之间可能有好几条)
        self.progress = Progress()
        self._saw_end = False

    def blocks(self):
        """产出块,直到 ``EndOfStream`` 或数据耗尽。"""
        reader = self._reader
        while not reader.eof():
            packet_type, payload, _ = read_packet(reader)
            if packet_type == PacketType.DATA:
                for block in decode_blocks(payload):
                    yield block
            elif packet_type == PacketType.PROGRESS:
                self.progress = Progress.parse(payload)
            elif packet_type == PacketType.EXCEPTION:
                raise ExceptionPacket.parse(payload).to_error()
            elif packet_type == PacketType.END_OF_STREAM:
                self._saw_end = True
                return
            else:
                # 未知帧:直接跳过载荷(前向兼容的常见做法)
                continue

    def blocks_list(self) -> List[Block]:
        return list(self.blocks())
