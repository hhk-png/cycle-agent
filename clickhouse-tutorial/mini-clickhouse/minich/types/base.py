"""数据类型系统的基类与注册表。

ClickHouse 的类型系统是**两段式**的,理解这一点是读懂整个代码库的钥匙:

.. code-block:: text

     DataType(类型的"元数据")                IColumn(类型的"数据")
     ┌────────────────────────┐             ┌─────────────────────────┐
     │ "UInt64"               │  create_    │ np.ndarray(dtype=uint64)│
     │ "Nullable(String)"     │ ─────────►  │ null_map + offsets+chars│
     │ "Array(DateTime64(3))" │  column()   │ offsets + DateTime64 列 │
     └────────────────────────┘             └─────────────────────────┘
        回答"这是什么类型"                      回答"值存在哪里"

为什么要分成两个类? 因为**同一个类型可以有多种物化方式**:

* ``UInt64`` 可以是稠密列(:class:`ColumnVector`)也可以是
  **常量列**(``ColumnConst``,存一个值 + 长度);
* ``String`` 可以是普通列、低基数列(``ColumnLowCardinality``)、
  或者是**稀疏列**;
* 聚合中间结果用 ``AggregateFunction(sum, UInt64)`` 类型,但它的列里
  存的根本不是 UInt64,而是一个**求和状态**(一串字节)。

所以真实 ClickHouse 里 ``DataType::createColumn()`` 只是"默认物化方式",
执行引擎可以在任意时刻换成别的列类型 —— 只要语义等价。
mini-clickhouse 完整继承这个设计,教程第 10 章会展示一个"同一块数据
在 pipeline 上以三种列类型流动"的例子。

.. note::

   真实 ClickHouse 的 ``DataType`` 还有一层 ``IDataType`` 接口 + ``DataTypeWithSimpleSerialization``
   之类的中间基类,以及 ``WhichDataType`` 这种"零开销类型判定"工具。
   mini-clickhouse 不做这层抽象,只保留 ``DataType`` 一个基类 ——
   因为我们的类型种类少到不需要分派优化。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..errors import ErrorCode, MiniChError, raise_error

__all__ = [
    "DataType",
    "DataTypePtr",
    "register_data_type",
    "get_data_type",
    "try_get_data_type",
    "parse_type_name",
    "DataTypeFactory",
]


class DataType(ABC):
    """所有数据类型的基类。

    子类**必须**实现 :meth:`get_name` 与 :meth:`create_column`;
    其余方法都有可用的默认实现(适合标量类型)。
    """

    #: 类型族名,例如 ``Nullable(UInt64)`` 的族名是 ``"Nullable"``。
    #: 标量类型族名与名字相同。
    family_name: str = "Unknown"

    #: 该类型是否允许出现在 ``Nullable(...)`` 里面。
    #: ``Nullable`` 自己不可以,``Array`` 可以,``Nothing`` 不可以。
    nullable_allowed: bool = True

    #: 该类型是否是"参数化"的(名字里要带括号),``Array``/``FixedString`` 是,
    #: ``UInt64`` 不是。用于 ``SHOW CREATE TABLE`` 时的格式化。
    parametric: bool = False

    # ── 名字 ────────────────────────────────────────────────────────────

    @abstractmethod
    def get_name(self) -> str:
        """返回完整类型名,例如 ``"Array(Nullable(UInt8))"``。

        这个名字是**往返可解析**的:``get_data_type(t.get_name())``
        必须得到等价类型。真实 ClickHouse 保证同样的性质(建表语句可以
        原样 dump 出来再 load 回去)。
        """

    def get_family_name(self) -> str:
        """返回族名,例如 ``Nullable(String)`` → ``"Nullable"``。"""
        return self.family_name

    def __str__(self) -> str:
        return self.get_name()

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}({self.get_name()!r})"

    # ── 列 ──────────────────────────────────────────────────────────────

    @abstractmethod
    def create_column(self) -> Any:
        """创建该类型的**默认物化列**(空的)。"""

    # ── 值 ──────────────────────────────────────────────────────────────

    def default_value(self) -> Any:
        """默认值 —— 真实 ClickHouse 里就是"零值":

        * 数值 ``0``;字符串 ``""``;``Array`` 是 ``[]``;
        * ``Nullable(T)`` 是 ``None``(NULL);
        * ``Date`` 是 ``1970-01-01``。

        这个"零值"不是随便定的,它同时是:

        1. ``DEFAULT`` 子句的默认表达式;
        2. 缺列补全(``ALTER TABLE ADD COLUMN``)时老 part 里那些行的值;
        3. ``Nullable`` 列的非空侧在 NULL 位置上的占位值。
        """
        return 0

    def is_null(self, value: Any) -> bool:
        """判断一个 Python 值在该类型下是否为 NULL。"""
        return value is None

    def normalize(self, value: Any) -> Any:
        """把一个"用户给的"值规范成该类型的内部表示。

        例如 ``Date`` 收 ``"2024-01-01"`` 或 ``datetime.date`` 都转成
        "自 1970-01-01 起的天数";``UInt8`` 收 ``True`` 转成 ``1``;
        ``String`` 收 ``bytes`` 转成 ``str``。

        越界/类型不符时抛 :class:`MiniChError`(``CANNOT_CONVERT_TYPE``),
        而不是静默截断 —— 真实 ClickHouse 只在 ``input_format_*``
        宽松模式下才截断。
        """
        return value

    # ── 文本序列化 ──────────────────────────────────────────────────────

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        """把值转成文本格式(**不加引号**)。

        ``INSERT ... VALUES`` / TSV / CSV 输出都走这里。
        """
        if value is None:
            return "NULL"
        return str(value)

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        """转成文本并**按需加引号**(``Values`` 格式、``SHOW CREATE`` 用)。"""
        return self.serialize_text(value, settings)

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        """把文本解析成值。``text`` 已经去掉了外层引号。"""
        return text

    def serialize_text_for_error(self, value: Any) -> str:
        """进错误消息的表示 —— 会加引号,便于定位是哪个值错了。"""
        return repr(value)

    # ── 属性判定 ────────────────────────────────────────────────────────

    def is_numeric(self) -> bool:
        return False

    def is_integer(self) -> bool:
        """整数类(含 Bool/Enum/Date/DateTime/Decimal 的整型存储)。"""
        return False

    def is_float(self) -> bool:
        return False

    def is_decimal(self) -> bool:
        return False

    def is_string(self) -> bool:
        return False

    def is_date(self) -> bool:
        return False

    def is_date_time(self) -> bool:
        return False

    def is_enum(self) -> bool:
        return False

    def is_uuid(self) -> bool:
        return False

    def is_array(self) -> bool:
        return False

    def is_tuple(self) -> bool:
        return False

    def is_map(self) -> bool:
        return False

    def is_nullable(self) -> bool:
        return False

    def is_low_cardinality(self) -> bool:
        return False

    def is_aggregate_function(self) -> bool:
        return False

    def is_nothing(self) -> bool:
        return False

    def is_const(self) -> bool:
        return False

    def is_variant(self) -> bool:
        return False

    def is_dynamic(self) -> bool:
        return False

    def is_json(self) -> bool:
        return False

    def is_value_representable_as_true(self, value: Any) -> bool:
        """转成 WHERE 里的布尔语义 —— ``0``/``''``/``[]`` 为假,其余为真。"""
        return bool(value)

    # ── 子列 ────────────────────────────────────────────────────────────

    def get_subcolumn_names(self) -> List[str]:
        """子列名。``Tuple(a UInt8, b String)`` → ``["a", "b"]``。

        只有具名 Tuple 与 ``Nested`` 会返回非空。
        这是 ``SELECT t.a`` 与 ``t.*`` 展开的基础。
        """
        return []

    def get_subcolumn_type(self, name: str) -> Optional["DataType"]:
        return None

    def get_subcolumn_index(self, name: str) -> int:
        return -1

    def get_elements_type(self) -> Optional["DataType"]:
        """``Array(T)``/``Map(K,V)`` 的元素类型,用于 ``arrayJoin`` 等。"""
        return None

    # ── 内存与比较 ──────────────────────────────────────────────────────

    def get_size_of_value_in_memory(self) -> int:
        """单个值在 mini-clickhouse 里占的近似字节数(用于 ``system.columns``
        与内存估算)。**不是**磁盘上的字节数 —— 磁盘大小由压缩编码决定。"""
        return 16

    def equals(self, other: "DataType") -> bool:
        """类型是否完全相同 —— 按**名字**比较。

        真实 ClickHouse 用递归的 ``equals()``,因为 ``Enum8('a'=1)`` 和
        ``Enum8('a'=1,'b'=2)`` 族名相同但类型不同。我们同样用**规范化名字**
        比较,这在我们的类型集合里是充分的。
        """
        return isinstance(other, DataType) and self.get_name() == other.get_name()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DataType) and self.equals(other)

    def __hash__(self) -> int:
        return hash(self.get_name())

    # ── 长度上限 ────────────────────────────────────────────────────────

    def have_maximum_size_of_value(self) -> bool:
        """值是否有固定/有界的长度(决定能不能做紧凑存储与 memcmp 比较)。"""
        return False

    def get_maximum_size_of_value(self) -> int:
        return 0

    # ── 与真实 ClickHouse 的差异 ────────────────────────────────────────
    #
    # * 没有 ``IDataType``/``WhichDataType`` 两层抽象;
    # * 没有 ``SerializationInfo``(用于稀疏/低基数编码的选择),
    #   因为我们只实现默认序列化 + 低基数特例;
    # * 没有 ``getCustomName()``(用户自定义的别名类型,如 ``CREATE TYPE``)。


DataTypePtr = DataType


# ── 注册表 ──────────────────────────────────────────────────────────────

#: 类型族名 → 工厂。工厂收一个 ``list[str]``(括号里的原始参数文本),
#: 返回 ``DataType``。用原始文本而不是已解析的类型,是因为
#: ``Enum8('a' = 1)`` 与 ``Decimal(10, 2)`` 的参数**不是类型**。
DataTypeFactory = Callable[[List[str]], DataType]

_REGISTRY: Dict[str, DataTypeFactory] = {}


def register_data_type(family_name: str, factory: DataTypeFactory) -> None:
    """注册一个类型族。重复注册会覆盖(便于测试里临时替换)。"""
    _REGISTRY[family_name] = factory


def registered_type_names() -> List[str]:
    """所有已注册的类型族名(``system.data_type_families`` 用)。"""
    return sorted(_REGISTRY)


# ── 类型名解析 ──────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*")


def _split_arguments(body: str) -> List[str]:
    """按**顶层**逗号切分参数,保留引号与嵌套括号里的逗号。

    ``Enum8('a, b' = 1, 'c' = 2)`` 必须切出 ``["'a, b' = 1", "'c' = 2"]``。
    """
    out: List[str] = []
    depth = 0
    quote: Optional[str] = None
    current: List[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            current.append(ch)
            if ch == "\\":
                # 转义:把下一个字符原样吞掉
                if i + 1 < len(body):
                    current.append(body[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            current.append(ch)
        elif ch == "(" or ch == "[":
            depth += 1
            current.append(ch)
        elif ch == ")" or ch == "]":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail or out:
        out.append(tail)
    return out


def parse_type_name(name: str) -> Tuple[str, List[str]]:
    """把 ``"Nullable(Array(UInt8))"`` 拆成 ``("Nullable", ["Array(UInt8)"])``。

    只剥**最外层**的括号 —— 嵌套交给递归。
    """
    text = name.strip()
    # 去掉可选的外层反引号/引号(从 system.columns 读回来的类型名可能带)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "`'\"":
        text = text[1:-1].strip()
    m = _IDENT_RE.match(text)
    if not m:
        raise_error(ErrorCode.UNKNOWN_TYPE, f"Cannot parse data type name: {name!r}")
    family = m.group(0)
    rest = text[m.end():].strip()
    if not rest:
        return family, []
    if not (rest.startswith("(") and rest.endswith(")")):
        raise_error(ErrorCode.UNKNOWN_TYPE, f"Malformed data type name: {name!r}")
    return family, _split_arguments(rest[1:-1])


def get_data_type(name: str) -> DataType:
    """按名字取类型(解析嵌套)。

    找不到时抛 ``UNKNOWN_TYPE`` —— 错误消息里会带上"最接近的候选",
    因为真实 ClickHouse 的类型名大小写很讲究(``UInt64`` 不是 ``uint64``,
    但 ``uint64`` 会被当成一个**别名**收下,见 ``types/__init__.py``)。
    """
    family, args = parse_type_name(name)
    factory = _REGISTRY.get(family)
    if factory is None:
        factory = _REGISTRY.get(_ALIASES.get(family.lower(), ""))
    if factory is None:
        raise_error(
            ErrorCode.UNKNOWN_TYPE,
            f"Unknown data type family: {family} (in type name {name!r})",
        )
    return factory(args)


def try_get_data_type(name: str) -> Optional[DataType]:
    """取不到返回 ``None`` 而不是抛异常(用于 `parse_type_name` 的探测)。"""
    try:
        return get_data_type(name)
    except MiniChError:
        return None


#: 小写别名 → 规范族名。真实 ClickHouse 的别名表见
#: ``src/DataTypes/DataTypeFactory.cpp`` 的 ``registerAlias`` 调用。
_ALIASES: Dict[str, str] = {
    "uint8": "UInt8",
    "uint16": "UInt16",
    "uint32": "UInt32",
    "uint64": "UInt64",
    "uint128": "UInt128",
    "uint256": "UInt256",
    "int8": "Int8",
    "int16": "Int16",
    "int32": "Int32",
    "int64": "Int64",
    "int128": "Int128",
    "int256": "Int256",
    "float32": "Float32",
    "float64": "Float64",
    "real": "Float32",
    "double": "Float64",
    "varchar": "String",
    "text": "String",
    "blob": "String",
    "tinyint": "Int8",
    "smallint": "Int16",
    "mediumint": "Int32",
    "int": "Int32",
    "integer": "Int32",
    "bigint": "Int64",
    "float": "Float32",
    "boolean": "Bool",
    "byte": "Int8",
    # 时间类型的 SQL 标准别名
    "timestamp": "DateTime",
    "datetime": "DateTime",
    "date": "Date",
    "char": "FixedString",
    # 复合类型的别名
    "array": "Array",
    "tuple": "Tuple",
    "map": "Map",
    "nullable": "Nullable",
    "lowcardinality": "LowCardinality",
    "decimal": "Decimal",
    "enum": "Enum8",
    "nest": "Nested",
}
