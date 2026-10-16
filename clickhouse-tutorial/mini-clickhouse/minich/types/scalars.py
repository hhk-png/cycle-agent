"""标量类型:数值、字符串、日期时间、枚举、UUID、IP、Interval。

这是 ``system.data_type_families`` 里最大的那一类。虽然"标量"听起来简单,
但 ClickHouse 在这里埋了**最多**的坑,而且几乎全是"从别的数据库迁过来的人"
会踩的:

.. code-block:: text

   坑 1  整数溢出是"回绕"而不是报错   UInt8 的 250 + 10 = 4
   坑 2  默认整数类型是"能装下就最小"  SELECT 1 的类型是 UInt8,不是 Int32
   坑 3  Float 的文本表示是"最短往返"  0.1 + 0.2 打印出来不是 0.30000000000000004
   坑 4  Date 只有 1970-01-01 ~ 2149-06-06,超出范围要用 Date32
   坑 5  DateTime 的时区是"列的属性"  同一张表里两列可以有不同的时区
   坑 6  Enum 的文本表示是"名字"      插进去 1 存的是名字,读出来也是名字

第 03 章会逐条展开。这里把**语义**做对是最重要的 ——
因为下游每一个函数都建立在这些语义之上。

.. note::

   真实 ClickHouse 的这些类型都在 ``src/DataTypes/DataTypeNumberBase.h``
   与 ``DataTypeNumber.cpp`` 里,由宏批量生成
   (``M(Int8) M(Int16) ...``)。mini-clickhouse 也用工厂批量生成,
   但显式列出了每一个,因为读者需要一个能跳转的类名。
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import struct
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..columns import (
    ColumnBigInt,
    ColumnFixedString,
    ColumnString,
    ColumnUUID,
    ColumnVector,
    IColumn,
)
from ..errors import ErrorCode, raise_error
from .base import DataType

__all__ = [
    "DataTypeNumber",
    "DataTypeBool",
    "DataTypeString",
    "DataTypeFixedString",
    "DataTypeUUID",
    "DataTypeIPv4",
    "DataTypeIPv6",
    "DataTypeDate",
    "DataTypeDate32",
    "DataTypeDateTime",
    "DataTypeDateTime64",
    "DataTypeEnum",
    "DataTypeNothing",
    "DataTypeInterval",
    "INTERVAL_KINDS",
    "EPOCH_DATE",
]


# ══════════════════════════════════════════════════════════════════════════
# 数值
# ══════════════════════════════════════════════════════════════════════════


class DataTypeNumber(DataType):
    """所有整数/浮点类型的共同实现。

    ``bits`` / ``signed`` / ``is_float`` 三个参数决定一切。
    真实 ClickHouse 里 ``DataTypeNumber<T>`` 是一个模板类,
    T 决定了值域、列类型与序列化方式 —— 完全相同的分工。
    """

    def __init__(self, name: str, bits: int, signed: bool, is_float: bool = False) -> None:
        self._name = name
        self.bits = bits
        self.signed = signed
        self.float = is_float
        self.family_name = name

    # ── 名字与列 ────────────────────────────────────────────────────────

    def get_name(self) -> str:
        return self._name

    def _numpy_dtype(self) -> Optional[np.dtype]:
        if self.bits > 64:
            return None
        if self.float:
            return np.dtype(np.float32 if self.bits == 32 else np.float64)
        prefix = "i" if self.signed else "u"
        return np.dtype(f"{prefix}{self.bits // 8}")

    def create_column(self) -> IColumn:
        dt = self._numpy_dtype()
        if dt is None:
            return ColumnBigInt(self.signed, self.bits)
        return ColumnVector(dt)

    # ── 属性 ────────────────────────────────────────────────────────────

    def is_numeric(self) -> bool:
        return True

    def is_integer(self) -> bool:
        return not self.float

    def is_float(self) -> bool:
        return self.float

    def get_size_of_value_in_memory(self) -> int:
        return max(1, self.bits // 8)

    def have_maximum_size_of_value(self) -> bool:
        return True

    def get_maximum_size_of_value(self) -> int:
        return max(1, self.bits // 8)

    # ── 值与范围 ────────────────────────────────────────────────────────

    def min_value(self) -> Any:
        if self.float:
            return float("-inf")
        if self.signed:
            return -(1 << (self.bits - 1))
        return 0

    def max_value(self) -> Any:
        if self.float:
            return float("inf")
        if self.signed:
            return (1 << (self.bits - 1)) - 1
        return (1 << self.bits) - 1

    def default_value(self) -> Any:
        return 0.0 if self.float else 0

    def wrap(self, value: int) -> int:
        """**回绕**到该类型的值域内(C++ 的整数溢出语义)。

        .. code-block:: python

           >>> uint8.wrap(250 + 10)
           4
           >>> int8.wrap(127 + 1)
           -128

        真实 ClickHouse 不报错也不 saturate,就是回绕。
        这条语义在 ``sum()`` 上尤其危险:``sum(UInt8)`` 的结果类型是
        ``UInt64``(因为 CH 会把小整数求和**自动提升**),所以聚合不会回绕,
        但 ``a + b`` 会 —— 这个不对称是第 03 章的经典考点。
        """
        if self.float:
            return value
        mask = (1 << self.bits) - 1
        v = int(value) & mask
        if self.signed and v >= (1 << (self.bits - 1)):
            v -= 1 << self.bits
        return v

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if self.float:
            if isinstance(value, bool):
                return 1.0 if value else 0.0
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                return self.deserialize_text(value)
            raise_error(
                ErrorCode.CANNOT_CONVERT_TYPE,
                f"Cannot convert {type(value).__name__} to {self._name}",
            )
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, int):
            return self.wrap(value)
        if isinstance(value, float):
            # 真实 CH 把 Float → Int 定义为**截断**(向零),不是四舍五入。
            # 这一条与 MySQL 的某些模式不同,容易被忽略。
            return self.wrap(int(value))
        if isinstance(value, str):
            return self.deserialize_text(value)
        if isinstance(value, (bytes, bytearray)):
            return self.deserialize_text(bytes(value).decode("utf-8", "surrogateescape"))
        raise_error(
            ErrorCode.CANNOT_CONVERT_TYPE,
            f"Cannot convert {type(value).__name__} to {self._name}",
        )

    # ── 文本 ────────────────────────────────────────────────────────────

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        if self.float:
            v = float(value)
            if math.isnan(v):
                return "nan"
            if math.isinf(v):
                return "inf" if v > 0 else "-inf"
            # 最短往返表示 —— 与真实 CH 的 DoubleConverter 目标一致:
            # 保证 parse(print(x)) == x,且尽可能短。
            return repr(v)
        return str(int(value))

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return self.serialize_text(value, settings)

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        s = text.strip()
        if s == "" or s.lower() in ("null", "\\n"):
            return self.default_value()
        if self.float:
            low = s.lower()
            if low in ("nan", "+nan", "-nan"):
                return float("nan")
            if low in ("inf", "+inf", "infinity", "+infinity"):
                return float("inf")
            if low in ("-inf", "-infinity"):
                return float("-inf")
            try:
                return float(s)
            except ValueError:
                raise_error(
                    ErrorCode.CANNOT_PARSE_FLOAT,
                    f"Cannot parse {s!r} as {self._name}",
                )
        try:
            # 真实 CH 接受 "0x1f" 十六进制、"1e3" 科学计数法
            if s.lower().startswith(("0x", "-0x", "+0x")):
                return self.wrap(int(s, 16))
            if "e" in s.lower() or "." in s:
                return self.wrap(int(float(s)))
            return self.wrap(int(s, 10))
        except ValueError:
            raise_error(
                ErrorCode.CANNOT_PARSE_NUMBER,
                f"Cannot parse {s!r} as {self._name}",
            )

    # ── 布尔语义 ────────────────────────────────────────────────────────

    def is_value_representable_as_true(self, value: Any) -> bool:
        return value not in (0, 0.0, None)


class DataTypeBool(DataType):
    """``Bool`` —— 独立类型,底层是 ``UInt8``。

    .. note::

       ``Bool`` 比较新(真实 ClickHouse 从 22.x 起才把它作为**独立类型**
       暴露出来,之前只有 ``UInt8``)。它的文本表示是 ``true``/``false``,
       但**数值比较时会先转成 0/1**。三值逻辑(``AND``/``OR`` 遇 NULL)
       见第 15 章的 ``and()``/``or()`` 函数。
    """

    family_name = "Bool"

    def get_name(self) -> str:
        return "Bool"

    def create_column(self) -> IColumn:
        return ColumnVector(np.uint8)

    def is_numeric(self) -> bool:
        return True

    def is_integer(self) -> bool:
        return True

    def default_value(self) -> Any:
        return False

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return self.deserialize_text(value)
        raise_error(
            ErrorCode.CANNOT_CONVERT_TYPE,
            f"Cannot convert {type(value).__name__} to Bool",
        )

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return "true" if value else "false"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        s = text.strip().lower()
        if s in ("true", "1", "t", "yes", "y"):
            return True
        if s in ("false", "0", "f", "no", "n", ""):
            return False
        raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"Cannot parse {text!r} as Bool")

    def is_value_representable_as_true(self, value: Any) -> bool:
        return bool(value)


# ══════════════════════════════════════════════════════════════════════════
# 字符串
# ══════════════════════════════════════════════════════════════════════════


class DataTypeString(DataType):
    """``String`` —— 变长、二进制安全、无编码校验。

    .. warning::

       与 PostgreSQL 的 ``text``、MySQL 的 ``varchar`` 最大的不同:
       **ClickHouse 的 String 不校验编码**。所以 ``length(s)`` 返回的
       是**字节数**,不是字符数;``upper('ß')`` 可能不会变成 ``'SS'``。
       要按字符处理必须显式用 ``lengthUTF8`` / ``upperUTF8``。
       这是中文用户最常见的踩坑点之一。
    """

    family_name = "String"

    def get_name(self) -> str:
        return "String"

    def create_column(self) -> IColumn:
        return ColumnString()

    def is_string(self) -> bool:
        return True

    def default_value(self) -> Any:
        return ""

    def get_size_of_value_in_memory(self) -> int:
        return 16  # 一个 offsets 项 + 平均一点字节

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", "surrogateescape")
        if isinstance(value, (int, float)):
            # 真实 CH 会拒绝把数字隐式插进 String 列(要求显式 toString)。
            # 我们允许,因为 Python 的隐式字符串化太常见了 —— 但要把
            # 这个偏离写在 :: 里,读者才不会以为真实 CH 也这样。
            return str(value)
        raise_error(
            ErrorCode.CANNOT_CONVERT_TYPE,
            f"Cannot convert {type(value).__name__} to String",
        )

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bytes):
            return value.decode("utf-8", "surrogateescape")
        return value

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        """``String`` 在 ``Values`` 格式里要加单引号并转义。"""
        s = self.serialize_text(value, settings)
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return _unescape_string(text)

    def is_value_representable_as_true(self, value: Any) -> bool:
        return bool(value)


class DataTypeFixedString(DataType):
    """``FixedString(N)`` —— 定长 N 字节。"""

    parametric = True

    def __init__(self, n: int) -> None:
        if n <= 0:
            raise_error(
                ErrorCode.SIZE_OF_FIXED_STRING_DOESNT_MATCH,
                f"FixedString size must be positive, got {n}",
            )
        self.n = n
        self.family_name = "FixedString"

    def get_name(self) -> str:
        return f"FixedString({self.n})"

    def create_column(self) -> IColumn:
        return ColumnFixedString(self.n)

    def is_string(self) -> bool:
        return True

    def default_value(self) -> Any:
        return ""

    def have_maximum_size_of_value(self) -> bool:
        return True

    def get_maximum_size_of_value(self) -> int:
        return self.n

    def get_size_of_value_in_memory(self) -> int:
        return self.n

    def normalize(self, value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "surrogateescape")
        if isinstance(value, str) and len(value.encode("utf-8", "surrogateescape")) > self.n:
            raise_error(
                ErrorCode.SIZE_OF_FIXED_STRING_DOESNT_MATCH,
                f"Too large value for FixedString({self.n}): {value!r}",
            )
        return value

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        # 真实 CH 的 FixedString 在 TSV 里输出**剥掉尾部零字节**的形式
        return value.decode("utf-8", "surrogateescape") if isinstance(value, bytes) else value

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return text


# ══════════════════════════════════════════════════════════════════════════
# UUID 与 IP
# ══════════════════════════════════════════════════════════════════════════

_UUID_RE = re.compile(
    r"^\{?([0-9a-fA-F]{8})-([0-9a-fA-F]{4})-([0-9a-fA-F]{4})-"
    r"([0-9a-fA-F]{4})-([0-9a-fA-F]{12})\}?$"
)


class DataTypeUUID(DataType):
    """``UUID`` —— 128 位无符号整数,文本形如 ``550e8400-e29b-41d4-a716-446655440000``。"""

    family_name = "UUID"

    def get_name(self) -> str:
        return "UUID"

    def create_column(self) -> IColumn:
        return ColumnUUID()

    def is_uuid(self) -> bool:
        return True

    def default_value(self) -> Any:
        return "00000000-0000-0000-0000-000000000000"

    def have_maximum_size_of_value(self) -> bool:
        return True

    def get_maximum_size_of_value(self) -> int:
        return 16

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, int):
            return value & ((1 << 128) - 1)
        return parse_uuid(value)

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return value
        return format_uuid(int(value))

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return f"'{self.serialize_text(value, settings)}'"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return parse_uuid(text)


def parse_uuid(value: Any) -> int:
    """把 UUID 的文本形式解析成 128 位整数。"""
    if isinstance(value, int):
        return value & ((1 << 128) - 1)
    s = str(value).strip()
    m = _UUID_RE.match(s)
    if m:
        return int("".join(m.groups()), 16)
    # 也接受纯 32 位十六进制(不带连字符)
    if len(s) == 32 and all(c in "0123456789abcdefABCDEF" for c in s):
        return int(s, 16)
    raise_error(ErrorCode.CANNOT_PARSE_UUID, f"Cannot parse UUID from {value!r}")


def format_uuid(v: int) -> str:
    """把 128 位整数格式化成标准的 8-4-4-4-12 形式。"""
    h = f"{v & ((1 << 128) - 1):032x}"
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class DataTypeIPv4(DataType):
    """``IPv4`` —— 存成 ``UInt32``(大端),文本形如 ``192.168.0.1``。

    ClickHouse 把 IP 存成整数,而不是字符串,带来两个好处:
    一是比较/排序是数值比较(比字符串快),二是可以直接做**网段运算**
    (``bitAnd(ip, mask)``),这也是 ``isIPAddressInRange`` 的实现方式。
    """

    family_name = "IPv4"

    def get_name(self) -> str:
        return "IPv4"

    def create_column(self) -> IColumn:
        return ColumnVector(np.uint32)

    def default_value(self) -> Any:
        return "0.0.0.0"

    def have_maximum_size_of_value(self) -> bool:
        return True

    def get_maximum_size_of_value(self) -> int:
        return 4

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, int):
            return value & 0xFFFFFFFF
        return ipv4_to_int(str(value))

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return value
        return int_to_ipv4(int(value))

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return f"'{self.serialize_text(value, settings)}'"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return ipv4_to_int(text)


def ipv4_to_int(text: str) -> int:
    parts = text.strip().split(".")
    if len(parts) != 4:
        raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"Cannot parse IPv4 from {text!r}")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"Cannot parse IPv4 from {text!r}")
    if any(n < 0 or n > 255 for n in nums):
        raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"IPv4 octet out of range in {text!r}")
    return (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]


def int_to_ipv4(v: int) -> str:
    v &= 0xFFFFFFFF
    return f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"


class DataTypeIPv6(DataType):
    """``IPv6`` —— 存成 16 字节定点,文本用标准压缩写法(``::``)。"""

    family_name = "IPv6"

    def get_name(self) -> str:
        return "IPv6"

    def create_column(self) -> IColumn:
        return ColumnFixedString(16)

    def default_value(self) -> Any:
        return "::"

    def have_maximum_size_of_value(self) -> bool:
        return True

    def get_maximum_size_of_value(self) -> int:
        return 16

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        return str(value)

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return str(value) if isinstance(value, str) else value.decode("utf-8", "replace")

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return f"'{self.serialize_text(value, settings)}'"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return text


# ══════════════════════════════════════════════════════════════════════════
# 日期与时间
# ══════════════════════════════════════════════════════════════════════════

#: ``Date`` 的纪元 —— ClickHouse 用的是 Unix 纪元(不像 Oracle 用 1970 之外的值)
EPOCH_DATE = _dt.date(1970, 1, 1)

#: ``Date`` 是 UInt16 天数,所以最大值是 65535 天 ≈ 2149-06-06
DATE_MAX_DAYS = 65535

#: ``Date32`` 是 Int32 天数,范围更宽
DATE32_MIN_DAYS = -25567   # 1900-01-01
DATE32_MAX_DAYS = 120529   # 2299-12-31


class DataTypeDate(DataType):
    """``Date`` —— ``UInt16``,自 1970-01-01 起的**天数**。

    .. warning::

       ``Date`` 只能表示 ``1970-01-01`` 到 ``2149-06-06``。
       超出范围**不会报错**,会被 wrap 成一个荒唐的日期(真实 CH 会
       在 ``date_time_overflow_behavior`` 下**饱和**到边界值)。
       需要更宽范围就用 ``Date32``(1900–2299)。
    """

    family_name = "Date"

    def get_name(self) -> str:
        return "Date"

    def create_column(self) -> IColumn:
        return ColumnVector(np.uint16)

    def is_date(self) -> bool:
        return True

    def is_integer(self) -> bool:
        return True

    def default_value(self) -> Any:
        return "1970-01-01"

    def have_maximum_size_of_value(self) -> bool:
        return True

    def get_maximum_size_of_value(self) -> int:
        return 2

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        return date_to_days(value)

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return value
        return days_to_date(value).isoformat()

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return f"'{self.serialize_text(value, settings)}'"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return date_to_days(text)

    def is_value_representable_as_true(self, value: Any) -> bool:
        return value != 0


class DataTypeDate32(DataType):
    """``Date32`` —— ``Int32`` 天数,1900-01-01 ~ 2299-12-31。"""

    family_name = "Date32"

    def get_name(self) -> str:
        return "Date32"

    def create_column(self) -> IColumn:
        return ColumnVector(np.int32)

    def is_date(self) -> bool:
        return True

    def is_integer(self) -> bool:
        return True

    def default_value(self) -> Any:
        return "1900-01-01"

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        return date_to_days(value) if not isinstance(value, int) else value

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return value
        return days_to_date(value).isoformat()

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return f"'{self.serialize_text(value, settings)}'"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return date_to_days(text)


_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def date_to_days(value: Any) -> int:
    """把日期解析成"自 1970-01-01 起的天数"。"""
    if isinstance(value, int):
        return value
    if isinstance(value, _dt.datetime):
        value = value.date()
    if isinstance(value, _dt.date):
        return (value - EPOCH_DATE).days
    s = str(value).strip()
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1]
    m = _DATE_RE.match(s)
    if not m:
        raise_error(ErrorCode.CANNOT_PARSE_DATE, f"Cannot parse Date from {value!r}")
    try:
        d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        raise_error(ErrorCode.CANNOT_PARSE_DATE, f"Cannot parse Date from {value!r}: {exc}")
    return (d - EPOCH_DATE).days


def days_to_date(days: int) -> _dt.date:
    """天数 → ``datetime.date``。超出 ``datetime`` 范围时钳到边界。"""
    try:
        return EPOCH_DATE + _dt.timedelta(days=int(days))
    except (OverflowError, ValueError):
        # Date 是 UInt16,所以 days 最大 65535 —— datetime 装得下。
        # 但 Date32 的边界(2299 年)也装得下,所以这里只是保险。
        return EPOCH_DATE if days < 0 else _dt.date(2149, 6, 6)


_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2}):(\d{1,2})(\.\d+)?$"
)


class DataTypeDateTime(DataType):
    """``DateTime`` —— ``UInt32`` 秒,可选时区。

    时区是**列级别的属性**,不是会话级别的:``DateTime`` 与
    ``DateTime('Asia/Shanghai')`` 是两个不同的类型。这一点和
    MySQL 的 ``timestamp``(会话时区)完全不同 —— 也是迁移时最容易
    出错的点。第 03 章有一节专讲。

    mini-clickhouse 支持 ``UTC`` 与固定偏移两种时区
    (``UTC+8`` 这类),**不**实现 IANA 时区数据库(那需要一份
    历史变迁表,和"教学实现"的目标不成比例)。
    """

    parametric = True

    def __init__(self, timezone: Optional[str] = None) -> None:
        self.timezone = timezone
        self.family_name = "DateTime"

    def get_name(self) -> str:
        return "DateTime" if not self.timezone else f"DateTime('{self.timezone}')"

    def create_column(self) -> IColumn:
        return ColumnVector(np.uint32)

    def is_date_time(self) -> bool:
        return True

    def is_integer(self) -> bool:
        return True

    def default_value(self) -> Any:
        return "1970-01-01 00:00:00"

    def have_maximum_size_of_value(self) -> bool:
        return True

    def get_maximum_size_of_value(self) -> int:
        return 4

    def tz_offset_seconds(self) -> int:
        return parse_timezone_offset(self.timezone)

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        return datetime_to_epoch(value, self.tz_offset_seconds())

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return value
        return format_datetime(int(value), self.tz_offset_seconds())

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return f"'{self.serialize_text(value, settings)}'"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return datetime_to_epoch(text, self.tz_offset_seconds())


class DataTypeDateTime64(DataType):
    """``DateTime64(precision[, tz])`` —— ``Int64`` 刻度,精度 0–9 位小数。"""

    parametric = True

    def __init__(self, precision: int = 3, timezone: Optional[str] = None) -> None:
        if precision < 0 or precision > 9:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"DateTime64 precision must be in [0, 9], got {precision}",
            )
        self.precision = precision
        self.timezone = timezone
        self.family_name = "DateTime64"

    def get_name(self) -> str:
        if self.timezone:
            return f"DateTime64({self.precision}, '{self.timezone}')"
        return f"DateTime64({self.precision})"

    def create_column(self) -> IColumn:
        return ColumnVector(np.int64)

    def is_date_time(self) -> bool:
        return True

    def is_integer(self) -> bool:
        return True

    def default_value(self) -> Any:
        return "1970-01-01 00:00:00"

    def have_maximum_size_of_value(self) -> bool:
        return True

    def get_maximum_size_of_value(self) -> int:
        return 8

    def scale(self) -> int:
        return 10 ** self.precision

    def tz_offset_seconds(self) -> int:
        return parse_timezone_offset(self.timezone)

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, _dt.datetime):
            secs = datetime_to_epoch(value, self.tz_offset_seconds())
            frac = value.microsecond / 1_000_000.0
            return secs * self.scale() + int(round(frac * self.scale()))
        s = str(value).strip()
        if s.startswith("'") and s.endswith("'"):
            s = s[1:-1]
        m = _DATETIME_RE.match(s)
        if not m:
            # 也接受纯日期
            if _DATE_RE.match(s):
                return date_to_days(s) * 86400 * self.scale() - \
                    self.tz_offset_seconds() * self.scale()
            raise_error(
                ErrorCode.CANNOT_PARSE_DATETIME,
                f"Cannot parse {self.get_name()} from {value!r}",
            )
        secs = datetime_to_epoch(s, self.tz_offset_seconds())
        frac = float(m.group(7)) if m.group(7) else 0.0
        return secs * self.scale() + int(round(frac * self.scale()))

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return value
        v = int(value)
        scale = self.scale()
        secs, frac = divmod(v, scale)
        base = format_datetime(secs, self.tz_offset_seconds())
        if self.precision == 0:
            return base
        frac_str = f"{frac:0{self.precision}d}" if self.precision <= 9 else str(frac)
        return f"{base}.{frac_str}"

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return f"'{self.serialize_text(value, settings)}'"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return self.normalize(text)


def parse_timezone_offset(tz: Optional[str]) -> int:
    """把时区名解析成"相对 UTC 的秒偏移"。

    支持 ``UTC``/``UTC+8``/``+08:00``/``Asia/Shanghai``(映射到 +8)等常见写法。
    不支持 IANA 夏令时规则 —— 教程会把这条限制明确标出来。
    """
    if not tz:
        return 0
    name = str(tz).strip()
    upper = name.upper()
    if upper in ("UTC", "GMT", "Z"):
        return 0
    # Asia/Shanghai 这类常见城市名
    if upper in ("ASIA/SHANGHAI", "ASIA/CHONGQING", "ASIA/HONG_KONG", "PRC"):
        return 8 * 3600
    if upper in ("AMERICA/NEW_YORK",):
        return -5 * 3600
    if upper in ("EUROPE/LONDON",):
        return 0
    m = re.match(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", upper)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        return sign * (hours * 3600 + minutes * 60)
    raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"Unknown timezone: {tz!r}")


def format_datetime(epoch_seconds: int, tz_offset: int = 0) -> str:
    """**秒(UTC) + 偏移 → 本地时间字符串。**

    这是时区语义的核心:列里存的永远是 UTC 秒,展示时才加偏移。
    """
    try:
        dt = _dt.datetime(1970, 1, 1) + _dt.timedelta(
            seconds=int(epoch_seconds) + tz_offset
        )
    except (OverflowError, OSError):
        return "2106-02-07 06:28:15"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def datetime_to_epoch(value: Any, tz_offset: int = 0) -> int:
    """把各种形式的时间解析成 UTC 秒。"""
    if isinstance(value, int):
        return value
    if isinstance(value, _dt.datetime):
        base = _dt.datetime(1970, 1, 1)
        return int((value.replace(tzinfo=None) - base).total_seconds()) - tz_offset
    if isinstance(value, _dt.date):
        return (value - EPOCH_DATE).days * 86400 - tz_offset
    s = str(value).strip()
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1]
    m = _DATETIME_RE.match(s)
    if m:
        dt = _dt.datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)),
        )
        return int((dt - _dt.datetime(1970, 1, 1)).total_seconds()) - tz_offset
    if _DATE_RE.match(s):
        return date_to_days(s) * 86400 - tz_offset
    if re.match(r"^-?\d+$", s):
        return int(s)
    raise_error(ErrorCode.CANNOT_PARSE_DATETIME, f"Cannot parse DateTime from {value!r}")


# ══════════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════════


class DataTypeEnum(DataType):
    """``Enum8`` / ``Enum16`` —— **名字 ↔ 整数**的双向映射。

    .. code-block:: sql

       CREATE TABLE t (level Enum8('debug' = 1, 'info' = 2, 'error' = 3)) ENGINE = Memory;
       INSERT INTO t VALUES ('info'), (2), ('error');
       SELECT * FROM t;   -- info / info / error

    三个容易踩的点:

    1. **存储的是整数**,但**文本表示是名字** —— 所以 ``SELECT`` 出来
       是 ``'info'``,而 ``ORDER BY`` 是按整数 2 排序;
    2. 写入时既可以给名字也可以给整数,给未定义的名字/整数会**报错**
       (不是静默插入);
    3. 枚举值**可以显式指定**,不指定就自增。所以 ``Enum8('a', 'b')``
       等价于 ``Enum8('a' = 1, 'b' = 2)``。
    """

    parametric = True

    def __init__(self, names: Sequence[str], values: Sequence[int], bits: int = 8) -> None:
        self.bits = bits
        self.family_name = f"Enum{bits}"
        if len(names) != len(values):
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                "Enum name/value counts differ",
            )
        if not names:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"Enum{bits} requires at least one element",
            )
        self.name_to_value: Dict[str, int] = {}
        self.value_to_name: Dict[int, str] = {}
        lo, hi = (-128, 127) if bits == 8 else (-32768, 32767)
        for n, v in zip(names, values):
            v = int(v)
            if v < lo or v > hi:
                raise_error(
                    ErrorCode.ARGUMENT_OUT_OF_BOUND,
                    f"Enum{bits} value {v} out of range [{lo}, {hi}]",
                )
            if n in self.name_to_value:
                raise_error(ErrorCode.DUPLICATE_COLUMN, f"Duplicate enum name {n!r}")
            if v in self.value_to_name:
                raise_error(ErrorCode.DUPLICATE_COLUMN, f"Duplicate enum value {v}")
            self.name_to_value[n] = v
            self.value_to_name[v] = n

    def get_name(self) -> str:
        parts = ", ".join(
            f"'{n}' = {v}"
            for n, v in sorted(self.name_to_value.items(), key=lambda kv: kv[1])
        )
        return f"Enum{self.bits}({parts})"

    def create_column(self) -> IColumn:
        return ColumnVector(np.int8 if self.bits == 8 else np.int16)

    def is_enum(self) -> bool:
        return True

    def is_integer(self) -> bool:
        return True

    def default_value(self) -> Any:
        return next(iter(self.name_to_value)) if self.name_to_value else ""

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("'") and s.endswith("'"):
                s = s[1:-1]
            if s in self.name_to_value:
                return self.name_to_value[s]
            # 名字也可能写成数字的字符串
            try:
                return self.normalize(int(s))
            except ValueError:
                raise_error(
                    ErrorCode.UNKNOWN_ELEMENT_OF_ENUM,
                    f"Unknown element '{value}' for {self.family_name} with values "
                    f"{sorted(self.name_to_value)}",
                )
        v = int(value)
        if v not in self.value_to_name:
            raise_error(
                ErrorCode.UNKNOWN_ELEMENT_OF_ENUM,
                f"Unknown element {v} for {self.family_name} with values "
                f"{sorted(self.value_to_name)}",
            )
        return v

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, str) and value in self.name_to_value:
            return value
        return self.value_to_name.get(int(value), str(value))

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return f"'{self.serialize_text(value, settings)}'"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return self.normalize(text)


# ══════════════════════════════════════════════════════════════════════════
# Nothing 与 Interval
# ══════════════════════════════════════════════════════════════════════════


class DataTypeNothing(DataType):
    """``Nothing`` —— 只有一个值 ``NULL``,而且**永远是空的**。

    真实 ClickHouse 用它表示"这个表达式的类型还推不出来":

    * ``SELECT NULL`` → ``Nullable(Nothing)``;
    * ``SELECT []`` → ``Array(Nothing)``;
    * ``['a'] = []`` 这种比较会走 ``Nothing`` 的特化路径。

    它不能建表列 —— ``CREATE TABLE t (x Nothing)`` 会被拒绝,
    因为一列什么都不能存。
    """

    family_name = "Nothing"

    #: ``Nullable(Nothing)`` **是合法的** —— ``SELECT NULL`` 的类型就是它。
    #: (实现初期把它标成"不可空"会导致 ``SELECT NULL`` 直接报错,
    #: 连带 ``if(x, NULL, y)``、``coalesce`` 这些常见写法一起挂掉。)
    nullable_allowed = True

    def get_name(self) -> str:
        return "Nothing"

    def create_column(self) -> IColumn:
        from ..columns import ColumnNothing

        return ColumnNothing()

    def is_nothing(self) -> bool:
        return True

    def default_value(self) -> Any:
        return None

    def is_null(self, value: Any) -> bool:
        return True

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        return "NULL"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return None


#: Interval 的种类 → (名字后缀, 一秒有多少个该单位)。``Interval`` 类型
#: 在真实 ClickHouse 里用来表示 ``INTERVAL 1 DAY`` 这样的字面量,
#: 参与日期运算(``date + INTERVAL 1 DAY``)。
INTERVAL_KINDS: Dict[str, Tuple[str, Optional[float]]] = {
    "NANOSECOND": ("Nanosecond", 1e-9),
    "MICROSECOND": ("Microsecond", 1e-6),
    "MILLISECOND": ("Millisecond", 1e-3),
    "SECOND": ("Second", 1.0),
    "MINUTE": ("Minute", 60.0),
    "HOUR": ("Hour", 3600.0),
    "DAY": ("Day", 86400.0),
    "WEEK": ("Week", 604800.0),
    "MONTH": ("Month", None),
    "QUARTER": ("Quarter", None),
    "YEAR": ("Year", None),
}


class DataTypeInterval(DataType):
    """``IntervalDay`` 这一族 —— 存一个 ``Int64`` 计数。"""

    def __init__(self, kind: str) -> None:
        kind = kind.upper()
        if kind not in INTERVAL_KINDS:
            raise_error(ErrorCode.UNKNOWN_TYPE, f"Unknown interval kind: {kind}")
        self.kind = kind
        self.suffix, self.seconds = INTERVAL_KINDS[kind]
        self.family_name = f"Interval{self.suffix}"

    def get_name(self) -> str:
        return f"Interval{self.suffix}"

    def create_column(self) -> IColumn:
        return ColumnVector(np.int64)

    def is_integer(self) -> bool:
        return True

    def default_value(self) -> Any:
        return 0

    def normalize(self, value: Any) -> Any:
        return 0 if value is None else int(value)

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        return str(int(value or 0))

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return int(text)


# ══════════════════════════════════════════════════════════════════════════
# 字符串转义
# ══════════════════════════════════════════════════════════════════════════

_UNESCAPE_MAP = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f",
    "a": "\a", "v": "\v", "\\": "\\", "'": "'", '"': '"', "/": "/", "`": "`",
}


def _unescape_string(text: str) -> str:
    """处理 ClickHouse 字符串字面量里的转义序列。

    与真实 ClickHouse 一致地支持 ``\\n \\t \\\\ \\' \\xNN`` 这些写法。
    未识别的转义**原样保留反斜杠**(而不是报错)—— 这和真实 CH 的行为一致,
    因为 String 是二进制安全的(反斜杠本身是合法内容)。
    """
    if "\\" not in text:
        return text
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in _UNESCAPE_MAP:
            out.append(_UNESCAPE_MAP[nxt])
            i += 2
        elif nxt == "x" and i + 3 < n and _is_hex(text[i + 2 : i + 4]):
            out.append(chr(int(text[i + 2 : i + 4], 16)))
            i += 4
        else:
            out.append("\\")
            i += 1
    return "".join(out)


def _is_hex(s: str) -> bool:
    return len(s) == 2 and all(c in "0123456789abcdefABCDEF" for c in s)
