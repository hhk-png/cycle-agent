"""类型系统测试 —— 注册表的往返、标量语义、复合类型行为、以及**已记录的偏离**。

这些测试覆盖 ``minich/types/`` 下的每一个类型族。重点是两类东西:

1. **往返性质** —— ``get_data_type(t.get_name()).equals(t)`` 必须成立(建表语句
   能原样 dump 再 load 回去);``deserialize_text(serialize_text(v)) == v`` 对
   绝大多数类型成立;
2. **语义与边界** —— 整数回绕、Enum 名字↔整数、Decimal 缩放、时区、NULL 的地位。

还有一类**刻意记录的偏离**(真实 ClickHouse 不这么做,mini-clickhouse 这么做):
文本表示里的空串/``NULL`` 歧义、``Map`` 的文本往返失败、``UInt64 + Int8``
提升到 ``Int128``。这些都单独用测试固定下来,这样文档和实现不会悄悄漂移。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from minich.columns import (
    ColumnArray,
    ColumnBigInt,
    ColumnFixedString,
    ColumnLowCardinality,
    ColumnMap,
    ColumnNothing,
    ColumnNullable,
    ColumnSparse,
    ColumnString,
    ColumnTuple,
    ColumnUUID,
    ColumnVector,
)
from minich.errors import ErrorCode, MiniChError
from minich.types import (
    get_data_type,
    get_wider_numeric_type,
    parse_type_name,
    registered_type_names,
    try_get_data_type,
)
from minich.types.scalars import (
    days_to_date,
    date_to_days,
    format_datetime,
    format_uuid,
    datetime_to_epoch,
    int_to_ipv4,
    ipv4_to_int,
    parse_timezone_offset,
    parse_uuid,
)


# ══════════════════════════════════════════════════════════════════════════
# 1. 类型名的往返
# ══════════════════════════════════════════════════════════════════════════

#: 一批"具体"类型名 —— 覆盖每个族,且每个都能独立构造。
CONCRETE_TYPE_NAMES = [
    "UInt8", "UInt16", "UInt32", "UInt64", "UInt128", "UInt256",
    "Int8", "Int16", "Int32", "Int64", "Int128", "Int256",
    "Float32", "Float64", "Bool", "String",
    "FixedString(16)", "UUID", "IPv4", "IPv6",
    "Date", "Date32", "DateTime", "DateTime('Asia/Shanghai')",
    "DateTime64(3)", "DateTime64(6, 'UTC')",
    "Decimal(10, 2)", "Decimal32(2)", "Decimal64(4)", "Decimal128(10)", "Decimal256(20)",
    "Enum8('a' = 1, 'b' = 2)", "Enum16('x' = -1)",
    "Nullable(UInt8)", "Array(Nullable(String))",
    "Tuple(UInt8, String)", "Tuple(a UInt8, b String)",
    "Map(String, UInt32)", "LowCardinality(String)",
    "Nested(a UInt8, b String)", "AggregateFunction(sum, UInt64)",
    "SimpleAggregateFunction(sum, UInt64)", "Variant(UInt64, String)",
    "Dynamic", "JSON", "IntervalDay",
]


@pytest.mark.parametrize("name", CONCRETE_TYPE_NAMES)
def test_type_name_roundtrip(name):
    """``get_name()`` 的结果必须能被重新解析成等价类型。"""
    t = get_data_type(name)
    reparsed = get_data_type(t.get_name())
    assert reparsed.equals(t)
    # equals 按名字比较,再确认哈希一致(类型是可哈希的)
    assert hash(reparsed) == hash(t)


def test_registered_type_names_is_sorted_and_covers_families():
    names = registered_type_names()
    assert names == sorted(names)
    for fam in ("UInt8", "Int256", "String", "Array", "Tuple", "Map",
                "Nullable", "LowCardinality", "FixedString", "DateTime64",
                "Enum8", "Decimal", "AggregateFunction"):
        assert fam in names, f"{fam} should be a registered type family"


def test_scalar_types_are_cached_identity():
    """标量类型没有参数,应当被缓存复用 —— 与真实 CH 的 DataTypeFactory 一致。"""
    assert get_data_type("UInt8") is get_data_type("UInt8")
    assert get_data_type("String") is get_data_type("String")


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("uint64", "UInt64"),
        ("tinyint", "Int8"),
        ("bigint", "Int64"),
        ("INT", "Int32"),
        ("varchar", "String"),
        ("text", "String"),
        ("double", "Float64"),
        ("real", "Float32"),
        ("boolean", "Bool"),
        ("timestamp", "DateTime"),
    ],
)
def test_aliases_resolve(alias, canonical):
    assert get_data_type(alias).get_name() == canonical


def test_unknown_type_raises_unknown_type_code():
    with pytest.raises(MiniChError) as ei:
        get_data_type("NoSuchType")
    assert ei.value.code == ErrorCode.UNKNOWN_TYPE
    assert try_get_data_type("NoSuchType") is None


def test_try_get_data_type_on_valid():
    assert try_get_data_type("Array(UInt8)").get_name() == "Array(UInt8)"


# ── parse_type_name ─────────────────────────────────────────────────────


def test_parse_type_name_splits_outer_only():
    assert parse_type_name("Nullable(Array(UInt8))") == ("Nullable", ["Array(UInt8)"])
    assert parse_type_name("UInt8") == ("UInt8", [])


def test_parse_type_name_keeps_comma_inside_quotes():
    fam, args = parse_type_name("Enum8('a, b' = 1, 'c' = 2)")
    assert fam == "Enum8"
    assert args == ["'a, b' = 1", "'c' = 2"]


def test_parse_type_name_rejects_malformed():
    with pytest.raises(MiniChError) as ei:
        parse_type_name("Nullable UInt8")  # 缺括号
    assert ei.value.code == ErrorCode.UNKNOWN_TYPE


# ══════════════════════════════════════════════════════════════════════════
# 2. 数值类型:回绕、提升规则、偏离
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "type_name,value,expected",
    [
        ("UInt8", 250 + 10, 4),
        ("UInt8", -1, 255),
        ("Int8", 127 + 1, -128),
        ("Int8", -129, 127),
        ("UInt16", 65536, 0),
        ("UInt32", 1 << 32, 0),
        ("UInt64", 1 << 64, 0),
        ("Int16", 32768, -32768),
    ],
)
def test_integer_overflow_wraps(type_name, value, expected):
    """整数溢出是**回绕**(C++ 无符号溢出语义),不是报错也不是饱和。"""
    t = get_data_type(type_name)
    assert t.normalize(value) == expected
    assert t.wrap(value) == expected


def test_float_to_int_truncates_toward_zero():
    """真实 CH 把 Float → Int 定义为**截断**(向零),不是四舍五入。"""
    assert get_data_type("Int32").normalize(2.9) == 2
    assert get_data_type("Int32").normalize(-2.9) == -2


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("UInt8", "UInt8", "UInt8"),
        ("UInt8", "UInt16", "UInt16"),
        ("UInt16", "Int16", "Int32"),       # 混符号:位宽翻倍并转有符号
        ("UInt32", "Int16", "Int64"),
        ("Float32", "Float64", "Float64"),  # 同族取更宽
        ("UInt64", "Float32", "Float64"),   # 整数 + 浮点:至少 Float64
        ("UInt16", "Float32", "Float32"),   # 小整数(<=24 bit)可留在 Float32
        ("UInt32", "Float32", "Float64"),   # 32 bit 整数 > 24,提到 Float64
    ],
)
def test_wider_numeric_type_rules(a, b, expected):
    got = get_wider_numeric_type(get_data_type(a), get_data_type(b))
    assert got.get_name() == expected


def test_uint64_plus_int8_widens_to_int128_documented_deviation():
    """**已记录的偏离**:真实 CH 是 ``Int64``(可能溢出),mini 提升到 ``Int128``。"""
    got = get_wider_numeric_type(get_data_type("UInt64"), get_data_type("Int8"))
    assert got.get_name() == "Int128"
    # 反向参数顺序结果相同(取更宽者)
    assert get_wider_numeric_type(
        get_data_type("Int8"), get_data_type("UInt64")
    ).get_name() == "Int128"


def test_wider_numeric_type_decimal_rules():
    d = get_data_type("Decimal(10, 2)")
    # Decimal 与整数混合 → 保留 Decimal
    assert get_wider_numeric_type(d, get_data_type("UInt64")).get_name() == "Decimal(10, 2)"
    # 两个 Decimal:精度取和,scale 取大
    d2 = get_data_type("Decimal(4, 3)")
    assert get_wider_numeric_type(d, d2).get_name() == "Decimal(14, 3)"


def test_wider_numeric_type_rejects_non_numeric():
    with pytest.raises(MiniChError) as ei:
        get_wider_numeric_type(get_data_type("String"), get_data_type("UInt8"))
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


# ══════════════════════════════════════════════════════════════════════════
# 3. 文本往返(安全集合)+ NULL 表示
# ══════════════════════════════════════════════════════════════════════════

#: ``(类型名, 内部值)`` —— 这些必须严格往返。内部值用的是类型的**内部表示**
#: (例如 Date 是天数、UUID 是 128 位整数),不是用户输入的字面量。
TEXT_ROUNDTRIP_CASES = [
    ("UInt8", 255),
    ("UInt8", 0),
    ("Int8", -128),
    ("UInt64", 2 ** 64 - 1),
    ("Int64", -(2 ** 63)),
    ("UInt128", 2 ** 128 - 1),
    ("Int256", -(2 ** 255)),
    ("Float64", 0.0),
    ("Float64", -0.0),
    ("Float64", 0.1),
    ("Float64", float("inf")),
    ("Float64", float("-inf")),
    ("Bool", True),
    ("Bool", False),
    ("String", ""),
    ("String", "hello"),
    ("String", "你好é🚀"),
    ("String", "a b\tc"),
    ("FixedString(4)", "ab"),
    ("UUID", 0x550E8400E29B41D4A716446655440000),
    ("UUID", 0),
    ("IPv4", 0x01020304),
    ("IPv4", 0),
    ("Date", 0),
    ("Date", 19723),
    ("Date32", -25567),
    ("Date32", 120529),
    ("DateTime", 1704067200),
    ("DateTime", 0),
    ("DateTime('Asia/Shanghai')", 1704067200),
    ("DateTime64(3)", 1704067200123),
    ("DateTime64(0)", 1704067200),
    ("Decimal(10, 2)", 123456789),
    ("Decimal(9, 2)", 0),
    ("Enum8('a' = 1, 'b' = 2)", 1),
    ("Enum16('x' = 1)", 1),
    ("Nullable(UInt8)", None),
    ("Nullable(UInt8)", 7),
    ("Array(UInt8)", [1, 2, 3]),
    ("Array(UInt8)", []),
    ("Tuple(UInt8, String)", (1, "a")),
    ("Map(String, UInt32)", {}),
    ("LowCardinality(String)", "GET"),
]


@pytest.mark.parametrize("name,value", TEXT_ROUNDTRIP_CASES)
def test_text_roundtrip(name, value):
    t = get_data_type(name)
    text = t.serialize_text(value)
    back = t.deserialize_text(text)
    if isinstance(value, float) and math.isnan(value):
        assert math.isnan(back)
    else:
        assert back == value


def test_nan_serializes_lowercase():
    assert get_data_type("Float64").serialize_text(float("nan")) == "nan"


def test_none_serializes_as_NULL_for_all_types():
    for name in ("UInt8", "String", "Date", "UUID", "Array(UInt8)",
                 "Nullable(UInt8)", "IPv4", "Decimal(9, 2)"):
        assert get_data_type(name).serialize_text(None) == "NULL"


# ── 已记录的**有损**文本往返 ─────────────────────────────────────────────


def test_nullable_string_empty_string_becomes_null():
    """**已记录偏离**:``Nullable(String)`` 的空串经文本往返会变成 NULL。

    原因是文本层的 NULL 哨兵用的就是空串 —— 真实 ClickHouse 在 TSV 里用 ``\\N``
    区分,这里的 ``DataType.deserialize_text`` 把空串当 NULL。所以走纯文本
    通道时空串与 NULL 不可分。
    """
    t = get_data_type("Nullable(String)")
    assert t.serialize_text("") == ""
    assert t.deserialize_text("") is None


def test_array_nullable_string_loses_empty_elements():
    """**已记录偏离**:``Array(Nullable(String))`` 文本往返会把 ``''`` 变成 NULL。"""
    t = get_data_type("Array(Nullable(String))")
    text = t.serialize_text(["a", None, ""])
    assert text == "[a,NULL,]"
    assert t.deserialize_text(text) == ["a", None, None]


def test_map_text_roundtrip_is_broken_documented():
    """**已记录缺陷**:``Map`` 的文本输出不是它自己 ``deserialize_text`` 能解析的形式。

    ``serialize_text``(以及 ``serialize_text_quoted``)对 key 用的是**不加引号**的
    ``serialize_text``,于是非空 Map 输出 ``{a:1,b:2}``;而 ``deserialize_text``
    走 ``parse_literal`` 要求带引号的 map 字面量,直接抛 ``CANNOT_PARSE_TEXT``(5)。
    空 Map ``{}`` 不受影响。
    """
    t = get_data_type("Map(String, UInt32)")
    text = t.serialize_text({"a": 1, "b": 2})
    assert text == "{a:1,b:2}"
    with pytest.raises(MiniChError) as ei:
        t.deserialize_text(text)
    assert ei.value.code == ErrorCode.CANNOT_PARSE_TEXT
    # 空 Map 是安全的
    assert t.deserialize_text(t.serialize_text({})) == {}


def test_string_deserialize_applies_escape_sequences():
    """``String`` 的**文本**反序列化会处理转义 —— 因此含反斜杠的串不适合文本往返。"""
    t = get_data_type("String")
    # 文本 "a\nb"(字面反斜杠 + n)会被解成真正的换行
    assert t.deserialize_text("a\\nb") == "a\nb"
    # 但字符串本身可以原样持有反斜杠(二进制安全靠的是列,不是文本层)
    assert t.serialize_text("a\\nb") == "a\\nb"


# ══════════════════════════════════════════════════════════════════════════
# 4. Enum / Decimal / 日期时间 / UUID / IP
# ══════════════════════════════════════════════════════════════════════════


def test_enum_name_int_mapping():
    t = get_data_type("Enum8('debug' = 1, 'info' = 2, 'error' = 3)")
    assert t.name_to_value == {"debug": 1, "info": 2, "error": 3}
    assert t.value_to_name[2] == "info"
    # 文本表示是名字
    assert t.serialize_text(2) == "info"
    # 写入既可给名字也可给整数
    assert t.normalize("info") == 2
    assert t.normalize(3) == 3
    # 整数可以写成字符串
    assert t.normalize("2") == 2


def test_enum_auto_increment_values():
    t = get_data_type("Enum8('a', 'b', 'c')")
    assert t.name_to_value == {"a": 1, "b": 2, "c": 3}


def test_enum_unknown_element_raises():
    t = get_data_type("Enum8('a' = 1)")
    with pytest.raises(MiniChError) as ei:
        t.normalize("nope")
    assert ei.value.code == ErrorCode.UNKNOWN_ELEMENT_OF_ENUM


def test_enum_out_of_range_value_raises():
    with pytest.raises(MiniChError) as ei:
        get_data_type("Enum8('a' = 200)")
    assert ei.value.code == ErrorCode.ARGUMENT_OUT_OF_BOUND


def test_enum_duplicate_name_raises():
    with pytest.raises(MiniChError) as ei:
        get_data_type("Enum8('a' = 1, 'a' = 2)")
    assert ei.value.code == ErrorCode.DUPLICATE_COLUMN


def test_decimal_scaling_and_text():
    t = get_data_type("Decimal(10, 2)")
    assert t.scale_factor() == 100
    assert t.normalize("1234567.89") == 123456789
    assert t.serialize_text(123456789) == "1234567.89"
    assert t.to_decimal(123456789) == __import__("decimal").Decimal("1234567.89")


def test_decimal_rounds_half_up():
    """超出 scale 的小数位按 ``ROUND_HALF_UP`` 舍入(与真实 CH 的 bround 略不同)。"""
    t = get_data_type("Decimal(10, 2)")
    assert t.normalize("0.125") == 13   # 0.125 → 0.13
    assert t.normalize("0.124") == 12


def test_decimal_out_of_range_raises():
    t = get_data_type("Decimal(4, 2)")
    with pytest.raises(MiniChError) as ei:
        t.normalize("1000.00")  # 超过 4 位整数部分
    assert ei.value.code == ErrorCode.ARGUMENT_OUT_OF_BOUND


def test_decimal_bad_precision_scale():
    with pytest.raises(MiniChError):
        get_data_type("Decimal(0, 0)")
    with pytest.raises(MiniChError):
        get_data_type("Decimal(5, 9)")  # scale > precision


@pytest.mark.parametrize(
    "lookup,bits,expected_precision",
    [("Decimal32(2)", 32, 9), ("Decimal64(2)", 64, 18),
     ("Decimal128(2)", 128, 38), ("Decimal256(2)", 256, 76)],
)
def test_decimal_alias_precision(lookup, bits, expected_precision):
    t = get_data_type(lookup)
    assert t.precision == expected_precision
    assert t._int_bits() == bits


# ── 日期时间 ─────────────────────────────────────────────────────────────


def test_date_to_days_and_back():
    assert date_to_days("1970-01-01") == 0
    assert date_to_days("2024-01-01") == 19723
    assert days_to_date(0).isoformat() == "1970-01-01"
    assert days_to_date(19723).isoformat() == "2024-01-01"


def test_date_rejects_bad_string():
    with pytest.raises(MiniChError) as ei:
        date_to_days("not-a-date")
    assert ei.value.code == ErrorCode.CANNOT_PARSE_DATE


def test_datetime_epoch_conversions():
    assert datetime_to_epoch("2024-01-01 00:00:00") == 1704067200
    assert format_datetime(1704067200) == "2024-01-01 00:00:00"
    # 时区偏移只在**展示**时加上,存储仍是 UTC 秒
    tz = get_data_type("DateTime('Asia/Shanghai')")
    assert tz.serialize_text(1704067200) == "2024-01-01 08:00:00"
    assert tz.deserialize_text("2024-01-01 08:00:00") == 1704067200


@pytest.mark.parametrize(
    "tz,offset",
    [("UTC", 0), (None, 0), ("UTC+8", 8 * 3600), ("+08:00", 8 * 3600),
     ("Asia/Shanghai", 8 * 3600), ("America/New_York", -5 * 3600)],
)
def test_parse_timezone_offset(tz, offset):
    assert parse_timezone_offset(tz) == offset


def test_parse_timezone_unknown_raises():
    with pytest.raises(MiniChError):
        parse_timezone_offset("Mars/Olympus")


def test_datetime64_precision_bounds():
    assert get_data_type("DateTime64(0)").scale() == 1
    assert get_data_type("DateTime64(3)").scale() == 1000
    with pytest.raises(MiniChError) as ei:
        get_data_type("DateTime64(10)")
    assert ei.value.code == ErrorCode.ARGUMENT_OUT_OF_BOUND


def test_datetime64_fractional_roundtrip():
    t = get_data_type("DateTime64(3)")
    v = t.normalize("2024-01-01 00:00:00.123")
    assert v == 1704067200123
    assert t.serialize_text(v) == "2024-01-01 00:00:00.123"


# ── UUID / IP ────────────────────────────────────────────────────────────


def test_uuid_parse_and_format():
    v = parse_uuid("550e8400-e29b-41d4-a716-446655440000")
    assert format_uuid(v) == "550e8400-e29b-41d4-a716-446655440000"
    # 不带连字符的 32 位十六进制也接受
    assert parse_uuid("550e8400e29b41d4a716446655440000") == v
    # 花括号包裹也接受
    assert parse_uuid("{550e8400-e29b-41d4-a716-446655440000}") == v


def test_uuid_bad_text_raises():
    with pytest.raises(MiniChError) as ei:
        parse_uuid("not-a-uuid")
    assert ei.value.code == ErrorCode.CANNOT_PARSE_UUID


def test_ipv4_parse_format_roundtrip():
    assert ipv4_to_int("192.168.0.1") == 0xC0A80001
    assert int_to_ipv4(0xC0A80001) == "192.168.0.1"
    assert ipv4_to_int("0.0.0.0") == 0


@pytest.mark.parametrize("bad", ["1.2.3", "1.2.3.256", "a.b.c.d", "1.2.3.-1"])
def test_ipv4_bad_text_raises(bad):
    with pytest.raises(MiniChError) as ei:
        ipv4_to_int(bad)
    assert ei.value.code == ErrorCode.CANNOT_PARSE_TEXT


# ══════════════════════════════════════════════════════════════════════════
# 5. 复合类型约束与行为
# ══════════════════════════════════════════════════════════════════════════


def test_nullable_of_nullable_raises():
    with pytest.raises(MiniChError) as ei:
        get_data_type("Nullable(Nullable(UInt8))")
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_nullable_of_nothing_is_allowed():
    """``Nullable(Nothing)`` 是合法的 —— ``SELECT NULL`` 的类型就是它。"""
    t = get_data_type("Nullable(Nothing)")
    assert t.get_name() == "Nullable(Nothing)"
    assert t.is_nullable()


def test_array_default_is_empty_not_zero():
    assert get_data_type("Array(UInt8)").default_value() == []


def test_array_rejects_scalar_insert():
    with pytest.raises(MiniChError) as ei:
        get_data_type("Array(UInt8)").normalize("not-a-list")
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_tuple_named_accessors():
    t = get_data_type("Tuple(a UInt8, b String)")
    assert t.get_subcolumn_names() == ["a", "b"]
    assert t.get_subcolumn_type("b").get_name() == "String"
    assert t.get_subcolumn_index("a") == 0
    assert t.get_subcolumn_index("z") == -1
    assert t.normalize({"a": 1, "b": "x"}) == (1, "x")


def test_tuple_unnamed_has_no_subcolumn_names():
    t = get_data_type("Tuple(UInt8, String)")
    assert t.get_subcolumn_names() == []
    # 往匿名元组插 dict 是非法的
    with pytest.raises(MiniChError):
        t.normalize({"a": 1})


def test_tuple_length_mismatch_raises():
    t = get_data_type("Tuple(UInt8, String)")
    with pytest.raises(MiniChError) as ei:
        t.normalize((1,))
    assert ei.value.code == ErrorCode.SIZES_OF_COLUMNS_IN_TUPLE_DOESNT_MATCH


@pytest.mark.parametrize("bad_key", ["Array(UInt8)", "Map(String, UInt8)", "Nullable(String)"])
def test_map_rejects_invalid_key_type(bad_key):
    with pytest.raises(MiniChError) as ei:
        get_data_type(f"Map({bad_key}, UInt8)")
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_map_normalize_from_list_of_pairs():
    t = get_data_type("Map(String, UInt8)")
    assert t.normalize([("a", 1), ("b", 2)]) == {"a": 1, "b": 2}


def test_low_cardinality_of_nullable_raises():
    with pytest.raises(MiniChError) as ei:
        get_data_type("LowCardinality(Nullable(String))")
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_variant_sorts_branches_and_rejects_duplicates():
    t = get_data_type("Variant(UInt64, String)")
    assert t.get_name() == "Variant(String, UInt64)"
    with pytest.raises(MiniChError) as ei:
        get_data_type("Variant(UInt8, UInt8)")
    assert ei.value.code == ErrorCode.DUPLICATE_COLUMN


def test_nothing_is_always_none():
    t = get_data_type("Nothing")
    assert t.default_value() is None
    assert t.is_null(123) is True


# ══════════════════════════════════════════════════════════════════════════
# 6. create_column 的物化方式
# ══════════════════════════════════════════════════════════════════════════


def test_create_column_returns_expected_column_class():
    assert isinstance(get_data_type("UInt64").create_column(), ColumnVector)
    assert isinstance(get_data_type("UInt128").create_column(), ColumnBigInt)
    assert isinstance(get_data_type("String").create_column(), ColumnString)
    assert isinstance(get_data_type("FixedString(3)").create_column(), ColumnFixedString)
    assert isinstance(get_data_type("UUID").create_column(), ColumnUUID)
    assert isinstance(get_data_type("Nothing").create_column(), ColumnNothing)
    assert isinstance(get_data_type("Nullable(UInt8)").create_column(), ColumnNullable)
    assert isinstance(get_data_type("Array(UInt8)").create_column(), ColumnArray)
    assert isinstance(get_data_type("Tuple(UInt8)").create_column(), ColumnTuple)
    assert isinstance(get_data_type("Map(String, UInt8)").create_column(), ColumnMap)
    assert isinstance(
        get_data_type("LowCardinality(String)").create_column(), ColumnLowCardinality
    )


def test_column_dtype_matches_type_bits():
    assert get_data_type("UInt8").create_column().dtype == np.uint8
    assert get_data_type("UInt32").create_column().dtype == np.uint32
    assert get_data_type("Float32").create_column().dtype == np.float32
    assert get_data_type("Date").create_column().dtype == np.uint16
    assert get_data_type("DateTime").create_column().dtype == np.uint32


def test_fixed_string_normalize_rejects_too_long():
    with pytest.raises(MiniChError) as ei:
        get_data_type("FixedString(2)").normalize("abc")
    assert ei.value.code == ErrorCode.SIZE_OF_FIXED_STRING_DOESNT_MATCH


def test_bool_normalize_and_text():
    t = get_data_type("Bool")
    assert t.normalize("yes") is True
    assert t.normalize("0") is False
    assert t.normalize(5) is True
    assert t.serialize_text(True) == "true"


def test_interval_kind_names():
    assert get_data_type("IntervalDay").get_name() == "IntervalDay"
    assert get_data_type("IntervalSecond").normalize(5) == 5
    with pytest.raises(MiniChError):
        get_data_type("IntervalFortnight")


# ══════════════════════════════════════════════════════════════════════════
# 7. 回归:Decimal 与浮点 / 精度校验
# ══════════════════════════════════════════════════════════════════════════


def test_decimal_and_float_have_no_common_type():
    """``Decimal`` 与 ``Float64`` **没有**公共类型 —— 真实 CH 直接报错。

    真实 ClickHouse 无法在不丢精度的前提下把浮点塞进定点数,所以
    ``toDecimal32(1.23, 2) + 0.5`` 会抛 ``ILLEGAL_TYPE_OF_ARGUMENT``
    ("Illegal types Decimal(9, 2) and Float64 of arguments of function plus"),
    而不是把 ``0.5`` 隐式截断成 0。
    """
    d = get_data_type("Decimal(9, 2)")
    f = get_data_type("Float64")
    with pytest.raises(MiniChError) as ei:
        get_wider_numeric_type(d, f)
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT
    # 参数顺序互换也必须拦住
    with pytest.raises(MiniChError):
        get_wider_numeric_type(f, d)


@pytest.mark.parametrize("op", ["add", "sub", "mul", "div"])
def test_decimal_float_arithmetic_is_rejected(op):
    """四种算术都不允许 Decimal 与 Float 混算(不得静默截断、更不得除零)。"""
    from minich.functions.arithmetic import arithmetic_result_type

    d = get_data_type("Decimal(9, 2)")
    f = get_data_type("Float64")
    for ta, tb in ((d, f), (f, d)):
        with pytest.raises(MiniChError) as ei:
            arithmetic_result_type(op, ta, tb)
        assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_decimal_integer_still_promotes_to_decimal():
    """护栏:Decimal 与**整数**仍按真实 CH 隐式提升 —— 上面的改动不能波及它。"""
    from minich.functions.arithmetic import arithmetic_result_type

    d = get_data_type("Decimal(9, 2)")
    u = get_data_type("UInt64")
    assert get_wider_numeric_type(d, u).get_name() == "Decimal(9, 2)"
    assert arithmetic_result_type("add", d, u).get_name() == "Decimal(9, 2)"


def test_decimal_normalize_checks_precision_for_int_input():
    """``Decimal(P, S).normalize(int)`` 必须和 float / str 分支一样做精度校验。

    否则 ``INSERT`` 的整数形式可以把装不下的值静默写进表里 —— 真实 CH
    会报 ``ARGUMENT_OUT_OF_BOUND``。
    """
    t = get_data_type("Decimal(18, 2)")
    # 10**16 - 1 是 16 位,乘 scale(100) 后 18 位,刚好装得下
    assert t.normalize(10**16 - 1) == (10**16 - 1) * 100
    with pytest.raises(MiniChError) as ei:
        t.normalize(10**16)  # 乘 scale 后 19 位 > P=18
    assert ei.value.code == ErrorCode.ARGUMENT_OUT_OF_BOUND
    # float 与 str 分支本来就会报错 —— 三条分支行为必须一致
    with pytest.raises(MiniChError) as ef:
        t.normalize(float(10**16))
    assert ef.value.code == ErrorCode.ARGUMENT_OUT_OF_BOUND


# ══════════════════════════════════════════════════════════════════════════
# 8. 回归:列实现(``clone_resized`` / 稀疏列 ``cut`` / 低基数默认值)
# ══════════════════════════════════════════════════════════════════════════


def test_clone_resized_copies_existing_data():
    """``clone_resized(n)`` 先**复制**再调整大小(真实 CH 的 ``cloneResized``)。

    ``n < size`` 保留前 ``n`` 行;``n > size`` 用默认值补足 —— 早先的实现从
    空列 resize,把已有数据全丢了(``[1,2,3,4] -> [0,0]``)。
    """
    c = ColumnVector(np.int64, [1, 2, 3, 4])
    assert c.clone_resized(2).to_list() == [1, 2]
    assert c.clone_resized(6).to_list() == [1, 2, 3, 4, 0, 0]
    # 原来的列不能被改动
    assert c.to_list() == [1, 2, 3, 4]


def test_column_sparse_cut_actually_removes():
    """``ColumnSparse.cut`` 曾经是空操作(把 ``None or 新列`` 当成结果)。"""
    c = ColumnSparse(ColumnVector(np.int32), default_value=0)
    c.append_many([0, 0, 7, 0, 3])
    c.cut(1, 2)  # 删掉第 1、2 行(值 0 与 7)
    assert c.to_list() == [0, 0, 3]
    # pop_back 走基类的 cut —— 一并验证它不再是空操作
    c.pop_back(1)
    assert c.to_list() == [0, 0]


def test_low_cardinality_default_value_is_nested_default():
    """``ColumnLowCardinality.default_value`` 必须来自字典列,而不是硬编码的 0。

    否则 ``append_default()`` 会把 ``int 0`` 塞进 ``String`` 字典,``INSERT NULL``
    之类的默认值路径直接抛 "Cannot insert int into a String column"。
    """
    lc = get_data_type("LowCardinality(String)").create_column()
    assert lc.default_value() == ""
    lc.append_default()
    assert lc.to_list() == [""]
