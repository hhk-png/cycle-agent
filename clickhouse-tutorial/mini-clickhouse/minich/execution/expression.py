"""表达式求值 —— AST → 列。**这是整个执行引擎的中枢。**

在行式数据库里,"求值"是一件很自然的事:给一行,算出值。
在 ClickHouse 里,求值的**单位是列**:给若干列(一个 block),
算出若干列。这个差别带来了三个必须在这一层解决的问题:

.. code-block:: text

   问题 1  列从哪来?
           `SELECT a + 1 FROM t` —— `a` 是表列;
           `SELECT a + 1 AS b FROM t ORDER BY b` —— `b` 是别名;
           `WITH x AS (SELECT 1) SELECT x` —— 是子查询;
           `SELECT arrayMap(y -> y + a, arr)` —— `y` 是 lambda 形参,`a` 是外层列。
           → 用一个**作用域链**(Scope)按顺序找。

   问题 2  什么时候算?
           常量折叠:`WHERE d = '2024-01-01'` 里的日期只在**准备阶段**
           解析一次,而不是每一行都解析。
           → 全常量参数的表达式在分析期求值,结果是一个 ColumnConst。

   问题 3  哪些是"聚合"?
           `SELECT sum(a)/count() FROM t GROUP BY b` 里,`sum(a)` 与
           `count()` 是**两阶段**的:先按 b 分组累计状态,再合并出结果。
           → 分析期把聚合函数调用**抽出来**,替换成对聚合结果的引用。

.. note::

   真实 ClickHouse 的对应物是 ``ExpressionAnalyzer`` +
   ``ExpressionActions``(一组"算这一列"的动作,按依赖排序)。
   它会产出一个**动作链**,而 mini-clickhouse 直接递归求值。
   区别在性能(动作链可以复用中间结果、可以多线程)与调试体验
   (动作链可以用 ``EXPLAIN`` 打出来)。第 10 章会解释为什么
   ClickHouse 要引入 `ActionsDAG`。
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np

from ..aggregates import aggregate_function_exists, get_aggregate_function
from ..block import Block, ColumnWithTypeAndName
from ..columns import (
    ColumnArray,
    ColumnConst,
    ColumnMap,
    ColumnNullable,
    ColumnString,
    ColumnTuple,
    ColumnVector,
    IColumn,
    column_from_values,
)
from ..errors import ErrorCode, MiniChError, raise_error
from ..functions.base import (
    FunctionBase,
    LambdaArgument,
    function_exists,
    get_function,
)
from ..sql import ast_ as A
from ..types import (
    DataType,
    DataTypeNullable,
    get_data_type,
    get_wider_numeric_type,
)
from ..types.composite import DataTypeArray, DataTypeMap, DataTypeTuple
from ..types.scalars import DataTypeNothing

__all__ = [
    "Scope",
    "Evaluator",
    "AggregateRef",
    "AggregateDescription",
    "ExpressionAnalyzer",
    "AnalysisResult",
    "infer_literal_type",
    "operator_to_function",
]


#: SQL 里的运算符 → 函数名。**运算符就是函数**,这是 ClickHouse 的核心设计:
#: ``a + b`` 与 ``plus(a, b)`` 在解析后是同一个东西,所以
#: ``EXPLAIN AST`` 会把前者显示成后者。
_OPERATOR_FUNCTIONS: Dict[str, str] = {
    "+": "plus",
    "-": "minus",
    "*": "multiply",
    "/": "divide",
    "%": "modulo",
    "||": "concat",
    "=": "equals",
    "==": "equals",
    "!=": "notEquals",
    "<>": "notEquals",
    "<": "less",
    "<=": "lessOrEquals",
    ">": "greater",
    ">=": "greaterOrEquals",
    "<=>": "isNotDistinctFrom",
    "AND": "and",
    "OR": "or",
    "NOT": "not",
    "~": "bitNot",
}


def operator_to_function(op: str) -> str:
    """把运算符映射成函数名(未知运算符抛错,不静默返回原名)。"""
    fn = _OPERATOR_FUNCTIONS.get(op)
    if fn is None:
        raise_error(ErrorCode.UNKNOWN_FUNCTION, f"Unknown operator {op!r}")
    return fn


# ══════════════════════════════════════════════════════════════════════════
# 作用域
# ══════════════════════════════════════════════════════════════════════════


class Scope:
    """名字 → 列 的**链式**查找表。

    .. code-block:: text

       Scope(lambda 形参)          ← 最内层,只有 y
            │ parent
       Scope(SELECT 的别名 a AS b)  ← 中间层:别名与其原始表达式
            │ parent
       Scope(表的列)                ← 最外层:表结构

    查找从内往外走,**内层优先**。这就是为什么
    ``SELECT arrayMap(x -> x + 1, x)`` 里的两个 ``x`` 含义不同 ——
    第一个是 lambda 形参,第二个是表列。真实 ClickHouse
    用 ``ExpressionActions`` 的输入列名做同样的事,但它**不允许**
    lambda 形参遮蔽外层列名(会报 ``AMBIGUOUS_COLUMN_NAME``)——
    我们的许可更宽松,这个差异写在教程第 07 章。
    """

    __slots__ = ("columns", "aliases", "parent", "_lambda_params")

    def __init__(
        self,
        columns: Optional[Dict[str, ColumnWithTypeAndName]] = None,
        aliases: Optional[Dict[str, A.Node]] = None,
        parent: Optional["Scope"] = None,
        lambda_params: Optional[Set[str]] = None,
    ) -> None:
        self.columns: Dict[str, ColumnWithTypeAndName] = dict(columns or {})
        #: 别名 → 它对应的 AST(``SELECT a+1 AS b`` 里 ``b → a+1``)
        self.aliases: Dict[str, A.Node] = dict(aliases or {})
        self.parent = parent
        self._lambda_params = lambda_params or set()

    def child(self, **kwargs: Any) -> "Scope":
        base = dict(parent=self)
        base.update(kwargs)
        return Scope(**base)

    def resolve_column(self, name: str) -> Optional[ColumnWithTypeAndName]:
        s: Optional[Scope] = self
        while s is not None:
            col = s.columns.get(name)
            if col is not None:
                return col
            s = s.parent
        return None

    def resolve_alias(self, name: str) -> Optional[A.Node]:
        s: Optional[Scope] = self
        while s is not None:
            node = s.aliases.get(name)
            if node is not None:
                return node
            s = s.parent
        return None

    def is_lambda_param(self, name: str) -> bool:
        s: Optional[Scope] = self
        while s is not None:
            if name in s._lambda_params:
                return True
            s = s.parent
        return False

    def add_column(self, name: str, col: ColumnWithTypeAndName) -> None:
        self.columns[name] = col

    def add_alias(self, name: str, node: A.Node) -> None:
        self.aliases[name] = node

    def flatten(self) -> Dict[str, ColumnWithTypeAndName]:
        """把整条链摊平成一张表(内层覆盖外层)—— 提交给处理器时用。"""
        out: Dict[str, ColumnWithTypeAndName] = {}
        chain: List[Scope] = []
        s: Optional[Scope] = self
        while s is not None:
            chain.append(s)
            s = s.parent
        for s in reversed(chain):
            out.update(s.columns)
        return out

    def available_names(self) -> List[str]:
        return list(self.flatten().keys())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Scope {sorted(self.available_names())}>"


# ══════════════════════════════════════════════════════════════════════════
# 聚合引用
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class AggregateRef(A.Node):
    """聚合函数的**占位引用** —— 分析期用它替换掉 ``sum(x)``。

    ``index`` 指向 :class:`AnalysisResult.aggregates` 里的第几项。
    这样"聚合后"的表达式里就看不到 ``sum(x)`` 了,只有
    "取第 i 个聚合的结果" —— 与真实 ClickHouse 把
    ``ASTFunction`` 换成 ``ColumnNode`` 是同一种手法。
    """

    index: int = 0

    def to_sql(self) -> str:
        return f"__aggregate_{self.index}__"

    def children(self) -> List[A.Node]:
        return []


@dataclass
class AggregateDescription:
    """一个待计算的聚合 —— 函数 + 实参表达式 + 参数。

    ``argument_names`` 是**预先算好的实参列名**。真实 ClickHouse 的
    ``AggregateDescription`` 里也有这个
    (``argument_names``),因为聚合器要按名字去 block 里取列。
    """

    func_name: str
    func: Any
    arg_exprs: List[A.Node]
    params: List[Any] = field(default_factory=list)
    argument_names: List[str] = field(default_factory=list)
    #: ``-If`` / ``-Array`` 等组合器的信息
    combinator: Optional[str] = None
    #: 结果列名(``-State`` 时是状态列)
    result_name: str = ""
    #: 是否是 ``-State``(输出状态而不是最终值)
    is_state: bool = False
    #: 是否是 ``-Merge``(输入是状态)
    is_merge: bool = False

    def __repr__(self) -> str:  # pragma: no cover
        return f"AggregateDescription({self.func_name}({', '.join(self.argument_names)}))"


@dataclass
class AnalysisResult:
    """一次查询的**表达式分析结果**。

    .. code-block:: text

       SELECT toDate(ts) AS d, uniqExact(u) FROM t GROUP BY d ORDER BY d

       before_aggregation:  [toDate(ts) AS d, u AS __agg_arg_0]
       aggregates:          [uniqExact(u)]
       after_aggregation:   [d, <finalize __aggregate_0> AS uniqExact(u)]
       key_exprs:           [d]
       order_by_exprs:      [d]
    """

    #: 聚合**之前**要算的表达式(表列 → 中间列)
    before_aggregation: List[A.Node] = field(default_factory=list)
    #: 聚合描述
    aggregates: List[AggregateDescription] = field(default_factory=list)
    #: 聚合**之后**要算的表达式(中间列 → 结果列)
    after_aggregation: List[A.Node] = field(default_factory=list)
    #: GROUP BY 的键表达式(已替换成中间列名)
    key_exprs: List[A.Node] = field(default_factory=list)
    #: 别名表
    aliases: Dict[str, A.Node] = field(default_factory=dict)
    #: 是否包含聚合
    has_aggregation: bool = False
    #: 是否包含窗口函数
    has_window: bool = False
    #: 用到的表列(列裁剪的依据)
    source_columns: List[str] = field(default_factory=list)
    #: 是否包含 ``arrayJoin``
    has_array_join: bool = False
    #: 聚合后要输出的列名与对应的表达式(顺序即 SELECT 顺序)
    output_exprs: List[A.Node] = field(default_factory=list)
    #: HAVING 表达式(聚合后求值)
    having_expr: Optional[A.Node] = None
    #: ``WITH TOTALS``
    with_totals: bool = False

    def describe(self) -> str:  # pragma: no cover
        lines = ["AnalysisResult:"]
        lines.append("  source columns: " + ", ".join(self.source_columns))
        if self.before_aggregation:
            lines.append("  before: " + ", ".join(e.to_sql() for e in self.before_aggregation))
        if self.aggregates:
            lines.append("  aggregates: " + ", ".join(str(a) for a in self.aggregates))
        if self.key_exprs:
            lines.append("  keys: " + ", ".join(e.to_sql() for e in self.key_exprs))
        if self.after_aggregation:
            lines.append("  after: " + ", ".join(e.to_sql() for e in self.after_aggregation))
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# 字面量类型推导
# ══════════════════════════════════════════════════════════════════════════


def infer_literal_type(value: Any) -> DataType:
    """推断一个字面量的类型 —— **必须与真实 ClickHouse 一致**。

    .. code-block:: text

       字面量        真实 ClickHouse 的类型      为什么
       ──────────  ──────────────────────────  ─────────────────────────────
       1            UInt8                       最小的、能装下的无符号类型
       -1           Int8                        负数 → 有符号
       255          UInt8
       256          UInt16                      "能装下就最小"
       1.5          Float64                     小数一律 Float64(不是 Float32!)
       'abc'        String
       NULL         Nullable(Nothing)           特殊:它还没有类型
       true         Bool
       [1, 2]       Array(UInt8)                取所有元素的**公共类型**
       (1, 'a')     Tuple(UInt8, String)
       {'a': 1}     Map(String, UInt8)

    "能装下就最小"这条规则会造成一个**非常常见的困惑**:

    .. code-block:: sql

       SELECT 1 + 1;              -- 2,类型 UInt16(两个 UInt8 相加会提升)
       SELECT toTypeName(1);      -- UInt8
       SELECT 1 / 2;              -- 0.5,类型 Float64

    第 03 章有一整节讲"为什么 ``SELECT 255 + 1`` 不会得到 256"。
    """
    if value is None:
        return DataTypeNullable(DataTypeNothing())
    if isinstance(value, bool):
        return get_data_type("Bool")
    if isinstance(value, int):
        if value >= 0:
            for name, limit in (("UInt8", 255), ("UInt16", 65535),
                                ("UInt32", 4294967295), ("UInt64", 2 ** 64 - 1)):
                if value <= limit:
                    return get_data_type(name)
            return get_data_type("UInt128")
        for name, limit in (("Int8", 127), ("Int16", 32767),
                            ("Int32", 2147483647), ("Int64", 2 ** 63 - 1)):
            if -value <= limit + 1:
                return get_data_type(name)
        return get_data_type("Int128")
    if isinstance(value, float):
        # 真实 ClickHouse 把带小数点的字面量一律当 Float64
        return get_data_type("Float64")
    if isinstance(value, str):
        return get_data_type("String")
    if isinstance(value, (list, tuple)):
        if not value:
            return DataTypeArray(DataTypeNothing())
        elements = [infer_literal_type(v) for v in value]
        elem = _common_type(elements)
        if isinstance(value, tuple):
            return DataTypeTuple(elements, None)
        return DataTypeArray(elem)
    if isinstance(value, dict):
        if not value:
            return DataTypeMap(get_data_type("String"), DataTypeNothing())
        keys = [infer_literal_type(k) for k in value]
        vals = [infer_literal_type(v) for v in value.values()]
        return DataTypeMap(_common_type(keys), _common_type(vals))
    if isinstance(value, _dt.date):
        return get_data_type("Date")
    if isinstance(value, _dt.datetime):
        return get_data_type("DateTime")
    raise_error(
        ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
        f"Cannot infer a ClickHouse type for a literal of {type(value).__name__}",
    )


def _common_type(types: Sequence[DataType]) -> DataType:
    """求若干个类型的"公共类型" —— 数组字面量的元素类型就是这么来的。

    规则(与真实 ClickHouse 的 ``getLeastSupertype`` 一致):

    * 全同 → 它自己;
    * 有 ``Nothing`` → 另一个(``[1, NULL]`` 是 ``Array(Nullable(UInt8))``);
    * 有 ``Nullable`` → 结果可空;
    * 数值之间 → 取更宽的那个;
    * 其余组合 → 报错(而不是静默退化成 String!),因为
      ``[1, 'a']`` 在 ClickHouse 里是一个**错误**。
    """
    if not types:
        return DataTypeNothing()
    first = types[0]
    if all(t.get_name() == first.get_name() for t in types):
        return first
    non_nothing = [t for t in types if not t.is_nothing()]
    if not non_nothing:
        return DataTypeNothing()
    if len(non_nothing) < len(types):
        inner = _common_type(non_nothing)
        return inner if inner.is_nullable() else DataTypeNullable(inner)
    nullable = any(t.is_nullable() for t in types)
    stripped = [t.nested if t.is_nullable() else t for t in types]  # type: ignore[attr-defined]
    # 去掉可空包装之后,里面可能还剩 ``Nothing``(``Nullable(Nothing)`` 剥出来就是它)——
    # 它不是"一种数据",只是一个占位,不参与公共类型推导。
    stripped = [t for t in stripped if not t.is_nothing()]
    if not stripped:
        return DataTypeNullable(DataTypeNothing()) if nullable else DataTypeNothing()
    numeric = [t for t in stripped if t.is_numeric()]
    if len(numeric) == len(stripped):
        result = numeric[0]
        for t in numeric[1:]:
            result = get_wider_numeric_type(result, t)
        return DataTypeNullable(result) if nullable else result
    strings = [t for t in stripped if t.is_string()]
    if len(strings) == len(stripped):
        return DataTypeNullable(strings[0]) if nullable else strings[0]
    raise_error(
        ErrorCode.TYPE_MISMATCH,
        "There is no supertype for types "
        + ", ".join(t.get_name() for t in types)
        + " (you may need an explicit CAST)",
    )


# ══════════════════════════════════════════════════════════════════════════
# 求值器
# ══════════════════════════════════════════════════════════════════════════


class Evaluator:
    """把一个 AST 表达式求值成一个 :class:`ColumnWithTypeAndName`。

    生命周期很短:每次要算一批表达式就建一个。它持有

    * ``ctx`` —— 查询上下文(设置、当前库、子查询入口);
    * ``scope`` —— 名字解析;
    * ``rows`` —— 当前 block 的行数(常量列要撑到这个行数)。

    .. warning::

       **行数不一致是这一层最容易出的 bug。** 常量列
       (``ColumnConst``)的 ``size()`` 是显式设的,而普通列是数出来的。
       如果一个常量列忘了 ``resize(rows)``,它就会在后续的
        ``filter`` 里长度对不上 —— 报错还算好,怕的是长度刚好相等
        (比如都是 1)而结果全错。所以这里有一条硬规则:
       **任何从这个类流出去的列,size() 必须等于 rows()。**
    """

    def __init__(
        self,
        ctx: Any,
        scope: Scope,
        rows: int = 1,
        aggregate_result: Optional[Dict[int, str]] = None,
        subquery_executor: Optional[Callable[[A.SelectQuery], "Any"]] = None,
        lambda_scope: Optional[Scope] = None,
        expression_lookup: Optional[Any] = None,
    ) -> None:
        self.ctx = ctx
        self.scope = scope
        self.rows = rows
        #: 聚合索引 → 状态列名(聚合后的求值用)
        self.aggregate_result = aggregate_result or {}
        #: 标量子查询的执行入口(由解释器注入)
        self.subquery_executor = subquery_executor
        self.lambda_scope = lambda_scope
        #: **表达式文本 → 已算好的列**。这是"分阶段求值"的粘合剂:
        #: 聚合后的阶段里,``sum(x)`` 不该再算一遍(它根本算不了),
        #: 而是来这张表里取"已经算好的那一列"。
        #: 见 :class:`minich.execution.analyzer.ExpressionLookup`。
        self.expression_lookup = expression_lookup
        #: 常量折叠的次数(测试与 EXPLAIN 会看)
        self.constant_folds = 0

    # ── 主入口 ──────────────────────────────────────────────────────────

    def evaluate(self, node: A.Node) -> ColumnWithTypeAndName:
        # ── 分阶段求值的入口:先查"这个表达式是不是已经算过了?" ───────
        # 聚合后的阶段里,``sum(x)``、``toDate(ts)`` 这类表达式不允许
        # 也不应该被重新求值 —— 它们的结果已经在聚合阶段算好了。
        if self.expression_lookup is not None:
            hit = self.expression_lookup.get(node)
            if hit is not None:
                return self._fit(hit, getattr(hit, "name", "expression"))
        method = _DISPATCH.get(type(node))
        if method is None:
            for cls, fn in _DISPATCH.items():
                if isinstance(node, cls):
                    method = fn
                    break
        if method is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Expression evaluator does not support {type(node).__name__}",
            )
        result = method(self, node)
        self._check_size(result, node)
        return result

    def evaluate_all(self, nodes: Sequence[A.Node]) -> List[ColumnWithTypeAndName]:
        return [self.evaluate(n) for n in nodes]

    def _check_size(self, col: ColumnWithTypeAndName, node: A.Node) -> None:
        if col.column.size() != self.rows:
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"Expression {node.to_sql()!r} produced {col.column.size()} rows "
                f"but the block has {self.rows} rows",
            )

    def _const(self, value: Any, dtype: DataType) -> ColumnWithTypeAndName:
        """造一个长度为 ``rows`` 的常量列(可选地做常量折叠)。"""
        inner = dtype.create_column()
        inner.append(value)
        const = ColumnConst(inner, self.rows)
        return ColumnWithTypeAndName(_const_name(value), dtype, const)

    # ── 各种节点 ────────────────────────────────────────────────────────

    def eval_identifier(self, node: A.Identifier) -> ColumnWithTypeAndName:
        """标识符 —— 名字解析按三步走(与真实 ClickHouse 的规则对齐)。

        .. code-block:: text

           ① 精确匹配                ``e.user``  → 找一个就叫 "e.user" 的列
                                    (JOIN 之后的右表列就是这种名字)
           ② 去掉限定前缀再找        ``e.user``  → 找 "user"
                                    (左表的列在 JOIN 后仍然是裸名)
           ③ 元组字段访问            ``t.name``  → 找到列 ``t`` 且它的类型是
                                    具名元组、里面有 name 这个字段
                                    → 变成 tupleElement(t, 'name')

        为什么第 ③ 步放在最后? 因为它**改变了语义**(从"取一列"变成
        "取元组的一个字段"),而前两步只是名字匹配。真实 ClickHouse
        在分析期就把 ``t.name`` 解析成 ``tupleElement``,因为它那时
        已经有完整的类型信息;我们只能按这个顺序试探。
        """
        name = node.name
        col = self.scope.resolve_column(name)
        if col is not None:
            return self._fit(col, name)
        if "." in name:
            head, _, tail = name.rpartition(".")
            col = self.scope.resolve_column(tail)
            if col is not None:
                return self._fit(col, tail)
            base = self.scope.resolve_column(head)
            if base is not None:
                from .expression import _tuple_field  # noqa: F401  (自引用占位)

                return self._eval_tuple_field(base, tail, name)
        # 别名:``SELECT a+1 AS b ... WHERE b > 2`` —— 把 b 展开成 a+1
        alias = self.scope.resolve_alias(name)
        if alias is not None:
            return self.evaluate(alias)
        if self.scope.is_lambda_param(name):
            raise_error(
                ErrorCode.UNKNOWN_IDENTIFIER,
                f"Lambda parameter '{name}' is used outside of the lambda body",
            )
        raise_error(
            ErrorCode.UNKNOWN_IDENTIFIER,
            f"Unknown identifier: {name!r}. Available: "
            f"{', '.join(sorted(self.scope.available_names())) or '(none)'}",
        )

    def _eval_tuple_field(self, base: ColumnWithTypeAndName, field: str,
                          display: str) -> ColumnWithTypeAndName:
        """``t.name`` —— 具名元组的字段访问。"""
        from ..types.composite import DataTypeTuple

        dtype = base.type
        if not isinstance(dtype, DataTypeTuple) or not dtype.names:
            raise_error(
                ErrorCode.UNKNOWN_IDENTIFIER,
                f"Unknown identifier: {display!r}. Available: "
                f"{', '.join(sorted(self.scope.available_names())) or '(none)'}",
            )
        idx = dtype.get_subcolumn_index(field)
        if idx < 0:
            raise_error(
                ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                f"Tuple {dtype.get_name()} has no element named {field!r}",
            )
        return self._tuple_element(base, idx)

    def _fit(self, col: ColumnWithTypeAndName, name: str) -> ColumnWithTypeAndName:
        """把列调整到当前行数(常量列需要 resize)。"""
        if col.column.size() == self.rows:
            return col
        if col.column.is_const():
            return ColumnWithTypeAndName(col.name, col.type, ColumnConst(col.column.data.clone(), self.rows))
        if col.column.size() == 1 and self.rows != 1:
            # 单行结果被用在多行上下文 —— 真实 ClickHouse 会报
            # SIZES_OF_COLUMNS_DOESNT_MATCH;我们给出更明确的提示
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"Column '{name}' has 1 row but {self.rows} are required",
            )
        raise_error(
            ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
            f"Column '{name}' has {col.column.size()} rows but {self.rows} are required",
        )

    def eval_literal(self, node: A.Literal) -> ColumnWithTypeAndName:
        if node.type_name:
            dtype = get_data_type(node.type_name)
        else:
            dtype = infer_literal_type(node.value)
        try:
            normalized = dtype.normalize(node.value)
        except MiniChError:
            normalized = node.value
        return self._const(normalized, dtype)

    def eval_parameter(self, node: A.ParameterExpr) -> ColumnWithTypeAndName:
        params = getattr(self.ctx, "query_parameters", None) or {}
        if node.name not in params:
            raise_error(
                ErrorCode.UNKNOWN_IDENTIFIER,
                f"Query parameter '{{{node.name}:{node.type_name}}}' was not provided "
                f"by the client",
            )
        dtype = get_data_type(node.type_name)
        return self._const(dtype.normalize(params[node.name]), dtype)

    def eval_aggregate_ref(self, node: AggregateRef) -> ColumnWithTypeAndName:
        state_name = self.aggregate_result.get(node.index)
        if state_name is None:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"Aggregate reference {node.index} has no state column",
            )
        col = self.scope.resolve_column(state_name)
        if col is None:
            raise_error(
                ErrorCode.NOT_FOUND_COLUMN_IN_BLOCK,
                f"Aggregate state column {state_name!r} is missing",
            )
        return self._fit(col, state_name)

    def eval_binary(self, node: A.BinaryOp) -> ColumnWithTypeAndName:
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)
        left, right = _coerce_stringly_pair(left, right, node)
        return self.call_function(operator_to_function(node.op), [left, right], node)

    #: **一元**运算符的名字与二元的不同。``-x`` 是 ``negate(x)``(一元取负),
    #: 不是 ``minus(x, y)``(二元减法);``+x`` 是 ``positive(x)``。
    #: 漏掉这个区分会让 ``arraySort(x -> -x, [...])`` 报
    #: "Number of arguments for function minus doesn't match" ——
    #: 一条把读者指向"参数个数"、而真实原因是"用错了函数"的消息。
    _UNARY_FUNCTIONS = {"-": "negate", "+": "positive", "NOT": "not", "~": "bitNot",
                        "!": "not"}

    def eval_unary(self, node: A.UnaryOp) -> ColumnWithTypeAndName:
        operand = self.evaluate(node.operand)
        fn = self._UNARY_FUNCTIONS.get(node.op) or operator_to_function(node.op)
        return self.call_function(fn, [operand], node)

    def eval_function(self, node: A.FunctionCall) -> ColumnWithTypeAndName:
        """函数调用 —— 这一层要处理四类特殊函数,其余走注册表。"""
        name = node.name.lower()

        # ① 聚合函数出现在这里,说明它没被抽出来 —— 那是分析期的问题
        if aggregate_function_exists(name) or aggregate_function_exists(_base_name(name)):
            raise_error(
                ErrorCode.ILLEGAL_AGGREGATION,
                f"Aggregate function {node.name} cannot be evaluated row-wise. "
                f"It must appear in SELECT/HAVING/ORDER BY of an aggregating query.",
            )

        # ② 惰性求值的分支函数:不能把两个分支都算出来
        if name == "if":
            return self._eval_if(node)
        if name in ("multiif", "casewithexpression"):
            return self._eval_multi_if(node)

        # ③ 带 lambda 的高阶函数:第一个参数不求值,交给 LambdaArgument
        if _is_higher_order(name):
            return self._eval_higher_order(node)

        # ④ 带参数但不带 lambda 的(例如 extractAllGroups)、以及普通函数
        return self._eval_plain_function(node)

    def _eval_plain_function(self, node: A.FunctionCall) -> ColumnWithTypeAndName:
        args = [self.evaluate(a) for a in node.args]
        params = [self._eval_constant_param(p) for p in node.params]
        return self.call_function(node.name, args, node, params)

    def _eval_constant_param(self, node: A.Node) -> Any:
        """函数参数必须是常量(``quantile(0.5)(x)`` 里的 0.5)。"""
        if isinstance(node, A.Literal):
            return node.value
        val = self._try_constant(node)
        if val is _NOT_CONSTANT:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Function parameters must be constants, got {node.to_sql()!r}",
            )
        return val

    def call_function(
        self,
        name: str,
        args: List[ColumnWithTypeAndName],
        node: Optional[A.Node] = None,
        params: Optional[List[Any]] = None,
    ) -> ColumnWithTypeAndName:
        """调用一个**标量**函数(统一入口,含 NULL 传播与常量折叠)。

        顺序很重要:

        1. 全常量参数 → **常量折叠**(在分析期算出结果,之后每行都不用再算);
        2. 否则查注册表,拿到 ``FunctionBase``;
        3. 交给 ``execute_with_null_propagation`` 处理 NULL。

        .. note::

           第 1 步是 ClickHouse 性能的关键之一,但它有一条**安全性约束**:
           只有 ``is_deterministic()`` 的函数才能折叠。
           ``now()``、``rand()``、``generateUUIDv4()`` **不能** ——
           否则整条查询里所有 ``now()`` 会变成同一个时刻,
           长查询会看到明显偏差(第 15 章有一个例子)。
        """
        from ..functions.base import execute_with_null_propagation

        if not function_exists(name):
            raise_error(
                ErrorCode.UNKNOWN_FUNCTION,
                f"Unknown function {name}"
                + (f" called as {node.to_sql()}" if node is not None else ""),
            )
        fn = get_function(name, params or [], self._arg_types(args))

        # ── 常量折叠 ────────────────────────────────────────────────────
        column_args = [a for a in args if isinstance(a, ColumnWithTypeAndName)]
        # ``materialize()`` 的**全部意义**就是"把这个常量展开成完整列",
        # 所以它必须豁免常量折叠 —— 否则 ``isConstant(materialize(1))``
        # 还是 1、``toColumnTypeName(materialize(1))`` 还是 ColumnConst,
        # 这个函数等于不存在。真实 ClickHouse 也是靠"声明为不可折叠"
        # 做到这一点的(它的 ``FunctionMaterialize`` 返回
        # ``isSuitableForConstantFolding() == false``)。
        foldable = (fn.is_suitable_for_constant_folding()
                    and name.lower() != "materialize")
        if column_args and all(a.column.is_const() for a in column_args) and foldable:
            folded = self._try_fold(fn, args)
            if folded is not None:
                self.constant_folds += 1
                return folded

        rows = self.rows
        try:
            column, return_type = execute_with_null_propagation(fn, args, rows, self.ctx)
        except MiniChError as exc:
            retried = self._retry_with_trailing_params(name, args, params, exc)
            if retried is None:
                raise
            column, return_type = retried
        # 结果列名只由**列实参**拼成 —— lambda 实参没有 .name
        return ColumnWithTypeAndName(
            _call_name(fn.name, [a for a in args if isinstance(a, ColumnWithTypeAndName)]),
            return_type, column,
        )

    def _retry_with_trailing_params(
        self, name: str, args: List[Any], params: Optional[List[Any]], exc: MiniChError,
    ) -> Optional[tuple]:
        """把**尾部常量实参**挪成函数参数,再试一次。

        .. warning::

           这是一个**兼容垫片**,补的是真实 ClickHouse 自己的一个签名歧义。

           ClickHouse 的函数参数分两类:

           * **实参(argument)** —— 逐行的数据,``toDecimal64(x, 4)`` 里的 ``x``;
           * **参数(parameter)** —— 决定"用哪个重载/结果类型"的常量,
             写在**第一个括号**里,例如 ``quantile(0.9)(x)`` 的 ``0.9``。

           问题是 ClickHouse 自己**两边都接受**:``toDecimal64(x, 4)`` 与
           ``toDecimal64(4)(x)`` 都能用,``toFixedString(s, 8)``、
           ``defaultValueOfTypeName('UInt8')`` 也一样。

           mini-clickhouse 的 :meth:`FunctionBase.get_return_type` 只拿到
           实参的**类型**、拿不到值 —— 而 ``Decimal(?, 4)`` 的精度必须
           由那个值决定。所以这些函数被实现成"参数形态"。

           这个垫片就是那条桥:如果一次调用因为**实参个数**失败,而尾部
           恰好是常量列,就把它们挪进参数里重试一次。代价是多一次尝试;

           收益是**读者不必知道这个内部区别**就能写出自然的那一行 SQL。
           真实 ClickHouse 不需要它,因为它的 ``getReturnTypeImpl``
           拿到的是完整的列(既有类型也有值)。
        """
        from ..errors import ErrorCode as _EC
        from ..functions.base import execute_with_null_propagation

        if exc.code != _EC.NUMBER_OF_ARGUMENTS_DOESNT_MATCH or params:
            return None
        head = list(args)
        trailing: List[Any] = []
        while len(head) > 1:
            last = head[-1]
            if not isinstance(last, ColumnWithTypeAndName) or not last.column.is_const():
                break
            if last.column.size() == 0:
                break
            trailing.insert(0, last.column.get(0))
            head.pop()
        if not trailing:
            return None
        try:
            fn2 = get_function(name, trailing, self._arg_types(head))
            return execute_with_null_propagation(fn2, head, self.rows, self.ctx)
        except MiniChError:
            return None

    def _try_fold(
        self, fn: FunctionBase, args: List[ColumnWithTypeAndName]
    ) -> Optional[ColumnWithTypeAndName]:
        """尝试把全常量调用折叠成一个常量列。

        失败(比如除零、类型不支持)时返回 ``None`` 并**放弃折叠**,
        让它按普通路径逐行执行 —— 因为逐行执行会产生**正确的报错**
        (例如 ``divide by zero``),而在分析期报错会掩盖"这一行本来
        不会被算到"的事实(``if(x != 0, 1/x, 0)`` 就是这种情况)。
        """
        from ..functions.base import execute_with_null_propagation

        try:
            expanded = [
                ColumnWithTypeAndName(a.name, a.type, _one_row(a.column))
                for a in args if isinstance(a, ColumnWithTypeAndName)
            ]
            column, return_type = execute_with_null_propagation(fn, expanded, 1, self.ctx)
            if column.size() != 1:
                return None
            return ColumnWithTypeAndName(
                _call_name(fn.name, args), return_type, ColumnConst(column, self.rows)
            )
        except MiniChError:
            return None
        except Exception:
            return None

    def _try_constant(self, node: A.Node) -> Any:
        """尝试把一个表达式**求值成常量**(用于函数参数)。"""
        save_rows = self.rows
        self.rows = 1
        try:
            col = self.evaluate(node)
            if col.column.size() != 1:
                return _NOT_CONSTANT
            return col.column.get(0)
        except MiniChError:
            return _NOT_CONSTANT
        finally:
            self.rows = save_rows

    # ── 惰性求值 ────────────────────────────────────────────────────────

    def _eval_if(self, node: A.FunctionCall) -> ColumnWithTypeAndName:
        """``if(cond, then, else)`` —— **只算被选中的那一侧**。

        为什么必须惰性? 看这个真实场景:

        .. code-block:: sql

           SELECT if(x != 0, 1 / x, 0) FROM t;

        如果先算 ``1/x`` 再挑,``x = 0`` 的行会触发除零。
        ClickHouse 的语义是"不会" —— 因为它按**列**分块求值:
        先算条件,再对条件为真的那些行算 then 分支。
        mini-clickhouse 用同样的策略(见下面的实现)。

        .. note::

           真实 ClickHouse 的实现比这更细:它在 ``ExpressionActions``
           阶段就把常量分支提出来了。我们这里用"逐列 mask 求值",
           语义一致,代价是每行都要走一次 Python 调用。
        """
        if len(node.args) < 3:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"Function 'if' requires 3 arguments, got {len(node.args)}",
            )
        cond = self.evaluate(node.args[0])
        cond_type = cond.type
        if cond_type.is_nullable():
            cond_type = cond_type.nested  # type: ignore[attr-defined]
        if not cond_type.is_numeric():
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Illegal type {cond.type.get_name()} of the first argument of 'if'",
            )
        then_col = self.evaluate(node.args[1])
        else_col = self.evaluate(node.args[2])
        return_type = _common_type([then_col.type, else_col.type])
        out = return_type.create_column()
        plain_then = then_col.column.convert_to_full_column()
        plain_else = else_col.column.convert_to_full_column()
        for i in range(self.rows):
            c = cond.column.get(i)
            out.append(plain_then.get(i) if c else plain_else.get(i))
        name = f"if({node.args[0].to_sql()}, {node.args[1].to_sql()}, {node.args[2].to_sql()})"
        return ColumnWithTypeAndName(name, return_type, out)

    def _eval_multi_if(self, node: A.FunctionCall) -> ColumnWithTypeAndName:
        """``multiIf(c1, v1, c2, v2, ..., default)`` —— 依次短路。"""
        args = node.args
        if len(args) < 3 or len(args) % 2 == 0:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "Function 'multiIf' requires an odd number of arguments >= 3",
            )
        # 参数布局是 ``c1, v1, c2, v2, …, else`` —— **参数个数一定是奇数**。
        # 条件在偶数下标 0,2,4,…(到 len-2);值在奇数下标 1,3,5,…;
        # 最后一个参数(len-1,偶数下标)是 else 分支。
        # ⚠️ 用 ``range(1, len(args), 2)`` 取值是**错的** ——
        # 它会把 else 漏掉,于是所有行都落到"第一个 then 分支"。
        conds = [self.evaluate(args[i]) for i in range(0, len(args) - 1, 2)]
        values = [self.evaluate(args[i]) for i in range(1, len(args) - 1, 2)]
        values.append(self.evaluate(args[-1]))
        return_type = _common_type([v.type for v in values])
        out = return_type.create_column()
        plains = [v.column.convert_to_full_column() for v in values]
        for i in range(self.rows):
            chosen = len(values) - 1    # 默认走最后一个
            for k, cond in enumerate(conds):
                if cond.column.get(i):
                    chosen = k
                    break
            out.append(plains[chosen].get(i))
        return ColumnWithTypeAndName("multiIf(...)", return_type, out)

    def _eval_higher_order(self, node: A.FunctionCall) -> ColumnWithTypeAndName:
        """高阶函数 —— 第一个参数是 lambda,不求值。"""
        if not node.args:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"Higher-order function {node.name} requires a lambda argument",
            )
        lambda_node = node.args[0]
        if not isinstance(lambda_node, A.Lambda):
            # **有一批"高阶"函数其实两种写法都支持。**
            #
            #   arraySort([3,1,2])             ← 不带 lambda:按值自己排
            #   arraySort(x -> -x, [3,1,2])    ← 带 lambda:按 key 排
            #   arrayReduce('sum', [1,2,3])    ← 第一个参数是**函数名字符串**
            #
            # 它们被登记在"高阶函数"集合里,是因为**有一个带 lambda 的重载**,
            # 而不是因为 lambda 是必需的。早期版本在这里无条件要求 lambda,
            # 于是 `arraySort([3,1,2])` 报
            # "The first argument of arraySort must be a lambda" ——
            # 而它的 docstring 里举的第一个例子就是不带 lambda 的那种写法。
            #
            # 不认识 lambda 的实现(或第一个参数不是 lambda 的调用)就交回
            # 普通函数那条路,由函数自己决定怎么处理。
            return self._eval_plain_function(node)
        rest = [self.evaluate(a) for a in node.args[1:]]

        def make_body(row_scope: Optional[Scope]) -> Any:
            def body(**bound: IColumn) -> IColumn:
                inner = Scope(parent=row_scope if row_scope is not None else self.scope,
                              lambda_params=set(lambda_node.params))
                for pname, col in bound.items():
                    # 形参列的行数 = 当前 lambda 调用的"元素数"
                    inner.add_column(pname, ColumnWithTypeAndName(pname, _infer_col_type(col), col))
                rows = next(iter(bound.values())).size() if bound else self.rows
                sub = Evaluator(
                    self.ctx, inner, rows,
                    aggregate_result=self.aggregate_result,
                    subquery_executor=self.subquery_executor,
                    lambda_scope=inner,
                )
                return sub.evaluate(lambda_node.body).column

            return body

        params = [self._eval_constant_param(p) for p in node.params]

        # ── lambda **捕获了外层列**时,必须逐行求值 ────────────────────────
        #
        # .. warning::
        #
        #    这是本项目里最容易"静默算错"的一处。看这一行::
        #
        #        SELECT arrayMap(x -> x + number, [1,2,3]) FROM numbers(3)
        #
        #    期望三行分别是 ``[1,2,3]``、``[2,3,4]``、``[3,4,5]`` ——
        #    ``number`` 是**当前行**的值。
        #
        #    但高阶函数的实现是"逐元素调 lambda":它把整列 ``number``
        #    (0,1,2)当成一个数组交给 lambda,于是三行都算成 ``[1,3,5]``。
        #    类型对、行数对、不报错,只是每行都一样。
        #
        #    修法:检测到 body 引用了外层作用域里的列时,改为**按行**
        #    求值 —— 每一行建一个"只有这一行值"的作用域(用单值常量列),
        #    再调一次函数。代价是 O(行数) 次调用,但正确性优先;
        #    而且只有"捕获外层列"这一种写法会走这条慢路。
        captures_outer = _references_outer_scope(lambda_node.body, set(lambda_node.params), self.scope)
        if captures_outer and self.rows > 1:
            results: List[Any] = []
            out_type: Optional[DataType] = None
            for row in range(self.rows):
                row_scope = _row_scope(self.scope, row)
                lam = LambdaArgument(list(lambda_node.params), make_body(row_scope))
                one_row = self.call_function(node.name, [lam] + rest, node, params)  # type: ignore[list-item]
                out_type = out_type or one_row.type
                results.append(one_row.column.get(0) if one_row.column.size() else None)
            col = (out_type or get_data_type("String")).create_column()
            for v in results:
                col.append_default() if v is None else col.append(v)
            return ColumnWithTypeAndName(
                _call_name(node.name, rest), out_type or get_data_type("String"), col
            )

        lam = LambdaArgument(list(lambda_node.params), make_body(None))
        return self.call_function(node.name, [lam] + rest, node, params)  # type: ignore[list-item]

    def _arg_types(self, args: Sequence[Any]) -> List[DataType]:
        """取出实参里**真正的列**的类型(跳过 lambda)。

        高阶函数把 lambda 混在实参列表里传下去,而
        ``get_function(name, params, arg_types)`` 只要类型 ——
        所以这里必须过滤,否则任何高阶函数都会在
        ``LambdaArgument.type`` 上崩掉。
        """
        return [a.type for a in args if isinstance(a, ColumnWithTypeAndName)]

    # ── 复合字面量 ──────────────────────────────────────────────────────

    def eval_array(self, node: A.ArrayLiteral) -> ColumnWithTypeAndName:
        if not node.items:
            # **空数组字面量是一个"值",不是"零行"**。
            # ``SELECT []`` 的结果是"一行,值是空数组";而
            # ``dtype.create_column()`` 造出来的是**零行**的列,于是解释器的
            # 行数校验会报 ``Expression '[]' produced 0 rows but the block has
            # 1 rows`` —— 一条完全合法的查询直接失败。
            # 正确做法是把它当成**常量列**(值 `[]`,长度 = 当前行数)。
            dtype = DataTypeArray(DataTypeNothing())
            return self._const([], dtype)
        elements = [self.evaluate(i) for i in node.items]
        elem_types = [e.type for e in elements]
        # 行数不同(常量 + 列)→ 按最大行数广播
        rows = max(e.column.size() for e in elements) if elements else 0
        if rows == 0:
            return ColumnWithTypeAndName("[]", DataTypeArray(_common_type(elem_types)),
                                         DataTypeArray(_common_type(elem_types)).create_column())
        all_same_row = all(e.column.size() == rows for e in elements)
        if all_same_row and rows == self.rows and self.rows > 1:
            # `SELECT [a, b] FROM t` —— 每一行是 (a_i, b_i)
            elem_type = _common_type(elem_types)
            out_type = DataTypeArray(elem_type)
            out = out_type.create_column()
            plains = [e.column.convert_to_full_column() for e in elements]
            for i in range(self.rows):
                out.append([p.get(i) for p in plains])
            return ColumnWithTypeAndName(_arr_name(node), out_type, out)
        # `SELECT [1,2,3]` 或 `[1,2,3][i]` 这种 —— 整个数组是一个值
        elem_type = _common_type(elem_types)
        values = [
            e.column.get(0) if e.column.is_const() or e.column.size() == 1
            else e.column.to_list()
            for e in elements
        ]
        out_type = DataTypeArray(elem_type)
        out = out_type.create_column()
        for _ in range(self.rows):
            out.append(list(values))
        return ColumnWithTypeAndName(_arr_name(node), out_type, out)

    def eval_tuple(self, node: A.TupleLiteral) -> ColumnWithTypeAndName:
        if not node.items:
            raise_error(ErrorCode.LOGICAL_ERROR, "Empty tuple literal")
        elements = [self.evaluate(i) for i in node.items]
        rows = self.rows
        cols: List[IColumn] = []
        types: List[DataType] = []
        for e in elements:
            c = e.column
            if c.is_const():
                full = c.clone_empty()
                full.insert_many_from(c.data, 0, rows)
                c = full
            elif c.size() == 1 and rows > 1:
                full = c.clone_empty()
                full.insert_many_from(c, 0, rows)
                c = full
            cols.append(c)
            types.append(e.type)
        out_type = DataTypeTuple(types, None)
        return ColumnWithTypeAndName(_tuple_name(node), out_type, ColumnTuple(cols))

    def eval_map(self, node: A.MapLiteral) -> ColumnWithTypeAndName:
        if not node.pairs:
            # 同 ``[]``:``SELECT {}`` 是"一行,值是空 Map"
            dtype = DataTypeMap(get_data_type("String"), get_data_type("String"))
            return self._const({}, dtype)
        keys = [self.evaluate(k) for k, _ in node.pairs]
        vals = [self.evaluate(v) for _, v in node.pairs]
        key_type = _common_type([k.type for k in keys])
        val_type = _common_type([v.type for v in vals])
        out_type = DataTypeMap(key_type, val_type)
        out = out_type.create_column()
        row = {
            keys[i].column.get(0): vals[i].column.get(0) for i in range(len(keys))
        }
        for _ in range(self.rows):
            out.append(dict(row))
        return ColumnWithTypeAndName(_map_name(node), out_type, out)

    def eval_index(self, node: A.IndexExpr) -> ColumnWithTypeAndName:
        """``a[i]`` / ``t.1`` / ``m['k']`` —— 三种含义,按**基类型的类型**分派。"""
        base = self.evaluate(node.base)
        if isinstance(node.index, A.Literal) and isinstance(node.index.value, str) \
                and isinstance(base.type, DataTypeTuple) and base.type.names:
            # ``t.name`` 是具名元组的字段访问
            idx = base.type.get_subcolumn_index(node.index.value)
            if idx < 0:
                raise_error(
                    ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                    f"Tuple {base.type.get_name()} has no element named "
                    f"{node.index.value!r}",
                )
            return self._tuple_element(base, idx)
        index = self.evaluate(node.index)
        if isinstance(base.type, DataTypeMap):
            # ``m['k']`` —— 真实 ClickHouse 里这是 ``mapGet(m, 'k')``。
            # 键不存在时返回**值类型的默认值**(不是 NULL、也不是报错)——
            # 与 ``Array`` 越界返回默认值是同一种设计:ClickHouse 倾向于
            # "给一个零值"而不是"中断查询"。
            #
            # 这里直接算而不调 ``mapGet``,是因为函数库里没有注册它
            # (注册表里只有 ``mapKeys``/``mapValues``/``mapContains``)。
            # 语义与 ``mapGet`` 一致,读者在真实 ClickHouse 里应当用
            # ``m['k']`` 或 ``mapGet(m, 'k')``,两者等价。
            return self._eval_map_get(base, index, node)
        if isinstance(base.type, DataTypeTuple):
            literal_idx = self._try_constant(node.index)
            if isinstance(literal_idx, int) and literal_idx > 0:
                return self._tuple_element(base, literal_idx - 1)
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Tuple index must be a positive integer literal, got "
                f"{node.index.to_sql()!r}",
            )
        return self.call_function("arrayElement", [base, index], node)

    def _eval_map_get(self, base: ColumnWithTypeAndName, index: ColumnWithTypeAndName,
                      node: A.IndexExpr) -> ColumnWithTypeAndName:
        """``m[key]`` —— 逐行查 Map,键不存在时给值类型的默认值。"""
        value_type = base.type.value
        out = value_type.create_column()
        base_col = base.column.convert_to_full_column() if base.column.is_const() else base.column
        for i in range(self.rows):
            mapping = base_col.get(i) or {}
            key = index.column.get(i)
            if key in mapping:
                out.append(mapping[key])
            else:
                out.append_default()
        return ColumnWithTypeAndName(
            f"{base.name}[{index.name}]", value_type, out
        )

    def _tuple_element(self, base: ColumnWithTypeAndName, index: int) -> ColumnWithTypeAndName:
        col = base.column
        if isinstance(col, ColumnTuple):
            sub = col.columns[index]
        elif col.is_const() and isinstance(col.data, ColumnTuple):
            sub = col.data.columns[index]
        else:
            raise_error(
                ErrorCode.ILLEGAL_COLUMN,
                f"Cannot take element {index} of a {type(col).__name__}",
            )
        dtype = base.type.get_subcolumn_type_by_index(index) if hasattr(
            base.type, "get_subcolumn_type_by_index"
        ) else None
        if dtype is None:
            dtype = base.type.elements[index]  # type: ignore[attr-defined]
        name = f"{base.name}.{index + 1}"
        if col.is_const():
            return ColumnWithTypeAndName(name, dtype, ColumnConst(sub, self.rows))
        return ColumnWithTypeAndName(name, dtype, sub)

    def eval_cast(self, node: A.CastExpr) -> ColumnWithTypeAndName:
        arg = self.evaluate(node.expr)
        from ..functions.base import execute_with_null_propagation

        fn = get_function("cast", [node.type_name], [arg.type])
        column, return_type = execute_with_null_propagation(fn, [arg], self.rows, self.ctx)
        return ColumnWithTypeAndName(f"CAST({node.expr.to_sql()}, '{node.type_name}')",
                                     return_type, column)

    def eval_case(self, node: A.CaseExpr) -> ColumnWithTypeAndName:
        if node.operand is not None:
            # CASE x WHEN v THEN r ... —— 改写成 multiIf(x = v, r, ...)
            args: List[A.Node] = []
            for cond, res in node.whens:
                args.append(A.BinaryOp(op="=", left=node.operand, right=cond))
                args.append(res)
            args.append(node.else_expr if node.else_expr is not None else A.Literal(value=None))
            return self._eval_multi_if(A.FunctionCall(name="multiIf", args=args))
        args = []
        for cond, res in node.whens:
            args.append(cond)
            args.append(res)
        args.append(node.else_expr if node.else_expr is not None else A.Literal(value=None))
        return self._eval_multi_if(A.FunctionCall(name="multiIf", args=args))

    def eval_is_null(self, node: A.IsNullExpr) -> ColumnWithTypeAndName:
        expr = self.evaluate(node.expr)
        name = "isNotNull" if node.negated else "isNull"
        return self.call_function(name, [expr], node)

    def eval_in(self, node: A.InExpr) -> ColumnWithTypeAndName:
        """``x IN (...)`` —— **直接求值**,不走函数注册表。

        .. note::

           真实 ClickHouse 把 ``IN`` 编译成一个 ``FunctionIn``,并且
           在准备阶段就把右操作数(常量元组/子查询结果)建成一个
           ``Set`` 对象 —— 之后每一行只做一次哈希查找。
           我们的实现等价,只是省掉了 ``Set`` 这一层抽象。

           **NULL 的语义**(容易踩):``x IN (1, NULL)`` 在
           ``x = 1`` 时为真、在 ``x = 2`` 时结果是 **NULL**(不是假),
           因为"2 可能等于某个 NULL"是不确定的。mini-clickhouse 把它
           简化成"NULL 不匹配任何值",与真实 ClickHouse 在
           ``WHERE`` 里的**可观察结果一致**(NULL 在 WHERE 里被当成假),
           但在 ``SELECT`` 出来看时会是一个 ``UInt8`` 的 0 而不是 NULL。
           第 07 章把这个差异写进了"与真实 ClickHouse 的差异"表。
        """
        left = self.evaluate(node.left)
        rhs = node.right
        if isinstance(rhs, A.SubqueryExpr):
            values = self._subquery_values(rhs.query)
        elif isinstance(rhs, (A.TupleLiteral, A.ArrayLiteral)):
            # ``x IN ('a','c')`` —— 右操作数是一个**元组字面量**,语义是
            # "把它的每一项当作一个候选值",**不是**把整个元组当一个值。
            #
            # 这个区分很容易搞错:只有左操作数本身是元组时
            # (``(a,b) IN ((1,2))``)才是"整元组比较"。
            # 搞错的症状是 ``x IN (...)" **永远返回空** —— 因为拿标量去
            # 和元组比,而且**不报错**。
            values = []
            for item in rhs.items:
                v = self._try_constant(item)
                if v is _NOT_CONSTANT:
                    v = self.evaluate(item).column.get(0)
                values.append((v, None))
        else:
            right = self.evaluate(rhs)
            values = [(right.column.get(i), right.type) for i in range(right.column.size())]
        wanted = set()
        for v, _ in values:
            wanted.add(_hash_key(v))
        out = ColumnVector(np.uint8)
        for i in range(self.rows):
            hit = _hash_key(left.column.get(i)) in wanted
            if node.negated:
                hit = not hit
            out.append(1 if hit else 0)
        name = f"{'notIn' if node.negated else 'in'}({left.name})"
        return ColumnWithTypeAndName(name, get_data_type("UInt8"), out)

    def eval_between(self, node: A.BetweenExpr) -> ColumnWithTypeAndName:
        expr = self.evaluate(node.expr)
        low = self.evaluate(node.low)
        high = self.evaluate(node.high)
        lo = self.call_function("greaterOrEquals", [expr, low], node)
        hi = self.call_function("lessOrEquals", [expr, high], node)
        combined = self.call_function("and", [lo, hi], node)
        if node.negated:
            return self.call_function("not", [combined], node)
        return combined

    def eval_like(self, node: A.LikeExpr) -> ColumnWithTypeAndName:
        left = self.evaluate(node.left)
        pattern = self.evaluate(node.pattern)
        if node.is_regexp:
            name = "match"
        elif node.case_insensitive:
            name = "ilike"
        else:
            name = "like"
        result = self.call_function(name, [left, pattern], node)
        if node.negated:
            return self.call_function("not", [result], node)
        return result

    def eval_interval(self, node: A.IntervalExpr) -> ColumnWithTypeAndName:
        amount = self.evaluate(node.amount)
        value = amount.column.get(0) if amount.column.size() else 0
        dtype = get_data_type(f"Interval{node.kind.capitalize()}")
        return self._const(dtype.normalize(value), dtype)

    def eval_subquery(self, node: A.SubqueryExpr) -> ColumnWithTypeAndName:
        """标量子查询 —— 必须恰好 1 行 1 列(0 行则取默认值)。

        这一条是**与 SQL 标准不同的**:标准里标量子查询返回多行是错误,
        而 ClickHouse 也报错(``Scalar subquery returned more than one row``),
        但**返回 0 行时不报错**,而是给一个默认值(通常是 0 或空串)。
        第 07 章有一个例子说明这个差异会怎么坑人。
        """
        values = self._subquery_values(node.query)
        if len(values) > 1:
            raise_error(
                ErrorCode.ILLEGAL_COLUMN,
                f"Scalar subquery returned more than one row ({len(values)} rows)",
            )
        if not values:
            dtype = self._subquery_type(node.query)
            return self._const(dtype.default_value(), dtype)
        value, dtype = values[0]
        return self._const(dtype.normalize(value), dtype)

    def _subquery_values(self, query: A.SelectQuery) -> List[Tuple[Any, DataType]]:
        if self.subquery_executor is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                "Subqueries require a subquery executor; this evaluator was built "
                "without one",
            )
        result = self.subquery_executor(query)
        column = result.column
        return [(column.get(i), result.type) for i in range(column.size())]

    def _subquery_type(self, query: A.SelectQuery) -> DataType:
        if self.subquery_executor is None:
            raise_error(ErrorCode.NOT_IMPLEMENTED, "Subqueries are not available here")
        return self.subquery_executor(query, header_only=True).type

    def eval_alias(self, node: A.AliasExpr) -> ColumnWithTypeAndName:
        """``expr AS name`` —— 求值内层,然后把结果**改名**。

        .. note::

           别名在这里只影响**结果列的名字**,不影响求值。别名的"作用域"
           (能不能在 ``WHERE``/``GROUP BY`` 里引用)是**分析期**的事
           (见 :mod:`minich.execution.analyzer` 的 ``expand_aliases``)。
           真实 ClickHouse 也是这么分的:``ASTAlias`` 在求值时不产生
           任何额外动作,它只是给列起名字。
        """
        inner = self.evaluate(node.expr)
        return ColumnWithTypeAndName(node.alias or inner.name, inner.type, inner.column)

    def eval_star(self, node: A.StarExpr) -> ColumnWithTypeAndName:
        raise_error(
            ErrorCode.LOGICAL_ERROR,
            "'*' must be expanded into concrete columns before evaluation",
        )

    def eval_lambda(self, node: A.Lambda) -> ColumnWithTypeAndName:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            "A lambda can only be used as an argument of a higher-order function",
        )

    def eval_window(self, node: A.WindowFunction) -> ColumnWithTypeAndName:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Window function {node.to_sql()} must be evaluated by the "
            f"WindowTransform, not the expression evaluator",
        )


_NOT_CONSTANT = object()


#: "长得像字符串但其实有自己类型"的那些类型。
#:
#: 它们都可以从文本构造(``toIPv4('10.0.0.1')``、``toUUID('...')``、
#: ``CAST('a' AS Enum8(...))``),所以拿字符串字面量与它们比较是**自然写法**。
_STRINGLY_TYPES = ("IPv4", "IPv6", "UUID", "Enum8", "Enum16", "FixedString")


def _coerce_stringly_pair(
    left: ColumnWithTypeAndName, right: ColumnWithTypeAndName, node: A.Node,
) -> Tuple[ColumnWithTypeAndName, ColumnWithTypeAndName]:
    """比较运算前,把"字符串 ↔ 字面类型"的一侧转过去。

    .. code-block:: sql

       WHERE ip = '10.0.0.1'            -- IPv4 与 String 比
       WHERE id = '550e8400-...'        -- UUID 与 String 比
       WHERE level = 'error'            -- Enum 与 String 比

    这三行在真实 ClickHouse 里都能跑:字符串字面量会被**隐式转换**成
    另一侧的类型。mini-clickhouse 早期版本会直接报
    ``Illegal types of arguments for function equals: IPv4, String`` ——
    技术上"严格",实际上把最自然的写法拒之门外。

    只转**字面量那一侧**(即常量列),而且只在另一侧确实是那几种类型时转。
    这样做不会掩盖真正的类型错误:两个都是列、类型又不同的时候,仍然报错。
    """
    ln, rn = left.type.get_name(), right.type.get_name()
    if ln == rn:
        return left, right
    for special, other in ((left, right), (right, left)):
        if not special.type.get_name().startswith(_STRINGLY_TYPES):
            continue
        if other.type.get_name() != "String" or not other.column.is_const():
            continue
        converted = _convert_one_literal(other, special.type)
        if converted is None:
            continue
        if special is left:
            return special, converted
        return converted, special
    return left, right


def _convert_one_literal(col: ColumnWithTypeAndName, target: DataType):
    """把一个常量字符串列按目标类型转一下;转不了返回 None(保持原样、让它报错)。"""
    try:
        from ..functions.base import execute_with_null_propagation, get_function

        fn = get_function("cast", [target.get_name()], [col.type])
        out, _ = execute_with_null_propagation(fn, [col], col.column.size(), None)
        return ColumnWithTypeAndName(col.name, target, out)
    except MiniChError:
        return None


def _hash_key(value: Any) -> Any:
    """把值转成可哈希的键(用于 ``IN`` 的集合与 ``DISTINCT``)。"""
    if isinstance(value, list):
        return tuple(_hash_key(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hash_key(v)) for k, v in value.items()))
    if isinstance(value, float) and math.isnan(value):
        # NaN 之间"等于" —— 与 ClickHouse 的分组语义一致
        return ("__nan__",)
    return value


def _one_row(col: IColumn) -> IColumn:
    """取列的第 0 行做成一个单行普通列(常量折叠用)。"""
    out = col.clone_empty()
    if col.size():
        out.append(col.get(0))
    return out


def _infer_col_type(col: IColumn) -> DataType:
    """从**列本身**反推类型 —— 只用在 lambda 形参上。

    .. warning::

       这是一个**有损**操作:列不携带"我是 UInt8 还是 UInt16"的信息,
       所以这里只能猜(int64/float64/String)。真实 ClickHouse 传的是
       带类型的 ``ColumnWithTypeAndName``,不会有这个问题。
       由于 mini-clickhouse 的 lambda 形参类型由函数实现(如
       ``arrayMap``)决定,猜错的代价通常只是数值范围放宽 ——
       但如果你在 lambda 里依赖精确类型(如 ``toTypeName``),就会看到差异。
    """
    if isinstance(col, ColumnVector):
        if col.dtype.kind == "f":
            return get_data_type("Float64")
        if col.dtype.kind == "u":
            return get_data_type("UInt64")
        return get_data_type("Int64")
    if isinstance(col, (ColumnString,)):
        return get_data_type("String")
    if isinstance(col, ColumnArray):
        return DataTypeArray(_infer_col_type(col.nested))
    if isinstance(col, ColumnNullable):
        return DataTypeNullable(_infer_col_type(col.nested))
    if isinstance(col, ColumnTuple):
        return DataTypeTuple([_infer_col_type(c) for c in col.columns], None)
    if isinstance(col, ColumnMap):
        return DataTypeMap(_infer_col_type(col.keys), _infer_col_type(col.values))
    return get_data_type("String")


_HIGHER_ORDER = frozenset({
    "arraymap", "arrayfilter", "arrayfold", "arraycount", "arrayexists",
    "arrayall", "arrayfirst", "arrayfirstindex", "arraylast", "arraylastindex",
    "arraysort", "arrayreversesort", "arraypartialsort", "arraypartialreversesort",
    "arraysum", "arrayavg", "arraymin", "arraymax", "arrayreduce", "arrayfill",
    "arraycompact", "arraycume", "mapapply", "mapfilter", "arrayproduct",
})


def _is_higher_order(name: str) -> bool:
    return name.lower() in _HIGHER_ORDER


def _base_name(name: str) -> str:
    """``sumIf`` → ``sum``(剥掉组合器后缀,用聚合模块的权威拆分)。"""
    from ..aggregates import split_combinators

    base, _, _, _ = split_combinators(name.lower())
    return base


def _const_name(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return repr(value)
    return str(value)


def _call_name(fn_name: str, args: Sequence[Any]) -> str:
    """结果列的默认名字 —— ``plus(a, b)`` 这种形式。

    只接受**列**实参:高阶函数的实参列表里混着 lambda,它没有 ``name``。
    """
    names = [a.name for a in args if isinstance(a, ColumnWithTypeAndName)]
    return f"{fn_name}({', '.join(names)})"


def _arr_name(node: A.ArrayLiteral) -> str:
    return "[" + ", ".join(i.to_sql() for i in node.items) + "]"


def _tuple_name(node: A.TupleLiteral) -> str:
    return "(" + ", ".join(i.to_sql() for i in node.items) + ")"


def _map_name(node: A.MapLiteral) -> str:
    return "{" + ", ".join(f"{k.to_sql()}:{v.to_sql()}" for k, v in node.pairs) + "}"


def _column_of_values(values: List[Tuple[Any, DataType]], target_type: DataType) -> ColumnWithTypeAndName:
    """把一个值列表做成列(``IN`` 的右操作数)。"""
    elem_type = target_type
    if target_type.is_nullable():
        elem_type = target_type.nested  # type: ignore[attr-defined]
    col = elem_type.create_column()
    for v, _ in values:
        col.append(elem_type.normalize(v))
    return ColumnWithTypeAndName("__set__", elem_type, col)


#: 节点类型 → 处理函数
_DISPATCH: Dict[type, Callable[[Evaluator, Any], ColumnWithTypeAndName]] = {
    A.Identifier: Evaluator.eval_identifier,
    A.Literal: Evaluator.eval_literal,
    A.ParameterExpr: Evaluator.eval_parameter,
    AggregateRef: Evaluator.eval_aggregate_ref,
    A.BinaryOp: Evaluator.eval_binary,
    A.UnaryOp: Evaluator.eval_unary,
    A.FunctionCall: Evaluator.eval_function,
    A.ArrayLiteral: Evaluator.eval_array,
    A.TupleLiteral: Evaluator.eval_tuple,
    A.MapLiteral: Evaluator.eval_map,
    A.IndexExpr: Evaluator.eval_index,
    A.CastExpr: Evaluator.eval_cast,
    A.CaseExpr: Evaluator.eval_case,
    A.IsNullExpr: Evaluator.eval_is_null,
    A.InExpr: Evaluator.eval_in,
    A.BetweenExpr: Evaluator.eval_between,
    A.LikeExpr: Evaluator.eval_like,
    A.IntervalExpr: Evaluator.eval_interval,
    A.SubqueryExpr: Evaluator.eval_subquery,
    A.StarExpr: Evaluator.eval_star,
    A.Lambda: Evaluator.eval_lambda,
    A.WindowFunction: Evaluator.eval_window,
    A.AliasExpr: Evaluator.eval_alias,
}


def _row_scope(scope: Scope, row: int) -> Scope:
    """把整个作用域**切成第 ``row`` 行** —— 每一列都变成单值常量列。

    这是"逐行求值"的基础设施:高阶函数的 lambda 捕获外层列时,
    外层列必须退化成"当前行的那一个值",否则整列会被当成一个数组
    参与运算(见 :meth:`Evaluator._eval_higher_order` 的说明)。
    """
    out = Scope(parent=scope.parent)
    for name, col in scope.flatten().items():
        if col.column.size() <= row:
            continue
        inner = col.type.create_column()
        inner.append(col.column.get(row))
        out.add_column(name, ColumnWithTypeAndName(
            name, col.type, ColumnConst(inner, 1)
        ))
    out.aliases = dict(scope.aliases)
    return out


def _references_outer_scope(node: Optional[A.Node], params: set, scope: Scope) -> bool:
    """lambda 体里是否引用了**外层作用域**的列(即捕获)?

    ``x -> x + 1`` 不捕获;``x -> x + number`` 捕获了 ``number``。
    只有捕获的情形才需要走"逐行求值"那条慢路。
    """
    if node is None:
        return False
    if isinstance(node, (A.SubqueryExpr, A.Lambda)):
        return False
    if isinstance(node, A.Identifier):
        return node.name not in params and scope.resolve_column(node.name) is not None
    return any(_references_outer_scope(c, params, scope) for c in node.children())
