"""part —— MergeTree 的磁盘布局,以及它的读写路径。

**这是整个 mini-clickhouse 里最重要的一段代码**,因为教程里
"part 格式""granule""稀疏索引""列裁剪"这几章讲的全是它。

.. code-block:: text

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  一个 Wide part 的目录长这样(``ORDER BY (id)``、4 列):              ║
   ╚══════════════════════════════════════════════════════════════════════╝

   202403_1_1_0/                  ← 目录名就是 part 名(见 ``format_part_name``)
   ├── columns.txt                ← 列名 + 类型,一行一列(TSV)
   ├── count.txt                  ← 行数(**一个数字**,不带换行)
   ├── partition.dat              ← 分区键取值的序列化形式
   ├── primary.idx                ← 稀疏主键索引:每个 granule 的第一个键值
   ├── checksums.txt              ← 每个文件的 size + CRC32(**损坏检测**的落点)
   ├── serialization.json         ← 每列用的编解码器(简化版)
   ├── uuid.txt                   ← part 的 UUID(复制用)
   ├── ttl.txt                    ← 有 TTL 时才写
   │
   ├── id.bin                     ← id 列的**数据**:一个 granule 一个压缩块
   ├── id.mrk2                    ← id 列的**位置索引**:每 granule 一个三元组
   ├── minmax_id.idx              ← id 列每 granule 的 min/max
   ├── city.bin                   ├── city.mrk2   ├── minmax_city.idx
   ├── d.bin                      ├── d.mrk2      ├── minmax_d.idx
   └── skp_idx_url.idx            ← url 列上的跳数索引(建表时声明才会出现)

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  一次写入(``INSERT``)的完整流程 —— 注意**两阶段提交**              │
   └──────────────────────────────────────────────────────────────────────┘

     ① 按排序键**排序**输入 block
                 │
     ② 按(行数 ‖ 字节数)切成 granule          ← marks.IndexGranularity
                 │
     ③ 每列每 granule:序列化 → 压缩 → 追加到 <col>.bin
                     同时记下 (压缩偏移, 解压偏移, 行数) → <col>.mrk2
                 │
     ④ 建 primary.idx / minmax_*.idx / skp_idx_*.idx
                 │
     ⑤ columns.txt / count.txt / partition.dat / serialization.json / uuid.txt
                 │
     ⑥ 算 checksums.txt(每个文件的 size + CRC32)
                 │
     ⑦ 全部写进一个 **临时目录** ``tmp_<随机>``
                 │
     ⑧ ``disk.move(tmp, 最终名)``  ← **原子**:这一刻之前,谁也看不到这个 part

   ⑧ 是"读方永远看不到半个 part"的全部秘密 —— 详见 :mod:`minich.storages.merge_tree.disk`。
   崩溃在 ⑦ 之前只会留下一个 ``tmp_*`` 目录(真实 ClickHouse 启动时会清理它)。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  一次读取为什么便宜:列裁剪 + granule 裁剪**相乘**                    │
   └──────────────────────────────────────────────────────────────────────┘

     SELECT count() FROM t WHERE d >= '2024-03-01'   -- 表有 20 列

     ┌── 列裁剪(读哪些 .bin)────────────────────────────────────────┐
     │  只读 d.bin —— 另外 19 列的 .bin **一个字节都不碰**            │
     │  这就是"列存"最直接的红利:IO 量 ∝ 用到的列数,与表宽无关      │
     └────────────────────────────────────────────────────────────────┘

     ┌── granule 裁剪(读 d.bin 的哪些段)────────────────────────────┐
     │  KeyCondition 在 primary.idx 上二分 → MarkRange [7, 12)        │
     │  只解压 d.bin 里第 7..11 个压缩块                              │
     │  —— 而不是从头解压到 12                                        │
     └────────────────────────────────────────────────────────────────┘

   两者相乘的效果就是 ClickHouse 那句著名的"扫描 10 亿行只要几十毫秒"。

.. warning::

   **mini-clickhouse 与真实 ClickHouse 的 part 格式差异(重要)**

   ============================  =========================================
   项                             真实 ClickHouse / mini-clickhouse
   ============================  =========================================
   ``.mrk2`` 每个 mark            真实:自适应粒度下 3×UInt64(压缩偏移、
                                  解压偏移、行数)。mini:同样 3×UInt64 ✓
   压缩块里装几个 granule          真实:多个 granule 共用一个压缩块
                                  (``min/max_compress_block_size``)。
                                  mini:**一个 granule 一个压缩块**,
                                  于是 ``offset_in_decompressed_block`` 恒为 0
   ``minmax_<col>.idx``            真实:为**分区键列**写。mini:同样只给
                                  分区键列与主键列写(避免文件爆炸) ✓
   跳数索引何时建                  真实:``INSERT`` 时不建,merge 时才建。
                                  mini:**写入时就建**(演示更直观)
   ``serialization.json``          真实:完整的 ``SerializationInfo`` 树。
                                  mini:一个简化对象(列 → codec 名)
   校验码                          真实:CRC-32C。mini:CRC-32(zlib)
   ``defaults.mrk`` / ``ttl.txt``  真实:有 DEFAULT 列 / TTL 时才写。
                                  mini:``ttl.txt`` 写;``defaults.mrk`` 不写
   ============================  =========================================
"""

from __future__ import annotations

import json
import struct
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ...block import Block, ColumnWithTypeAndName
from ...columns import IColumn
from ...errors import ErrorCode, MiniChError, raise_error
from ...serialization import (
    deserialize_binary,
    read_var_uint,
    serialize_binary,
    write_var_uint,
)
from ...types import DataType, get_data_type
from ...util.hashing import crc32
from ...util.keys import normalize_key_value
from ...util.logger import get_logger
from ...util.strings import escape_tsv, unescape_tsv
from ..base import ReadContext
from .disk import IDisk
from .expression import evaluate_expression
from .marks import (
    DEFAULT_INDEX_GRANULARITY,
    DEFAULT_INDEX_GRANULARITY_BYTES,
    Mark,
    MarkRange,
    IndexGranularity,
    RANGE_MAX,
)
from .skip_index import IndexDescription, IndexHelper, MergeTreeIndexReader

_log = get_logger("MergeTree")


class _NullsLast:
    """排序键里的 ``NULL`` —— ClickHouse 里 **NULL 是最大值**(ASC 排最后)。

    ``minich.util.keys.normalize_key_value`` 把 ``NaN`` 收成一个
    "比 ``+inf`` 还大"的哨兵,但**不动 NULL**(它原样返回 ``None``)。
    而 ``None`` 与 ``int``/``str`` 比较会抛裸 ``TypeError`` ——
    ``ORDER BY k`` 的 ``k`` 是 ``Nullable`` 且含 NULL 时,``INSERT`` 在
    排序这一步就直接崩。所以排序键的元素要再包一层:NULL → 这个哨兵。
    """

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _NullsLast)

    def __hash__(self) -> int:
        return 0x4E554C4C4E554C4C  # "NULLNULL"

    def __lt__(self, other: Any) -> bool:
        # 没有任何值比 NULL 更大(``_NaNKey`` 也一样:NULL > NaN)
        return False

    def __gt__(self, other: Any) -> bool:
        return not isinstance(other, _NullsLast)

    def __repr__(self) -> str:  # pragma: no cover
        return "NULL"


#: NULL 的全局单例 —— 保证所有 NULL 的键都是**同一个对象**(身份短路)。
NULL_KEY = _NullsLast()


def sorting_key_value(value: Any) -> Any:
    """排序键元素的规范化 —— ``normalize_key_value`` + "NULL 视为最大值"。

    写 part 的排序(:func:`_sort_block`)、合并时的 k 路归并
    (:meth:`MergeTreePartReader._build_keys` / :class:`BlockRowSource`)
    以及在 ``primary.idx`` 上二分(:func:`compare_index_values`)必须用
    **同一个**函数 —— 否则"NULL 在哪一端"会自相矛盾,轻则排错,
    重则把含 NULL 的 granule 整段裁掉(数据丢失)。
    """
    v = normalize_key_value(value)
    return NULL_KEY if v is None else v


__all__ = [
    "PartName",
    "parse_part_name",
    "format_part_name",
    "format_partition_id",
    "PartWriteContext",
    "MergeTreePart",
    "write_part",
    "load_part",
    "DEFAULT_MIN_BYTES_FOR_WIDE_PART",
    "DEFAULT_MIN_ROWS_FOR_WIDE_PART",
    "write_compressed_block",
    "read_compressed_block",
]


# ══════════════════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════════════════

#: ``min_bytes_for_wide_part`` —— 小于这个字节数的 part 用 Compact 格式。
#:
#: .. warning::
#:
#:    **真实 ClickHouse 的默认值是 10 MiB(``10485760``)**,也就是说
#:    "刚 INSERT 进来的小 part 是 Compact 的"。mini-clickhouse 把默认值
#:    设成 **0(永远 Wide)**,因为教程的整条主线 —— "一个 part 有一堆
#:    ``<column>.bin`` 文件""``bytes_on_disk`` 能按列拆开看" —— 只有在
#:    Wide 格式下才成立。想复现真实行为,建表时给
#:    ``SETTINGS min_bytes_for_wide_part = 10485760`` 即可。
DEFAULT_MIN_BYTES_FOR_WIDE_PART = 0

#: ``min_rows_for_wide_part`` —— 同上,真实默认也是 10 MiB(行数用于
#: "行很宽但压缩后很小"的场景)。mini 默认 0。
DEFAULT_MIN_ROWS_FOR_WIDE_PART = 0

#: 压缩块头的大小:method(1) + compressed(4) + uncompressed(4) + crc32(4)
_BLOCK_HEADER = struct.Struct("<BII I")

#: 默认编解码器(与真实 ClickHouse 的 ``compression`` 表默认值一致)
DEFAULT_CODEC = "LZ4"

#: 数据块大小上限(真实 CH 的 ``max_compress_block_size``,1 MiB)。
#: 我们会检查一个 granule 的未压缩数据是否超过它 —— 超过只说明
#: "这个 granule 真大",并不改变布局(见模块 docstring 的差异表)。
MAX_COMPRESS_BLOCK_SIZE = 1024 * 1024


# ══════════════════════════════════════════════════════════════════════════
# 编解码器解析(惰性导入 —— compression.py 由另一个模块负责)
# ══════════════════════════════════════════════════════════════════════════


def _named_codec(name: str) -> Any:
    """按名字取一个压缩编解码器。

    ``minich.compression`` 可能**还不存在**(它由另一个模块负责),
    也可能只实现了一部分。所以这里:

    * 全部放在函数内部导入 —— **模块级导入会让整个包起不来**;
    * 取不到就返回 ``None``,调用方退回"不压缩";
    * 认不出这个名字也返回 ``None``(而不是抛错)—— 因为
      "压缩没生效"是**性能问题**,"读不出来"才是**正确性问题**。
    """
    key = (name or "").upper()
    if key in ("", "NONE", "AUTO"):
        return None
    try:
        from ... import compression as _c
    except Exception:
        return None

    factory = getattr(_c, "CompressionCodecFactory", None)
    if factory is not None:
        for attr in ("get", "get_by_name", "create", "try_get", "tryGet"):
            fn = getattr(factory, attr, None)
            if callable(fn):
                try:
                    codec = fn(name)
                    if codec is not None:
                        return codec
                except Exception:
                    pass
    # 退路:直接按类名构造(``CompressionCodecLZ4()`` 这种)
    cls = getattr(_c, f"CompressionCodec{key}", None)
    if cls is None:
        return None
    try:
        return cls()
    except Exception:
        return None


def _codec_method_byte(codec: Any) -> int:
    if codec is None:
        return 0x02  # CompressionMethodByte.NONE
    return int(getattr(codec, "method_byte", 0x02))


def write_compressed_block(data: bytes, codec_name: str) -> bytes:
    """写一个压缩块 —— 布局见 :mod:`minich.compression` 的模块文档。

    .. code-block:: text

       ┌──────────┬───────────────┬─────────────────┬───────────────┬─────────┐
       │ method(1)│ compressed(4) │ uncompressed(4) │ checksum(4)   │ payload │
       └──────────┴───────────────┴─────────────────┴───────────────┴─────────┘

    ``uncompressed`` 长度**必须**写下来:像 Gorilla / DoubleDelta 这种
    编解码器不把原始长度编进数据流(真实 CH 也一样)。
    """
    codec = _named_codec(codec_name)
    method = _codec_method_byte(codec)
    if codec is None:
        payload = bytes(data)
    else:
        payload = codec.compress(bytes(data))
    if len(payload) > 0xFFFFFFFF or len(data) > 0xFFFFFFFF:
        raise_error(
            ErrorCode.ARGUMENT_OUT_OF_BOUND,
            f"Compressed block too large: {len(payload)}/{len(data)} bytes",
        )
    return _BLOCK_HEADER.pack(method, len(payload), len(data), crc32(payload)) + payload


def read_compressed_block(blob: bytes, pos: int = 0) -> Tuple[bytes, int]:
    """读一个压缩块,返回 ``(未压缩字节, 新位置)``。

    校验失败会抛 ``CHECKSUM_DOESNT_MATCH`` —— 这是数据损坏的**第一道闸门**
    (第二道是 part 级的 ``checksums.txt``)。
    """
    if pos + _BLOCK_HEADER.size > len(blob):
        raise_error(
            ErrorCode.UNEXPECTED_END_OF_FILE,
            f"Truncated compressed block header at offset {pos}",
        )
    method, csize, usize, checksum = _BLOCK_HEADER.unpack_from(blob, pos)
    pos += _BLOCK_HEADER.size
    payload = blob[pos:pos + csize]
    if len(payload) != csize:
        raise_error(
            ErrorCode.UNEXPECTED_END_OF_FILE,
            f"Compressed block declares {csize} bytes but only "
            f"{len(payload)} are available",
        )
    actual = crc32(payload)
    if actual != checksum:
        raise_error(
            ErrorCode.CHECKSUM_DOESNT_MATCH,
            f"Compressed block checksum mismatch at offset {pos}: "
            f"expected {checksum:#010x}, got {actual:#010x} (data is corrupted)",
        )
    if method == 0x02:
        out = payload
    else:
        codec = _codec_by_method(method)
        if codec is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Cannot decompress block with method byte {method:#04x}: "
                f"the codec is not available (minich.compression is missing or "
                f"incomplete). The data was written with compression enabled.",
            )
        out = codec.decompress(payload, usize)
    if len(out) != usize:
        raise_error(
            ErrorCode.CANNOT_READ_ALL_DATA,
            f"Decompressed {len(out)} bytes, expected {usize}",
        )
    return out, pos + csize


#: method byte → codec 名(用于反查)。真实 CH 的映射表在
#: ``CompressionCodecFactory`` 里;我们这里只列已知的几个。
_METHOD_TO_NAME = {
    0x82: "LZ4",
    0x83: "Delta",
    0x84: "T64",
    0x85: "DoubleDelta",
    0x86: "Gorilla",
    0x87: "FPC",
    0x88: "DeflateQPL",
    0x90: "ZSTD",
    0x91: "Multiple",
}


def _codec_by_method(method: int) -> Any:
    name = _METHOD_TO_NAME.get(method)
    if name is None:
        return None
    return _named_codec(name)


# ══════════════════════════════════════════════════════════════════════════
# part 名
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PartName:
    """part 目录名拆开之后的四个部分 —— ``<partition_id>_<min>_<max>_<level>``。

    .. code-block:: text

       202403_5_5_0
       │      │ │ │
       │      │ │ └── level:合并层级。每 merge 一次 +1,越大说明数据越"熟"
       │      │ └──── max_block:这个 part 里**最大**的 block 号
       │      └────── min_block:最小的 block 号
       └───────────── partition_id:分区键取值(202403 = 2024 年 3 月)

    **block 号**是 part 名里最容易看不懂的一处。它不是"有几个数据块",
    而是"这个 part 覆盖了输入流的哪一段":一次 INSERT 拿一个**递增**的
    block 号,一个 part 记录自己合并了哪些 block。于是
    ``min_block..max_block`` 唯一标识"这批数据"。

    **为什么要有 block 号?** 为了**幂等**(idempotency):
    客户端重试 ``INSERT`` 时,ClickHouse 靠 block 号识别"这个 block
    我已经写过了",直接把重复的 part 丢掉。这就是 ClickHouse 宣称
    "至少一次写入也能得到不重复数据"的机制。

    **level 有什么用?** 合并策略用它判断"哪些 part 该优先合并":
    优先合并 level 相近的小 part(TTL/大小策略都基于这个)。
    """

    partition_id: str
    min_block: int
    max_block: int
    level: int

    def format(self) -> str:
        return format_part_name(self.partition_id, self.min_block, self.max_block, self.level)


def format_partition_id(value: Any) -> str:
    """把分区键取值转成 ``partition_id`` 字符串。

    真实 ClickHouse 的 ``partition_id`` 是**序列化后的分区键值**再经过
    一层转义(把 ``/``、空格之类的字符换掉,因为它们不能出现在目录名里)。
    单列分区键直接用值的文本形式,和真实行为一致(``202403``);
    多列分区键我们用 ``-`` 连接(真实 CH 用 ``\0`` 分隔并转义 ——
    这一点写在下面的 warning 里)。
    """
    if isinstance(value, tuple):
        return "-".join(format_partition_id(v) for v in value)
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _escape_partition_component(value)
    return _escape_partition_component(str(value))


def _escape_partition_component(text: str) -> str:
    out = []
    for ch in text:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_%02x" % (ord(ch) & 0xFF))
    return "".join(out)


def format_part_name(partition_id: str, min_block: int, max_block: int, level: int) -> str:
    """拼一个 part 名 —— ``<partition_id>_<min_block>_<max_block>_<level>``。"""
    if min_block > max_block:
        raise_error(
            ErrorCode.LOGICAL_ERROR,
            f"part name would have min_block ({min_block}) > max_block ({max_block})",
        )
    return f"{partition_id}_{min_block}_{max_block}_{level}"


def parse_part_name(name: str) -> PartName:
    """解析 part 名,失败时抛 ``BAD_DATA_PART_NAME``。

    错误消息刻意做成真实 ClickHouse 的形状,方便对照::

        Code: 246. DB::Exception: Cannot parse part name "abc": ...

    .. note::

       真实 ClickHouse 还会处理 ``tmp_`` 前缀、``detached`` 目录、
       ``.tmp`` 后缀等一堆变体(``MergeTreePartInfo::tryParsePartName``)。
       我们只认正经的 ``<pid>_<min>_<max>_<level>`` 四段式。
    """
    text = name.strip().rstrip("/")
    if not text:
        raise_error(ErrorCode.BAD_DATA_PART_NAME, "Cannot parse an empty part name")
    parts = text.split("_")
    if len(parts) < 4:
        raise_error(
            ErrorCode.BAD_DATA_PART_NAME,
            f'Cannot parse part name "{name}": expected '
            f'"<partition_id>_<min_block>_<max_block>_<level>"',
        )
    pid = "_".join(parts[:-3])
    try:
        min_block = int(parts[-3])
        max_block = int(parts[-2])
        level = int(parts[-1])
    except ValueError:
        raise_error(
            ErrorCode.BAD_DATA_PART_NAME,
            f'Cannot parse part name "{name}": min_block/max_block/level must be '
            f"integers, got ({parts[-3]}, {parts[-2]}, {parts[-1]})",
        )
    if min_block < 0 or max_block < 0 or level < 0:
        raise_error(
            ErrorCode.BAD_DATA_PART_NAME,
            f'Cannot parse part name "{name}": block numbers and level must be '
            f"non-negative",
        )
    if min_block > max_block:
        raise_error(
            ErrorCode.BAD_DATA_PART_NAME,
            f'Cannot parse part name "{name}": min_block {min_block} > max_block '
            f"{max_block}",
        )
    if not pid:
        raise_error(
            ErrorCode.BAD_DATA_PART_NAME,
            f'Cannot parse part name "{name}": empty partition id',
        )
    return PartName(pid, min_block, max_block, level)


# ══════════════════════════════════════════════════════════════════════════
# 写入上下文
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class PartWriteContext:
    """一次 ``write_part`` 需要的全部输入 —— 相当于真实 CH 的
    ``MergeTreeDataPartWriter`` + ``MergeTreeData::PartInfo`` 的组合。

    关键字段:

    * ``column_names`` —— **真正落盘**的列(ALIAS / EPHEMERAL 不在其中);
    * ``sorting_key_exprs`` —— 排序键表达式(决定数据怎么排序、
      以及 ``primary.idx`` 里记什么);
    * ``index_granularity`` —— :class:`IndexGranularity`,决定 granule 边界;
    * ``partition_value`` —— 这一整块数据的分区键取值
      (**一个 block 只能属于一个分区**,``MergeTreeData.write`` 负责先切分)。
    """

    table: Any = None
    #: 落盘的列(顺序 = 表定义顺序 = ``columns.txt`` 里的顺序)
    column_names: List[str] = field(default_factory=list)
    column_types: Dict[str, DataType] = field(default_factory=dict)
    #: 每列的编解码器名;缺省用 :data:`DEFAULT_CODEC`
    codecs: Dict[str, str] = field(default_factory=dict)

    #: 排序键表达式列表(``ORDER BY`` 的每一项)
    sorting_key_exprs: List[Any] = field(default_factory=list)
    sorting_key_reverse: List[bool] = field(default_factory=list)
    #: 主键在排序键里的**位置**(``PRIMARY KEY`` 是 ``ORDER BY`` 的前缀)
    primary_key_positions: List[int] = field(default_factory=list)

    #: 分区键表达式与它们的类型(用于写 ``partition.dat``)
    partition_key_exprs: List[Any] = field(default_factory=list)
    partition_key_types: List[DataType] = field(default_factory=list)
    partition_value: Any = None
    partition_id: str = ""

    index_granularity: Optional[IndexGranularity] = None
    indexes: List[IndexDescription] = field(default_factory=list)

    level: int = 0
    block_number: int = 1
    #: ``ProfileEvents`` —— 传了就会累加写入相关的计数器
    profile_events: Any = None
    table_uuid: str = ""
    part_uuid: str = ""

    min_bytes_for_wide_part: int = DEFAULT_MIN_BYTES_FOR_WIDE_PART
    min_rows_for_wide_part: int = DEFAULT_MIN_ROWS_FOR_WIDE_PART

    #: TTL 信息(:class:`minich.storages.merge_tree.ttl.TTLInfo`),可空
    ttl_info: Any = None
    #: 写入时是否构建跳数索引(真实 CH 在 INSERT 时**不建**)
    build_skip_indexes: bool = True

    def codec_for(self, column: str) -> str:
        return self.codecs.get(column) or DEFAULT_CODEC

    def effective_partition_id(self) -> str:
        return self.partition_id or format_partition_id(self.partition_value)


# ══════════════════════════════════════════════════════════════════════════
# 把 block 切成 granule / 排序
# ══════════════════════════════════════════════════════════════════════════


def _fixup_aggregate_column(col: IColumn, dtype: DataType) -> IColumn:
    """给刚反序列化出来的 ``AggregateFunction`` 列**接上聚合函数**。

    .. warning::

       **这一步不能省,否则 AggregatingMergeTree 的合并必然崩。**

       问题出在"状态"这个概念的两种表示上:

       .. code-block:: text

         写入时:  状态是一个 Python 对象(比如 sum 的 {"sum": 10})
                   ColumAggregateFunction.func 指向聚合函数实例
                   serialize_state(obj) → 一段字节

         读出时:  SerializationAggregateFunction.deserialize_binary
                   只能读出**字节**(状态格式是函数私有的,
                   序列化层根本不知道它属于哪个函数)
                   → ColumnAggregateFunction(None).states = [b'\x0a\x00...']

       于是 ``func.merge(state, state)`` 会在字节上做 ``+=`` 而崩掉。
       解法:列类型 ``DataTypeAggregateFunction`` 里**带着函数名与参数类型**,
       读完之后按它取出聚合函数实例,再把每个字节 blob 反序列化成真正的状态。
       真实 ClickHouse 里状态是"原始内存 + 函数知道怎么读它",
       所以不存在这一步;我们是"Python 对象 + 需要显式还原"。
    """
    from ...types.composite import DataTypeAggregateFunction
    from ...columns import ColumnAggregateFunction

    if not isinstance(dtype, DataTypeAggregateFunction):
        return col
    if not isinstance(col, ColumnAggregateFunction):
        return col
    try:
        from ...aggregates import get_aggregate_function

        fn = get_aggregate_function(dtype.func_name, dtype.params, dtype.arg_types)
    except Exception:  # pragma: no cover - 聚合函数缺失时保持原样
        return col
    col.func = fn
    col.states = [
        fn.deserialize_state(s) if isinstance(s, (bytes, bytearray)) else s
        for s in col.states
    ]
    return col


def create_column_for_type(dtype: DataType) -> IColumn:
    """建一个空列,**并接上聚合函数**(如果类型是 ``AggregateFunction``)。

    .. code-block:: text

       为什么每处都该用它而不是 ``dtype.create_column()``?

         DataTypeAggregateFunction.create_column() → ColumnAggregateFunction(None)
                                                        ↑ func 是 None!

       而写 part 时,``SerializationAggregateFunction`` 需要 ``column.func``
       才能把状态对象序列化成字节;func 为 None 时它会退化成
       ``bytes(state)`` —— 对一个整数状态(比如 sum 的 10)那等于
       ``b'\\x00' * 10``,**写出一列全零的垃圾**。

       这个坑的症状是"合并之后聚合状态变成乱码",而且**不报错**,
       所以把它收成一个工厂函数、让所有建列的地方都走它。
    """
    return _fixup_aggregate_column(dtype.create_column(), dtype)


#: 哨兵:表示"DEFAULT 表达式求不出常量"。
class _NoDefault:
    __slots__ = ()


_NO_DEFAULT = _NoDefault()


def column_expression(cdef: Any) -> Any:
    """给 ALIAS / MATERIALIZED / DEFAULT 列取它的表达式 AST。

    ``ColumnDefinition`` 只存了 ``default_expr_sql``(**文本**),
    真正求值需要一棵 AST。这里用 ``minich.sql.parse_expression_text``
    解析(那就是解释器用的同一个函数);解析器不可用就抛一个明确的错误
    —— 绝不静默返回零值。
    """
    sql = getattr(cdef, "default_expr_sql", None)
    if not sql:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Column {cdef.name!r} is not stored and has no expression; "
            f"mini-clickhouse cannot compute it at read time",
        )
    parser = None
    for importer in (
        lambda: __import__("minich.sql", fromlist=["parse_expression_text"]).parse_expression_text,
        lambda: __import__("minich.sql.parser", fromlist=["parse_expression_text"]).parse_expression_text,
    ):
        try:
            parser = importer()
            break
        except Exception:
            parser = None
        continue
    if parser is None:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Cannot evaluate the expression of column {cdef.name!r} ({sql!r}): "
            f"no SQL expression parser is available",
        )
    return parser(sql)


def try_constant_default(cdef: Any) -> Any:
    """把 DEFAULT 表达式求成**常量**;求不出来返回 ``_NO_DEFAULT``。"""
    try:
        expr = column_expression(cdef)
    except MiniChError:
        return _NO_DEFAULT
    try:
        from .expression import evaluate_constant

        return evaluate_constant(expr)
    except Exception:
        return _NO_DEFAULT


def convert_to_column_type(col: IColumn, target: DataType) -> IColumn:
    """把"算出来的默认值列"规范化到列的声明类型。

    表达式求值的类型可能与列声明不同(``MATERIALIZED dbl UInt8`` 里写
    ``v * 2``,而 ``v`` 是 ``UInt64``)→ 结果按列类型 ``normalize``,
    与 ``INSERT`` 时的行为保持一致(溢出即回转,不会静默丢精度)。
    """
    out = create_column_for_type(target)
    for i in range(col.size()):
        value = col.get(i)
        out.append(value if value is None else target.normalize(value))
    return out


def missing_column_default(cdef: Any, rows: int) -> IColumn:
    """一个"表里有、这个 part 里没有"的列该读成什么 —— **唯一的实现**。

    .. code-block:: text

       ALTER TABLE t ADD COLUMN extra UInt64 DEFAULT 7
            │
            ├── 老 part 里**没有** extra.bin(它们是在 ADD 之前写的)
            │
            └── 读的时候要补上 —— 而补什么 **不是零值那么简单**:
                  DEFAULT 7        → 老数据读出来必须是 **7**
                  DEFAULT now()    → 每次读都要重新求值
                  无 DEFAULT       → 类型零值

    .. warning::

       **合并(mutation/TTL/OPTIMIZE 的重写)也必须走这个函数。**

       以前 ``merger.MergeTreePartReader._default_column`` 用的是
       ``append_many_default()`` —— 于是:

       .. code-block:: text

          INSERT ...; INSERT ...; ALTER TABLE a ADD COLUMN w UInt64 DEFAULT 5
          SELECT w FROM a          → 5, 5      ✅(读时补默认值)
          OPTIMIZE TABLE a
          SELECT w FROM a          → 0, 0      ❌ 合并把 0 **物理写进**了新 part

       更糟的是后续的 mutation:"UPDATE w = 9 WHERE w = 5" 会因为
       那一列已经变成 0 而**匹配不到任何行**,却报告成功。

       所以缺列只允许有一个实现,读路径与合并路径都调它。

    .. warning::

       依赖其它列的 DEFAULT 表达式(``DEFAULT k * 2``)在这里退化成
       "类型零值"并打警告 —— 逐行求值需要先把整块拼出来,
       那与列裁剪冲突。真实 ClickHouse 支持任意依赖。
    """
    rows = max(0, rows)
    if getattr(cdef, "default_expr_sql", None):
        value = try_constant_default(cdef)
        if value is not _NO_DEFAULT:
            col = create_column_for_type(cdef.type)
            for _ in range(rows):
                col.append(cdef.type.normalize(value))
            return col
        _log.warning(
            f"Column {getattr(cdef, 'name', '?')!r} is missing from an old part and "
            f"its DEFAULT expression ({cdef.default_expr_sql!r}) is not constant; "
            f"falling back to the type's zero value."
        )
    col = create_column_for_type(cdef.type)
    col.append_many_default(rows)
    return col


def _materialize_block(block: Block) -> Block:
    """把所有**常量列**摊平成完整列(就地改 block,返回同一个对象)。

    .. warning::

       **这一步绝对不能省**,而且它防的是一类"静默写错数据"的事故:

       .. code-block:: text

          ColumnConst(data=[0], sizeness=100)   ← 一个"100 行的 0"
               │
               │  序列化时逐值调 column.get(i)
               ▼
          get(i) 对 ColumnConst 永远返回 data.get(0)  → 全是 0

       如果一个上层的 block 里混进了常量列(解释器做常量折叠、或者
       ``INSERT ... SELECT`` 的某个投影把整列变成了常量),
       **按"该有的值"写出去是不可能的** —— 常量列里根本没有别的值。
       所以要么在这一步摊平(此时常量列已经携带了正确的 sizeness 与值),
       要么就会写出一整列垃圾。

       真实 ClickHouse 在 ``MergeTreeDataWriter`` 里同样先做
       ``block = block.cloneWithColumns(columns.convertToFullColumns())``。
    """
    for c in block.data:
        if c.column.is_const():
            c.column = c.column.convert_to_full_column()
    return block


def _evaluate_sorting_key(
    block: Block, exprs: Sequence[Any]
) -> List[ColumnWithTypeAndName]:
    """求值排序键的每个表达式,返回"带类型的列"。

    带上类型很重要:排序键可以是**表达式**(``toYYYYMM(d)``),
    而 ``primary.idx`` 读回来时必须知道每个键列的类型,
    否则无法反序列化。真实 ClickHouse 从 ``SortDescription`` 里拿
    同样的信息。
    """
    out: List[ColumnWithTypeAndName] = []
    for e in exprs:
        c = evaluate_expression(e, block)
        if c.column.is_const():
            c = ColumnWithTypeAndName(c.name, c.type, c.column.convert_to_full_column())
        out.append(c)
    return out


def _sorting_key_columns(block: Block, exprs: Sequence[Any]) -> List[IColumn]:
    return [c.column for c in _evaluate_sorting_key(block, exprs)]


def _sort_block(block: Block, key_columns: Sequence[IColumn], reverse: Sequence[bool]) -> Block:
    """按排序键就地重排 block,返回同一个 block(已排序)。

    .. code-block:: text

       ORDER BY (date, id) DESC
                    │
       比较键 = (date, -id)   ← 方向统一成"升序"再排,避免写两套比较逻辑
                    │
       sorted(range(rows), key=...)   ← Python 的稳定排序,O(n log n)
                    │
       block.permute(indexes)

    用 :func:`sorting_key_value`(``normalize_key_value`` + "NULL 视为最大值"
    的哨兵)处理 NULL / NaN / 数组 —— 这样"排序键里的 NULL 在哪一端"
    与真实 ClickHouse 一致(NULL 最大),而且 NULL 与 int/str 比较不会抛裸
    ``TypeError``(主键是 ``Nullable`` 时的老 bug)。
    """
    rows = block.rows()
    if rows <= 1 or not key_columns:
        return block
    n = len(key_columns)

    def key_of(i: int) -> Tuple[Any, ...]:
        vals = []
        for j in range(n):
            v = sorting_key_value(key_columns[j].get(i))
            if j < len(reverse) and reverse[j]:
                v = _Descending(v)
            vals.append(v)
        return tuple(vals)

    indexes = sorted(range(rows), key=key_of)
    block.permute(indexes)
    return block


class _Descending:
    """把值反过来比 —— ``ORDER BY x DESC`` 的实现技巧(避免写第二套比较)。"""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Descending) and self.value == other.value

    def __lt__(self, other: "_Descending") -> bool:
        try:
            return other.value < self.value
        except TypeError:  # pragma: no cover
            return repr(other.value) < repr(self.value)

    def __hash__(self) -> int:  # pragma: no cover
        return hash(self.value)

    def __repr__(self) -> str:  # pragma: no cover
        return f"desc({self.value!r})"


# ══════════════════════════════════════════════════════════════════════════
# 写 part
# ══════════════════════════════════════════════════════════════════════════


def write_part(
    disk: IDisk,
    path: str,
    block: Block,
    context: PartWriteContext,
) -> "MergeTreePart":
    """把 ``block`` 写成一个 part 并**原子提交**到 ``path``。

    返回一个已经从磁盘加载回来的 :class:`MergeTreePart`
    —— 顺手做了一次"写进去就要能读回来"的自检。
    """
    if block.rows() == 0:
        raise_error(
            ErrorCode.LOGICAL_ERROR,
            "write_part called with an empty block; MergeTree never writes empty parts",
        )
    events = context.profile_events
    rows = block.rows()

    # ── ⓪ 把常量列摊平 ──────────────────────────────────────────────────
    block = _materialize_block(block)

    # ── ① 排序 ──────────────────────────────────────────────────────────
    key_cwtns = _evaluate_sorting_key(block, context.sorting_key_exprs)
    block = _sort_block(block, [c.column for c in key_cwtns], context.sorting_key_reverse)
    key_cwtns = _evaluate_sorting_key(block, context.sorting_key_exprs)

    # ── ② 分区(整块必须属于同一个分区)────────────────────────────────────
    if context.partition_value is None and context.partition_key_exprs:
        context.partition_value = _compute_partition_value(block, context)
    partition_id = context.effective_partition_id()

    # ── ③ 粒度 ──────────────────────────────────────────────────────────
    granularity = context.index_granularity or IndexGranularity()
    total_bytes = sum(c.column.byte_size() for c in block.data)
    granularity.compute_granularity(rows, total_bytes)
    granularities = list(granularity.granularities)

    # ── ④ 主键索引值(每个 granule 的第一行)──────────────────────────────
    primary_positions = context.primary_key_positions or list(range(len(key_cwtns)))
    primary_columns = [key_cwtns[p].column for p in primary_positions]
    primary_types = [key_cwtns[p].type for p in primary_positions]
    primary_names = [_key_column_name(context, p) for p in primary_positions]

    # ── ⑤ 每个 part 一个 UUID ───────────────────────────────────────────
    part_uuid = context.part_uuid or _uuid.uuid4().hex

    # 是否用 Compact 格式(见模块 docstring 的差异表)
    is_compact = _should_be_compact(rows, total_bytes, context)

    tmp_path = disk.temp_path_in(path, prefix="tmp_insert")
    disk.create_dir(tmp_path)
    try:
        _write_part_files(disk, tmp_path, block, context, granularity, granularities,
                          primary_columns, primary_types, partition_id, part_uuid,
                          is_compact, events)
        disk.move(tmp_path, path)
    except Exception:
        # 写失败 → 把临时目录清掉,不要留下半个 part
        try:
            disk.remove_dir(tmp_path)
        except Exception:  # pragma: no cover
            pass
        raise

    if events is not None:
        events.increment("MergeTreeDataWriterBlocks")
        events.increment("MergeTreeDataWriterRows", rows)

    part = load_part(
        disk, path, context.table,
        indexes=context.indexes,
        partition_key_types=context.partition_key_types,
        sorting_key_reverse=context.sorting_key_reverse,
        primary_key_columns=primary_names,
        primary_key_types=primary_types,
        table_columns=getattr(context.table, "columns", None),
    )
    return part


def _should_be_compact(rows: int, bytes_: int, context: PartWriteContext) -> bool:
    """Wide 还是 Compact?

    规则(与真实 ClickHouse 的语义一致):

    .. code-block:: text

       如果 (min_rows_for_wide_part > 0 且 rows < min_rows_for_wide_part)
       或者 (min_bytes_for_wide_part > 0 且 bytes < min_bytes_for_wide_part)
           → Compact(所有列挤进一个 data.bin)
       否则
           → Wide(每列一个 <col>.bin)

    ``min_*`` 都是 0 时**永远 Wide** —— 这是 mini-clickhouse 的默认,
    不是真实 CH 的默认(真实是 10 MiB)。
    """
    by_rows = context.min_rows_for_wide_part > 0 and rows < context.min_rows_for_wide_part
    by_bytes = context.min_bytes_for_wide_part > 0 and bytes_ < context.min_bytes_for_wide_part
    return bool(by_rows or by_bytes)


def _key_column_name(context: PartWriteContext, key_index: int) -> str:
    """排序键第 ``key_index`` 个表达式对应的**底层列名**(取不到就返回空串)。

    ``toYYYYMM(d)`` 的底层列是 ``d``;``d`` 的底层列就是 ``d``。
    只有"表达式的结果是某个落盘列"时这个函数才有意义 ——
    对函数键,``primary.idx`` 里存的是**函数结果**,与任何列都不同名。

    ``CAST`` 是**透明**的:``CAST(id AS UInt64)`` 的底层列就是 ``id``
    (与 :func:`_column_name_of_expr` 的处理一致)。
    """
    from ...sql import ast_ as A

    expr = context.sorting_key_exprs[key_index] if key_index < len(context.sorting_key_exprs) else None
    if isinstance(expr, A.Identifier):
        return expr.name
    if isinstance(expr, A.FunctionCall) and len(expr.args) == 1 and isinstance(expr.args[0], A.Identifier):
        return expr.args[0].name
    if isinstance(expr, A.CastExpr):
        # ★ 必须递归到**内层表达式**。以前这里写的是
        #   ``return _key_column_name(context, key_index)`` —— 同样的参数
        #   递归了自己,于是 ``ORDER BY CAST(id AS UInt64)`` 每一次 INSERT
        #   都直接撞 ``RecursionError``。
        return _column_name_of_expr(expr.expr)
    return ""


def _compute_partition_value(block: Block, context: PartWriteContext) -> Any:
    vals = []
    for e in context.partition_key_exprs:
        c = evaluate_expression(e, block).column
        col = c.convert_to_full_column() if c.is_const() else c
        vals.append(col.get(0))
    return vals[0] if len(vals) == 1 else tuple(vals)


def _write_part_files(
    disk: IDisk,
    tmp_path: str,
    block: Block,
    context: PartWriteContext,
    granularity: IndexGranularity,
    granularities: List[int],
    primary_columns: List[IColumn],
    primary_types: List[DataType],
    partition_id: str,
    part_uuid: str,
    is_compact: bool,
    events: Any,
) -> None:
    """把 part 的全部文件写进临时目录(此时 path 还不可见)。"""
    rows = block.rows()
    written: Dict[str, bytes] = {}   # 文件名 → 字节(小文件直接留在内存里算 checksum)

    # ── 列数据 ──────────────────────────────────────────────────────────
    # 每个 granule 的起始行号:预先算一次前缀和,避免 O(n²) 的重复求和
    granule_starts: List[int] = []
    acc = 0
    for g in granularities:
        granule_starts.append(acc)
        acc += g

    compact_marks: List[Tuple[int, int]] = []   # (comp_offset, decomp_offset)
    if is_compact:
        data_bin = bytearray()
        for gi, grow in enumerate(granularities):
            start = granule_starts[gi]
            for name in context.column_names:
                col = block.get_by_name(name).column
                payload = _serialize_rows(col, context.column_types[name], start, grow)
                comp_off = len(data_bin)
                data_bin += write_compressed_block(payload, context.codec_for(name))
                compact_marks.append((comp_off, 0))
                if events is not None:
                    events.increment("CompressedWriteBufferBlocks")
                    events.increment("UncompressedSize", len(payload))
                    events.increment("CompressionSize",
                                     len(data_bin) - comp_off - _BLOCK_HEADER.size)
        written["data.bin"] = bytes(data_bin)
        written["data.mrk3"] = _encode_mrk3(len(context.column_names), granularities,
                                            compact_marks)
    else:
        for name in context.column_names:
            col = block.get_by_name(name).column
            dtype = context.column_types[name]
            codec_name = context.codec_for(name)
            data = bytearray()
            marks: List[Mark] = []
            for gi, grow in enumerate(granularities):
                payload = _serialize_rows(col, dtype, granule_starts[gi], grow)
                compressed = write_compressed_block(payload, codec_name)
                marks.append(Mark(len(data), 0, grow))
                data += compressed
                if events is not None:
                    events.increment("CompressedWriteBufferBlocks")
                    events.increment("UncompressedSize", len(payload))
                    events.increment("CompressionSize", len(compressed) - _BLOCK_HEADER.size)
            written[f"{name}.bin"] = bytes(data)
            written[f"{name}.mrk2"] = _encode_mrk2(marks)
            if events is not None:
                events.increment("CreatedWriteBufferMergeTree")

    # ── primary.idx(列优先:先写完第 0 个键列的所有 granule 值,再写第 1 个)──
    written["primary.idx"] = _encode_primary_index(primary_columns, primary_types,
                                                   granularities)

    # ── minmax 索引(分区键列 + 主键列)──────────────────────────────────
    minmax_columns = _minmax_target_columns(context)
    minmax_values: Dict[str, Tuple[Any, Any]] = {}
    for name in minmax_columns:
        if not block.has(name):
            continue
        dtype = context.column_types[name]
        col = block.get_by_name(name).column
        written[f"minmax_{name}.idx"] = _encode_minmax(col, dtype, granularities)
        vals = [col.get(i) for i in range(col.size()) if col.get(i) is not None]
        if vals:
            try:
                minmax_values[name] = (min(vals), max(vals))
            except TypeError:  # pragma: no cover
                pass

    # ── 跳数索引 ────────────────────────────────────────────────────────
    if context.indexes and context.build_skip_indexes:
        helper = IndexHelper(context.indexes)
        for idx_name, data in helper.build_all(block, granularity).items():
            written[f"skp_idx_{idx_name}.idx"] = data

    # ── 元数据文件 ──────────────────────────────────────────────────────
    written["columns.txt"] = _encode_columns_txt(context.column_names, context.column_types)
    written["count.txt"] = str(rows).encode("ascii")
    written["partition.dat"] = _encode_partition_dat(context, block)
    written["serialization.json"] = _encode_serialization_json(context)
    written["uuid.txt"] = part_uuid.encode("ascii")
    if context.ttl_info is not None:
        written["ttl.txt"] = _encode_ttl_txt(context.ttl_info)

    # ── 小文件落盘 + 大文件流式落盘 ──────────────────────────────────────
    for filename, data in written.items():
        disk.write_file(f"{tmp_path}/{filename}", data)

    # ── checksums.txt(必须**最后**算,而且不含自己)──────────────────────
    checksums = {name: (len(data), crc32(data)) for name, data in written.items()}
    disk.write_file(f"{tmp_path}/checksums.txt", _encode_checksums_txt(checksums))


def _serialize_rows(col: IColumn, dtype: DataType, start: int, count: int) -> bytes:
    out = bytearray()
    from ...serialization import get_serialization

    get_serialization(dtype).serialize_binary(col, out, start, count)
    return bytes(out)


def _encode_mrk2(marks: Sequence[Mark]) -> bytes:
    """``.mrk2`` —— 每个 mark 三个 UInt64 小端(压缩偏移、解压偏移、行数)。

    真实 ClickHouse 在**自适应粒度**下正是这个布局(``MergeTreeMarksLoader``
    会按 ``index_granularity_bytes > 0`` 判断要不要读第三个数)。
    """
    out = bytearray()
    for m in marks:
        out += struct.pack("<QQQ", m.offset_in_compressed_file,
                           m.offset_in_decompressed_block, m.rows_in_granule)
    return bytes(out)


def _decode_mrk2(data: bytes) -> List[Mark]:
    if len(data) % 24 != 0:
        raise_error(
            ErrorCode.BAD_DATA_PART_NAME,
            f"Malformed .mrk2: size {len(data)} is not a multiple of 24",
        )
    marks = []
    for off in range(0, len(data), 24):
        comp, decomp, rows = struct.unpack_from("<QQQ", data, off)
        marks.append(Mark(comp, decomp, rows))
    return marks


def _encode_mrk3(num_columns: int, granularities: Sequence[int],
                 marks: Sequence[Tuple[int, int]]) -> bytes:
    """``data.mrk3`` —— Compact part 的位置索引。

    .. warning::

       **这不是真实 ClickHouse 的 MRK3 格式。** 真实格式有一串
       varint 组合成的"列 substream 表"与跳过索引的位置,并且
       为了省空间做了 delta 编码。我们用一个直白的布局:

       .. code-block:: text

          "MRK3"(4 字节魔数)
          varint num_columns
          varint num_marks
          num_marks × num_columns × varint(comp_offset, decomp_offset)
          num_marks × varint rows_in_granule

       好处是"用十六进制看起来一眼就懂",坏处是不能和真实 CH 互读 ——
       这在模块 docstring 的差异表里已经声明。
    """
    out = bytearray(b"MRK3")
    write_var_uint(out, num_columns)
    write_var_uint(out, len(granularities))
    for comp, decomp in marks:
        write_var_uint(out, comp)
        write_var_uint(out, decomp)
    for g in granularities:
        write_var_uint(out, g)
    return bytes(out)


def _decode_mrk3(data: bytes) -> Tuple[int, List[int], List[Tuple[int, int]]]:
    if not data.startswith(b"MRK3"):
        raise_error(ErrorCode.BAD_DATA_PART_NAME, "data.mrk3 has a bad magic header")
    pos = 4
    num_columns, pos = read_var_uint(data, pos)
    num_marks, pos = read_var_uint(data, pos)
    offsets: List[Tuple[int, int]] = []
    for _ in range(num_marks * num_columns):
        comp, pos = read_var_uint(data, pos)
        decomp, pos = read_var_uint(data, pos)
        offsets.append((comp, decomp))
    granularities: List[int] = []
    for _ in range(num_marks):
        g, pos = read_var_uint(data, pos)
        granularities.append(g)
    return num_columns, granularities, offsets


def _encode_primary_index(
    primary_columns: Sequence[IColumn],
    primary_types: Sequence[DataType],
    granularities: Sequence[int],
) -> bytes:
    """``primary.idx`` —— **列优先**存每个 granule 的第一个键值。

    .. code-block:: text

       ORDER BY (date, id),4 个 granule:
          date 列:  [d0][d1][d2][d3]     ← 先写完第一个键列的全部 granule
          id   列:  [i0][i1][i2][i3]     ← 再写第二个键列的全部 granule

       读的时候按同样的顺序反序列化 —— 真实 ClickHouse 的
       ``MergeTreeDataPart::loadIndex`` 就是这么做的(对每个键列调一次
       ``deserializeBinaryBulk``)。

    ``primary.idx`` **不压缩**:它小到可以整体放进页缓存,
    压缩反而让"每次二分都要解压"变慢。真实 CH 同理。
    """
    out = bytearray()
    offsets: List[int] = []
    acc = 0
    for grow in granularities:
        offsets.append(acc)
        acc += grow
    for col, dtype in zip(primary_columns, primary_types):
        for row in offsets:
            out += _serialize_rows(col, dtype, row, 1)
    return bytes(out)


def _encode_minmax(col: IColumn, dtype: DataType, granularities: Sequence[int]) -> bytes:
    """``minmax_<col>.idx`` —— 每个 granule 的 min/max。

    布局:每个 granule 写 ``varint(len(min)) + min + varint(len(max)) + max``。

    .. warning::

       真实 ClickHouse 用的是"整个 min 列一次序列化 + 整个 max 列一次序列化"
       (定长类型下可以直接算出偏移)。我们逐 granule 加长度前缀,
       因为变长类型(String)在多 granule 拼接时无法自描述长度 ——
       这是一个**有意的简化**,布局与真实 CH 不同。
    """
    out = bytearray()
    start = 0
    for grow in granularities:
        vals = [col.get(i) for i in range(start, start + grow)]
        vals = [v for v in vals if v is not None]
        if vals:
            try:
                lo, hi = min(vals), max(vals)
            except TypeError:  # pragma: no cover
                lo = hi = vals[0]
            lo_b = _serialize_single(lo, dtype)
            hi_b = _serialize_single(hi, dtype)
        else:
            lo_b = hi_b = b""
        write_var_uint(out, len(lo_b))
        out += lo_b
        write_var_uint(out, len(hi_b))
        out += hi_b
        start += grow
    return bytes(out)


def _serialize_single(value: Any, dtype: DataType) -> bytes:
    col = dtype.create_column()
    if not dtype.is_nothing():
        col.append(dtype.normalize(value))
    return serialize_binary(dtype, col)


def _decode_minmax(data: bytes, dtype: DataType) -> List[Tuple[Any, Any]]:
    out: List[Tuple[Any, Any]] = []
    pos = 0
    while pos < len(data):
        ln, pos = read_var_uint(data, pos)
        lo = deserialize_binary(dtype, data[pos:pos + ln], 1).get(0) if ln else None
        pos += ln
        hn, pos = read_var_uint(data, pos)
        hi = deserialize_binary(dtype, data[pos:pos + hn], 1).get(0) if hn else None
        pos += hn
        out.append((lo, hi))
    return out


def _minmax_target_columns(context: PartWriteContext) -> List[str]:
    """哪些列需要 ``minmax_<col>.idx``。

    真实 ClickHouse 为**分区键列**写 minmax(分区裁剪用),
    主键列也常写。我们把两者合起来,去重并保持表定义顺序。
    """
    names: List[str] = []
    for e in context.partition_key_exprs:
        n = _column_name_of_expr(e)
        if n and n not in names:
            names.append(n)
    for p in (context.primary_key_positions or []):
        n = _key_column_name(context, p)
        if n and n not in names:
            names.append(n)
    return [n for n in names if n in context.column_names]


def _column_name_of_expr(expr: Any) -> str:
    """分区/主键表达式里引用的那个**落盘列**(取不到返回空串)。"""
    from ...sql import ast_ as A

    if isinstance(expr, A.Identifier):
        return expr.name
    if isinstance(expr, A.FunctionCall) and len(expr.args) == 1 and \
            isinstance(expr.args[0], A.Identifier):
        return expr.args[0].name
    if isinstance(expr, A.CastExpr):
        return _column_name_of_expr(expr.expr)
    return ""


def _encode_columns_txt(names: Sequence[str], types: Dict[str, DataType]) -> bytes:
    """``columns.txt`` —— ``<名字>\\t<类型>``,一行一列。

    用 TAB 而不是空格分隔:类型名里可以出现空格
    (``Decimal(10, 2)``、``Tuple(a UInt8, b String)``),空格分隔会歧义。
    """
    lines = []
    for n in names:
        t = types.get(n)
        if t is None:
            raise_error(ErrorCode.LOGICAL_ERROR, f"No type recorded for column {n!r}")
        lines.append(f"{escape_tsv(n)}\t{escape_tsv(t.get_name())}")
    return ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape")


def _decode_columns_txt(data: bytes) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for line in data.decode("utf-8", "surrogateescape").split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise_error(
                ErrorCode.BAD_DATA_PART_NAME,
                f"Malformed columns.txt line: {line!r} (expected '<name>\\t<type>')",
            )
        out.append((unescape_tsv(parts[0]), unescape_tsv(parts[1])))
    return out


def _encode_partition_dat(context: PartWriteContext, block: Block) -> bytes:
    """``partition.dat`` —— 分区键取值的序列化形式。

    真实 ClickHouse 用**分区键的类型**序列化它,并在启动时用它快速
    重建"分区 → part"的映射(不需要重算分区表达式)。
    多列分区键:每列依次独立序列化(与 ``primary.idx`` 的列优先一致)。
    """
    value = context.partition_value
    values = value if isinstance(value, tuple) else (value,)
    out = bytearray()
    for i, v in enumerate(values):
        dtype = context.partition_key_types[i] if i < len(context.partition_key_types) else None
        if dtype is None:
            dtype = get_data_type("String")
        blob = _serialize_single(v, dtype)
        write_var_uint(out, len(blob))
        out += blob
    return bytes(out)


def _decode_partition_dat(data: bytes, types: Sequence[DataType]) -> Any:
    pos = 0
    values: List[Any] = []
    i = 0
    while pos < len(data):
        ln, pos = read_var_uint(data, pos)
        dtype = types[i] if i < len(types) else get_data_type("String")
        values.append(deserialize_binary(dtype, data[pos:pos + ln], 1).get(0))
        pos += ln
        i += 1
    return values[0] if len(values) == 1 else tuple(values)


def _encode_serialization_json(context: PartWriteContext) -> bytes:
    """``serialization.json`` —— **简化版**:列 → 编解码器名。

    真实 ClickHouse 在这里存的是每个列的完整 ``SerializationInfo``
    (序列化类名 + 编解码器链 + 是否需要"默认值标记"等),
    用它实现"同一个类型可以有多种序列化"以及向后兼容。
    我们每个类型只有一种序列化,所以只需要记住 codec。
    """
    obj = {
        "version": 1,
        "columns": {
            n: {"serialization": "Default", "codec": context.codec_for(n)}
            for n in context.column_names
        },
    }
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def _encode_ttl_txt(ttl_info: Any) -> bytes:
    """``ttl.txt`` —— TTL 的"最大/最小过期时间",真实 CH 用它做 part 级裁剪。

    真实格式是 JSON,每个动作一个时间戳:
    ``{"delete": "2024-04-01T00:00:00", "recompress": "…", "to_disk": "…"}``

    mini-clickhouse 多存一个极值,用**嵌套**形状::

        {"delete": {"min": "…", "max": "…"}, "recompress": {...}, …}

    因为 part 级判定需要两个极值(``max`` → 整块过期;``min`` → 有行要删),
    见 :meth:`TTLInfo.to_ttl_txt`。

    .. warning::

       **必须走 ``TTLInfo.to_ttl_txt()``,不能自己拼键名。** 以前这里读的是
       ``getattr(ttl_info, "delete")`` / ``"recompress"`` / ``"to_disk"``
       —— 而 :class:`TTLInfo` 上的字段叫 ``delete_at`` / ``delete_min`` /
       ``recompress_at`` … 于是每个 ``getattr`` 都拿到 ``None``,``ttl.txt``
       **永远写成 ``{}``**。下游 :func:`_ttl_info_of` 看到空 dict 就以为
       "这个 part 没有 TTL 信息",每次都退回 ``_read_part_fully`` 重算一遍
       —— 那条"一次 JSON 比较"的快路径从来没被走到过,而且完全静默。
    """
    if isinstance(ttl_info, dict):
        payload: Dict[str, Any] = ttl_info
    elif hasattr(ttl_info, "to_ttl_txt"):
        payload = ttl_info.to_ttl_txt()
    else:  # pragma: no cover - 防御性兜底:不认识的形状写成空对象
        payload = {}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def read_part_file(disk: IDisk, path: str) -> bytes:
    """读 part 目录里的一个文件;目录已被删掉时抛 ``MiniChError``。

    part 目录可能在"读到一个 part 的过程中"被合并/删除回收掉。真实
    ClickHouse 用引用计数保证"还有人在读就不删"(mini-clickhouse 的对应物
    是 :meth:`MergeTreeData.pin_parts`),但**任何**绕过引擎的读取路径
    (比如直接拿着一个旧 part 对象去读)仍然可能撞上。

    撞上时必须是带错误码的 :class:`MiniChError`:裸的 ``FileNotFoundError``
    会绕过上层所有"按错误编号分派"的处理,而且对用户来说毫无信息量。
    """
    try:
        return disk.read_file(path)
    except FileNotFoundError as exc:
        raise_error(
            ErrorCode.CANNOT_READ_FROM_ISTREAM,
            f"Cannot read {path}: the part directory is gone "
            f"(it was replaced by a merge or dropped): {exc}",
        )
    except IsADirectoryError as exc:  # pragma: no cover - 防御性兜底
        raise_error(
            ErrorCode.CANNOT_READ_FROM_ISTREAM,
            f"Cannot read {path}: expected a file, found a directory: {exc}",
        )


def _encode_checksums_txt(checksums: Dict[str, Tuple[int, int]]) -> bytes:
    """``checksums.txt`` —— 每个文件一行 ``<名字>\\t<大小>\\t<crc32>``。

    .. code-block:: text

       columns.txt     1234   3735928559
       count.txt          6   4242424242
       id.bin          8192   1111111111
       ...

    **它自己不在其中** —— 否则就成了"校验和包含了校验和"的递归。
    真实 ClickHouse 还多两列(加密的 ``file_offset`` 与
    ``file_size``),用于 ``encrypted`` 存储策略。
    """
    lines = [
        f"{escape_tsv(name)}\t{size}\t{crc}"
        for name, (size, crc) in sorted(checksums.items())
    ]
    return ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape")


def _decode_checksums_txt(data: bytes) -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = {}
    for line in data.decode("utf-8", "surrogateescape").split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            raise_error(
                ErrorCode.BAD_DATA_PART_NAME,
                f"Malformed checksums.txt line: {line!r}",
            )
        out[unescape_tsv(parts[0])] = (int(parts[1]), int(parts[2]))
    return out


# ══════════════════════════════════════════════════════════════════════════
# MergeTreePart
# ══════════════════════════════════════════════════════════════════════════


class MergeTreePart:
    """一个已提交(或刚从磁盘加载)的 part。

    这个对象是**只读**的:写入路径产出它、合并路径消费它、删除路径丢弃它,
    没有任何路径会去改它。真实 ClickHouse 里 ``IMergeTreeDataPart`` 同样
    被 ``shared_ptr`` 共享,并且**加上了引用来计数** —— 因为一个长时间
    运行的查询可能还在读它,而 merge 已经把它标记成"待删除"了。
    mini-clickhouse 不做引用计数,但会在 :class:`MergeTreeData` 里
    维护 ``active``/``obsolete`` 两份清单(见那里的文档)。
    """

    def __init__(
        self,
        name: str,
        path: str,
        disk: IDisk,
        table: Any,
    ) -> None:
        self.name = name
        self.part_name = parse_part_name(name)
        self.path = path
        self.disk = disk
        self.table = table

        #: 落盘的列名(顺序 = ``columns.txt`` 的顺序)
        self.column_names: List[str] = []
        self.column_types: Dict[str, DataType] = {}
        #: 表里"有定义但不在这个 part 里的列" —— ``ALTER ADD COLUMN`` 之后
        #: 老 part 就是这种情况,读的时候补默认值(**不重写数据**)
        self.missing_columns: List[str] = []

        self.rows = 0
        self.index_granularity = IndexGranularity()
        #: 每列的 mark 列表
        self.marks: Dict[str, List[Mark]] = {}
        #: 稀疏主键索引:每个 mark 一个键值元组
        self.primary_index: List[tuple] = []
        #: 主键列的方向(``ORDER BY a DESC`` → True)
        self.sorting_key_reverse: List[bool] = []
        #: 每个 minmax 列的 (min, max)
        self.minmax: Dict[str, Tuple[Any, Any]] = {}
        self._minmax_per_granule: Dict[str, List[Tuple[Any, Any]]] = {}
        #: 跳数索引的读取器
        self.index_readers: Dict[str, MergeTreeIndexReader] = {}
        self.checksums: Dict[str, Tuple[int, int]] = {}
        self.checksums_verified = False

        #: 哪些 mutation 已经作用在这个 part 上了(见
        #: ``MergeTreeData._apply_pending_mutations``)。重写/合并时
        #: 会把它**传递**给新 part —— 少了它,mutation 的进度就只能靠
        #: "part 名字"猜,而重写会让名字变掉,于是要么重复作用、要么静默跳过。
        self.applied_mutations: Set[str] = set()

        self.is_compact = False
        self.compact_offsets: List[Tuple[int, int]] = []
        self.codecs: Dict[str, str] = {}
        self.partition_value: Any = None
        self.part_uuid = ""
        self.ttl_info: Optional[Dict[str, Any]] = None
        self._size_cache: Optional[int] = None
        self._column_sizes: Dict[str, int] = {}

    # ── 身份 ────────────────────────────────────────────────────────────

    def get_partition_id(self) -> str:
        return self.part_name.partition_id

    def get_level(self) -> int:
        return self.part_name.level

    def get_min_block_number(self) -> int:
        return self.part_name.min_block

    def get_max_block_number(self) -> int:
        return self.part_name.max_block

    def get_name(self) -> str:
        return self.name

    def get_uuid(self) -> str:
        return self.part_uuid

    # ── 规模 ────────────────────────────────────────────────────────────

    def rows_count(self) -> int:
        return self.rows

    def is_empty(self) -> bool:
        return self.rows == 0

    def get_marks_count(self) -> int:
        if not self.marks:
            return self.index_granularity.get_mark_count()
        return len(next(iter(self.marks.values())))

    def get_index_granularity(self) -> IndexGranularity:
        return self.index_granularity

    def get_index_granularity_rows(self) -> int:
        return self.index_granularity.index_granularity

    def bytes_on_disk(self) -> int:
        """part 在磁盘上的**总**字节数(所有文件,含元数据)。"""
        if self._size_cache is None:
            total = 0
            for f in self.disk.list_dir(self.path):
                if self.disk.exists_directory(f"{self.path}/{f}"):
                    continue
                total += self.disk.file_size(f"{self.path}/{f}")
            self._size_cache = total
        return self._size_cache

    def column_bytes_on_disk(self, column: str) -> int:
        """某一列的数据 + 索引占多少字节。

        .. note::

           这是"列裁剪能省多少 IO"的直接量化依据 ——
           ``system.parts.columns`` 里那个 ``bytes_on_disk`` 列。
           Compact part 所有列挤在一个 ``data.bin`` 里,
           这个函数返回 0(并说明原因)。
        """
        if self.is_compact:
            return 0
        total = 0
        for suffix in (".bin", ".mrk2", ".idx"):
            name = f"{column}{suffix}" if suffix != ".idx" else f"minmax_{column}.idx"
            size = self.checksums.get(name)
            if size is not None:
                total += size[0]
        return total

    def column_compressed_size(self, column: str) -> int:
        """某一列 ``.bin`` 的压缩后字节数(不含索引)。"""
        entry = self.checksums.get(f"{column}.bin")
        return entry[0] if entry else 0

    def uncompressed_size(self) -> int:
        """全体列的**解压后**字节数(用于算压缩率)。

        真实 ClickHouse 把这个值记在 ``system.parts`` 的
        ``bytes_uncompressed`` 列里,写入时就统计好。我们不做增量统计
        (那需要在 ``.mrk2`` 之外再存一份),而是在第一次需要时
        **真解压一遍**量出来 —— 对一个 part 只有几 MB 的教学实现,
        这个代价可以接受;真实 CH 不行(它有 TB 级 part)。
        """
        total = 0
        for name, marks in self.marks.items():
            dtype = self.column_types.get(name)
            if dtype is None:
                continue
            blob = self.disk.read_file(
                f"{self.path}/{'data.bin' if self.is_compact else name + '.bin'}"
            )
            for m in marks:
                payload, _ = read_compressed_block(blob, m.offset_in_compressed_file)
                total += len(payload)
        return total

    def get_stats(self) -> Dict[str, Any]:
        """``system.parts`` 一行所需要的信息。"""
        return {
            "name": self.name,
            "partition": self.get_partition_id(),
            "rows": self.rows,
            "marks": self.get_marks_count(),
            "bytes_on_disk": self.bytes_on_disk(),
            "level": self.get_level(),
            "type": "Compact" if self.is_compact else "Wide",
            "part_uuid": self.part_uuid,
        }

    # ── 列 ──────────────────────────────────────────────────────────────

    def has_column(self, column: str) -> bool:
        return column in self.column_types

    def get_column_type(self, column: str) -> Optional[DataType]:
        return self.column_types.get(column)

    def get_minmax_value(self, column: str) -> Optional[Tuple[Any, Any]]:
        """该列的 part 级 ``(min, max)`` —— 分区/part 级裁剪用。

        真实 ClickHouse 在读 part 时就从 ``minmax_<col>.idx`` 里
        取出第一/最后一个 granule 的极值。我们直接把写的时候算好的
        整体极值存下来。
        """
        return self.minmax.get(column)

    def get_minmax_for_granule(self, column: str, granule: int) -> Optional[Tuple[Any, Any]]:
        vals = self._minmax_per_granule.get(column)
        if vals is None or granule >= len(vals):
            return None
        return vals[granule]

    # ── 稀疏索引 ────────────────────────────────────────────────────────

    def get_index_value(self, mark: int) -> Optional[tuple]:
        """``primary.idx`` 的第 ``mark`` 行 —— 越界返回 ``None``。

        ``KeyCondition`` 的二分就调用它。返回 ``None`` 表示"这里已经是
        part 的末尾",二分时会被当作"正无穷"(所有取值都更小)。
        """
        if mark < 0 or mark >= len(self.primary_index):
            return None
        return self.primary_index[mark]

    def get_mark(self, column: str, mark: int) -> Mark:
        marks = self.marks.get(column)
        if marks is None:
            raise_error(
                ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                f"Part {self.name} has no column {column!r}",
            )
        if mark < 0 or mark >= len(marks):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Mark {mark} out of range for column {column!r} "
                f"(part has {len(marks)} marks)",
            )
        return marks[mark]

    # ── 读 ──────────────────────────────────────────────────────────────

    def read_column(
        self,
        ctx: Optional[ReadContext],
        column: str,
        mark_range: MarkRange,
        events: Any = None,
    ) -> IColumn:
        """只读**指定 granule 区间**的**一列**。

        这就是"列裁剪 + granule 裁剪"两个优化**真正兑现**的地方:

        .. code-block:: text

           read_column(ctx, 'd', MarkRange(7, 12))
              │
              ├─ 别的 19 列的 .bin **根本不会被打开**      ← 列裁剪
              │
              └─ 只 seek 到 d.bin 的第 7 个压缩块开始,
                 解压到第 11 个块结束                        ← granule 裁剪
                 (前面的 0..6 个块不会被解压)

        ``mark_range`` 必须是**已经收口**的(``end`` 不是 ``RANGE_MAX``),
        因为要算 IO 偏移。
        """
        if not self.has_column(column):
            raise_error(
                ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                f"Column {column!r} does not exist in part {self.name} "
                f"(available: {', '.join(self.column_names)})",
            )
        dtype = self.column_types[column]
        marks = self.marks[column]
        begin = mark_range.begin
        end = mark_range.end
        if end == RANGE_MAX and end > len(marks):
            end = len(marks)
        if begin < 0 or begin > end or end > len(marks):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"MarkRange [{begin}, {end}) out of bounds for column {column!r} "
                f"(part has {len(marks)} marks)",
            )
        out = dtype.create_column()
        # 文件**只读一次** —— 在 granule 循环里再读就是 O(granules × 文件大小)。
        # 真实 CH 用带 seek 的 ReadBuffer 只读需要的字节;我们读整文件 +
        # 按偏移解压,IO 略多但语义一致(见 disk.py 的说明)。
        blob = read_part_file(self.disk, f"{self.path}/{'data.bin' if self.is_compact else column + '.bin'}")
        for m in range(begin, end):
            mark = marks[m]
            comp_off = mark.offset_in_compressed_file
            payload, _ = read_compressed_block(blob, comp_off)
            col = deserialize_binary(dtype, payload, mark.rows_in_granule)
            col = _fixup_aggregate_column(col, dtype)
            out.insert_range_from(col, 0, col.size())
            if events is not None:
                events.increment("CompressedReadBufferBlocks")
                events.increment("ReadCompressedBytes", mark.rows_in_granule)
                events.increment("ReadUncompressedBytes", len(payload))
                events.increment("CreatedReadBufferMergeTree")
        return out

    def read_columns(
        self,
        ctx: Optional[ReadContext],
        columns: Sequence[str],
        mark_range: MarkRange,
        events: Any = None,
    ) -> Dict[str, IColumn]:
        """一次读多列(共享同一段 mark range)。"""
        return {c: self.read_column(ctx, c, mark_range, events) for c in columns}

    def _read_compact_block(self, column: str, mark: int, m: Mark) -> bytes:
        col_index = self.column_names.index(column)
        idx = mark * len(self.column_names) + col_index
        comp_off = self.compact_offsets[idx][0]
        blob = read_part_file(self.disk, f"{self.path}/data.bin")
        payload, _ = read_compressed_block(blob, comp_off)
        return payload

    def _deserialize(self, dtype: DataType, payload: bytes, rows: int) -> Tuple[IColumn, int]:
        return deserialize_binary(dtype, payload, rows), len(payload)

    def read_column_by_row_range(
        self, ctx: Optional[ReadContext], column: str, begin_row: int, end_row: int,
        events: Any = None,
    ) -> IColumn:
        """按**行区间**读一列 —— 内部先换算成 mark,再按行裁掉多余部分。

        上层(``MergeTreeData.read``)要的是"一行一行拼满一个 block",
        所以这个函数负责把"多读出来的那几行"切掉。
        """
        mr = self.index_granularity.mark_range_for_rows(begin_row, end_row)
        col = self.read_column(ctx, column, mr, events)
        granule_start = self.index_granularity.get_rows_before_mark(mr.begin)
        offset = begin_row - granule_start
        count = end_row - begin_row
        if offset == 0 and count == col.size():
            return col
        out = col.clone_empty()
        out.insert_range_from(col, offset, count)
        return out

    # ── 校验 ────────────────────────────────────────────────────────────

    def verify_checksums(self) -> None:
        """重算每个文件的 CRC32 并与 ``checksums.txt`` 比对。

        不一致就抛 ``CHECKSUM_DOESNT_MATCH`` —— 真实 ClickHouse 的
        ``CHECK TABLE`` / ``MergeTreeDataPart::checkConsistency`` 做同样的事。
        这是"静默的数据损坏"唯一能被发现的时刻:

        .. code-block:: text

           磁盘坏道 / 写了一半就被断电 / 有人手改了文件
                    │
                    ▼
           verify_checksums()  →  CHECKSUM_DOESNT_MATCH
                                  ↑ 计数到 "expected …, got …"

        **没有这一步,损坏会一路流到查询结果里** —— 那才是最可怕的。
        """
        if not self.checksums:
            return
        errors: List[str] = []
        for filename, (expected_size, expected_crc) in sorted(self.checksums.items()):
            full = f"{self.path}/{filename}"
            if not self.disk.exists(full):
                errors.append(f"{filename}: file is missing")
                continue
            data = self.disk.read_file(full)
            if len(data) != expected_size:
                errors.append(
                    f"{filename}: size mismatch (expected {expected_size}, got {len(data)})"
                )
                continue
            if crc32(data) != expected_crc:
                errors.append(
                    f"{filename}: checksum mismatch "
                    f"(expected {expected_crc:#010x}, got {crc32(data):#010x})"
                )
        if errors:
            raise_error(
                ErrorCode.CHECKSUM_DOESNT_MATCH,
                f"Part {self.name} failed checksum verification: " + "; ".join(errors),
            )
        self.checksums_verified = True

    # ── 展示 ────────────────────────────────────────────────────────────

    def describe(self) -> str:
        return (
            f"Part {self.name}: {self.rows} rows, {self.get_marks_count()} marks, "
            f"{len(self.column_names)} columns, "
            f"{'Compact' if self.is_compact else 'Wide'}, "
            f"{self.bytes_on_disk()} bytes"
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MergeTreePart {self.name} rows={self.rows}>"


# ══════════════════════════════════════════════════════════════════════════
# 加载 part(**不读列数据**)
# ══════════════════════════════════════════════════════════════════════════


def load_part(disk: IDisk, path: str, table: Any = None,
              indexes: Optional[Sequence[IndexDescription]] = None,
              partition_key_types: Optional[Sequence[DataType]] = None,
              sorting_key_reverse: Optional[Sequence[bool]] = None,
              primary_key_columns: Optional[Sequence[str]] = None,
              primary_key_types: Optional[Sequence[DataType]] = None,
              table_columns: Optional[Sequence[Any]] = None) -> MergeTreePart:
    """从磁盘恢复一个 part 的**元数据**,一行列数据都不读。

    这一步很关键:ClickHouse 启动时要在几秒内"认识"几百万个 part,
    它不可能去读数据。所以启动路径只读这些小文件:

    .. code-block:: text

       columns.txt      有哪些列、什么类型
       count.txt        多少行        ← 于是 count() 不用扫数据
       <col>.mrk2       每列每个 granule 的位置
       primary.idx      稀疏索引 ← KeyCondition 二分要用
       minmax_*.idx     part 级极值
       checksums.txt    各文件 size + crc
       partition.dat    分区取值
       serialization.json 每列的 codec

    这些都是"KB 级"的文件,加起来比数据小几个数量级。
    """
    name = path.replace("\\", "/").rstrip("/").split("/")[-1]
    part = MergeTreePart(name, path, disk, table)
    part.partition_key_types = list(partition_key_types or [])

    # ── columns.txt ─────────────────────────────────────────────────────
    cols_data = disk.read_file(f"{path}/columns.txt")
    columns = _decode_columns_txt(cols_data)
    part.column_names = [n for n, _ in columns]
    for n, tn in columns:
        part.column_types[n] = get_data_type(tn)

    # 表里定义了、但 part 里没有的列(ALTER ADD COLUMN 之后的老 part)
    if table_columns is not None:
        for c in table_columns:
            cname = getattr(c, "name", c)
            if cname not in part.column_types:
                part.missing_columns.append(cname)

    # ── count.txt ───────────────────────────────────────────────────────
    part.rows = int(disk.read_file(f"{path}/count.txt").decode("ascii").strip())

    # ── 是不是 Compact ──────────────────────────────────────────────────
    part.is_compact = disk.exists(f"{path}/data.bin")

    # ── marks ───────────────────────────────────────────────────────────
    granularities: List[int] = []
    if part.is_compact:
        num_columns, granularities, offsets = _decode_mrk3(
            disk.read_file(f"{path}/data.mrk3")
        )
        part.compact_offsets = offsets
        for i, n in enumerate(part.column_names):
            part.marks[n] = [
                Mark(offsets[g * num_columns + i][0], offsets[g * num_columns + i][1],
                     granularities[g])
                for g in range(len(granularities))
            ]
    else:
        for n in part.column_names:
            marks = _decode_mrk2(disk.read_file(f"{path}/{n}.mrk2"))
            part.marks[n] = marks
            if marks:
                granularities = [m.rows_in_granule for m in marks]
    if granularities:
        part.index_granularity.set_granularities(granularities)
    else:
        part.index_granularity.total_rows = part.rows

    # ── primary.idx ─────────────────────────────────────────────────────
    key_types = list(primary_key_types or [])
    if not key_types and primary_key_columns:
        key_types = [part.column_types[n] for n in primary_key_columns
                     if n in part.column_types]
    part.primary_key_columns = list(primary_key_columns or [])
    part.primary_index = _decode_primary_index(
        disk.read_file(f"{path}/primary.idx"),
        key_types,
        part.index_granularity,
    )
    part.sorting_key_reverse = list(sorting_key_reverse or [])

    # ── minmax ──────────────────────────────────────────────────────────
    for n in part.column_names:
        fname = f"minmax_{n}.idx"
        if disk.exists(f"{path}/{fname}"):
            per = _decode_minmax(disk.read_file(f"{path}/{fname}"),
                                 part.column_types[n])
            part._minmax_per_granule[n] = per
            vals = [p for p in per if p[0] is not None]
            if vals:
                try:
                    part.minmax[n] = (
                        min(p[0] for p in vals), max(p[1] for p in vals)
                    )
                except TypeError:  # pragma: no cover
                    pass

    # ── 跳数索引 ────────────────────────────────────────────────────────
    if indexes:
        helper = IndexHelper(indexes)
        for d in indexes:
            fname = f"skp_idx_{d.name}.idx"
            if disk.exists(f"{path}/{fname}"):
                # 传的是"几个 granule 一组"(= 索引声明里的 GRANULARITY),
                # **不是**行数 —— 行数在自适应粒度下是变化的,
                # 用它算组边界会让 mark→组 的映射错位 → 丢数据。
                part.index_readers[d.name] = helper.load(
                    d.name, disk.read_file(f"{path}/{fname}"),
                    max(1, int(getattr(d, "granularity", 1) or 1)),
                )

    # ── 分区取值 ────────────────────────────────────────────────────────
    if disk.exists(f"{path}/partition.dat") and part.partition_key_types:
        part.partition_value = _decode_partition_dat(
            disk.read_file(f"{path}/partition.dat"), part.partition_key_types
        )
    else:
        part.partition_value = part.part_name.partition_id

    # ── 其它元数据 ──────────────────────────────────────────────────────
    if disk.exists(f"{path}/uuid.txt"):
        part.part_uuid = disk.read_file(f"{path}/uuid.txt").decode("ascii")
    if disk.exists(f"{path}/checksums.txt"):
        part.checksums = _decode_checksums_txt(disk.read_file(f"{path}/checksums.txt"))
    if disk.exists(f"{path}/serialization.json"):
        try:
            obj = json.loads(disk.read_file(f"{path}/serialization.json").decode("utf-8"))
            for n, info in obj.get("columns", {}).items():
                part.codecs[n] = info.get("codec", DEFAULT_CODEC)
        except Exception:  # pragma: no cover - 元数据坏了不该让加载失败
            pass
    if disk.exists(f"{path}/ttl.txt"):
        try:
            part.ttl_info = json.loads(disk.read_file(f"{path}/ttl.txt").decode("utf-8"))
        except Exception:  # pragma: no cover
            part.ttl_info = {}

    return part


def _avg_rows(part: MergeTreePart) -> int:
    """一个 part 的**平均** granule 行数 —— 只用于展示/估算。

    .. warning::

       **不要拿它去算跳数索引的组边界。** 自适应粒度下每个 granule
       的行数都不同,"平均行数"会让组与 granule 错位,进而产生
       **false negative(跳过含有匹配行的 granule → 丢数据)**。
       组边界只能由 ``IndexGranularity.granularities`` 精确给出,
       见 :func:`minich.storages.merge_tree.skip_index._build_groups`。
    """
    marks = part.get_marks_count()
    if marks:
        return max(1, part.rows // marks)
    return DEFAULT_INDEX_GRANULARITY


def _decode_primary_index(
    data: bytes,
    key_types: Sequence[DataType],
    granularity: IndexGranularity,
) -> List[tuple]:
    """``primary.idx`` 读回来 —— 列优先,每个键列依次读出所有 granule 的值。

    需要知道"有几个键列、各自什么类型"。这两个信息真实 CH 从
    ``primary_key`` 的表达式拿;调用方(``load_part`` / ``write_part``)
    在写入时就算好了类型,存在 ``serialization.json`` 之外的参数里传进来。
    """
    if not key_types or not data:
        return []
    marks = granularity.get_mark_count()
    if marks == 0:
        return []
    per_column: List[List[Any]] = []
    pos = 0
    for t in key_types:
        vals = []
        for _ in range(marks):
            col, pos = deserialize_from(data, t, pos)
            vals.append(col.get(0))
        per_column.append(vals)
    out: List[tuple] = []
    for m in range(marks):
        out.append(tuple(per_column[j][m] for j in range(len(key_types))))
    return out


def deserialize_from(data: bytes, dtype: DataType, pos: int) -> Tuple[IColumn, int]:
    """从 ``data[pos:]`` 解出 1 行,返回 ``(列, 新位置)``。"""
    from ...serialization import get_serialization

    return get_serialization(dtype).deserialize_binary(data, 1, pos)
