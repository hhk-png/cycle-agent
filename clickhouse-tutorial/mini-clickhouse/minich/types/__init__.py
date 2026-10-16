"""类型注册表 —— 把类型名解析成 :class:`DataType` 对象。

这里是"字符串 → 类型"的唯一入口。真实 ClickHouse 的对应物是
``DataTypeFactory``(``src/DataTypes/DataTypeFactory.cpp``),
它维护一张 ``family_name → creator`` 的表,并提供:

* ``get(name)`` —— 解析一个类型名(支持任意嵌套);
* ``registerAlias(from, to)`` —— 别名(``INT`` → ``Int32``、``VARCHAR`` → ``String``);
* ``tryGet(name)`` —— 解析失败返回空指针。

mini-clickhouse 的对应函数是 :func:`get_data_type` / :func:`try_get_data_type`,
外加 :func:`registered_type_names`(供 ``system.data_type_families`` 使用)。

.. code-block:: python

   >>> from minich.types import get_data_type
   >>> get_data_type("Array(Nullable(UInt8))")
   DataTypeArray('Array(Nullable(UInt8))')

解析的关键难点是**参数不一定是类型**:

===================  ================================  ==================
类型名               参数                              参数是什么
===================  ================================  ==================
``Array(T)``         ``UInt8``                        类型
``FixedString(16)``  ``16``                           整数
``DateTime('UTC')``  ``'UTC'``                        字符串
``Decimal(10, 2)``   ``10, 2``                        两个整数
``Enum8('a' = 1)``   ``'a' = 1``                      名字 = 整数
``Tuple(a UInt8)``   ``a UInt8``                      名字 + 类型
``AggregateFunction(quantiles(0.5), UInt8)``          **带参数的函数名**
===================  ================================  ==================

所以工厂收到的是**原始参数文本**(``list[str]``),由每个类型自己决定怎么解析。
这个设计是照搬真实 ClickHouse 的 ``DataTypeFactory::get`` —— 它也是把
`ASTPtr` 交给 creator 自己处理。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

from ..columns import IColumn
from ..errors import ErrorCode, MiniChError, raise_error
from .base import (
    DataType,
    DataTypeFactory,
    get_data_type,
    parse_type_name,
    register_data_type,
    registered_type_names,
    try_get_data_type,
)
from .composite import (
    DataTypeAggregateFunction,
    DataTypeArray,
    DataTypeDecimal,
    DataTypeDynamic,
    DataTypeJSON,
    DataTypeLowCardinality,
    DataTypeMap,
    DataTypeNested,
    DataTypeNullable,
    DataTypeSimpleAggregateFunction,
    DataTypeTuple,
    DataTypeVariant,
    decimal_alias,
)
from .scalars import (
    DataTypeBool,
    DataTypeDate,
    DataTypeDate32,
    DataTypeDateTime,
    DataTypeDateTime64,
    DataTypeEnum,
    DataTypeFixedString,
    DataTypeIPv4,
    DataTypeIPv6,
    DataTypeInterval,
    DataTypeNothing,
    DataTypeNumber,
    DataTypeString,
    DataTypeUUID,
    INTERVAL_KINDS,
    days_to_date,
    date_to_days,
    format_datetime,
    format_uuid,
    int_to_ipv4,
    ipv4_to_int,
    parse_timezone_offset,
    parse_uuid,
)

__all__ = [
    "DataType",
    "get_data_type",
    "try_get_data_type",
    "registered_type_names",
    "parse_type_name",
    "register_data_type",
    "get_numeric_type",
    "get_wider_numeric_type",
    "INTEGER_TYPES",
    "NUMERIC_TYPES",
]


# ══════════════════════════════════════════════════════════════════════════
# 标量类型工厂
# ══════════════════════════════════════════════════════════════════════════

#: 位宽 → (名字, 有符号, 是否浮点)
_NUMERIC_SPECS: List[Tuple[str, int, bool, bool]] = [
    ("UInt8", 8, False, False),
    ("UInt16", 16, False, False),
    ("UInt32", 32, False, False),
    ("UInt64", 64, False, False),
    ("UInt128", 128, False, False),
    ("UInt256", 256, False, False),
    ("Int8", 8, True, False),
    ("Int16", 16, True, False),
    ("Int32", 32, True, False),
    ("Int64", 64, True, False),
    ("Int128", 128, True, False),
    ("Int256", 256, True, False),
    ("Float32", 32, True, True),
    ("Float64", 64, True, True),
]

#: 整数类型名(不含浮点与 Bool)—— 类型推导时按这个顺序做"提升"
INTEGER_TYPES = [n for n, _, _, is_float in _NUMERIC_SPECS if not is_float]

#: 全部数值类型名
NUMERIC_TYPES = [n for n, _, _, _ in _NUMERIC_SPECS] + ["Bool"]

#: 类型名 → 已构造好的单例。标量类型没有参数,可以复用同一个对象 ——
#: 真实 ClickHouse 的 DataTypeFactory 也做同样的缓存(避免每次查表都 new)。
_SCALAR_CACHE: dict = {}


def _make_scalar(name: str) -> DataType:
    cached = _SCALAR_CACHE.get(name)
    if cached is not None:
        return cached
    for n, bits, signed, is_float in _NUMERIC_SPECS:
        if n == name:
            obj: DataType = DataTypeNumber(name, bits, signed, is_float)
            _SCALAR_CACHE[name] = obj
            return obj
    simple = {
        "String": DataTypeString,
        "Bool": DataTypeBool,
        "UUID": DataTypeUUID,
        "IPv4": DataTypeIPv4,
        "IPv6": DataTypeIPv6,
        "Date": DataTypeDate,
        "Date32": DataTypeDate32,
        "DateTime": DataTypeDateTime,
        "Nothing": DataTypeNothing,
        "Dynamic": DataTypeDynamic,
        "JSON": DataTypeJSON,
    }
    factory = simple.get(name)
    if factory is None:
        raise_error(ErrorCode.UNKNOWN_TYPE, f"Unknown numeric/scalar type: {name}")
    obj = factory()  # type: ignore[call-arg]
    _SCALAR_CACHE[name] = obj
    return obj


def _numeric_factory(name: str) -> DataTypeFactory:
    def factory(args: List[str]) -> DataType:
        if args:
            raise_error(
                ErrorCode.UNSUPPORTED_PARAMETER,
                f"Type {name} does not accept parameters, got ({', '.join(args)})",
            )
        return _make_scalar(name)

    return factory


for _name, _bits, _signed, _is_float in _NUMERIC_SPECS:
    register_data_type(_name, _numeric_factory(_name))
del _name, _bits, _signed, _is_float

register_data_type("String", _numeric_factory("String"))
register_data_type("Bool", _numeric_factory("Bool"))
register_data_type("UUID", _numeric_factory("UUID"))
register_data_type("IPv4", _numeric_factory("IPv4"))
register_data_type("IPv6", _numeric_factory("IPv6"))
register_data_type("Date", _numeric_factory("Date"))
register_data_type("Date32", _numeric_factory("Date32"))
register_data_type("Nothing", _numeric_factory("Nothing"))
register_data_type("Dynamic", _numeric_factory("Dynamic"))


def _fixed_string_factory(args: List[str]) -> DataType:
    if len(args) != 1:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            f"FixedString requires exactly one argument, got {len(args)}",
        )
    try:
        n = int(args[0])
    except ValueError:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"FixedString size must be an integer, got {args[0]!r}",
        )
    return DataTypeFixedString(n)


register_data_type("FixedString", _fixed_string_factory)


def _datetime_factory(args: List[str]) -> DataType:
    if not args:
        return _make_scalar("DateTime")
    if len(args) > 1:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "DateTime accepts at most one argument (timezone)",
        )
    return DataTypeDateTime(_unquote(args[0]))


register_data_type("DateTime", _datetime_factory)


def _datetime64_factory(args: List[str]) -> DataType:
    if not args:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "DateTime64 requires a precision argument, e.g. DateTime64(3)",
        )
    try:
        precision = int(args[0])
    except ValueError:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"DateTime64 precision must be an integer, got {args[0]!r}",
        )
    tz = _unquote(args[1]) if len(args) > 1 else None
    if len(args) > 2:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "DateTime64 accepts at most two arguments (precision, timezone)",
        )
    return DataTypeDateTime64(precision, tz)


register_data_type("DateTime64", _datetime64_factory)


def _decimal_factory(args: List[str]) -> DataType:
    if len(args) != 2:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            f"Decimal requires (precision, scale), got {len(args)} argument(s)",
        )
    try:
        return DataTypeDecimal(int(args[0]), int(args[1]))
    except ValueError:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Decimal arguments must be integers, got ({args[0]}, {args[1]})",
        )


register_data_type("Decimal", _decimal_factory)


def _decimal_alias_factory(bits: int) -> DataTypeFactory:
    def factory(args: List[str]) -> DataType:
        if len(args) != 1:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"Decimal{bits} requires exactly one argument (scale)",
            )
        try:
            return decimal_alias(bits, int(args[0]))
        except ValueError:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Decimal{bits} scale must be an integer, got {args[0]!r}",
            )

    return factory


for _bits in (32, 64, 128, 256):
    register_data_type(f"Decimal{_bits}", _decimal_alias_factory(_bits))
del _bits


def _enum_factory(bits: int) -> DataTypeFactory:
    def factory(args: List[str]) -> DataType:
        names: List[str] = []
        values: List[int] = []
        next_value = 1
        for raw in args:
            name, explicit = _parse_enum_element(raw)
            if explicit is None:
                value = next_value
            else:
                value = explicit
            names.append(name)
            values.append(value)
            next_value = value + 1
        return DataTypeEnum(names, values, bits)

    return factory


register_data_type("Enum8", _enum_factory(8))
register_data_type("Enum16", _enum_factory(16))
# 裸 `Enum` 在真实 ClickHouse 里要求显式指定 8/16;我们默认 8,
# 并在类型名里体现出实际宽度(解析出来的 get_name() 会带 8)。
register_data_type("Enum", _enum_factory(8))


def _parse_enum_element(raw: str) -> Tuple[str, Optional[int]]:
    """解析 ``'debug' = 1`` 或 ``'debug'``。"""
    from ..literals import split_top_level

    parts = split_top_level(raw, "=")
    if len(parts) > 2:
        raise_error(
            ErrorCode.SYNTAX_ERROR,
            f"Malformed enum element {raw!r}: expected 'name' or 'name' = value",
        )
    name = _unquote(parts[0])
    if not name:
        raise_error(ErrorCode.SYNTAX_ERROR, f"Malformed enum element {raw!r}: empty name")
    if len(parts) == 1:
        return name, None
    try:
        return name, int(parts[1])
    except ValueError:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Enum value must be an integer, got {parts[1]!r}",
        )


# ── Interval ────────────────────────────────────────────────────────────


def _interval_factory(kind: str) -> DataTypeFactory:
    def factory(args: List[str]) -> DataType:
        if args:
            raise_error(
                ErrorCode.UNSUPPORTED_PARAMETER,
                f"{kind} interval type does not accept parameters",
            )
        return DataTypeInterval(kind)

    return factory


for _kind in INTERVAL_KINDS:
    register_data_type(f"Interval{INTERVAL_KINDS[_kind][0]}", _interval_factory(_kind))
del _kind


# ══════════════════════════════════════════════════════════════════════════
# 复合类型工厂
# ══════════════════════════════════════════════════════════════════════════


def _nullable_factory(args: List[str]) -> DataType:
    if len(args) != 1:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "Nullable requires exactly one type argument",
        )
    return DataTypeNullable(get_data_type(args[0]))


register_data_type("Nullable", _nullable_factory)


def _array_factory(args: List[str]) -> DataType:
    if len(args) != 1:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "Array requires exactly one type argument",
        )
    return DataTypeArray(get_data_type(args[0]))


register_data_type("Array", _array_factory)


def _low_cardinality_factory(args: List[str]) -> DataType:
    if len(args) != 1:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "LowCardinality requires exactly one type argument",
        )
    return DataTypeLowCardinality(get_data_type(args[0]))


register_data_type("LowCardinality", _low_cardinality_factory)


def _tuple_factory(args: List[str]) -> DataType:
    if not args:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "Tuple requires at least one type argument",
        )
    types: List[DataType] = []
    names: List[str] = []
    named_count = 0
    for raw in args:
        name, type_text = _split_named_field(raw)
        types.append(get_data_type(type_text))
        names.append(name or "")
        if name:
            named_count += 1
    if named_count == 0:
        return DataTypeTuple(types, None)
    if named_count != len(args):
        raise_error(
            ErrorCode.SYNTAX_ERROR,
            "Tuple cannot mix named and unnamed elements: "
            f"{args}",
        )
    return DataTypeTuple(types, names)


register_data_type("Tuple", _tuple_factory)


def _map_factory(args: List[str]) -> DataType:
    if len(args) != 2:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "Map requires exactly two type arguments: Map(K, V)",
        )
    return DataTypeMap(get_data_type(args[0]), get_data_type(args[1]))


register_data_type("Map", _map_factory)


def _nested_factory(args: List[str]) -> DataType:
    if not args:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "Nested requires at least one field",
        )
    types: List[DataType] = []
    names: List[str] = []
    for raw in args:
        name, type_text = _split_named_field(raw)
        if not name:
            raise_error(
                ErrorCode.SYNTAX_ERROR,
                f"Nested fields must be named, got {raw!r}",
            )
        names.append(name)
        types.append(get_data_type(type_text))
    return DataTypeNested(types, names)


register_data_type("Nested", _nested_factory)


def _aggregate_function_factory(args: List[str]) -> DataType:
    if not args:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "AggregateFunction requires a function name",
        )
    func_name, params = _split_function_with_params(args[0])
    arg_types = [get_data_type(a) for a in args[1:]]
    return DataTypeAggregateFunction(func_name, arg_types, params)


register_data_type("AggregateFunction", _aggregate_function_factory)


def _simple_aggregate_function_factory(args: List[str]) -> DataType:
    if len(args) != 2:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "SimpleAggregateFunction requires (function_name, type)",
        )
    func_name, _ = _split_function_with_params(args[0])
    return DataTypeSimpleAggregateFunction(func_name, get_data_type(args[1]))


register_data_type("SimpleAggregateFunction", _simple_aggregate_function_factory)


def _variant_factory(args: List[str]) -> DataType:
    if not args:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "Variant requires at least one type",
        )
    return DataTypeVariant([get_data_type(a) for a in args])


register_data_type("Variant", _variant_factory)


def _json_factory(args: List[str]) -> DataType:
    if not args:
        return DataTypeJSON()
    # Object('json') 是旧写法;真实 CH 里 Object 只接受 'json' 一个参数
    if len(args) == 1 and _unquote(args[0]).lower() == "json":
        return DataTypeJSON()
    raise_error(
        ErrorCode.UNSUPPORTED_PARAMETER,
        f"JSON type does not accept these parameters: {args}",
    )


register_data_type("JSON", _json_factory)
register_data_type("Object", _json_factory)


# ══════════════════════════════════════════════════════════════════════════
# 解析工具
# ══════════════════════════════════════════════════════════════════════════


def _unquote(text: str) -> str:
    """去掉一层引号/反引号。"""
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"`":
        return s[1:-1]
    return s


def _split_named_field(raw: str) -> Tuple[Optional[str], str]:
    """解析 ``a UInt8`` 这种"字段名 + 类型",或纯类型 ``UInt8``。

    做法是**先试着把整段当类型解析** —— 能解析成功就说明没有字段名。
    这比"按空格切第一个 token"更可靠,因为类型名里可以带空格
    (``Decimal(10, 2)``、``Tuple(a UInt8, b String)``)。
    """
    text = raw.strip()
    if not text:
        raise_error(ErrorCode.SYNTAX_ERROR, "Empty field in composite type")
    if try_get_data_type(text) is not None:
        return None, text
    # 反引号包裹的名字
    if text.startswith("`"):
        end = text.find("`", 1)
        if end < 0:
            raise_error(ErrorCode.SYNTAX_ERROR, f"Unterminated backtick name in {raw!r}")
        return text[1:end], text[end + 1 :].strip()
    # 普通标识符当名字
    i = 0
    while i < len(text) and (text[i].isalnum() or text[i] in "_$"):
        i += 1
    if i == 0:
        raise_error(ErrorCode.SYNTAX_ERROR, f"Cannot parse field {raw!r}")
    name, rest = text[:i], text[i:].strip()
    if not rest:
        raise_error(
            ErrorCode.SYNTAX_ERROR,
            f"Field {raw!r} has a name but no type",
        )
    return name, rest


def _split_function_with_params(text: str) -> Tuple[str, List[Any]]:
    """把 ``quantiles(0.5, 0.9)`` 拆成 ``("quantiles", [0.5, 0.9])``。"""
    from ..literals import parse_literal, split_top_level

    s = text.strip()
    if "(" not in s:
        return s, []
    name = s[: s.index("(")].strip()
    body = s[s.index("(") + 1 :]
    if not body.endswith(")"):
        raise_error(ErrorCode.SYNTAX_ERROR, f"Malformed function parameters in {text!r}")
    params = [parse_literal(p) for p in split_top_level(body[:-1]) if p != ""]
    return name, params


# ══════════════════════════════════════════════════════════════════════════
# 类型推导辅助
# ══════════════════════════════════════════════════════════════════════════


def get_numeric_type(name: str) -> DataType:
    """按名字取一个数值类型(要求已是数值类型)。"""
    t = get_data_type(name)
    if not t.is_numeric():
        raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, f"{name} is not a numeric type")
    return t


#: 数值类型的"提升顺序":算两个不同类型时取**较宽**的那个。
#: 真实 ClickHouse 的规则见 ``src/DataTypes/NumberTraits.h`` 与
#: ``src/Functions/FunctionBinaryArithmetic.h`` —— 大方向一致,
#: 但真实实现里 UInt64 + Int8 会得到 Int64(可能溢出),
#: 我们保守地提升到 Int128,并在教程里标注这个差异。
_PROMOTION_ORDER = [
    "UInt8", "UInt16", "UInt32", "UInt64", "UInt128", "UInt256",
    "Int8", "Int16", "Int32", "Int64", "Int128", "Int256",
    "Float32", "Float64",
]


def get_wider_numeric_type(a: DataType, b: DataType) -> DataType:
    """取两个数值类型里**更宽**的那个(用于二元算术的结果类型推导)。

    规则(与真实 ClickHouse 一致的部分):

    * 浮点 > 整数:``UInt64 + Float32 → Float64``;
    * 同为整数时,**有符号**优先(避免负数丢符号),但位宽取两者较大;
    * 同族取位宽较大者。

    与真实 CH 的差异:``UInt64 + Int8`` 在真实 CH 里是 ``Int64``
    (可能溢出),我们提升到 ``Int128`` —— 更安全但不完全一致。
    这一条写在教程第 03 章的"与真实 ClickHouse 的差异"表里。
    """
    if a.is_decimal() or b.is_decimal():
        return _wider_decimal(a, b)
    if not a.is_numeric() or not b.is_numeric():
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Cannot compute a common numeric type for {a.get_name()} and {b.get_name()}",
        )
    na, nb = a.get_name(), b.get_name()
    if na == nb:
        return a
    fa = na.startswith("Float")
    fb = nb.startswith("Float")
    if fa or fb:
        if fa and fb:
            return a if a.bits >= b.bits else b
        other = b if fa else a
        # 整数与浮点混合:结果至少 Float64(整数可能超出 float32 精度)
        return _make_scalar("Float64") if other.bits > 24 else _make_scalar(na if fa else nb)
    bits = max(a.bits, b.bits)
    signed = a.signed or b.signed
    # 有符号与无符号混用:为了避免溢出,位宽 +1(除非已经到 256)
    if a.signed != b.signed:
        bits = min(bits * 2, 256)
        signed = True
    name = f"{'Int' if signed else 'UInt'}{bits}"
    if name not in _PROMOTION_ORDER:
        name = f"{'Int' if signed else 'UInt'}256"
    return _make_scalar(name)


def _wider_decimal(a: DataType, b: DataType) -> DataType:
    """Decimal 之间的公共类型:精度取和,scale 取大。

    ``Decimal`` 与**浮点**没有公共类型 —— 真实 ClickHouse 无法在不丢精度的
    前提下把 ``Float32``/``Float64`` 塞进定点数,于是直接报错
    (``toDecimal32(1.23, 2) + 0.5`` → ``ILLEGAL_TYPE_OF_ARGUMENT``:
    ``Illegal types Decimal(9, 2) and Float64 of arguments of function plus``)。
    这里同样**拒绝**,而不是把浮点当整数隐式提升、再用 ``int()`` 截断
    (那会把 ``0.5`` 变成 ``0``,除法还会撞上"除零")。

    ``Decimal`` 与**整数**混合仍然隐式提升到 ``Decimal``(与真实 CH 一致)。
    """
    if not a.is_decimal():
        if a.is_float():
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot compute a common numeric type for "
                f"{a.get_name()} and {b.get_name()}",
            )
        return b
    if not b.is_decimal():
        if b.is_float():
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot compute a common numeric type for "
                f"{a.get_name()} and {b.get_name()}",
            )
        return a
    precision = min(a.precision + b.precision, 76)
    scale = max(a.scale, b.scale)
    return DataTypeDecimal(precision, scale)
