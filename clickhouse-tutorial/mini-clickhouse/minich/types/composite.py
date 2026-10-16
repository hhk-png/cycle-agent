"""复合类型:Decimal、Nullable、Array、Tuple、Map、LowCardinality、Nested、
AggregateFunction、Variant、Dynamic、JSON。

"复合"这个词在这里有两层意思,分开看会清楚很多:

.. code-block:: text

   第一层:嵌套别的类型(容器)
     Array(T)         一行的值是"一串 T"
     Tuple(A, B, …)   一行的值是"一组不同类型的值"
     Map(K, V)        一行的值是"若干 K→V 对"
     Nullable(T)      一行的值"可能是 NULL"
     LowCardinality(T) 一行的值"落在一个小字典里"

   第二层:承载执行引擎的语义
     AggregateFunction(f, T…)  一行的值是"一个聚合的中间状态"
     SimpleAggregateFunction(f, T)  同上,但状态就是普通值(如 sum/min/max)
     Variant(T1, T2…)          一行的值"是这几个类型之一"(动态类型)
     Dynamic                   连类型都可以逐行不同
     JSON                      "把 JSON 当列存起来"

第二层才是 ClickHouse 真正的特色 —— 别的数据库里"列的类型"是静态的,
而在 ClickHouse 里,类型可以**携带状态机的信息**(聚合状态)、
甚至可以是**逐行变化的**(Dynamic/Variant)。

.. note::

   ``Object('json')`` 是旧写法,新版本用 ``JSON`` 类型(24.x 起逐渐成熟)。
   两者的区别是:旧的 ``Object('json')`` 只是"把 JSON 字符串当一列 + 允许
   点号访问",新的 ``JSON`` 类型会把路径**物化成子列**并各自独立压缩。
   mini-clickhouse 实现 ``JSON`` 的"路径物化"这一面,但不做它的完整
   动态类型推导(那需要一套类型提升规则表)。第 03 章会讲清两个版本的差异。
"""

from __future__ import annotations

import decimal as _decimal
from decimal import Decimal as _PyDecimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..columns import (
    ColumnAggregateFunction,
    ColumnArray,
    ColumnBigInt,
    ColumnConst,
    ColumnLowCardinality,
    ColumnMap,
    ColumnNullable,
    ColumnString,
    ColumnTuple,
    ColumnVector,
    IColumn,
)
from ..errors import ErrorCode, raise_error
from ..literals import parse_literal, split_top_level
from .base import DataType, DataTypePtr, get_data_type
from .scalars import DataTypeNumber, DataTypeString

__all__ = [
    "DataTypeDecimal",
    "DataTypeNullable",
    "DataTypeArray",
    "DataTypeTuple",
    "DataTypeMap",
    "DataTypeLowCardinality",
    "DataTypeNested",
    "DataTypeAggregateFunction",
    "DataTypeSimpleAggregateFunction",
    "DataTypeVariant",
    "DataTypeDynamic",
    "DataTypeJSON",
]


# ══════════════════════════════════════════════════════════════════════════
# Decimal
# ══════════════════════════════════════════════════════════════════════════


class DataTypeDecimal(DataType):
    """``Decimal(P, S)`` —— **定点数**。

    .. code-block:: text

       Decimal(9, 2) 表示"总共 9 位十进制数字,小数点后 2 位"
       值 1234567.89  →  存储的整数是 123456789,scale = 2

    为什么 ClickHouse 用它而不是 Float? 因为**钱不能用浮点算**:
    ``0.1 + 0.2 != 0.3``。Decimal 把值存成整数,加减法在这种表示下
    是精确的,只有在**乘除**时才需要处理 scale 的变化。

    底层整数宽度由 P 决定(与真实 CH 完全一致):

    P 范围        存储类型      别名
    1 – 9         Int32         Decimal32(S)
    10 – 18       Int64         Decimal64(S)
    19 – 38       Int128        Decimal128(S)
    39 – 76       Int256        Decimal256(S)

    .. warning::

       **乘法会放大 scale**。``Decimal(10,2) * Decimal(10,2)`` 的
       结果类型是 ``Decimal(18,4)`` —— 因为 ``1.23 * 4.56`` 需要 4 位小数。
       这是真实 CH 的类型推导规则;而**除法**的结果 scale 是
       被除数的 scale(不会变成 4 位),所以 ``1.00 / 3.00 = 0.33``,
       不是 ``0.3333``。这条让很多人意外,第 03 章有专门的例子。
    """

    parametric = True

    def __init__(self, precision: int, scale: int) -> None:
        if precision < 1 or precision > 76:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Decimal precision must be in [1, 76], got {precision}",
            )
        if scale < 0 or scale > precision:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Decimal scale must be in [0, {precision}], got {scale}",
            )
        self.precision = precision
        self.scale = scale
        self.family_name = "Decimal"

    def get_name(self) -> str:
        return f"Decimal({self.precision}, {self.scale})"

    def _int_bits(self) -> int:
        if self.precision <= 9:
            return 32
        if self.precision <= 18:
            return 64
        return 128 if self.precision <= 38 else 256

    def create_column(self) -> IColumn:
        bits = self._int_bits()
        if bits == 32:
            return ColumnVector(np.int32)
        if bits == 64:
            return ColumnVector(np.int64)
        return ColumnBigInt(True, bits)

    def is_decimal(self) -> bool:
        return True

    def is_numeric(self) -> bool:
        return True

    def default_value(self) -> Any:
        return _PyDecimal(0).scaleb(-self.scale)

    def have_maximum_size_of_value(self) -> bool:
        return True

    def get_maximum_size_of_value(self) -> int:
        return self._int_bits() // 8

    def scale_factor(self) -> int:
        return 10 ** self.scale

    def normalize(self, value: Any) -> Any:
        """把值转成**未缩放的整数**。

        真实 ClickHouse 对"小数位超出 scale"的处理是**四舍五入**
        (``bround`` 语义,银行家舍入的变体)。mini-clickhouse 用
        ``ROUND_HALF_UP``,与绝大多数人的直觉一致;差异写在
        ``types/__init__.py`` 的对照表里。
        """
        if value is None:
            return None
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            iv = value * self.scale_factor()
            limit = 10 ** self.precision
            if abs(iv) >= limit:
                raise_error(
                    ErrorCode.ARGUMENT_OUT_OF_BOUND,
                    f"Decimal value {value} does not fit into "
                    f"Decimal({self.precision}, {self.scale})",
                )
            return iv
        if isinstance(value, float):
            value = _PyDecimal(repr(value))
        if isinstance(value, str):
            s = value.strip().strip("'")
            if s == "":
                return 0
            try:
                value = _PyDecimal(s)
            except _decimal.InvalidOperation:
                raise_error(
                    ErrorCode.CANNOT_PARSE_TEXT,
                    f"Cannot parse Decimal({self.precision}, {self.scale}) from {value!r}",
                )
        if isinstance(value, _PyDecimal):
            scaled = (value * self.scale_factor()).quantize(
                _PyDecimal(1), rounding=_decimal.ROUND_HALF_UP
            )
            iv = int(scaled)
            limit = 10 ** self.precision
            if abs(iv) >= limit:
                raise_error(
                    ErrorCode.ARGUMENT_OUT_OF_BOUND,
                    f"Decimal value {value} does not fit into "
                    f"Decimal({self.precision}, {self.scale})",
                )
            return iv
        raise_error(
            ErrorCode.CANNOT_CONVERT_TYPE,
            f"Cannot convert {type(value).__name__} to Decimal",
        )

    def to_decimal(self, unscaled: Any) -> _PyDecimal:
        if unscaled is None:
            return None  # type: ignore[return-value]
        return _PyDecimal(int(unscaled)).scaleb(-self.scale)

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return f"{self.to_decimal(value):.{self.scale}f}"

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return self.serialize_text(value, settings)

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return self.normalize(text)


def decimal_alias(bits: int, scale: int) -> DataTypeDecimal:
    """``Decimal32(S)`` 这样的别名 → 标准 ``Decimal(P, S)``。

    ``Decimal32(S)`` 的 P 固定是 9,``Decimal64`` 是 18,以此类推 ——
    这正是真实 ClickHouse 的定义(``DataTypeDecimal32`` 就是
    ``DataTypeDecimal<Decimal32>``,而 ``Decimal32`` 是 9 位精度)。
    """
    precision = {32: 9, 64: 18, 128: 38, 256: 76}[bits]
    return DataTypeDecimal(precision, scale)


# ══════════════════════════════════════════════════════════════════════════
# Nullable
# ══════════════════════════════════════════════════════════════════════════


class DataTypeNullable(DataType):
    """``Nullable(T)`` —— 在 ``T`` 之外多一个 NULL 值。

    .. warning::

       **Nullable 不是免费的**。它给每一行多加一个字节(空位图),
       并且**关掉了一整类优化**:

       * ``Nullable`` 列不能当主键/排序键(真实 CH 会在建表时直接报错);
       * ``Nullable`` 列上的比较要走"先判空"的分支,向量化被打破;
       * 聚合时要额外维护"是否全为 NULL"的状态;
       * 很多函数遇到 ``Nullable`` 参数会返回 ``Nullable``,导致
         类型一路"传染"上去。

       所以官方推荐用**类型自带的零值**代替 NULL(``''`` 表示未知字符串、
       ``0`` 表示未知数值),只有在"真的区分不了零值和缺失"时才用 Nullable。
       第 03 章有一个"改成 Nullable 之后查询慢了 3 倍"的实测。

    ``Nullable(Nullable(T))`` 是非法的 —— 真实 CH 会直接抛
    ``ILLEGAL_TYPE_OF_ARGUMENT``。``Array(Nullable(T))`` 是合法的。
    """

    parametric = True
    nullable_allowed = False

    def __init__(self, nested: DataType) -> None:
        if nested.is_nullable():
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "Nested Nullable is not allowed: Nullable(Nullable(T)) is invalid",
            )
        if not nested.nullable_allowed:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Type {nested.get_name()} cannot be wrapped in Nullable",
            )
        self.nested = nested
        self.family_name = "Nullable"

    def get_name(self) -> str:
        return f"Nullable({self.nested.get_name()})"

    def create_column(self) -> IColumn:
        return ColumnNullable(self.nested.create_column())

    def is_nullable(self) -> bool:
        return True

    # ── 值域判定必须**透传**给内层类型 ──────────────────────────────────
    #
    # ``Nullable(T)`` 的列里存的就是 ``T`` 的物理表示(多一个空位图),
    # 所以"这个值是不是日期/时间"这个问题,答案与 ``T`` 完全一致。
    # 不透传会让所有"按日期分流"的代码走错分支,而且是**静默**走错:
    #
    # .. code-block:: text
    #
    #   Nullable(DateTime).is_date_time() 曾经返回 False
    #     → ``_eval_interval_arith`` 把 ``d + INTERVAL 30 DAY`` 当成
    #       Date(天数)+ 常数,于是给"秒"的基数加了 **30 秒**而不是 30 天
    #     → TTL 判定立刻认为数据已过期,``OPTIMIZE`` 把整张表删光。
    #
    #   真实 ClickHouse 的 ``DataTypeNullable`` 同样把这类判定交给内层类型
    #   (见 ``DataTypeNullable::getNestedType`` 及其调用点的 ``removeNullable``
    #   语义);mini-clickhouse 在这里直接透传,省掉包装层的往返。

    def is_date(self) -> bool:
        return self.nested.is_date()

    def is_date_time(self) -> bool:
        return self.nested.is_date_time()

    def default_value(self) -> Any:
        return None

    def is_null(self, value: Any) -> bool:
        return value is None

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        return self.nested.normalize(value)

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return self.nested.serialize_text(value, settings)

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return self.nested.serialize_text_quoted(value, settings)

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        s = text.strip()
        if s == "NULL" or s == "\\N" or s == "":
            return None
        return self.nested.deserialize_text(s, settings)

    def get_subcolumn_names(self) -> List[str]:
        return self.nested.get_subcolumn_names()

    def get_subcolumn_type(self, name: str) -> Optional[DataType]:
        sub = self.nested.get_subcolumn_type(name)
        return DataTypeNullable(sub) if sub is not None else None

    def get_elements_type(self) -> Optional[DataType]:
        inner = self.nested.get_elements_type()
        return DataTypeNullable(inner) if inner is not None else None

    def get_size_of_value_in_memory(self) -> int:
        return self.nested.get_size_of_value_in_memory() + 1


# ══════════════════════════════════════════════════════════════════════════
# Array / Tuple / Map / Nested
# ══════════════════════════════════════════════════════════════════════════


class DataTypeArray(DataType):
    """``Array(T)`` —— 变长数组,元素类型固定。

    .. code-block:: sql

       SELECT [1,2,3] AS a, arrayMap(x -> x*2, a), a[1], length(a);
       -- [1,2,3]   [2,4,6]   1   3

    **下标从 1 开始**(与 SQL 标准一致,与 Python 不同)。
    ``a[0]`` 是合法的,但返回**元素的默认值**(不是报错)——
    这一点和很多人的预期相反。

    数组**不能有 NULL 元素**,除非显式声明 ``Array(Nullable(T))``。
    ``[1, NULL]`` 的类型推导结果就是 ``Array(Nullable(UInt8))``
    (ClickHouse 会自动把元素类型"提升"为可空)。
    """

    parametric = True

    def __init__(self, element: DataType) -> None:
        self.element = element
        self.family_name = "Array"

    def get_name(self) -> str:
        return f"Array({self.element.get_name()})"

    def create_column(self) -> IColumn:
        return ColumnArray(self.element.create_column())

    def is_array(self) -> bool:
        return True

    def default_value(self) -> Any:
        return []

    def get_elements_type(self) -> Optional[DataType]:
        return self.element

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, bytes)):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot insert a scalar into Array({self.element.get_name()})",
            )
        if isinstance(value, tuple):
            value = list(value)
        if hasattr(value, "tolist"):  # numpy 数组
            value = value.tolist()
        if not isinstance(value, list):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot convert {type(value).__name__} to Array",
            )
        return [self.element.normalize(v) for v in value]

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return "[" + ",".join(self.element.serialize_text(v, settings) for v in value) + "]"

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        inner = ", ".join(
            self.element.serialize_text_quoted(v, settings) for v in value
        )
        return f"[{inner}]"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        parsed = parse_literal(text)
        if isinstance(parsed, str):
            # 未加方括号的裸串:当成空数组(与真实 CH 的容错一致)
            return []
        if isinstance(parsed, tuple):
            parsed = list(parsed)
        if not isinstance(parsed, list):
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Cannot parse {text!r} as {self.get_name()}",
            )
        return [self.element.normalize(v) for v in parsed]

    def get_size_of_value_in_memory(self) -> int:
        return 8  # 一个 offset


class DataTypeTuple(DataType):
    """``Tuple(T1, T2, …)`` 或 ``Tuple(name1 T1, name2 T2, …)``。

    **具名元组**是有名字的,所以 ``t.name1`` 可用;匿名元组只能用
    ``t.1`` / ``t.2``(下标同样从 1 开始)。

    .. code-block:: sql

       SELECT tuple(1, 'a') AS t, t.1, t.2;
       SELECT CAST((1, 'a'), 'Tuple(id UInt8, name String)') AS t, t.name;

    一个不太直观的事实:``Array(Tuple(...))`` 是 ClickHouse 里
    **最常用的"复杂结构"表示法** —— 没有真正的 struct/map 之前,
    人们就是靠它来表达嵌套 JSON 的。
    """

    parametric = True

    def __init__(self, elements: Sequence[DataType], names: Optional[Sequence[str]] = None) -> None:
        if not elements:
            raise_error(ErrorCode.LOGICAL_ERROR, "Tuple requires at least one element")
        self.elements = list(elements)
        if names is not None:
            if len(names) != len(elements):
                raise_error(
                    ErrorCode.SIZES_OF_COLUMNS_IN_TUPLE_DOESNT_MATCH,
                    f"Tuple has {len(elements)} elements but {len(names)} names",
                )
            if len(set(names)) != len(names):
                raise_error(ErrorCode.DUPLICATE_COLUMN, f"Duplicate tuple field name in {names}")
        self.names = list(names) if names else None
        self.family_name = "Tuple"

    def get_name(self) -> str:
        if self.names:
            parts = ", ".join(
                f"{n} {t.get_name()}" for n, t in zip(self.names, self.elements)
            )
        else:
            parts = ", ".join(t.get_name() for t in self.elements)
        return f"Tuple({parts})"

    def create_column(self) -> IColumn:
        return ColumnTuple([t.create_column() for t in self.elements])

    def is_tuple(self) -> bool:
        return True

    def default_value(self) -> Any:
        return tuple(t.default_value() for t in self.elements)

    def get_subcolumn_names(self) -> List[str]:
        return list(self.names) if self.names else []

    def get_subcolumn_type(self, name: str) -> Optional[DataType]:
        if not self.names or name not in self.names:
            return None
        return self.elements[self.names.index(name)]

    def get_subcolumn_index(self, name: str) -> int:
        if not self.names or name not in self.names:
            return -1
        return self.names.index(name)

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            if not self.names:
                raise_error(
                    ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                    "Cannot insert a dict into an unnamed Tuple",
                )
            return tuple(
                self.elements[i].normalize(value.get(n, self.elements[i].default_value()))
                for i, n in enumerate(self.names)
            )
        if isinstance(value, (list, tuple)):
            if len(value) != len(self.elements):
                raise_error(
                    ErrorCode.SIZES_OF_COLUMNS_IN_TUPLE_DOESNT_MATCH,
                    f"Tuple expects {len(self.elements)} elements, got {len(value)}",
                )
            return tuple(t.normalize(v) for t, v in zip(self.elements, value))
        raise_error(
            ErrorCode.CANNOT_CONVERT_TYPE,
            f"Cannot convert {type(value).__name__} to {self.get_name()}",
        )

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return "(" + ",".join(
            t.serialize_text(v, settings) for t, v in zip(self.elements, value)
        ) + ")"

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return "(" + ",".join(
            t.serialize_text_quoted(v, settings) for t, v in zip(self.elements, value)
        ) + ")"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        parsed = parse_literal(text)
        if isinstance(parsed, (list, tuple)):
            return self.normalize(list(parsed))
        raise_error(
            ErrorCode.CANNOT_PARSE_TEXT,
            f"Cannot parse {text!r} as {self.get_name()}",
        )


class DataTypeMap(DataType):
    """``Map(K, V)`` —— 存储上等价于 ``Array(Tuple(K, V))``。

    ``K`` 必须是可比较的标量类型(数值/字符串/日期/UUID/IP),
    不能是 ``Array``/``Map``/``Nullable``。
    """

    parametric = True

    def __init__(self, key: DataType, value: DataType) -> None:
        if key.is_array() or key.is_map() or key.is_nullable():
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Map key type cannot be {key.get_name()}",
            )
        self.key = key
        self.value = value
        self.family_name = "Map"

    def get_name(self) -> str:
        return f"Map({self.key.get_name()}, {self.value.get_name()})"

    def create_column(self) -> IColumn:
        return ColumnMap(self.key.create_column(), self.value.create_column())

    def is_map(self) -> bool:
        return True

    def default_value(self) -> Any:
        return {}

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            return {self.key.normalize(k): self.value.normalize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            # [(k, v), ...] 的形式也接受
            return {self.key.normalize(k): self.value.normalize(v) for k, v in value}
        raise_error(
            ErrorCode.CANNOT_CONVERT_TYPE,
            f"Cannot convert {type(value).__name__} to Map",
        )

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        body = ",".join(
            f"{self.key.serialize_text(k, settings)}:"
            f"{self.value.serialize_text(v, settings)}"
            for k, v in value.items()
        )
        return "{" + body + "}"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        parsed = parse_literal(text)
        if isinstance(parsed, dict):
            return self.normalize(parsed)
        if isinstance(parsed, (list, tuple)):
            return self.normalize(parsed)
        raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"Cannot parse {text!r} as Map")


class DataTypeNested(DataType):
    """``Nested(a T1, b T2)`` —— 历史上是"数组的元组",现在是别名。

    .. code-block:: sql

       -- 老写法(仍然支持,但会被自动展开成一堆 Array 列)
       CREATE TABLE t (n Nested(a UInt8, b String)) ENGINE = Memory;
       -- 等价于
       CREATE TABLE t (n.a Array(UInt8), n.b Array(String)) ENGINE = Memory;

       -- 新写法(官方推荐)
       CREATE TABLE t (n Array(Tuple(a UInt8, b String))) ENGINE = Memory;

    关键区别:**老写法下同一个 Nested 里的各列长度必须一致**
    (因为它们在语义上是一个数组),新写法没有这个约束
    (``Array(Tuple)`` 里每个元素是一个元组,元组内部固定)。
    真实 ClickHouse 在新版本里已经把 ``Nested`` 变成
    "``Array(Tuple(...))`` + 名字前缀"的语法糖。
    """

    parametric = True

    def __init__(self, elements: Sequence[DataType], names: Sequence[str]) -> None:
        self.elements = list(elements)
        self.names = list(names)
        # 真实 CH: Nested 的每一列都变成 Array
        self.arrays = [DataTypeArray(t) for t in self.elements]
        self.family_name = "Nested"

    def get_name(self) -> str:
        parts = ", ".join(f"{n} {t.get_name()}" for n, t in zip(self.names, self.elements))
        return f"Nested({parts})"

    def create_column(self) -> IColumn:
        # 默认物化 = 数组的元组
        return ColumnArray(ColumnTuple([t.create_column() for t in self.elements]))

    def default_value(self) -> Any:
        return []

    def get_subcolumn_names(self) -> List[str]:
        return list(self.names)

    def normalize(self, value: Any) -> Any:
        if value is None or value == []:
            return []
        if isinstance(value, dict):
            cols = [value.get(n, []) for n in self.names]
            n = len(cols[0]) if cols else 0
            for c in cols:
                if len(c) != n:
                    raise_error(
                        ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                        "Nested subcolumns must have equal lengths",
                    )
            return [
                tuple(self.elements[i].normalize(cols[i][j]) for i in range(len(self.names)))
                for j in range(n)
            ]
        if isinstance(value, (list, tuple)):
            return [
                tuple(
                    self.elements[i].normalize(item[i] if isinstance(item, (list, tuple)) else item)
                    for i in range(len(self.names))
                )
                for item in value
            ]
        raise_error(ErrorCode.CANNOT_CONVERT_TYPE, f"Cannot convert to {self.get_name()}")

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if not value:
            return "[]"
        body = ",".join(
            "(" + ",".join(
                self.elements[i].serialize_text(item[i], settings)
                for i in range(len(self.names))
            ) + ")"
            for item in value
        )
        return f"[{body}]"

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return self.normalize(parse_literal(text))


# ══════════════════════════════════════════════════════════════════════════
# LowCardinality
# ══════════════════════════════════════════════════════════════════════════


class DataTypeLowCardinality(DataType):
    """``LowCardinality(T)`` —— 字典编码。

    .. code-block:: sql

       CREATE TABLE t (url String, method LowCardinality(String)) ENGINE = MergeTree ORDER BY url;

    什么时候该用:

    * 基数**低于 10 万**(``method`` 只有 5 个取值、``country`` 有 200 个);
    * 该列**频繁出现在 GROUP BY / WHERE 等值比较**里;
    * 该列**重复度高**(压缩收益大)。

    什么时候**不该**用:

    * 高基数列(如 ``user_id``)—— 字典本身会变得和原列一样大,
      还要额外维护索引,反而更慢更占内存;
    * 需要 ``ORDER BY`` 该列做范围扫描 —— 低基数列上的范围查询
      退化成"遍历所有字典值再取并集",可能比普通列慢。

    真实 ClickHouse 会在写入时做**自适应降级**:字典超过
    ``低基数阈值``(默认 100,000 个不同值)时,该列自动退回普通列。
    这个降级**不可逆**,而且**不报错** —— 你只会看到查询变慢。
    """

    parametric = True

    def __init__(self, nested: DataType) -> None:
        if nested.is_low_cardinality():
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "Nested LowCardinality is not allowed",
            )
        if nested.is_nullable():
            # 真实 CH 支持 LowCardinality(Nullable(String)),但内部表示不同
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "LowCardinality(Nullable(T)) is not supported by mini-clickhouse; "
                "use Nullable(LowCardinality(T)) in real ClickHouse if you need it",
            )
        self.nested = nested
        self.family_name = "LowCardinality"

    def get_name(self) -> str:
        return f"LowCardinality({self.nested.get_name()})"

    def create_column(self) -> IColumn:
        return ColumnLowCardinality(self.nested.create_column())

    def is_low_cardinality(self) -> bool:
        return True

    def default_value(self) -> Any:
        return self.nested.default_value()

    def normalize(self, value: Any) -> Any:
        return self.nested.normalize(value)

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        return self.nested.serialize_text(value, settings)

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        return self.nested.serialize_text_quoted(value, settings)

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return self.nested.deserialize_text(text, settings)

    def get_elements_type(self) -> Optional[DataType]:
        return self.nested


# ══════════════════════════════════════════════════════════════════════════
# 聚合状态类型
# ══════════════════════════════════════════════════════════════════════════


class DataTypeAggregateFunction(DataType):
    """``AggregateFunction(sum, UInt64)`` —— "这一列存的是**聚合状态**"。

    它存在的前提是聚合函数有**可合并的中间状态**:

    .. code-block:: text

       sum  的状态 = 一个累加值           → 可合并(相加即可)
       avg  的状态 = (和, 计数)          → 可合并(分别相加)
       uniq 的状态 = 一个 HyperLogLog 草图 → 可合并(取并)
       groupArray 的状态 = 一个数组       → 可合并(**代价高**)

    ``AggregatingMergeTree`` 就是把这个状态**直接写进 part**,
    读的时候再 ``-Merge`` 出来。所以"物化视图的增量聚合"靠的就是它。

    .. warning::

       **状态是二进制的、不透明的**。你不能对 ``AggregateFunction(sum, UInt64)``
       列做 ``+``,也不能 ``SELECT`` 出来看(会输出一堆字节)。
       要用 ``-Merge`` 组合器消费它。
    """

    parametric = True

    def __init__(self, func_name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        self.func_name = func_name
        self.arg_types = list(arg_types)
        self.params = list(params)
        self.family_name = "AggregateFunction"

    def get_name(self) -> str:
        args = ", ".join(t.get_name() for t in self.arg_types)
        if self.params:
            params = ", ".join(str(p) for p in self.params)
            return f"AggregateFunction({self.func_name}({params}), {args})" if args else \
                f"AggregateFunction({self.func_name}({params}))"
        return f"AggregateFunction({self.func_name}, {args})" if args else \
            f"AggregateFunction({self.func_name})"

    def create_column(self) -> IColumn:
        return ColumnAggregateFunction(None)

    def is_aggregate_function(self) -> bool:
        return True

    def default_value(self) -> Any:
        return None

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return value.hex() if isinstance(value, (bytes, bytearray)) else str(value)

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        s = text.strip().strip("'")
        try:
            return bytes.fromhex(s)
        except ValueError:
            return s.encode("utf-8", "surrogateescape")


class DataTypeSimpleAggregateFunction(DataType):
    """``SimpleAggregateFunction(sum, UInt64)`` —— 状态**就是**普通值。

    和 :class:`DataTypeAggregateFunction` 的区别非常好记:

    .. code-block:: text

       SimpleAggregateFunction(sum, UInt64)   列里存的是 42
       AggregateFunction(sum, UInt64)         列里存的是 <一段二进制状态>

    所以前者可以直接读、可以直接比较、可以直接参与运算 ——
    只要聚合函数满足 **f(f(a,b),c) == f(a,f(b,c))** 且中间结果
    可以原样存储(``sum``/``min``/``max``/``any``/``groupBitAnd`` 都满足;
    ``avg``/``uniq`` 不满足,必须用 ``AggregateFunction``)。

    这条规则让 ``SimpleAggregateFunction`` 的读写代价几乎为零,
    是"预聚合"场景的首选。
    """

    parametric = True

    def __init__(self, func_name: str, value_type: DataType) -> None:
        self.func_name = func_name
        self.value_type = value_type
        self.family_name = "SimpleAggregateFunction"

    def get_name(self) -> str:
        return f"SimpleAggregateFunction({self.func_name}, {self.value_type.get_name()})"

    def create_column(self) -> IColumn:
        return self.value_type.create_column()

    def is_aggregate_function(self) -> bool:
        return True

    def default_value(self) -> Any:
        return self.value_type.default_value()

    def normalize(self, value: Any) -> Any:
        return self.value_type.normalize(value)

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        return self.value_type.serialize_text(value, settings)

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        return self.value_type.deserialize_text(text, settings)


# ══════════════════════════════════════════════════════════════════════════
# 动态类型
# ══════════════════════════════════════════════════════════════════════════


class DataTypeVariant(DataType):
    """``Variant(T1, T2, …)`` —— "这一行的值是其中之一"。

    .. code-block:: sql

       CREATE TABLE t (v Variant(UInt64, String)) ENGINE = Memory;
       INSERT INTO t VALUES (42), ('hello'), (NULL);
       SELECT v, variantType(v) FROM t;
       -- 42     UInt64
       -- hello  String
       -- NULL   None

    真实含义比"联合体"更精确:每个值**同时**知道自己属于哪个类型
    (``variantType()`` 能查到),而且 NULL 是隐式的第四种状态。

    读的时候有两种方式:直接取(得到对应类型)或者
    ``v.UInt64`` / ``v.String`` 取某个分支(不匹配则返回该类型的默认值)。
    """

    parametric = True

    def __init__(self, variants: Sequence[DataType]) -> None:
        variants = list(variants)
        if len(variants) < 1:
            raise_error(ErrorCode.LOGICAL_ERROR, "Variant requires at least one type")
        seen = set()
        for v in variants:
            if v.get_name() in seen:
                raise_error(ErrorCode.DUPLICATE_COLUMN, f"Duplicate variant type {v.get_name()}")
            seen.add(v.get_name())
            if v.is_nullable():
                raise_error(
                    ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                    "Variant cannot contain Nullable types",
                )
        # 按名字排序 —— 真实 CH 也用"字典序"来规范化 Variant 的判别位
        self.variants = sorted(variants, key=lambda t: t.get_name())
        self.family_name = "Variant"

    def get_name(self) -> str:
        return "Variant(" + ", ".join(v.get_name() for v in self.variants) + ")"

    def create_column(self) -> IColumn:
        # 用 Tuple(判别位, 各分支列) 是不够的 —— 真实 CH 是"判别数组 + 各分支列"。
        # 这里简化为一个 String 列存判别类型名 + 各分支列,语义一致。
        return ColumnTuple(
            [DataTypeString().create_column()]
            + [v.create_column() for v in self.variants]
        )

    def is_variant(self) -> bool:
        return True

    def default_value(self) -> Any:
        return None

    def pick(self, value: Any) -> Optional[DataType]:
        """挑出与 ``value`` 匹配的分支类型(Python 层面的宽松匹配)。"""
        if value is None:
            return None
        if isinstance(value, bool):
            for v in self.variants:
                if isinstance(v, DataTypeNumber) and v.bits == 8 and not v.signed:
                    return v
            return None
        if isinstance(value, int):
            for v in self.variants:
                if isinstance(v, DataTypeNumber) and not v.float and not v.signed:
                    return v
            for v in self.variants:
                if isinstance(v, DataTypeNumber) and v.is_integer():
                    return v
            return None
        if isinstance(value, float):
            for v in self.variants:
                if isinstance(v, DataTypeNumber) and v.float:
                    return v
            return None
        if isinstance(value, (str, bytes)):
            for v in self.variants:
                if v.is_string() or v.is_enum():
                    return v
            return None
        return None

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        picked = self.pick(value)
        return picked.serialize_text(value, settings) if picked else str(value)

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        picked = self.pick(value)
        return picked.serialize_text_quoted(value, settings) if picked else \
            DataTypeString().serialize_text_quoted(str(value))

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        s = text.strip()
        if s in ("NULL", "\\N"):
            return None
        parsed = parse_literal(s)
        picked = self.pick(parsed)
        if picked is None:
            return parsed
        return picked.normalize(parsed)


class DataTypeDynamic(DataType):
    """``Dynamic`` —— 连"有哪些分支"都是逐行决定的。

    ``Variant`` 的分支是**建表时固定**的;``Dynamic`` 会在写入过程中
    **自动发现**新类型并追加分支(受 ``dynamic_max_types`` 限制)。
    它主要服务于:

    * 半结构化数据(每条日志的字段都不一样);
    * ``JSON`` 类型的底层实现(每个 JSON 路径都是一个 Dynamic 列)。

    .. warning::

       自动类型发现是**有代价**的:写入路径要做类型判定,
       而且分支会随数据分布漂移。真实 ClickHouse 对高基数的 Dynamic
       会退化成"一个 String 列存序列化后的值"(``String`` 模式)。
       我们用同样的降级策略。
    """

    family_name = "Dynamic"

    def __init__(self, max_types: int = 32) -> None:
        self.max_types = max_types

    def get_name(self) -> str:
        return "Dynamic"

    def create_column(self) -> IColumn:
        # 简化实现:存"类型名 + 值的文本",与真实 CH 的 String 降级模式一致
        return ColumnTuple([ColumnString(), ColumnString()])

    def is_dynamic(self) -> bool:
        return True

    def default_value(self) -> Any:
        return None

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        return "NULL" if value is None else str(value)

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return DataTypeString().serialize_text_quoted(str(value))

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        s = text.strip()
        if s in ("NULL", "\\N"):
            return None
        return parse_literal(s)


class DataTypeJSON(DataType):
    """``JSON`` —— 把 JSON 的**每个路径物化成一个子列**。

    .. code-block:: sql

       CREATE TABLE t (j JSON) ENGINE = MergeTree ORDER BY tuple();
       INSERT INTO t FORMAT JSONEachRow {"j": {"a": 1, "b": "x"}}
       SELECT j.a, j.b FROM t;   -- 1   x

    它的关键在于**不是把 JSON 串存在一个 String 列里**,而是这样:

    .. code-block:: text

       JSON 文档 {"a": 1, "b": "x"}
            ↓ 路径提取
       .a  →  UInt8 列
       .b  →  String 列
       .shared  →  Map(String, String) 列   ← 高频/稀有路径分开存

    好处是:``j.a`` 可以直接当数值列参与运算和压缩;坏处是:
    **列的集合取决于数据**,所以 ``DESCRIBE`` 出来的结果可能随写入而变。

    真实 ClickHouse 还区分"类型化路径"与"共享数据"(稀有路径塞进一个
    残差 Map)。mini-clickhouse 只实现"路径物化 + 点号访问"这一层。
    """

    parametric = True

    def __init__(self, max_dynamic_paths: int = 1024) -> None:
        self.max_dynamic_paths = max_dynamic_paths
        self.family_name = "JSON"

    def get_name(self) -> str:
        return "JSON"

    def create_column(self) -> IColumn:
        return ColumnString()

    def is_json(self) -> bool:
        return True

    def default_value(self) -> Any:
        return {}

    def normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            return parse_literal(value)
        return value

    def serialize_text(self, value: Any, settings: Any = None) -> str:
        from ..util.json_text import to_json_text

        return "NULL" if value is None else to_json_text(value)

    def serialize_text_quoted(self, value: Any, settings: Any = None) -> str:
        if value is None:
            return "NULL"
        return DataTypeString().serialize_text_quoted(self.serialize_text(value, settings))

    def deserialize_text(self, text: str, settings: Any = None) -> Any:
        from ..util.json_text import from_json_text

        s = text.strip()
        if s in ("NULL", "\\N", ""):
            return None
        return from_json_text(s)
