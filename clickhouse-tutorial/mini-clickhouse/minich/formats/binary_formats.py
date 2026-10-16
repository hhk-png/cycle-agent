"""二进制格式:``RowBinary*`` 与 ``Native``。

文本格式和二进制格式的分工,一句话说清:

.. code-block:: text

   文本格式(TSV/CSV/JSON)          二进制格式(RowBinary/Native)
   给**人和别的程序**看              给**ClickHouse 自己**看
   有转义、有引号、有列名            只有字节,没有"转义"这个概念
   值 → 文本 → 值 要往返            值 → 字节 → 值 必须**逐字节**一致
   体积大 2–5 倍                    体积小,而且可以直接 mmap

.. code-block:: text

   RowBinary(按**行**写)                Native(按**列**写)
   ┌────────────────────────────┐      ┌──────────────────────────────┐
   │ [r0c0][r0c1][r0c2]         │      │ 块头:列数,行数                │
   │ [r1c0][r1c1][r1c2]         │      │ 列0: 名字 + 类型 + **整列数据** │
   │ …                          │      │ 列1: 名字 + 类型 + **整列数据** │
   └────────────────────────────┘      └──────────────────────────────┘
    一行写完再写下一行                  一列写完整列再写下一列
    ↑ 逐行追加很方便                    ↑ 这就是"列存"的落盘形态

``Native`` 的"按列"不是为了好看:整数列可以整块 ``memcpy``,压缩器能
在整列上找重复模式,读取时可以只解压需要的那几列 —— 这三条好处
都建立在"同一列的数据连在一起"之上。

**本模块对"值 → 字节"的映射规则**(两种格式共用):

.. code-block:: text

   类型              字节数     布局
   UInt8/16/32/64    1/2/4/8   小端
   Int8/16/32/64     1/2/4/8   小端(补码)
   Int128/256        16/32     小端(补码)
   Float32/64        4/8       IEEE-754 小端
   Bool              1         0 / 1
   String            变长      [varint 长度][UTF-8 字节]
   FixedString(N)    N         定长,尾部补 0
   UUID              16        128 位小端
   IPv4 / IPv6       4 / 16    网络序整数 / 16 字节
   Date / Date32     2 / 4     UInt16 天数 / Int32 天数
   DateTime          4         UInt32(UTC 秒)
   DateTime64        8         Int64(刻度)
   Enum8 / Enum16    1 / 2     底层整数
   Decimal           4/8/16/32 未缩放整数(宽度由精度决定)
   Array(T)          变长      varint 元素个数 + 元素
   Tuple(…)          变长      各成员依次
   Map(K,V)          变长      varint 对数 + 所有键 + 所有值
   Nullable(T)       ≥1        **1 字节空标志**(1 = NULL),空时**不写负载**

.. warning::

   ``Nullable`` 的"空时不写负载"是一个**容易踩的坑**:意味着
   ``RowBinary`` 里同一列不同行的长度是**不一样**的。想把文件按固定
   行宽 mmap 是做不到的 —— 这也是为什么"要随机读"的场合用 ``Native``
   而不是 ``RowBinary``。
"""

from __future__ import annotations

from typing import Any, Iterator, List, Tuple

from ..block import Block, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ..serialization import get_serialization, read_var_uint, write_var_uint
from ..types import DataType, get_data_type
from ..types.composite import DataTypeNested
from .base import (
    IInputFormat,
    IOutputFormat,
    block_from_columns,
    chunk_lines,
    get_setting,
    read_all_bytes,
)

__all__ = [
    "RowBinaryOutputFormat",
    "RowBinaryInputFormat",
    "RowBinaryWithNamesOutputFormat",
    "RowBinaryWithNamesInputFormat",
    "RowBinaryWithNamesAndTypesOutputFormat",
    "RowBinaryWithNamesAndTypesInputFormat",
    "NativeOutputFormat",
    "NativeInputFormat",
]


# ══════════════════════════════════════════════════════════════════════════
# 值的二进制编码 —— RowBinary 用(逐值)
# ══════════════════════════════════════════════════════════════════════════


def write_value_binary(dtype: DataType, column: Any, row: int, out: bytearray) -> None:
    """把 ``column[row]`` 按 ``dtype`` 的规则写进 ``out``。

    **只处理"必须逐值决定"的类型**(可空、容器、低基数),其余交给
    :mod:`minich.serialization` 的 ``serialize_binary`` —— 那一层已经按
    类型把布局写死好了,重写一份只会多一份出错的机会。

    .. note::

       ``Array`` / ``Tuple`` / ``Map`` 为什么要自己写? 因为序列化层用的是
       **批量**布局(``Array`` 写 ``rows-1`` 个累积偏移,没有"本行有几个元素"
       这个前缀),而 ``RowBinary`` 是**逐行**布局(每行都要一个 varint
       元素个数)。两者语义相同、字节不同 —— 这正是"内存/批量布局 ≠
       逐值布局"的又一个例子。
    """
    if dtype.is_nullable():
        is_null = bool(column.null_map[row])
        out.append(1 if is_null else 0)
        if not is_null:
            write_value_binary(dtype.nested, column.nested, row, out)  # type: ignore[attr-defined]
        return
    if dtype.is_low_cardinality():
        # 低基数在 RowBinary 里**退化成普通列**:只写值,不写字典。
        # 真实 ClickHouse 的 SerializationLowCardinality 也是这么做的
        # (逐值时字典没有意义,一行只有一个值)。
        write_value_binary(
            dtype.nested, column.dictionary, column.indexes[row], out  # type: ignore[attr-defined]
        )
        return
    if dtype.is_array():
        start = column.offsets[row - 1] if row > 0 else 0
        end = column.offsets[row]
        write_var_uint(out, end - start)
        for i in range(start, end):
            write_value_binary(dtype.element, column.nested, i, out)  # type: ignore[attr-defined]
        return
    if dtype.is_tuple():
        for i, element_type in enumerate(dtype.elements):  # type: ignore[attr-defined]
            write_value_binary(element_type, column.columns[i], row, out)
        return
    if dtype.is_map():
        start = column.offsets[row - 1] if row > 0 else 0
        end = column.offsets[row]
        write_var_uint(out, end - start)
        # 键全部写完,再写值 —— 与 Array(Tuple) 的"交错"写法不同。
        # 这是真实 ClickHouse 的 RowBinary 布局(Map 被当成两个平行数组)。
        for i in range(start, end):
            write_value_binary(dtype.key, column.keys, i, out)  # type: ignore[attr-defined]
        for i in range(start, end):
            write_value_binary(dtype.value, column.values, i, out)  # type: ignore[attr-defined]
        return
    if isinstance(dtype, DataTypeNested):
        start = column.offsets[row - 1] if row > 0 else 0
        end = column.offsets[row]
        write_var_uint(out, end - start)
        for i in range(start, end):
            for j, element_type in enumerate(dtype.elements):
                write_value_binary(element_type, column.nested.columns[j], i, out)
        return
    get_serialization(dtype).serialize_binary(column, out, row, 1)


def read_value_binary(dtype: DataType, data: bytes, pos: int) -> Tuple[Any, int]:
    """``write_value_binary`` 的逆操作:``(值, 新位置)``。"""
    if dtype.is_nullable():
        if pos >= len(data):
            raise_error(ErrorCode.UNEXPECTED_END_OF_FILE, "Truncated NULL flag")
        is_null = data[pos] != 0
        pos += 1
        if is_null:
            return None, pos
        return read_value_binary(dtype.nested, data, pos)  # type: ignore[attr-defined]
    if dtype.is_low_cardinality():
        return read_value_binary(dtype.nested, data, pos)  # type: ignore[attr-defined]
    if dtype.is_array():
        count, pos = read_var_uint(data, pos)
        values = []
        for _ in range(count):
            value, pos = read_value_binary(dtype.element, data, pos)  # type: ignore[attr-defined]
            values.append(value)
        return values, pos
    if dtype.is_tuple():
        values: List[Any] = []
        for element_type in dtype.elements:  # type: ignore[attr-defined]
            value, pos = read_value_binary(element_type, data, pos)
            values.append(value)
        return tuple(values), pos
    if dtype.is_map():
        count, pos = read_var_uint(data, pos)
        keys: List[Any] = []
        for _ in range(count):
            key, pos = read_value_binary(dtype.key, data, pos)  # type: ignore[attr-defined]
            keys.append(key)
        values = []
        for _ in range(count):
            value, pos = read_value_binary(dtype.value, data, pos)  # type: ignore[attr-defined]
            values.append(value)
        return dict(zip(keys, values)), pos
    if isinstance(dtype, DataTypeNested):
        count, pos = read_var_uint(data, pos)
        items: List[Any] = []
        for _ in range(count):
            members: List[Any] = []
            for element_type in dtype.elements:
                value, pos = read_value_binary(element_type, data, pos)
                members.append(value)
            items.append(tuple(members))
        return items, pos
    column, pos = get_serialization(dtype).deserialize_binary(data, 1, pos)
    return column.get(0), pos


# ══════════════════════════════════════════════════════════════════════════
# RowBinary
# ══════════════════════════════════════════════════════════════════════════


def _require_bytearray(out: Any, format_name: str) -> None:
    if not isinstance(out, bytearray):
        raise_error(
            ErrorCode.CANNOT_WRITE_TO_OSTREAM,
            f"Binary format {format_name} writes raw bytes and needs a bytearray, "
            f"got {type(out).__name__}",
        )


def _write_binary_string(out: bytearray, text: str) -> None:
    """``String`` 的二进制形式:``[varint 长度][UTF-8 字节]``。"""
    raw = text.encode("utf-8", "surrogateescape")
    write_var_uint(out, len(raw))
    out += raw


def _read_binary_string(data: bytes, pos: int) -> Tuple[str, int]:
    length, pos = read_var_uint(data, pos)
    if pos + length > len(data):
        raise_error(
            ErrorCode.UNEXPECTED_END_OF_FILE,
            f"Truncated string: declared {length} bytes, only {len(data) - pos} left",
        )
    return data[pos : pos + length].decode("utf-8", "surrogateescape"), pos + length


class _RowBinaryOutput(IOutputFormat):
    """``RowBinary*`` 输出的公共骨架。"""

    with_names = False
    with_types = False
    name = "RowBinary"

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        _require_bytearray(out, self.name)
        if self.with_names:
            # 先写列数 —— 没有它,读取端在不知道 schema 时就无法知道该读几个名字
            write_var_uint(out, sample_block.columns())
            for c in sample_block.data:
                _write_binary_string(out, c.name)
        if self.with_types:
            for c in sample_block.data:
                _write_binary_string(out, c.type.get_name())

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        _require_bytearray(out, self.name)
        for row in range(block.rows()):
            for c in block.data:
                write_value_binary(c.type, c.column, row, out)


class RowBinaryOutputFormat(_RowBinaryOutput):
    """``RowBinary`` —— 纯粹的值,一行接一行,**没有任何元数据**。

    .. code-block:: text

       SELECT 1 AS x, 'ab' AS s FORMAT RowBinary
       ┌───┬────┬───┬───┐
       │01 │61 62│01 │…  │
       └───┴────┴───┴───┘
        x=1   "ab"  x=2
        (1 字节)(varint 长度 2 + 两个字节)

    读取端**必须知道 schema**:文件里一个字节的列名/类型都没有。
    这就是"最省字节"的代价。
    """

    name = "RowBinary"


class RowBinaryWithNamesOutputFormat(_RowBinaryOutput):
    """``RowBinaryWithNames`` —— 开头多一段 ``[列数][名字]…``。

    .. code-block:: text

       前缀:  varint 列数 │ 名字 0 │ 名字 1 │ …
              └── 02 ──┘  │ "x"   │ "s"   │
       数据:  [01][01 61 62][02][01 63 64]…   ← 第 0 行,第 1 行
    """

    name = "RowBinaryWithNames"
    with_names = True


class RowBinaryWithNamesAndTypesOutputFormat(_RowBinaryOutput):
    """``RowBinaryWithNamesAndTypes`` —— 名字之后**再跟一段类型名**。

    .. code-block:: text

       前缀:  varint 列数 │ 名字…        │ 类型…
              └── 02 ──┘  │ "x"  "s"    │ "UInt8"  "String"
       数据:  数据行

    有了它,读取端**不需要外部 schema** —— 列名与类型都是从文件里读出来的。
    前缀里的"列数"是关键:没有它,读取端不知道该读几个名字
    (真实 ClickHouse 的 ``RowBinaryWithNamesAndTypes`` 同样先写列数)。
    """

    name = "RowBinaryWithNamesAndTypes"
    with_names = True
    with_types = True


class _RowBinaryInput(IInputFormat):
    """``RowBinary*`` 输入。

    .. code-block:: text

       数据是"自描述"的吗?
         RowBinary                     ✗ 必须给 sample_block
         RowBinaryWithNames            ✗ 名字不是类型,还得靠 sample_block
         RowBinaryWithNamesAndTypes    ✓ 类型就在文件里,可以不传 sample_block

    读取时的循环很简单:一直读到**数据耗尽**为止。之所以能这样,
    是因为 RowBinary 里每一种类型都是**自定界**的(定长,或者带长度前缀)。
    """

    with_names = False
    with_types = False
    name = "RowBinary"

    def read(self, source: Any, sample_block: Block, settings: Any = None) -> Iterator[Block]:
        data = read_all_bytes(source)
        pos = 0
        schema = sample_block

        if self.with_names:
            declared, pos = read_var_uint(data, pos)
            names = []
            for _ in range(declared):
                name, pos = _read_binary_string(data, pos)
                names.append(name)
            if self.with_types:
                types = []
                for _ in range(declared):
                    type_name, pos = _read_binary_string(data, pos)
                    types.append(type_name)
                if sample_block.columns() == 0:
                    # schema 完全可以从文件里读出来
                    schema = Block(
                        [
                            ColumnWithTypeAndName(n, get_data_type(t))
                            for n, t in zip(names, types)
                        ]
                    )
                else:
                    expected_names = sample_block.get_names()
                    expected_types = [c.type.get_name() for c in sample_block.data]
                    if names != expected_names or types != expected_types:
                        raise_error(
                            ErrorCode.TYPE_MISMATCH,
                            f"{self.name} prefix declares ({names}, {types}) but the "
                            f"expected schema is ({expected_names}, {expected_types})",
                        )
            elif sample_block.columns() > 0:
                if names != sample_block.get_names():
                    raise_error(
                        ErrorCode.TYPE_MISMATCH,
                        f"{self.name} prefix names {names} do not match the expected "
                        f"columns {sample_block.get_names()}",
                    )
                if declared != sample_block.columns():
                    raise_error(
                        ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                        f"{self.name} prefix declares {declared} column(s) but the "
                        f"sample block has {sample_block.columns()}",
                    )

        if schema.columns() == 0:
            raise_error(
                ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                f"Format {self.name} needs a non-empty sample block to read the rows",
            )

        rows: List[List[Any]] = []
        while pos < len(data):
            row: List[Any] = []
            for cwtn in schema.data:
                value, pos = read_value_binary(cwtn.type, data, pos)
                row.append(value)
            rows.append(row)

        block_size = int(get_setting(settings, "max_block_size", 65536) or 65536)
        for chunk in chunk_lines(rows, block_size):
            if chunk:
                yield block_from_columns(
                    schema, [[row[i] for row in chunk] for i in range(schema.columns())]
                )


class RowBinaryInputFormat(_RowBinaryInput):
    """``RowBinary`` 输入(必须提供 sample_block)。"""

    name = "RowBinary"


class RowBinaryWithNamesInputFormat(_RowBinaryInput):
    """``RowBinaryWithNames`` 输入。"""

    name = "RowBinaryWithNames"
    with_names = True


class RowBinaryWithNamesAndTypesInputFormat(_RowBinaryInput):
    """``RowBinaryWithNamesAndTypes`` 输入(可以只靠文件里的类型)。"""

    name = "RowBinaryWithNamesAndTypes"
    with_names = True
    with_types = True


# ══════════════════════════════════════════════════════════════════════════
# Native
# ══════════════════════════════════════════════════════════════════════════


# ── 整列的"批量"编码(容器要自己写,见下面的 warning)────────────────────


def write_column_bulk(dtype: DataType, column: Any, out: bytearray, num_rows: int) -> None:
    """把**整列**按 Native 的批量布局写进 ``out``。

    .. warning::

       **为什么容器这一层要自己写,而不是直接用
       :mod:`minich.serialization`?**

       因为 ``SerializationArray`` / ``SerializationMap`` 的读写**不对称**:

       .. code-block:: text

          写:for i in range(count - 1): writeVarUInt(offsets[i])   ← rows-1 个
              然后写 offsets[count-1] 个嵌套元素
          读:for _ in range(num_rows - 1): read 偏移
              total = offsets[-1]        ← 这是"倒数第二个偏移",不是总数!
              然后按 total 个元素去读嵌套列        ✗ 最后一行的元素丢了

       实测:``Array(UInt8)`` 的值 ``[[], [1,2,3], [255], [0]]`` 写出来是
       ``00 03 04 | 01 02 03 ff 00``,读回来却按 4 个元素解,最后 1 个字节
       没人管 —— 于是 ``[0]`` 变成 ``[]``,``255`` 被挤掉。

       真实 ClickHouse 的批量布局写的是**完整的 num_rows 个偏移**
       (嵌套流的长度必须能从中推出),所以这里按真实布局来写:
       ``数量 + 累积偏移(每行一个) + 嵌套元素``。

    ``Nullable`` / ``LowCardinality`` / 标量仍然交给
    :func:`minich.serialization.get_serialization` —— 它们的读写是对称的
    (``Nullable`` 写"嵌套列 + 空位图",读的时候两边都用 ``num_rows``,
    没有"少写一个"的问题)。
    """
    if dtype.is_nullable():
        write_column_bulk(dtype.nested, column.nested, out, num_rows)  # type: ignore[attr-defined]
        out += bytes(1 if flag else 0 for flag in column.null_map[:num_rows])
        return
    if dtype.is_low_cardinality() or dtype.is_variant():
        get_serialization(dtype).serialize_binary(column, out, 0, num_rows)
        return
    if dtype.is_array():
        total = _write_offsets(column, out, num_rows)
        write_column_bulk(dtype.element, column.nested, out, total)  # type: ignore[attr-defined]
        return
    if dtype.is_tuple():
        for i, element_type in enumerate(dtype.elements):  # type: ignore[attr-defined]
            write_column_bulk(element_type, column.columns[i], out, num_rows)
        return
    if dtype.is_map():
        total = _write_offsets(column, out, num_rows)
        write_column_bulk(dtype.key, column.keys, out, total)  # type: ignore[attr-defined]
        write_column_bulk(dtype.value, column.values, out, total)  # type: ignore[attr-defined]
        return
    if isinstance(dtype, DataTypeNested):
        total = _write_offsets(column, out, num_rows)
        for j, element_type in enumerate(dtype.elements):
            write_column_bulk(element_type, column.nested.columns[j], out, total)
        return
    get_serialization(dtype).serialize_binary(column, out, 0, num_rows)


def _write_offsets(column: Any, out: bytearray, num_rows: int) -> int:
    """写每行的累积偏移,返回元素总数。"""
    for i in range(num_rows):
        write_var_uint(out, column.offsets[i])
    return column.offsets[num_rows - 1] if num_rows else 0


def read_column_bulk(dtype: DataType, data: bytes, pos: int, num_rows: int) -> Tuple[Any, int]:
    """:func:`write_column_bulk` 的逆操作:``(列, 新位置)``。"""
    if dtype.is_nullable():
        nested, pos = read_column_bulk(dtype.nested, data, pos, num_rows)  # type: ignore[attr-defined]
        if pos + num_rows > len(data):
            raise_error(ErrorCode.UNEXPECTED_END_OF_FILE, "Truncated Native null map")
        null_map = [bool(b) for b in data[pos : pos + num_rows]]
        pos += num_rows
        from ..columns import ColumnNullable

        column = ColumnNullable(nested)
        column.null_map = null_map
        return column, pos
    if dtype.is_low_cardinality() or dtype.is_variant():
        return get_serialization(dtype).deserialize_binary(data, num_rows, pos)
    if dtype.is_array():
        offsets, pos = _read_offsets(data, pos, num_rows)
        total = offsets[-1] if offsets else 0
        nested, pos = read_column_bulk(dtype.element, data, pos, total)  # type: ignore[attr-defined]
        from ..columns import ColumnArray

        column = ColumnArray(nested)
        column.offsets = offsets
        return column, pos
    if dtype.is_tuple():
        from ..columns import ColumnTuple

        members: List[Any] = []
        for element_type in dtype.elements:  # type: ignore[attr-defined]
            member, pos = read_column_bulk(element_type, data, pos, num_rows)
            members.append(member)
        return ColumnTuple(members), pos
    if dtype.is_map():
        offsets, pos = _read_offsets(data, pos, num_rows)
        total = offsets[-1] if offsets else 0
        keys, pos = read_column_bulk(dtype.key, data, pos, total)  # type: ignore[attr-defined]
        values, pos = read_column_bulk(dtype.value, data, pos, total)  # type: ignore[attr-defined]
        from ..columns import ColumnMap

        column = ColumnMap(keys, values)
        column.offsets = offsets
        return column, pos
    if isinstance(dtype, DataTypeNested):
        from ..columns import ColumnArray, ColumnTuple

        offsets, pos = _read_offsets(data, pos, num_rows)
        total = offsets[-1] if offsets else 0
        members = []
        for element_type in dtype.elements:
            member, pos = read_column_bulk(element_type, data, pos, total)
            members.append(member)
        column = ColumnArray(ColumnTuple(members))
        column.offsets = offsets
        return column, pos
    return get_serialization(dtype).deserialize_binary(data, num_rows, pos)


def _read_offsets(data: bytes, pos: int, num_rows: int) -> Tuple[List[int], int]:
    offsets: List[int] = []
    previous = 0
    for _ in range(num_rows):
        value, pos = read_var_uint(data, pos)
        if value < previous:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Native array offsets are not monotonic: {previous} then {value}",
            )
        offsets.append(value)
        previous = value
    return offsets, pos


class NativeOutputFormat(IOutputFormat):
    """``Native`` —— ClickHouse 的**队列格式**(``clickhouse-client`` 默认用它)。

    ``Native`` 是"表 ↔ 网络"的二进制协议格式:``SELECT`` 的结果、
    ``INSERT`` 的输入、以及 part 文件里的数据段,用的都是这一套。

    **本实现的帧格式(逐字节)**

    .. code-block:: text

       一个块:
       ┌────────────────────────────────────────────────────────────────┐
       │ varint  列数 num_columns                                        │
       │ varint  行数 num_rows                                           │
       ├─ 每列重复 ─────────────────────────────────────────────────────┤
       │ String  列名        ([varint 长度][UTF-8 字节])                  │
       │ String  类型名      (同上,例如 "Array(Nullable(UInt8))")        │
       │ varint  本列行数    (**只在 num_rows != 0 时写**)                │
       │ bytes   列数据      (按列的批量布局:                              │
       │                      Nullable  先写嵌套列再写空位图;             │
       │                      Array/Map 先写 **num_rows 个累积偏移**      │
       │                               再写嵌套元素;                     │
       │                      Tuple     各成员列依次;                    │
       │                      标量      交给 minich/serialization)        │
       └────────────────────────────────────────────────────────────────┘

       流的结束:
       ┌───────────────────────────────┐
       │ 0x00 0x00   列数=0,行数=0     │  ← 两个零字节,读到这里就停
       └───────────────────────────────┘

    .. code-block:: text

       完整例子(1 列 UInt8,两行 1 与 2):

         01        num_columns = 1
         02        num_rows    = 2
         01 78     String "x"          (长度 1,字节 'x')
         05 55 49 6e 74 38   String "UInt8"
         02        本列行数 = 2
         01 02     列数据:两个 UInt8
         00 00     结束标记

    .. warning::

       **这里没有复刻真实 ``Native`` 的全部内容**,如实列出来:

       * **BlockInfo 没有写**:真实 ClickHouse 在每个块的列数据之后还会写
         ``is_overflows``(UInt8)+ ``bucket_num``(Int32)共 5 个字节,
         用于分布式查询的桶信息(见 :class:`minich.block.BlockInfo`)。
         mini-clickhouse 不写这 5 个字节,所以**我们生成的 Native 文件
         真实 ClickHouse 读不了**(反过来也一样)。
       * **没有 revision / 协议版本号**:真实 Native 的开头(以及客户端
         握手时)会带一个 DBMS 版本号与 revision,用来做**向后兼容**
         (新版客户端能读旧版服务端的数据)。这里只认一种布局。
       * **没有 custom-serialization 标志字节**:24.x 起,类型名后面会多
         一个字节表示"这个列用了哪种序列化变体"(稀疏/低基数)。
         我们每个类型只有一种序列化,所以省掉了它。
       * **每列多了一个行数 varint**:真实 Native 的行数只在**块头**里出现
         一次;这里每列再写一遍。多花的字节可以忽略,但它让"列数据"
         变成自描述的(读者可以只看一列就知道该解多少行)。
         这是 mini-clickhouse 的**刻意偏离**,不是笔误。
    """

    name = "Native"

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        _require_bytearray(out, self.name)
        if block.columns() == 0:
            return
        write_var_uint(out, block.columns())
        write_var_uint(out, block.rows())
        for c in block.data:
            _write_binary_string(out, c.name)
            _write_binary_string(out, c.type.get_name())
            if block.rows() != 0:
                write_var_uint(out, block.rows())
            write_column_bulk(c.type, c.column, out, block.rows())

    def write_suffix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        _require_bytearray(out, self.name)
        # 结束标记:列数 = 0,行数 = 0 —— 两个零字节
        write_var_uint(out, 0)
        write_var_uint(out, 0)


class NativeInputFormat(IInputFormat):
    """``Native`` 输入。

    类型是从流里读出来的,所以 ``sample_block`` **可以**是空的:
    只要给了它,读出来的块就必须与之匹配(名字与类型都要对上,
    对不上抛 ``TYPE_MISMATCH``,而不是悄悄换一套结构)。

    .. warning::

       和 :class:`NativeOutputFormat` 一样,这里只认"本实现写出来的
       Native" —— 真实 ClickHouse 的 Native 在每个块后面还有
       BlockInfo 的 5 个字节,我们的读取器会把它们当成下一个块的块头,
       于是解析出一堆垃圾。要读真实的 Native 文件,需要把 BlockInfo 的
       读写补上(那正是"协议兼容"这件事的全部代价)。
    """

    name = "Native"

    def read(self, source: Any, sample_block: Block, settings: Any = None) -> Iterator[Block]:
        data = read_all_bytes(source)
        pos = 0
        while pos < len(data):
            num_columns, pos = read_var_uint(data, pos)
            num_rows, pos = read_var_uint(data, pos)
            if num_columns == 0:
                # 结束标记 —— 后面再有字节就是垃圾了
                break
            columns: List[ColumnWithTypeAndName] = []
            for _ in range(num_columns):
                name, pos = _read_binary_string(data, pos)
                type_name, pos = _read_binary_string(data, pos)
                if num_rows != 0:
                    declared, pos = read_var_uint(data, pos)
                    if declared != num_rows:
                        raise_error(
                            ErrorCode.CANNOT_PARSE_TEXT,
                            f"Native column '{name}' declares {declared} rows but the "
                            f"block header says {num_rows}",
                        )
                dtype = get_data_type(type_name)
                column, pos = read_column_bulk(dtype, data, pos, num_rows)
                columns.append(ColumnWithTypeAndName(name, dtype, column))

            block = Block(columns)
            block.check_number_of_rows()
            if sample_block.columns() > 0:
                expected_names = sample_block.get_names()
                expected_types = [c.type.get_name() for c in sample_block.data]
                got_names = block.get_names()
                got_types = [c.type.get_name() for c in block.data]
                if got_names != expected_names or got_types != expected_types:
                    raise_error(
                        ErrorCode.TYPE_MISMATCH,
                        f"Native block has ({got_names}, {got_types}) but the expected "
                        f"schema is ({expected_names}, {expected_types})",
                    )
            if block.columns():
                yield block
