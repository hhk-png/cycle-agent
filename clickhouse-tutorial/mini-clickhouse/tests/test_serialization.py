"""序列化测试 —— **列 ↔ 字节** 的往返。

序列化是存储层的地基:它按**列**、流式地写字节,格式完全由**类型**决定。
这个文件把每一种类型的二进制往返都跑一遍,并且**专门**覆盖两个历史坑点:

* 数组的"最后一个元素"问题 —— 曾经少写一个偏移,导致 ``[[], [1,2,3],[255],[0]]``
  被静默解成 ``[[], [1,2,3],[255],[]]``(数据少了却不报错)。现在写 rows 个偏移,
  必须精确往返;
* varint 的边界值(0、127、128、16383、16384…)与"负数非法"。
"""

from __future__ import annotations

import math

import pytest

from minich.columns import (
    ColumnAggregateFunction,
    ColumnArray,
    ColumnBigInt,
    ColumnFixedString,
    ColumnLowCardinality,
    ColumnMap,
    ColumnNullable,
    ColumnString,
    ColumnTuple,
    ColumnUUID,
    ColumnVector,
)
from minich.errors import ErrorCode, MiniChError
from minich.serialization import (
    SerializeBinaryBulkState,
    VarUIntReader,
    deserialize_binary,
    get_serialization,
    read_var_uint,
    serialize_binary,
    write_var_uint,
)
from minich.types import get_data_type


def _roundtrip(type_name, values):
    """把 ``values`` 装进该类型的列,序列化再反序列化,返回值的列表。"""
    t = get_data_type(type_name)
    col = t.create_column()
    col.append_many(values)
    blob = serialize_binary(t, col)
    back = deserialize_binary(t, blob, col.size())
    return back.to_list()


# ══════════════════════════════════════════════════════════════════════════
# varint
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "value,nbytes",
    [
        (0, 1), (1, 1), (127, 1),
        (128, 2), (16383, 2),
        (16384, 3), (2 ** 21 - 1, 3),
        (2 ** 63 - 1, 9),
    ],
)
def test_varint_roundtrip_and_width(value, nbytes):
    out = bytearray()
    write_var_uint(out, value)
    assert len(out) == nbytes
    decoded, pos = read_var_uint(bytes(out), 0)
    assert decoded == value
    assert pos == len(out)


def test_varint_negative_is_illegal():
    out = bytearray()
    with pytest.raises(MiniChError) as ei:
        write_var_uint(out, -1)
    assert ei.value.code == ErrorCode.LOGICAL_ERROR


def test_varint_truncated_raises():
    with pytest.raises(MiniChError) as ei:
        read_var_uint(b"\x80", 0)   # 高位标记了"还有下一字节",但没有
    assert ei.value.code == ErrorCode.UNEXPECTED_END_OF_FILE


def test_varuint_reader_cursor():
    out = bytearray()
    for v in (1, 300, 70000):
        write_var_uint(out, v)
    r = VarUIntReader(bytes(out))
    assert r.read_var_uint() == 1
    assert r.read_var_uint() == 300
    assert r.read_var_uint() == 70000
    assert r.eof() is True
    assert r.remaining() == 0


def test_varuint_reader_read_bytes_and_truncate():
    r = VarUIntReader(b"hello")
    assert r.read(2) == b"he"
    assert r.remaining() == 3
    with pytest.raises(MiniChError) as ei:
        r.read(10)
    assert ei.value.code == ErrorCode.UNEXPECTED_END_OF_FILE


# ══════════════════════════════════════════════════════════════════════════
# 标量类型的往返
# ══════════════════════════════════════════════════════════════════════════


SCALAR_CASES = [
    ("UInt8", [0, 1, 255]),
    ("Int8", [-128, 0, 127]),
    ("UInt16", [0, 65535]),
    ("Int16", [-32768, 32767]),
    ("UInt32", [0, 2 ** 32 - 1]),
    ("Int32", [-2 ** 31, 2 ** 31 - 1]),
    ("UInt64", [0, 2 ** 64 - 1]),
    ("Int64", [-2 ** 63, 2 ** 63 - 1]),
    ("UInt128", [0, 2 ** 128 - 1]),
    ("Int256", [-(2 ** 255), 2 ** 255 - 1]),
    ("Float32", [0.0, -0.0, 1.5, float("inf"), float("-inf")]),
    ("Float64", [0.0, -0.0, 1.5, float("inf"), float("-inf")]),
    ("Bool", [True, False, True]),
    ("String", ["", "hello", "你好", "a\tb"]),
    ("FixedString(4)", ["ab", "abcd", ""]),
    ("UUID", [0, 0x550E8400E29B41D4A716446655440000]),
    ("IPv4", [0, 0xFFFFFFFF, 0xC0A80001]),
    ("IPv6", ["::", "::1"]),
    ("Date", [0, 19723, 65535]),
    ("Date32", [-25567, 0, 120529]),
    ("DateTime", [0, 1704067200, 2 ** 32 - 1]),
    ("DateTime64(3)", [-(2 ** 63), 0, 1704067200123]),
    ("Decimal(10, 2)", [0, 123456789]),
    ("Decimal(38, 10)", [0, 2 ** 100]),
    ("Enum8('a' = 1, 'b' = 2)", [1, 2]),
    ("Enum16('x' = -1, 'y' = 1)", [-1, 1]),
    ("IntervalDay", [0, 5]),
    ("Nothing", []),
]


@pytest.mark.parametrize("type_name,values", SCALAR_CASES)
def test_scalar_binary_roundtrip(type_name, values):
    got = _roundtrip(type_name, values)
    assert got == values


# ══════════════════════════════════════════════════════════════════════════
# 复合类型
# ══════════════════════════════════════════════════════════════════════════


def test_nullable_binary_roundtrip():
    assert _roundtrip("Nullable(UInt8)", [1, None, 0, None]) == [1, None, 0, None]
    assert _roundtrip("Nullable(String)", ["", None, "x"]) == ["", None, "x"]


def test_array_binary_roundtrip_including_last_element_bug():
    """**历史 bug 的回归** —— ``[[], [1,2,3], [255], [0]]`` 必须精确往返。

    最初的实现只写了 rows-1 个偏移,最后一个数组的元素会在反序列化时**丢失**
    (解成 ``[[], [1,2,3],[255],[]]``)。写满 rows 个偏移后修复。
    """
    values = [[], [1, 2, 3], [255], [0]]
    assert _roundtrip("Array(UInt8)", values) == values


def test_array_of_nullable_string_binary_roundtrip():
    values = [["a", None, ""], [], ["x"]]
    assert _roundtrip("Array(Nullable(String))", values) == values


def test_array_offsets_are_monotonic_check():
    """伪造一段偏移非单调的字节 —— 反序列化必须报错而不是产出垃圾。"""
    t = get_data_type("Array(UInt8)")
    bad = bytes([3, 1, 1, 0, 0, 0])   # 偏移从 3 掉回 1
    with pytest.raises(MiniChError) as ei:
        deserialize_binary(t, bad, 3)
    assert ei.value.code == ErrorCode.CANNOT_PARSE_TEXT


def test_tuple_binary_roundtrip():
    assert _roundtrip("Tuple(UInt8, String)", [(1, "a"), (2, "")]) == [(1, "a"), (2, "")]


def test_map_binary_roundtrip():
    assert _roundtrip("Map(String, UInt8)", [{"a": 1, "b": 2}, {}]) == [{"a": 1, "b": 2}, {}]


def test_low_cardinality_binary_roundtrip_preserves_values():
    assert _roundtrip("LowCardinality(String)", ["GET", "POST", "GET", "PUT"]) == \
        ["GET", "POST", "GET", "PUT"]


def test_nested_binary_roundtrip():
    # Nested 的物化是 Array(Tuple(...))
    assert _roundtrip("Nested(a UInt8, b String)", [[(1, "x"), (2, "y")], []]) == \
        [[(1, "x"), (2, "y")], []]


def test_aggregate_function_state_binary_roundtrip():
    t = get_data_type("AggregateFunction(sum, UInt64)")
    col = ColumnAggregateFunction(None)
    col.append(b"\x01\x02\x03")
    col.append(b"")
    blob = serialize_binary(t, col)
    back = deserialize_binary(t, blob, 2)
    assert back.states == [b"\x01\x02\x03", b""]


# ══════════════════════════════════════════════════════════════════════════
# 序列化接口的细节
# ══════════════════════════════════════════════════════════════════════════


def test_deserialize_binary_rejects_leftover_bytes():
    t = get_data_type("UInt8")
    col = t.create_column()
    col.append(1)
    blob = serialize_binary(t, col) + b"\x00"   # 多出一个字节
    with pytest.raises(MiniChError) as ei:
        deserialize_binary(t, blob, 1)
    assert ei.value.code == ErrorCode.CANNOT_READ_ALL_DATA


def test_nullable_layout_writes_nested_then_nullmap():
    """``Nullable(T)`` 磁盘布局 = 先嵌套列,再空位图 —— 顺序要固定。"""
    t = get_data_type("Nullable(UInt8)")
    col = t.create_column()
    col.append_many([7, None])
    blob = serialize_binary(t, col)
    # 嵌套列(2 字节:7,0)在前,空位图(2 字节:0,1)在后
    assert blob == bytes([7, 0, 0, 1])


def test_string_layout_is_varint_length_prefixed():
    t = get_data_type("String")
    col = ColumnString(["hi", ""])
    assert serialize_binary(t, col) == bytes([2]) + b"hi" + bytes([0])


def test_serialization_is_cached_per_type_name():
    a = get_serialization(get_data_type("Array(UInt8)"))
    b = get_serialization(get_data_type("Array(UInt8)"))
    assert a is b


def test_fixed_size_serialization_declares_width():
    ser = get_serialization(get_data_type("UInt32"))
    assert ser.has_fixed_size() is True
    assert ser.fixed_size() == 4
    # String 是变长
    assert get_serialization(get_data_type("String")).has_fixed_size() is False


def test_bulk_state_get_set():
    s = SerializeBinaryBulkState(x=1)
    assert s.get("x") == 1
    assert s.get("y", "default") == "default"
    s.set("y", 2)
    assert s.get("y") == 2


def test_number_serialization_fallback_for_const_like_column():
    """当列不是 ColumnVector 时,SerializationNumber 走逐值打包的退路。"""
    t = get_data_type("UInt16")
    from minich.columns import ColumnConst
    inner = ColumnVectorLike()
    col = ColumnConst(inner, 3)
    inner.append(258)
    col.sizeness = 3
    blob = serialize_binary(t, col)
    assert blob == bytes([2, 1]) * 3   # 258 = 0x0102,小端
    back = deserialize_binary(t, blob, 3)
    assert back.to_list() == [258, 258, 258]


class ColumnVectorLike:
    """一个只实现 ``get`` 的最小列(让 SerializationNumber 走退路分支)。"""

    def __init__(self):
        self._v = None

    def size(self):
        return 0 if self._v is None else 1

    def append(self, v):
        self._v = v

    def get(self, n):
        return self._v

    def append_default(self):
        pass

    def clone_empty(self):
        return ColumnVectorLike()
