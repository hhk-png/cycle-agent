"""客户端库 —— ``Client``,以及"服务端"那一侧的实现。

真实 ClickHouse 有**两个**东西,名字都叫"client",经常被混为一谈:

.. code-block:: text

   clickhouse-client(命令行程序)      clickhouse-driver / clickhouse-connect(Python 库)
   ─────────────────────────────     ──────────────────────────────────────────
   给人敲 SQL 用的交互式 REPL         给**程序**用的 API
   内部用 Client 这一层的"连接"       就是"连接"这一层
   在 minich 里对应 minich/cli.py     在 minich 里对应**本模块**

本模块的 :class:`Client` 两种模式都能用,而**这不是为了兼容,而是教学**:

.. code-block:: text

   ┌───────────────────────── in-process 模式 ──────────────────────────┐
   │  Client ──► minich.execution.Context ──► 解释器 ──► 结果            │
   │  同一个进程,直接函数调用 —— 没有 socket、没有序列化                 │
   └────────────────────────────────────────────────────────────────────┘
   ┌────────────────────────── socket 模式 ─────────────────────────────┐
   │  Client ──(minich.protocol 的帧)──► _MinichServer ──► Context       │
   │  跨进程/跨机器,走的正是 clickhouse-client 与 server 之间那根线      │
   └────────────────────────────────────────────────────────────────────┘

**为什么两种都要有?** 因为真实 ClickHouse **没有 in-process 模式** ——
你要么连 TCP(9000),要么连 HTTP(8123)。它没有"在同一个进程里直接调用
服务端"这回事(``clickhouse-local`` 是个例外,它就是一个进程内跑查询的
工具,但它不是"客户端库")。

mini-clickhouse 把 in-process 定为**默认**,理由是:教程、示例和测试
99% 的时间只关心"SQL 进去、结果出来",不该被"先起服务端、再连、再关"
的仪式感拖累;而且不用 socket 也让测试不依赖端口。socket 模式则是
**用来验证协议那一层真的能跑**的 —— 它才是"两个进程隔着网络聊天"
的忠实缩影。

.. note::

   **in-process 与 socket 模式的结果一致性**是本模块的一条隐性契约:
   同一个 ``SELECT`` 无论走哪条路,i``rows()`` 必须相同。第 18 章会拿
   一批查询做"两条路径对拍",这在真实项目里叫 **differential testing**
   (差分测试):用一个可信的参照实现去校验另一个实现。

**流式插入**:``insert`` 故意接受**生成器**并分块发送,不把它整成列表 ——
因为"往列存里灌大量数据"本来就是 INSERT 的主场(第 05 章),
而"为了插 1000 万行先把 1000 万行装进内存"会让这个主场的优势全部抵消。
"""

from __future__ import annotations

import itertools
import socket
import socketserver
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .block import Block, ColumnWithTypeAndName
from .errors import ErrorCode, MiniChError, raise_error
from .execution.context import Context, QueryInfo, QueryResult
from .formats import get_input_format
from .protocol import (
    ExceptionPacket,
    Hello,
    PacketType,
    Progress,
    Query,
    decode_blocks,
    encode_blocks,
    write_packet,
)
from .sql import parse as parse_sql
from .sql.lexer import TokenKind, tokenize
from .types import get_data_type

__all__ = ["Client", "split_statements", "MinichServer"]

#: 真实 ClickHouse 的默认原生协议端口。
DEFAULT_PORT = 9000

#: 默认的 ``max_insert_block_size``(与 :mod:`minich.settings` 里一致)。
DEFAULT_INSERT_BLOCK_SIZE = 1048449


# ══════════════════════════════════════════════════════════════════════════
# 多语句脚本的切分
# ══════════════════════════════════════════════════════════════════════════


def split_statements(text: str) -> List[str]:
    """把一段**可能含多条语句**的脚本切成一条条 SQL。

    切分点只有一个:分号。但"哪个分号"很讲究 —— 下面这些分号**不能**切:

    .. code-block:: text

       SELECT ';';                     ← 引号里的分号是字符串内容
       SELECT 1 -- 注释里的 ; 也别切    ← 行注释里的分号
       SELECT /* ; */ 1;               ← 块注释里的分号

    硬造一个"引号状态机 + 注释状态机"能把这三条都处理掉,但那是**第二次
    实现词法分析**。所以这里**复用** :func:`minich.sql.lexer.tokenize`:
    它本来就会把字符串收成一个 STRING token、把注释整段丢掉,于是
    "切分点"就退化成"找出所有 ``PUNCT ';'`` token" —— 引号/注释里的分号
    根本不是 token,自然不会误切。

    .. code-block:: text

       "SELECT 1; SELECT ';'"  ──tokenize──►
         [IDENT(SELECT) NUM(1) PUNCT(;) IDENT(SELECT) STRING(;) EOF]
                               ▲ 切              ▲ 不切(是 STRING)
       结果: ["SELECT 1", " SELECT ';'"]

    纯注释或空白的片段(比如语句之间夹的注释)会被剔除,免得执行到一个
    "空查询"而报 ``EMPTY_QUERY``。
    """
    tokens = tokenize(text)
    segments: List[str] = []
    start = 0
    for token in tokens:
        if token.kind == TokenKind.EOF:
            segments.append(text[start:token.pos])
            break
        if token.is_punct(";"):
            segments.append(text[start:token.pos])
            start = token.pos + 1

    out: List[str] = []
    for segment in segments:
        if not segment.strip():
            continue
        # 片段里如果只有注释/空白,tokenize 后只剩一个 EOF —— 丢掉。
        if len(tokenize(segment)) <= 1:
            continue
        out.append(segment)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 套接字上的帧收发
# ══════════════════════════════════════════════════════════════════════════


class _ConnectionClosed(Exception):
    """对端把连接关了 —— 内部信号,不对外抛。"""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """从套接字上**精确**读 ``n`` 个字节(``recv`` 不保证一次读完)。

    这是套接字编程里最经典的一个坑:``sock.recv(1024)`` 完全可能只返回
    37 个字节。所以"我要 8 个字节"必须写成循环,不能指望一次 ``recv``。
    """
    chunks: List[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            raise _ConnectionClosed()
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_varint(sock: socket.socket) -> int:
    """从套接字上按 LEB128 读一个变长无符号整数(逐字节读)。"""
    result = 0
    shift = 0
    while True:
        byte = _recv_exact(sock, 1)[0]
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result
        shift += 7
        if shift > 63:  # pragma: no cover - 对端坏掉了
            raise_error(ErrorCode.CANNOT_PARSE_NUMBER, "Varint on the wire is too long")


def _recv_packet(sock: socket.socket) -> Tuple[int, bytes]:
    """从套接字上凑齐**一条完整消息**,返回 ``(类型号, 载荷)``。

    这正是 :func:`minich.protocol.read_packet` 的"字节流版":先逐字节读
    类型号与载荷长度,再精确读那么多字节。帧里那个长度前缀就是为这件事
    存在的(见 :mod:`minich.protocol` 的"简化"第 1 条)。
    """
    packet_type = _recv_varint(sock)
    length = _recv_varint(sock)
    payload = _recv_exact(sock, length) if length else b""
    return packet_type, payload


def _send_packet(sock: socket.socket, packet_type: int, payload: bytes = b"") -> None:
    """把一条消息发到套接字上。"""
    buf = bytearray()
    write_packet(buf, packet_type, payload)
    sock.sendall(bytes(buf))


# ══════════════════════════════════════════════════════════════════════════
# Client
# ══════════════════════════════════════════════════════════════════════════


class Client:
    """mini-clickhouse 的客户端 —— 两种模式共用一个 API。

    .. code-block:: python

       # ① in-process(默认;测试与教程用这个)
       c = Client.local()
       c.execute("SELECT 1").scalar()

       # ② socket(连一个真正在跑的 minich server)
       c = Client.connect("127.0.0.1", 9000)
       c.execute("SELECT 1").scalar()

    两种模式的方法签名**故意完全一致** —— 这样"把本地脚本改成连服务端"
    只需要换一行构造代码,其余不动。这条性质在真实世界里对应
    "本地开发用 SQLite、上线用 PostgreSQL",而能这么做的前提就是
    客户端 API 没有泄漏底层实现。

    :param host: 主机名(socket 模式)
    :param port: 端口(socket 模式,默认 9000)
    :param database: 连接后默认使用的数据库
    :param user: 用户名
    :param password: 密码(mini-clickhouse 不校验,见下)
    :param data_path: **in-process 模式**:数据落盘目录(None = 纯内存)
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = DEFAULT_PORT,
        database: str = "default",
        user: str = "default",
        password: str = "",
        data_path: Optional[str] = None,
        settings: Any = None,
    ) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.data_path = data_path
        self.settings = settings

        #: "inprocess" 或 "socket"
        self._mode = "inprocess"
        self._ctx: Optional[Context] = None
        self._sock: Optional[socket.socket] = None
        #: 服务端在 Hello 里回敬的版本信息(socket 模式)
        self.server_hello: Optional[Hello] = None

    # ── 构造:两种模式 ──────────────────────────────────────────────────

    @classmethod
    def local(
        cls,
        database: str = "default",
        user: str = "default",
        data_path: Optional[str] = None,
        settings: Any = None,
    ) -> "Client":
        """in-process 模式 —— 直接对一个 :class:`minich.execution.Context` 说话。

        .. note::

           真实 ClickHouse **没有**这个模式:客户端一定要通过网络连上服务端
           (或 HTTP)。``clickhouse-local`` 是唯一"单进程跑查询"的东西,
           但它不是客户端库,而是一个独立程序。

           我们把它做成**默认**,是为了让"跑一条 SQL 看结果"这件事不被
           网络与生命周期管理污染 —— 教程里的每一段示例都从
           ``Client.local()`` 开始。它的代价是:你对服务端做的任何操作
           (建表、插数)都只活在**当前进程**的内存里,进程一退就没了
           (除非给了 ``data_path`` 让 MergeTree 落盘)。
        """
        client = cls(database=database, user=user, data_path=data_path, settings=settings)
        client._ctx = Context(
            data_path=data_path, database=database, user=user, settings=settings
        )
        client._mode = "inprocess"
        return client

    @classmethod
    def connect(
        cls,
        host: str = "localhost",
        port: int = DEFAULT_PORT,
        database: str = "default",
        user: str = "default",
        password: str = "",
        timeout: float = 10.0,
    ) -> "Client":
        """socket 模式 —— 连一个正在跑的 ``minich server``。

        连接后立刻做一次握手(发 ``Hello``、收 ``Hello``),就像真实
        ``clickhouse-client`` 一样。握手失败(比如对端根本不是 minich
        server)会当场报错,而不是等到第一条查询才炸。
        """
        client = cls(host=host, port=port, database=database, user=user, password=password)
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client._sock = sock
        client._mode = "socket"
        client._handshake()
        return client

    def _handshake(self) -> None:
        """发 Hello、收 Hello —— 会话的第一件事。"""
        sock = self._sock
        assert sock is not None
        hello = Hello(
            client_name="mini-clickhouse client",
            client_version_major=0,
            client_version_minor=1,
            protocol_version=1,
            default_database=self.database,
            user=self.user,
            password=self.password,
        )
        _send_packet(sock, PacketType.HELLO, hello.serialize())
        packet_type, payload = _recv_packet(sock)
        if packet_type == PacketType.EXCEPTION:
            raise ExceptionPacket.parse(payload).to_error()
        if packet_type != PacketType.HELLO:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"Expected a Hello from the server, got packet type {packet_type}",
            )
        self.server_hello = Hello.parse(payload)

    # ── 查询 ────────────────────────────────────────────────────────────

    def execute(self, sql: str, settings: Optional[Dict[str, Any]] = None) -> QueryResult:
        """执行一条 SQL 并返回 :class:`minich.execution.QueryResult`。

        两种模式在**返回类型**上完全一致(都是 ``QueryResult``),差别只在
        结果是怎么来的:in-process 直接来自解释器;socket 模式则是把服务端
        发回来的 ``Data`` 帧解码成的块重新包一个 ``QueryResult``
        (它的 ``ctx`` 是 ``None`` —— 客户端本来就不该有服务端上下文)。
        """
        if self._mode == "socket":
            return self._execute_socket(sql, settings)
        assert self._ctx is not None
        return self._ctx.execute(sql, settings=settings)

    #: ``query`` 是真实 clickhouse-driver 里的名字 —— 与 :meth:`execute` 等价。
    def query(self, sql: str, settings: Optional[Dict[str, Any]] = None) -> QueryResult:
        """:meth:`execute` 的别名(真实客户端库里两个名字都存在)。"""
        return self.execute(sql, settings)

    def command(self, sql: str) -> str:
        """执行 SQL 并把结果**当成一段文本**返回。

        真实 ``clickhouse-driver`` 的 ``command()`` 用在"我不关心列结构,
        只要一段能看/能存的值文本"的场合(DDL 的回执、``SHOW`` 的输出)。
        这里直接返回 :meth:`QueryResult.text` 的默认渲染。
        """
        return self.execute(sql).text()

    # ── 脚本 ────────────────────────────────────────────────────────────

    def execute_script(
        self, text: str, settings: Optional[Dict[str, Any]] = None
    ) -> List[QueryResult]:
        """执行一段**多语句脚本**,一条一条来,**遇到第一个错误就停**。

        这正是 ``clickhouse-client --multiquery`` 的语义(以及绝大多数
        SQL 客户端跑 ``.sql`` 文件时的语义):**不做事务、不回滚** ——
        前面已经成功的语句保持生效。所以报错信息里必须说清"是哪一条
        语句挂了",否则用户面对一个几百行的脚本根本无从下手。

        :raises MiniChError: 失败时抛出,``message`` 里带语句序号与原 SQL。
        """
        statements = split_statements(text)
        results: List[QueryResult] = []
        for index, statement in enumerate(statements, start=1):
            try:
                results.append(self.execute(statement, settings=settings))
            except MiniChError as exc:
                raise_error(
                    exc.code,
                    f"Statement #{index} failed: {exc.message}\n"
                    f"  (statement text: {statement.strip()})",
                )
        return results

    # ── 插入 ────────────────────────────────────────────────────────────

    def insert(
        self,
        table: str,
        data: Any,
        columns: Optional[Sequence[str]] = None,
        format: Optional[str] = None,
    ) -> int:
        """向 ``table`` 写入数据,**流式处理,不物化整个输入**。返回写入行数。

        三种 ``data`` 形态:

        .. code-block:: text

           format 给了        data 是"一段用该格式编码的文本/字节"
                              (str/bytes/文件对象) → 走输入格式解析
           data 是 Block 的迭代 data 已经是带 schema 的 Block,直接写
           其余                data 是"行的迭代"(每行是一个序列)
                              → 按 max_insert_block_size 分块,边收边写

        **为什么一定要分块?** 这是列存 + ``INSERT`` 这一条路径的全部意义:
        服务端**不必**先把所有行收齐再落盘,而是"攒够一个块就写一个块"。
        真实 ClickHouse 的客户端就是这么做的 —— 它把数据按
        ``max_insert_block_size``(默认约 100 万行)切块,每块走一个 ``Data``
        包发出去。后果是:插 1 亿行,客户端内存峰值也只是"一个块"那么大,
        而不是"1 亿行"。

        :param columns: 只写这些列(按名字);缺的列由引擎补默认值。
        :param format: 输入格式名(``JSONEachRow`` / ``TSV`` / ``Native`` …)。
        """
        if format is not None or self._looks_like_formatted(data):
            return self._insert_with_format(table, data, columns, format)
        return self._insert_rows(table, data, columns)

    @staticmethod
    def _looks_like_formatted(data: Any) -> bool:
        """``data`` 是不是"一段文本/字节"(而不是行迭代)。"""
        return isinstance(data, (str, bytes, bytearray, memoryview)) or hasattr(data, "read")

    def _resolve_insert_columns(self, table: str, columns: Optional[Sequence[str]]) -> List[str]:
        """算出这次插入要写哪些列 —— 给的是列名,缺省用表的全部落盘列。"""
        db, tbl = _split_table(table, self.database)
        t = self._ctx.catalog.get_table(db, tbl) if self._ctx else None
        if t is None:
            # socket 模式没有目录,只能相信调用方给的列名
            if not columns:
                raise_error(
                    ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                    "insert() over a socket needs explicit column names",
                )
            return list(columns)
        return list(columns) if columns else t.stored_column_names()

    def _table_header(self, table: str, columns: Sequence[str]) -> Block:
        """按列名造一个零行 Block —— 插入块与格式解析都以它为 schema。"""
        db, tbl = _split_table(table, self.database)
        t = self._ctx.catalog.get_table(db, tbl) if self._ctx else None
        cols: List[ColumnWithTypeAndName] = []
        for name in columns:
            if t is not None:
                col = t.get_column(name)
                if col is None:
                    raise_error(
                        ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                        f"Table {t.full_name()} has no column '{name}'",
                    )
                dtype = col.type
            else:
                dtype = get_data_type("String")
            cols.append(ColumnWithTypeAndName(name, dtype))
        return Block(cols)

    def _socket_table_header(
        self, table: str, columns: Optional[Sequence[str]]
    ) -> Optional[Block]:
        """socket 模式下问服务端要表结构,造一个零行 Block。

        ``DESCRIBE TABLE`` 的每一行是
        ``(name, type, default_type, default_expression, …)`` ——
        ``default_type`` 为 ``ALIAS``/``MATERIALIZED``/``EPHEMERAL`` 的列不落盘,
        插入时不该带上(与 :meth:`minich.catalog.Table.stored_column_names` 同义)。
        """
        rows = self.execute(f"DESCRIBE TABLE {_safe_identifier(table)}").rows()
        schema: List[Tuple[str, str]] = []
        for row in rows:
            name, type_name = row[0], row[1]
            default_kind = row[2] if len(row) > 2 else ""
            if default_kind in ("ALIAS", "MATERIALIZED", "EPHEMERAL"):
                continue
            schema.append((name, type_name))
        if columns:
            wanted = set(columns)
            schema = [item for item in schema if item[0] in wanted]
        if not schema:
            return None
        return Block(
            [ColumnWithTypeAndName(name, get_data_type(type_name))
             for name, type_name in schema]
        )

    def _insert_block_size(self) -> int:
        if self._ctx is not None:
            try:
                value = self._ctx.settings.get("max_insert_block_size")
                if value:
                    return int(value)
            except MiniChError:
                pass
        return DEFAULT_INSERT_BLOCK_SIZE

    def _insert_with_format(
        self, table: str, data: Any, columns: Optional[Sequence[str]], format_name: Optional[str]
    ) -> int:
        """按输入格式解析 ``data`` 再写 —— 复用 ``FORMAT`` 子系统的输入侧。

        ``fmt.read`` 返回的是**块迭代器**,所以这条路本身也是流式的:
        解析出一个块就写/发一个块,不把整段输入物化成一个大 Block。
        """
        if format_name is None:
            raise_error(ErrorCode.UNKNOWN_FORMAT, "insert(format=...) requires a format name")
        fmt = get_input_format(format_name)
        if self._mode == "socket":
            header = self._socket_table_header(table, columns)
            names = header.get_names() if header is not None else list(columns or [])
            blocks = fmt.read(data, header if header is not None else Block(), None)
            return self._send_insert_stream(table, blocks, names)
        names = self._resolve_insert_columns(table, columns)
        header = self._table_header(table, names)
        written = 0
        for block in fmt.read(data, header, self._ctx.settings if self._ctx else None):
            written += block.rows()
            self._write_block(table, block)
        return written

    def _insert_rows(
        self, table: str, data: Iterable[Sequence[Any]], columns: Optional[Sequence[str]]
    ) -> int:
        """把"行的迭代"分块写入 —— **不物化整个输入**。

        实现要点就一句:用 ``itertools.islice`` 每次从生成器上**拽一个批次**,
        建一个 Block、写掉(或发走)、丢掉,再拽下一批。所以不管 ``data`` 是
        1000 行还是 1 亿行,同一时刻内存里只有**一个批次的 Block**。

        in-process 与 socket 两种模式在这里分道:前者直接调
        :meth:`minich.execution.Context.insert_block`,后者把每个块塞进一个
        ``Data`` 帧发出去 —— 但**分块逻辑完全相同**(都是 ``_iter_blocks`` /
        ``islice``),这正是"换一行构造代码就能换模式"的底气所在。
        """
        batch_size = self._insert_block_size()
        if self._mode == "socket":
            header = self._socket_table_header(table, columns)
            names = header.get_names() if header is not None else list(columns or [])
            blocks = _iter_blocks(data, header, batch_size)
            return self._send_insert_stream(table, blocks, names)

        names = self._resolve_insert_columns(table, columns)
        header = self._table_header(table, names)
        written = 0
        for block in _iter_blocks(data, header, batch_size):
            written += block.rows()
            self._write_block(table, block)
        return written

    def _write_block(self, table: str, block: Block) -> None:
        """把**一个块**写进表(in-process 的快路径)。"""
        assert self._ctx is not None
        if block.rows() == 0:
            return
        self._ctx.insert_block(table, block)

    # ── socket 模式的内部实现 ───────────────────────────────────────────

    def _execute_socket(
        self, sql: str, settings: Optional[Dict[str, Any]]
    ) -> QueryResult:
        """socket 模式执行:发 Query,收 Data/Progress/Exception/EndOfStream。

        .. note::

           这里把结果**收齐**再返回(而不是返回一个"边收边解"的迭代器)。
           原因是 ``QueryResult`` 支持"流式消费",但一条连接上同时只能有
           一个在飞的查询 —— 返回惰性结果会让"先发 A 再发 B、再消费 A"
           这种写法把两条查询的包搅在一起。真实客户端允许并行查询,是
           靠"每个查询一个连接/一个 query_id 通道"实现的,那超出了本实现
           的范围。我们把这点偏差写在这里,而不是假装支持。
        """
        sock = self._sock
        assert sock is not None
        query_id = _new_query_id()
        message = Query(
            query_id=query_id,
            client_info=f"{self.user}@client",
            settings=dict(settings or {}),
            query_text=sql,
        )
        _send_packet(sock, PacketType.QUERY, message.serialize())

        info = QueryInfo(query_id, sql, self.user, self.database)
        blocks: List[Block] = []
        start = time.perf_counter()
        while True:
            packet_type, payload = _recv_packet(sock)
            if packet_type == PacketType.DATA:
                blocks.extend(decode_blocks(payload))
            elif packet_type == PacketType.PROGRESS:
                progress = Progress.parse(payload)
                info.read_rows = progress.read_rows
                info.read_bytes = progress.read_bytes
            elif packet_type == PacketType.EXCEPTION:
                raise ExceptionPacket.parse(payload).to_error(query_id)
            elif packet_type == PacketType.END_OF_STREAM:
                break
            else:  # pragma: no cover - 未知帧,跳过
                continue
        info.elapsed = time.perf_counter() - start
        info.status = "QueryFinish"

        header = blocks[0].clone_empty() if blocks else Block()
        fmt_name = _format_of(sql) or "PrettyCompact"
        return QueryResult(None, query_id, header, fmt_name, info, iter(blocks))

    def _send_insert_stream(
        self, table: str, blocks: Iterable[Block], columns: Sequence[str]
    ) -> int:
        """socket 模式插入:``INSERT ... FORMAT Native`` + 一串 ``Data`` 帧。

        服务端收到 ``INSERT ... FORMAT Native`` 后不再去解析 SQL 里的数据
        (那本来也没有),而是**读后续的 Data 帧**、每读到一个块就写一个块。
        这条路径把"插入是流式的"落到了协议上:客户端不必等整个输入收齐。

        ``blocks`` 是个**块迭代器**(来自行分块或输入格式解析),这里对它做的
        只有"来一个发一个",所以客户端的内存峰值同样只有一个块那么大。

        列的**类型**得先问服务端(socket 模式没有本地目录),这一步在调用方
        (``_insert_rows`` / ``_insert_with_format``)里用 ``DESCRIBE TABLE``
        完成 —— 真实客户端也是这么干的,否则它没法把值编码成正确的字节。
        """
        sock = self._sock
        assert sock is not None
        cols_sql = f" ({', '.join('`%s`' % c for c in columns)})" if columns else ""
        db, tbl = _split_table(table, self.database)
        target = f"{db}.{tbl}" if db else tbl
        sql = f"INSERT INTO {target}{cols_sql} FORMAT Native"

        query_id = _new_query_id()
        _send_packet(
            sock, PacketType.QUERY, Query(query_id=query_id, query_text=sql).serialize()
        )

        written = 0
        for block in blocks:
            if block.rows() == 0:
                continue
            written += block.rows()
            _send_packet(sock, PacketType.DATA, encode_blocks([block]))
        _send_packet(sock, PacketType.END_OF_STREAM, b"")

        # 读服务端回执:Progress(写了多少行) + EndOfStream(或 Exception)
        while True:
            packet_type, payload = _recv_packet(sock)
            if packet_type == PacketType.EXCEPTION:
                raise ExceptionPacket.parse(payload).to_error(query_id)
            if packet_type == PacketType.END_OF_STREAM:
                break
            if packet_type == PacketType.PROGRESS:
                Progress.parse(payload)
        return written

    # ── 健康检查与生命周期 ──────────────────────────────────────────────

    def ping(self) -> bool:
        """探活 —— ``True`` 表示对端还在(in-process 模式永远 ``True``)。

        真实 ``clickhouse-client`` 与各种连接池都用它做"连接还活着吗",
        而不是靠"发一条 ``SELECT 1``" —— ping/pong 是最廉价的一个来回。
        """
        if self._mode != "socket":
            return True
        sock = self._sock
        if sock is None:
            return False
        try:
            _send_packet(sock, PacketType.PING, b"")
            packet_type, _ = _recv_packet(sock)
            return packet_type == PacketType.PONG
        except (OSError, _ConnectionClosed):
            return False

    def close(self) -> None:
        """关掉连接(幂等)。in-process 模式下会 ``shutdown`` 上下文。"""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:  # pragma: no cover
                pass
            self._sock = None
        if self._ctx is not None:
            self._ctx.shutdown()
            self._ctx = None

    @property
    def context(self) -> Optional[Context]:
        """in-process 模式的上下文(None 表示这是 socket 模式)。"""
        return self._ctx

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover
        target = "in-process" if self._mode == "inprocess" else f"{self.host}:{self.port}"
        return f"<Client {self._mode} {target} db={self.database}>"

    # ── 服务端 ──────────────────────────────────────────────────────────

    @classmethod
    def create_server(
        cls,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        data_path: Optional[str] = None,
        max_threads: Optional[int] = None,
    ) -> "MinichServer":
        """建一个 **服务端**(不阻塞),返回的对象可以 ``shutdown()`` 关掉。

        测试里最常用的形态::

            server = Client.create_server(port=0)      # 0 = 让系统挑端口
            threading.Thread(target=server.serve_forever).start()
            client = Client.connect("127.0.0.1", server.server_address[1])
            ...
            server.shutdown()
        """
        ctx = Context(data_path=data_path)
        return MinichServer((host, port), _QueryHandler, ctx, max_threads=max_threads)

    @classmethod
    def serve(
        cls,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        data_path: Optional[str] = None,
        max_threads: Optional[int] = None,
    ) -> None:
        """起服务端并**阻塞**在 ``serve_forever()`` 上,直到 Ctrl-C。"""
        server = cls.create_server(host=host, port=port, data_path=data_path,
                                   max_threads=max_threads)
        try:
            server.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover - 交互式退出
            pass
        finally:
            server.shutdown()
            server.server_close()


# ══════════════════════════════════════════════════════════════════════════
# 服务端
# ══════════════════════════════════════════════════════════════════════════


class MinichServer(socketserver.ThreadingTCPServer):
    """一个"每个连接一个线程"的 TCP 服务端。

    .. code-block:: text

       MinichServer
         ├─ context: Context          ← 所有会话**共享**一个目录/统计
         ├─ 每个连接 ─► _QueryHandler ─► 线程
         │                            │
         │   读 Hello ─► 回 Hello       │  握手
         │   循环:                     │
         │     Ping ──► Pong           │
         │     Query ─► 跑它 ─► 若干 Data ─► EndOfStream
         │     (Insert 的 Data 在这里被消费)

    ``ThreadingTCPServer`` 是 Python 标准库自带的"线程池版
    ``TCPServer``" —— 真实 ClickHouse 用的是自己的线程池 + epoll,
    但对教学来说"一连接一线程"的模型更好读,而且足够演示"多个客户端
    并发打一个服务端"。
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: Tuple[str, int],
        handler: Any,
        context: Context,
        max_threads: Optional[int] = None,
    ) -> None:
        #: 所有连接共享的服务端上下文(真实 ClickHouse 的 Server 级 Context)
        self.context = context
        #: 并发上限(用信号量实现 —— ThreadingTCPServer 自己不设上限)
        self._slots = threading.BoundedSemaphore(max_threads) if max_threads else None
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        """``max_threads`` 的落地:占不到名额就在**这里**等,不新开线程。"""
        if self._slots is not None:
            self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            if self._slots is not None:
                self._slots.release()
            raise

    def shutdown_request(self, request: Any) -> None:
        try:
            super().shutdown_request(request)
        finally:
            if self._slots is not None:
                self._slots.release()

    def server_close(self) -> None:
        """关掉服务端 —— **顺带关掉它持有的 Context**。

        .. warning::

           ``ThreadingTCPServer`` 的 ``shutdown()`` / ``server_close()``
           **不认识** ``self.context``:它们只停 accept 循环、关监听 socket。
           而 Context 背后挂着 MergeTree 的**后台合并线程** —— 那些线程
           ``while not self._merge_stop.is_set()`` 地转,只要没人调
           ``Context.shutdown()`` 就**永远不退出**。

           实测:建销 30 次服务端之后,``threading.active_count()``
           从 1 涨到 31,监听 socket 也还开着(fileno 没变成 -1)。
           在长跑的进程里(测试、Notebook)这就是一条稳定的线程泄漏。

           修法就是在这里显式关掉 context。**幂等**:Context.shutdown()
           可以被调用任意多次,所以 ``with MinichServer(...)`` 与手动
           ``close()`` 不会互相打架。
        """
        try:
            super().server_close()
        finally:
            ctx = getattr(self, "context", None)
            if ctx is not None:
                try:
                    ctx.shutdown()
                except Exception:  # pragma: no cover - 关停失败不该掩盖真实错误
                    pass


class _QueryHandler(socketserver.BaseRequestHandler):
    """一个客户端连接 = 一个 ``_QueryHandler`` 实例(每连接新建)。"""

    def handle(self) -> None:
        sock = self.request
        ctx: Context = self.server.context  # type: ignore[attr-defined]
        try:
            if not self._handshake(sock):
                return
            while True:
                packet_type, payload = _recv_packet(sock)
                if packet_type == PacketType.PING:
                    _send_packet(sock, PacketType.PONG, b"")
                elif packet_type == PacketType.QUERY:
                    self._handle_query(ctx, sock, Query.parse(payload))
                elif packet_type == PacketType.CANCEL:
                    continue  # mini-clickhouse 不真的取消(见文档)
                elif packet_type == PacketType.HELLO:
                    continue  # 重复握手,忽略
                else:
                    continue
        except _ConnectionClosed:
            return
        except OSError:  # pragma: no cover - 对端硬断
            return

    # ── 握手 ────────────────────────────────────────────────────────────

    def _handshake(self, sock: socket.socket) -> bool:
        packet_type, payload = _recv_packet(sock)
        if packet_type != PacketType.HELLO:
            _send_packet(
                sock,
                PacketType.EXCEPTION,
                ExceptionPacket(
                    code=ErrorCode.LOGICAL_ERROR,
                    message=f"Expected Hello, got packet type {packet_type}",
                ).serialize(),
            )
            return False
        Hello.parse(payload)  # 只为解一下,不校验(见 protocol.Hello 的说明)
        reply = Hello(
            client_name="mini-clickhouse server",
            client_version_major=0,
            client_version_minor=1,
            protocol_version=1,
            default_database="default",
            user="default",
            password="",
        )
        _send_packet(sock, PacketType.HELLO, reply.serialize())
        return True

    # ── 一条查询 ────────────────────────────────────────────────────────

    def _handle_query(self, ctx: Context, sock: socket.socket, query: Query) -> None:
        try:
            if _is_streaming_insert(query.query_text):
                self._stream_insert(ctx, sock, query)
            else:
                self._run_select(ctx, sock, query)
        except MiniChError as exc:
            _send_packet(
                sock,
                PacketType.EXCEPTION,
                ExceptionPacket(
                    code=exc.code,
                    name=exc.name if hasattr(exc, "name") else "DB::Exception",
                    message=exc.message,
                ).serialize(),
            )
            _send_packet(sock, PacketType.END_OF_STREAM, b"")
        except Exception as exc:  # pragma: no cover - 兜底,别让线程静默死掉
            _send_packet(
                sock,
                PacketType.EXCEPTION,
                ExceptionPacket(
                    code=ErrorCode.LOGICAL_ERROR_IN_MINICH,
                    message=f"Unhandled server error: {exc!r}",
                ).serialize(),
            )
            _send_packet(sock, PacketType.END_OF_STREAM, b"")

    def _run_select(self, ctx: Context, sock: socket.socket, query: Query) -> None:
        """跑一条普通查询,把结果**按块**发回去(Data → Progress → EndOfStream)。"""
        start = time.perf_counter()
        result = ctx.execute(query.query_text, settings=query.settings or None)
        rows = 0
        for block in result.stream():          # ← 流式:有块就发,不攒到最后
            if block.rows() == 0:
                continue
            _send_packet(sock, PacketType.DATA, encode_blocks([block]))
            rows += block.rows()
            if rows and rows % 65536 == 0:      # 每 64K 行报一次进度
                elapsed_ns = int((time.perf_counter() - start) * 1e9)
                _send_packet(
                    sock,
                    PacketType.PROGRESS,
                    Progress(read_rows=rows, total_rows_to_read=result.row_count(),
                             elapsed_ns=elapsed_ns).serialize(),
                )
        _send_packet(sock, PacketType.END_OF_STREAM, b"")

    def _stream_insert(self, ctx: Context, sock: socket.socket, query: Query) -> None:
        """消费 ``Data`` 帧直到 ``EndOfStream``,每块写一次表。

        这就是"服务端不需要把整个 INSERT 收进内存"的落地:块一到就写、
        写完就丢,内存里只有当前块。真实 ClickHouse 的 ``TCPHandler``
        收到 ``INSERT`` 时也是这个循环(收 Data → 落临时 part → 收完提交)。
        """
        node = parse_sql(query.query_text)
        database = getattr(node, "database", None) or ctx.database
        table_name = getattr(node, "table", None)
        if not table_name:
            raise_error(ErrorCode.SYNTAX_ERROR, "Malformed INSERT for the streaming path")
        table = ctx.catalog.get_table(database, table_name)
        written = 0
        start = time.perf_counter()
        while True:
            packet_type, payload = _recv_packet(sock)
            if packet_type == PacketType.DATA:
                for block in decode_blocks(payload):
                    if block.rows() == 0:
                        continue
                    table.write(ctx, block)
                    written += block.rows()
                    ctx.profile_events.increment("InsertedRows", block.rows())
            elif packet_type == PacketType.END_OF_STREAM:
                break
            elif packet_type == PacketType.CANCEL:
                break
            else:  # pragma: no cover
                continue
        elapsed_ns = int((time.perf_counter() - start) * 1e9)
        _send_packet(
            sock,
            PacketType.PROGRESS,
            Progress(read_rows=written, elapsed_ns=elapsed_ns).serialize(),
        )
        _send_packet(sock, PacketType.END_OF_STREAM, b"")


# ══════════════════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════════════════


def _split_table(name: str, default_database: str) -> Tuple[str, str]:
    """``"db.t"`` → ``("db", "t")``;``"t"`` → ``(default, "t")``。"""
    if "." in name:
        db, _, tbl = name.partition(".")
        return db.strip("`"), tbl.strip("`")
    return default_database, name.strip("`")


def _safe_identifier(name: str) -> str:
    """给标识符加反引号 —— 拼 ``DESCRIBE TABLE x`` 时避免注入/歧义。"""
    parts = [p.strip("`") for p in name.split(".")]
    return ".".join(f"`{p}`" for p in parts)


def block_from_rows_coerced(header: Block, rows: Sequence[Sequence[Any]]) -> Block:
    """按 ``header`` 的类型把"行的序列"装成一个 Block,**并做类型归一化**。

    与 :func:`minich.formats.base.block_from_rows` 的区别只有一步:每个值先过
    一遍 ``dtype.normalize(v)``。

    为什么这步是必须的? 因为列存的列是**定长数值数组**(见
    :class:`minich.columns.base.ColumnVector`):``DateTime`` 列在内存里存的
    是 **UTC 秒数**(``UInt32``),不是一个 ``datetime`` 对象。用户交给
    ``insert`` 的是自然形态(``datetime.datetime`` / ``date`` / ``Decimal``),
    必须以**列的类型**为准把它们换算成内部表示 —— 这一步就是真实
    ClickHouse 客户端序列化前做的 ``Field → 二进制`` 的一半工作。

    ``normalize`` 失败的(比如类型不认识这个值)原样放行,让
    ``column.append`` 去报具体错误 —— 至少错误信息会指到出错的那一列。
    """
    columns = [cwtn.type.create_column() for cwtn in header.data]
    width = header.columns()
    for row in rows:
        if len(row) != width:
            raise_error(
                ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                f"Row has {len(row)} values but the insert has {width} columns",
            )
        for cwtn, col, value in zip(header.data, columns, row):
            col.append(_normalize_value(cwtn.type, value))
    return Block(
        [ColumnWithTypeAndName(c.name, c.type, col) for c, col in zip(header.data, columns)]
    )


def _normalize_value(dtype: Any, value: Any) -> Any:
    """把一个 Python 值归一化成列类型期望的内部表示(失败就原样返回)。"""
    if value is None:
        return None
    try:
        return dtype.normalize(value)
    except (MiniChError, TypeError, ValueError):
        return value


def _new_query_id() -> str:
    """客户端侧的查询 ID —— 真实客户端用随机 UUID,这里用"时间戳+随机"。"""
    import random

    return f"c{int(time.time() * 1000) % 100000000:08d}{random.randint(0, 0xFFFF):04x}"


def _format_of(sql: str) -> Optional[str]:
    """从 SQL 文本里抠出 ``FORMAT X`` 的那个 ``X``(socket 模式用)。

    服务端发回来的只是块,没有"这条查询用了哪个格式"这个信息 ——
    而格式是**客户端**的选择。所以要么在客户端解析一次 SQL,要么让服务端
    把格式名也塞进响应里(真实协议走的是后者,在 ``Query`` 的 settings/
    ``ResultFormat`` 里带)。我们用前者:解析一次,拿到 ``FORMAT`` 子句。
    """
    try:
        node = parse_sql(sql)
    except MiniChError:
        return None
    return getattr(node, "format", None)


def _is_streaming_insert(sql: str) -> bool:
    """这条查询是不是"数据跟着 Data 帧来"的 INSERT。

    判据:它是 ``INSERT`` 且带 ``FORMAT`` 子句 —— 也就是
    ``INSERT INTO t (...) FORMAT Native``。看到它,服务端就知道
    "别在 SQL 文本里找数据,去后面的帧里拿"。
    """
    try:
        node = parse_sql(sql)
    except MiniChError:
        return False
    return type(node).__name__ == "InsertQuery" and getattr(node, "format", None) is not None


def _iter_blocks(
    data: Any, header: Optional[Block], batch_size: int
) -> Iterator[Block]:
    """把 ``insert`` 的 ``data`` 统一成"一批一批的 Block"。

    支持三种输入:已经是 ``Block`` 的迭代、行的迭代、以及单个 ``Block``。
    分块逻辑只在"行的迭代"这一支:``islice`` 每次拽一批,所以在整段
    ``for`` 循环里,内存里永远只有一个 batch 的 Block。
    """
    iterator = iter(data)
    first = next(iterator, _MISSING)
    if first is _MISSING:
        return
    if isinstance(first, Block):
        yield first
        for block in iterator:
            yield block
        return

    iterator = itertools.chain([first], iterator)
    assert header is not None
    while True:
        batch = list(itertools.islice(iterator, batch_size))
        if not batch:
            return
        yield block_from_rows_coerced(header, batch)


class _Missing:
    """一个"什么都没找到"的哨兵(比 ``None`` 更明确 —— 数据里可能真有 None)。"""


_MISSING = _Missing()
