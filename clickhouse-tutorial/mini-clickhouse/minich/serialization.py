"""序列化 —— **列 → 字节** 与 **字节 → 列**。

这是整个存储层的地基。ClickHouse 的序列化有一组贯穿全代码库的设计约束,
理解它们就理解了"为什么 ClickHouse 的文件长得那么怪":

.. code-block:: text

   约束 1  按**列**序列化,不按行
           一个 part 里,url 列的全部值连续写在一起,
           然后才是 ts 列的全部值。这就是"列存"的字面含义。

   约束 2  序列化是**流式**的,而且**可以只读一部分**
           read(buf, from, to, n) 允许"跳过前 100 万个值,只解出后 10 个"
           —— 因为 granule 定位之后,这是真的发生的。

   约束 3  变长结构要用**偏移数组**而不是分隔符
           String 写成 [长度][字节],Array 写成 [累积偏移][元素...],
           所以反序列化**不需要扫描整个文件**就能跳到第 i 个值。

   约束 4  格式由**类型**决定,不由数据决定
           同一个列类型在任何 part 里的字节布局完全相同 ——
           这是"零拷贝 mmap 读"的前提。

.. note::

   真实 ClickHouse 的序列化在 ``src/DataTypes/Serializations/`` 下,
   而且有一个我们这里没有的复杂度:**同一个类型可以有多种序列化**
   (``SerializationLowCardinality``、``SerializationSparse``、
   ``SerializationObject``…),由 ``SerializationInfo`` 在
   part 写入时选择并**记在 part 的元数据里**。mini-clickhouse
   每个类型只有一种序列化 —— 但键的编码(:mod:`minich.util.keys`)
   和文本格式(:mod:`minich.formats`)是分开的两套,这一点与真实一致。
"""

from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from typing import Any, BinaryIO, List, Optional, Sequence

import numpy as np

from .columns import (
    ColumnAggregateFunction,
    ColumnArray,
    ColumnBigInt,
    ColumnConst,
    ColumnFixedString,
    ColumnLowCardinality,
    ColumnMap,
    ColumnNullable,
    ColumnString,
    ColumnTuple,
    ColumnUUID,
    ColumnVector,
    IColumn,
)
from .errors import ErrorCode, MiniChError, raise_error
from .types.base import DataType

__all__ = [
    "ISerialization",
    "ISerializationPtr",
    "SerializeBinaryBulkState",
    "get_serialization",
    "deserialize_binary",
    "serialize_binary",
    "write_var_uint",
    "read_var_uint",
    "VarUIntReader",
]


# ══════════════════════════════════════════════════════════════════════════
# varint —— ClickHouse 的变长整数
# ══════════════════════════════════════════════════════════════════════════


def write_var_uint(out: bytearray, value: int) -> None:
    """写一个 LEB128 变长无符号整数。

    ``0``–``127`` 占 1 字节,``128``–``16383`` 占 2 字节,以此类推。
    真实 ClickHouse 的 ``writeVarUInt`` 就是这套编码,
    它用在 ``String`` 的长度前缀、``Array`` 的偏移、``Native`` 的块大小上。
    """
    if value < 0:
        raise_error(ErrorCode.LOGICAL_ERROR, f"write_var_uint got a negative value: {value}")
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def read_var_uint(data: bytes, pos: int = 0) -> tuple:
    """读一个变长无符号整数,返回 ``(值, 新位置)``。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise_error(ErrorCode.UNEXPECTED_END_OF_FILE, "Truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise_error(ErrorCode.CANNOT_PARSE_NUMBER, "Varint is too long")


class VarUIntReader:
    """带游标的字节读取器 —— 反序列化时的"当前指针"。"""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise_error(
                ErrorCode.UNEXPECTED_END_OF_FILE,
                f"Tried to read {n} bytes at offset {self.pos} but only "
                f"{len(self.data) - self.pos} are available",
            )
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def read_var_uint(self) -> int:
        v, self.pos = read_var_uint(self.data, self.pos)
        return v

    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def remaining(self) -> int:
        return len(self.data) - self.pos


# ══════════════════════════════════════════════════════════════════════════
# 序列化接口
# ══════════════════════════════════════════════════════════════════════════


class SerializeBinaryBulkState:
    """批量序列化的**跨块状态**。

    真实 ClickHouse 的 ``ISerialization::SerializeBinaryBulkState`` 用来在
    "一个 part 分多次写出"时保存上下文 —— 例如:

    * ``SerializeBinaryBulkStatePrefix`` 写"前缀"(比如变长结构的元信息);
    * ``...Suffix`` 写"后缀";
    * 中间状态(比如 LowCardinality 的字典)在多个块之间累积。

    mini-clickhouse 的实现里,真正需要跨块状态的只有
    :class:`SerializationLowCardinality`(字典去重)。其余类型
    返回 ``None`` 即可。
    """

    __slots__ = ("data",)

    def __init__(self, **data: Any) -> None:
        self.data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class ISerialization(ABC):
    """序列化接口 —— 与真实 ClickHouse 的 ``ISerialization`` 同名同义。"""

    #: 是否需要把"默认值"编码成特殊标记(ClickHouse 的 sparse/default 优化)
    supports_sparse: bool = False

    @abstractmethod
    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        """把 ``column[start : start+count]`` 写进 ``out``(小端)。"""

    @abstractmethod
    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        """从 ``data[pos:]`` 解出 ``num_rows`` 行,返回 ``(column, 新位置)``。"""

    # ── 文本(单值)────────────────────────────────────────────────────

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"{type(self).__name__} does not implement serialize_text",
        )

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"{type(self).__name__} does not implement deserialize_text",
        )

    # ── 前缀/后缀 ──────────────────────────────────────────────────────

    def serialize_binary_bulk_prefix(
        self, column: IColumn, out: bytearray, state: Optional[SerializeBinaryBulkState]
    ) -> None:
        """写 part 级的"前缀"—— 默认什么都不写。"""

    def serialize_binary_bulk_suffix(
        self, column: IColumn, out: bytearray, state: Optional[SerializeBinaryBulkState]
    ) -> None:
        """写 part 级的"后缀"—— 默认什么都不写。"""

    def create_bulk_state(self) -> Optional[SerializeBinaryBulkState]:
        return None

    # ── 声明的大小 ──────────────────────────────────────────────────────

    def has_fixed_size(self) -> bool:
        """是否是定长编码 —— 定长才能做"随机位置直接读"。"""
        return False

    def fixed_size(self) -> int:
        return 0


ISerializationPtr = ISerialization


# ══════════════════════════════════════════════════════════════════════════
# 定长类型
# ══════════════════════════════════════════════════════════════════════════


class SerializationNumber(ISerialization):
    """定长数值 —— 直接按小端写出定长缓冲区。

    这是最快的一种序列化:整列一次 ``np.ndarray.tobytes()``,
    没有任何逐值循环。**这也是"列存为什么快"的最直接证据** ——
    对比一下 :class:`SerializationString`:它必须逐值拼字节。
    """

    def __init__(self, dtype: np.dtype, size: Optional[int] = None) -> None:
        self.dtype = np.dtype(dtype)
        self._size = size if size is not None else self.dtype.itemsize

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        if count == 0:
            return
        if isinstance(column, ColumnVector) and column.dtype == self.dtype:
            arr = column.numpy_view()[start : start + count]
            out += arr.astype(self.dtype).tobytes()
            return
        # 退路:逐值打包(常量列、稀疏列走到这里)
        fmt = "<" + {1: "B", 2: "H", 4: "I", 8: "Q"}[self._size]
        for i in range(start, start + count):
            out += struct.pack(fmt, int(column.get(i)))

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        nbytes = num_rows * self._size
        if pos + nbytes > len(data):
            raise_error(
                ErrorCode.UNEXPECTED_END_OF_FILE,
                f"Not enough bytes for {num_rows} values of {self.dtype}: "
                f"need {nbytes}, have {len(data) - pos}",
            )
        raw = data[pos : pos + nbytes]
        arr = np.frombuffer(raw, dtype=self.dtype).copy()
        return ColumnVector(self.dtype, arr), pos + nbytes

    def has_fixed_size(self) -> bool:
        return True

    def fixed_size(self) -> int:
        return self._size


class SerializationBigInt(ISerialization):
    """128/256 位整数的序列化 —— 小端定长。"""

    def __init__(self, bits: int, signed: bool = True) -> None:
        self.bits = bits
        self.signed = signed

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        width = self.bits // 8
        mask = (1 << self.bits) - 1
        for i in range(start, start + count):
            v = int(column.get(i)) & mask
            out += v.to_bytes(width, "little")

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        width = self.bits // 8
        col = ColumnBigInt(self.signed, self.bits)
        for _ in range(num_rows):
            if pos + width > len(data):
                raise_error(ErrorCode.UNEXPECTED_END_OF_FILE, "Truncated big integer")
            v = int.from_bytes(data[pos : pos + width], "little")
            if self.signed and v >= (1 << (self.bits - 1)):
                v -= 1 << self.bits
            col.append(v)
            pos += width
        return col, pos

    def has_fixed_size(self) -> bool:
        return True

    def fixed_size(self) -> int:
        return self.bits // 8


class SerializationUUID(ISerialization):
    """``UUID`` —— 16 字节小端。"""

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        for i in range(start, start + count):
            out += (int(column.get(i)) & ((1 << 128) - 1)).to_bytes(16, "little")

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        col = ColumnUUID()
        for _ in range(num_rows):
            col.append(int.from_bytes(data[pos : pos + 16], "little"))
            pos += 16
        return col, pos

    def has_fixed_size(self) -> bool:
        return True

    def fixed_size(self) -> int:
        return 16


class SerializationFixedString(ISerialization):
    """``FixedString(N)`` —— N 字节定长,尾部补零。"""

    def __init__(self, n: int) -> None:
        self.n = n

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        if isinstance(column, ColumnFixedString) and column.n == self.n:
            begin = start * self.n
            out += column.chars[begin : begin + count * self.n]
            return
        for i in range(start, start + count):
            data = str(column.get(i)).encode("utf-8", "surrogateescape")[: self.n]
            out += data + b"\x00" * (self.n - len(data))

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        nbytes = num_rows * self.n
        if pos + nbytes > len(data):
            raise_error(ErrorCode.UNEXPECTED_END_OF_FILE, "Truncated FixedString column")
        col = ColumnFixedString(self.n)
        col.chars += data[pos : pos + nbytes]
        return col, pos + nbytes

    def has_fixed_size(self) -> bool:
        return True

    def fixed_size(self) -> int:
        return self.n


# ══════════════════════════════════════════════════════════════════════════
# String
# ══════════════════════════════════════════════════════════════════════════


class SerializationString(ISerialization):
    """``String`` —— ``[varint 长度][字节]`` 重复。

    .. note::

       真实 ClickHouse 的 ``SerializationString`` 用的是
       ``[varint 长度][数据]`` 的**交错**布局,而**不是**我们先前提到的
       "offsets + chars" 布局! 这是两件不同的事:

       * **内存里**(``ColumnString``)用 offsets + chars,为了随机访问快;
       * **磁盘上**用长度前缀,为了**流式写**(写第 i 个值时不需要
         回头改 offsets)与**逐值跳过**。

       这个"内存布局 ≠ 磁盘布局"的落差在 ClickHouse 里到处都是,
       是理解它存储格式的关键。第 05 章会画一张对照表。
    """

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        if isinstance(column, ColumnString):
            for i in range(start, start + count):
                s, e = column.offsets[i - 1] if i > 0 else 0, column.offsets[i]
                write_var_uint(out, e - s)
                out += column.chars[s:e]
            return
        for i in range(start, start + count):
            data = str(column.get(i)).encode("utf-8", "surrogateescape")
            write_var_uint(out, len(data))
            out += data

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        col = ColumnString()
        for _ in range(num_rows):
            length, pos = read_var_uint(data, pos)
            if pos + length > len(data):
                raise_error(
                    ErrorCode.UNEXPECTED_END_OF_FILE,
                    f"Truncated String value: expected {length} bytes, "
                    f"have {len(data) - pos}",
                )
            col.append_data(data[pos : pos + length])
            pos += length
        return col, pos


# ══════════════════════════════════════════════════════════════════════════
# 复合类型
# ══════════════════════════════════════════════════════════════════════════


class SerializationNullable(ISerialization):
    """``Nullable(T)`` —— **先写嵌套列,再写空位图**。

    .. code-block:: text

       values = [1, NULL, 3]
       磁盘布局:  [嵌套列: 1, 0, 3]  [空位图: 0, 1, 0]

    顺序很重要,而且与直觉相反 —— 为什么不是"空位图在前"?
    因为真实 ClickHouse 的实现里,读写是**分两遍**做的:
    第一遍处理嵌套数据(可以整块 memcpy),第二遍写位图(一个字节一个)。
    把位图放最后,意味着"读数据块"和"读位图块"可以在磁盘上**分别连续**,
    两个都是顺序 IO。

    mini-clickhouse 照抄这个顺序 —— 这样"用 hexdump 看 part 文件"
    的演示才能和真实 CH 的解释对上。
    """

    def __init__(self, nested: ISerialization) -> None:
        self.nested = nested

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        if not isinstance(column, ColumnNullable):
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"SerializationNullable got a {type(column).__name__}",
            )
        self.nested.serialize_binary(column.nested, out, start, count)
        for i in range(start, start + count):
            out.append(1 if column.null_map[i] else 0)

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        nested, pos = self.nested.deserialize_binary(data, num_rows, pos)
        if pos + num_rows > len(data):
            raise_error(ErrorCode.UNEXPECTED_END_OF_FILE, "Truncated null map")
        null_map = data[pos : pos + num_rows]
        col = ColumnNullable(nested)
        col.null_map = [bool(b) for b in null_map]
        return col, pos + num_rows


class SerializationArray(ISerialization):
    """``Array(T)`` —— ``[累积偏移(见下方说明)][元素...]``。

    布局的两个要点:

    1. 偏移写的是**累积元素个数**,所以"第 i 个数组的长度" =
       ``offsets[i] - offsets[i-1]``;
    2. 偏移的个数决定了"怎么算最后一个数组的长度"。

    .. warning::

       **这里有一处与真实 ClickHouse 不同的取舍,必须写清楚。**

       真实 ClickHouse 只写 **rows − 1** 个偏移,最后一个数组的长度
       靠**列级元数据**(每个列流有自己的 size 信息)推出来。
       这在它自己的 part 格式里没问题,因为列的长度是**单独记录的**。

       mini-clickhouse 的序列化是"给定 ``num_rows`` 自己解出全部信息"
       (没有列级 size 记录),所以少写一个偏移就会**丢掉最后一个数组的
       元素** —— 这是一个非常隐蔽的错误:``[[], [1,2,3], [255], [0]]``
       会解成 ``[[], [1,2,3], [255], []]``,**不报错,只是数据少了**。
       (这个 bug 在实现这一层时确实出现过,被格式往返测试抓了出来。)

       所以我们写 **rows** 个偏移(把最后一行的累积量也写出去),
       代价是每个数组列多一个 varint,换来的是**自描述**。
       格式不兼容真实 ClickHouse 的 ``Native``,这一点写在
       ``formats/binary_formats.py`` 的警告里。
    """

    def __init__(self, nested: ISerialization) -> None:
        self.nested = nested

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        if not isinstance(column, ColumnArray):
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"SerializationArray got a {type(column).__name__}",
            )
        base = column.offsets[start - 1] if start > 0 else 0
        for i in range(start, start + count):
            write_var_uint(out, column.offsets[i] - base)
        total = column.offsets[start + count - 1] - base if count else 0
        self.nested.serialize_binary(column.nested, out, base, total)

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        offsets: List[int] = []
        for _ in range(num_rows):
            v, pos = read_var_uint(data, pos)
            offsets.append(v)
        total = offsets[-1] if offsets else 0
        nested, pos = self.nested.deserialize_binary(data, total, pos)
        col = ColumnArray(nested)
        prev = 0
        for v in offsets:
            if v < prev:
                raise_error(
                    ErrorCode.CANNOT_PARSE_TEXT,
                    f"Array offsets are not monotonic: {prev} then {v}",
                )
            col.offsets.append(v)
            prev = v
        return col, pos


class SerializationTuple(ISerialization):
    """``Tuple(...)`` —— 每个成员**依次整列**写完。"""

    def __init__(self, elements: Sequence[ISerialization]) -> None:
        self.elements = list(elements)

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        if not isinstance(column, ColumnTuple):
            raise_error(ErrorCode.LOGICAL_ERROR, "SerializationTuple got a non-tuple column")
        for ser, sub in zip(self.elements, column.columns):
            ser.serialize_binary(sub, out, start, count)

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        cols: List[IColumn] = []
        for ser in self.elements:
            col, pos = ser.deserialize_binary(data, num_rows, pos)
            cols.append(col)
        return ColumnTuple(cols), pos


class SerializationMap(ISerialization):
    """``Map(K, V)`` —— 与 Array 同构:偏移 + keys 列 + values 列。"""

    def __init__(self, key: ISerialization, value: ISerialization) -> None:
        self.key = key
        self.value = value

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        if not isinstance(column, ColumnMap):
            raise_error(ErrorCode.LOGICAL_ERROR, "SerializationMap got a non-map column")
        base = column.offsets[start - 1] if start > 0 else 0
        for i in range(start, start + count):
            write_var_uint(out, column.offsets[i] - base)
        total = column.offsets[start + count - 1] - base if count else 0
        self.key.serialize_binary(column.keys, out, base, total)
        self.value.serialize_binary(column.values, out, base, total)

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        offsets: List[int] = []
        for _ in range(num_rows):
            v, pos = read_var_uint(data, pos)
            offsets.append(v)
        total = offsets[-1] if offsets else 0
        keys, pos = self.key.deserialize_binary(data, total, pos)
        values, pos = self.value.deserialize_binary(data, total, pos)
        col = ColumnMap(keys, values)
        col.offsets = list(offsets)
        return col, pos


class SerializationLowCardinality(ISerialization):
    """``LowCardinality(T)`` —— 字典 + 索引。

    真实 ClickHouse 的 ``SerializationLowCardinality`` 分了三种"索引类型"::

       UInt8   —— 字典 ≤ 256 项      (最常见的互联网数据)
       UInt16  —— 字典 ≤ 65536 项
       UInt32  —— 更大

    并且把"索引类型"写在磁盘上(一个字节),这样读取时才知道
    每个索引占几个字节。写字典时它是一个**独立的 stream**
    (``<column>.dict``),索引在另一个 stream —— 这是真实 CH 的做法,
    mini-clickhouse 把两者写在同一个流里(字典长度 + 字典 + 索引),
    语义一致,布局更简单。
    """

    def __init__(self, nested: ISerialization) -> None:
        self.nested = nested

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        if not isinstance(column, ColumnLowCardinality):
            raise_error(ErrorCode.LOGICAL_ERROR, "SerializationLowCardinality got a wrong column")
        dictionary = column.dictionary
        write_var_uint(out, dictionary.size())
        self.nested.serialize_binary(dictionary, out, 0, dictionary.size())
        write_var_uint(out, count)
        for i in range(start, start + count):
            write_var_uint(out, column.indexes[i])

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        dict_size, pos = read_var_uint(data, pos)
        dictionary, pos = self.nested.deserialize_binary(data, dict_size, pos)
        count, pos = read_var_uint(data, pos)
        if count != num_rows:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"LowCardinality column declares {count} rows but {num_rows} were requested",
            )
        col = ColumnLowCardinality(dictionary)
        for _ in range(count):
            idx, pos = read_var_uint(data, pos)
            col.indexes.append(idx)
        return col, pos


class SerializationAggregateFunction(ISerialization):
    """``AggregateFunction`` 状态 —— ``[varint 长度][状态字节]``。

    状态是**不透明的二进制**,所以序列化只能用
    "长度 + 原样字节"。真实 ClickHouse 的
    ``SerializationAggregateFunction`` 就是这样,
    并且它**不支持**跨版本兼容(状态格式随函数实现变化)——
    升级 ClickHouse 大版本时 AggregatingMergeTree 的 part 可能需要
    重新计算,这是那条"不要跨大版本升级物化视图"警告的来源。
    """

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        if not isinstance(column, ColumnAggregateFunction):
            raise_error(ErrorCode.LOGICAL_ERROR, "SerializationAggregateFunction got a wrong column")
        for i in range(start, start + count):
            blob = column.func.serialize_state(column.states[i]) if column.func \
                else bytes(column.states[i])
            write_var_uint(out, len(blob))
            out += blob

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        col = ColumnAggregateFunction(None)
        for _ in range(num_rows):
            length, pos = read_var_uint(data, pos)
            col.states.append(bytes(data[pos : pos + length]))
            pos += length
        return col, pos


class SerializationNothing(ISerialization):
    """``Nothing`` —— 零字节。"""

    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None:
        return

    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple:
        from .columns import ColumnNothing

        return ColumnNothing(), pos


# ══════════════════════════════════════════════════════════════════════════
# 工厂
# ══════════════════════════════════════════════════════════════════════════

#: 类型族名 → 序列化构造器。惰性初始化,避免 import 环。
_SERIALIZATION_CACHE: dict = {}


def get_serialization(dtype: DataType) -> ISerialization:
    """按类型取序列化器(带缓存 —— 真实 CH 也缓存 ``SerializationPtr``)。

    缓存键是**类型名**:``Array(Nullable(UInt8))`` 只构造一次。
    这是安全的,因为 :class:`DataType` 是不可变的,
    且 :meth:`DataType.equals` 就是按名字比较。
    """
    name = dtype.get_name()
    cached = _SERIALIZATION_CACHE.get(name)
    if cached is not None:
        return cached
    ser = _build_serialization(dtype)
    _SERIALIZATION_CACHE[name] = ser
    return ser


def _build_serialization(dtype: DataType) -> ISerialization:
    from .types import composite as _c
    from .types import scalars as _s

    if isinstance(dtype, _s.DataTypeNumber):
        if dtype.bits > 64:
            return SerializationBigInt(dtype.bits, dtype.signed)
        return SerializationNumber(dtype._numpy_dtype())
    if isinstance(dtype, _s.DataTypeBool):
        return SerializationNumber(np.dtype(np.uint8))
    if isinstance(dtype, _c.DataTypeDecimal):
        bits = dtype._int_bits()
        if bits > 64:
            return SerializationBigInt(bits, True)
        return SerializationNumber(np.dtype(np.int32 if bits == 32 else np.int64))
    if isinstance(dtype, _s.DataTypeString):
        return SerializationString()
    if isinstance(dtype, _s.DataTypeFixedString):
        return SerializationFixedString(dtype.n)
    if isinstance(dtype, _s.DataTypeUUID):
        return SerializationUUID()
    if isinstance(dtype, _s.DataTypeIPv4):
        return SerializationNumber(np.dtype(np.uint32))
    if isinstance(dtype, _s.DataTypeIPv6):
        return SerializationFixedString(16)
    if isinstance(dtype, _s.DataTypeDate):
        return SerializationNumber(np.dtype(np.uint16))
    if isinstance(dtype, _s.DataTypeDate32):
        return SerializationNumber(np.dtype(np.int32))
    if isinstance(dtype, _s.DataTypeDateTime):
        return SerializationNumber(np.dtype(np.uint32))
    if isinstance(dtype, _s.DataTypeDateTime64):
        return SerializationNumber(np.dtype(np.int64))
    if isinstance(dtype, _s.DataTypeEnum):
        return SerializationNumber(np.dtype(np.int8 if dtype.bits == 8 else np.int16))
    if isinstance(dtype, _s.DataTypeInterval):
        return SerializationNumber(np.dtype(np.int64))
    if isinstance(dtype, _s.DataTypeNothing):
        return SerializationNothing()
    if isinstance(dtype, _c.DataTypeNullable):
        return SerializationNullable(get_serialization(dtype.nested))
    if isinstance(dtype, _c.DataTypeArray):
        return SerializationArray(get_serialization(dtype.element))
    if isinstance(dtype, _c.DataTypeTuple):
        return SerializationTuple([get_serialization(t) for t in dtype.elements])
    if isinstance(dtype, _c.DataTypeNested):
        return SerializationArray(SerializationTuple(
            [get_serialization(t) for t in dtype.elements]
        ))
    if isinstance(dtype, _c.DataTypeMap):
        return SerializationMap(
            get_serialization(dtype.key), get_serialization(dtype.value)
        )
    if isinstance(dtype, _c.DataTypeLowCardinality):
        return SerializationLowCardinality(get_serialization(dtype.nested))
    if isinstance(dtype, _c.DataTypeAggregateFunction):
        return SerializationAggregateFunction()
    if isinstance(dtype, _c.DataTypeSimpleAggregateFunction):
        return get_serialization(dtype.value_type)
    if isinstance(dtype, _c.DataTypeVariant):
        # 判别列(String) + 各分支列,与 ColumnTuple 的物化方式一致
        from .types import get_data_type

        sers = [SerializationString()] + [get_serialization(v) for v in dtype.variants]
        return SerializationTuple(sers)
    if isinstance(dtype, (_c.DataTypeDynamic, _c.DataTypeJSON)):
        return SerializationString()
    raise_error(
        ErrorCode.NOT_IMPLEMENTED,
        f"No serialization implemented for type {dtype.get_name()}",
    )


# ══════════════════════════════════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════════════════════════════════


def serialize_binary(dtype: DataType, column: IColumn) -> bytes:
    """把整列序列化成字节。"""
    out = bytearray()
    get_serialization(dtype).serialize_binary(column, out, 0, column.size())
    return bytes(out)


def deserialize_binary(dtype: DataType, data: bytes, num_rows: int) -> IColumn:
    """从字节解出整列(要求刚好用完)。"""
    col, pos = get_serialization(dtype).deserialize_binary(data, num_rows, 0)
    if pos != len(data):
        raise_error(
            ErrorCode.CANNOT_READ_ALL_DATA,
            f"Deserialized {pos} of {len(data)} bytes for type {dtype.get_name()}; "
            f"{len(data) - pos} bytes left over",
        )
    return col
