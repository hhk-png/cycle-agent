"""谓词改写与下推 —— 表达式从"用户写的"变成"引擎能执行的"。

一个 ``WHERE`` 条件在引擎里会**同时**走三条路，这个模块负责把它们拆开：

1. **标量索引**（:func:`to_index_expr`）——
   BTREE / BITMAP / LABEL_LIST 这些索引只能回答特定形状的问题。
   真实 Lance 的 ``ScalarIndex::search`` 拿到的不是"一棵表达式树"，
   而是 :class:`~mini_lancedb.index.base.ScalarIndexExpr`：区间、集合、
   与或非的代数。索引的职责就是**把谓词翻译成这个代数**，
   答不了就返回 ``None``（"我不行，你全表扫吧"）。
   翻译出来的是**行号集合**，不是逐行布尔值 —— 索引的价值就在于
   它根本不去读那些不可能命中的行所在的页。

2. **zone map / min-max 剪枝**（:func:`extract_zone_map_predicate`）——
   每个数据页/行组带一个 ``(min, max)``。``WHERE ts > '2024-01-01'``
   只要页的 ``max <= '2024-01-01'``，这一整页就不用解码了。
   能参与剪枝的谓词形状比索引还要窄：必须是"列 op 常量"，
   而且常量不能是 NULL。

3. **真正的过滤器**（留在原地，由 :mod:`mini_lancedb.expr.eval` 逐行算）——
   前两条都是"尽量少读"，剩下的部分必须精确执行，不能有近似。

除了拆分，这个模块还负责两件改写工作：

* :func:`simplify` —— 让表达式变短。变短不只是好看：
  ``a > 1 + 2`` 化简成 ``a > 3`` 之后，**索引改写才认得它**（第 1 条路
  只认字面量）；
* :func:`estimate_selectivity` —— 猜"这个条件能筛掉多少行"。
  扫描器靠它决定 prefilter 还是 postfilter：向量检索里，
  如果过滤条件能把 100 万行筛到 1000 行，就**先过滤再搜向量**
  （prefilter）；如果它几乎筛不掉什么，就先做 ANN 再对候选集过滤
  （postfilter）。猜错的代价可能是十倍以上的延迟差距，
  所以这个估计量必须**确定、可解释、可回归测试**。

## 一个必须说清楚的正确性边界

``AND`` 允许"部分下推"：``a = 1 AND b > 2`` 在只有 ``a`` 的索引时，
可以只把 ``a = 1`` 交给索引，剩下 ``b > 2`` 由过滤器补上 —— 因为
索引返回的超集再交一次过滤，结果仍然精确。

``OR`` **不允许**：``a = 1 OR b > 2`` 里只要有一边下推不了，
整个 ``OR`` 就必须放弃（返回 ``None``）。少下推一边会让结果集**变小**，
也就是静默丢数据 —— 这正是查询引擎里最危险的一类 bug。
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence

from ..errors import PlanError
from ..index.base import (
    And as IndexAnd,
    IsIn as IndexIsIn,
    Not as IndexNot,
    Or as IndexOr,
    RangeQuery,
    ScalarIndexExpr,
)
from ..types import BOOL
from .eval import compile_like, fold_constants
from .nodes import (
    COMPARISON_OPS,
    FLIP_COMPARISON,
    Between,
    BinaryOp,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    IsIn,
    IsNull,
    Like,
    Literal,
    UnaryOp,
    literal_value,
)

__all__ = [
    "split_conjuncts",
    "split_disjuncts",
    "to_index_expr",
    "extract_zone_map_predicate",
    "simplify",
    "estimate_selectivity",
    "DEFAULT_SELECTIVITY",
    "DEFAULT_EQ_SELECTIVITY",
    "DEFAULT_RANGE_SELECTIVITY",
    "DEFAULT_NULL_FRACTION",
]


# ---------------------------------------------------------------------------
# 拆分 AND / OR
# ---------------------------------------------------------------------------


def split_conjuncts(expr: Expr) -> List[Expr]:
    """把 ``a AND b AND c`` 摊平成 ``[a, b, c]``。

    ``AND`` 是可交换可结合的单位元运算，摊平之后每一步改写都只需
    处理一层列表，不必递归左右子树 —— 这也是所有查询优化器的标准起手式。
    """

    out: List[Expr] = []

    def rec(e: Expr) -> None:
        if isinstance(e, BinaryOp) and e.op == "AND":
            rec(e.left)
            rec(e.right)
        else:
            out.append(e)

    rec(expr)
    return out


def split_disjuncts(expr: Expr) -> List[Expr]:
    """把 ``a OR b OR c`` 摊平成 ``[a, b, c]``。"""

    out: List[Expr] = []

    def rec(e: Expr) -> None:
        if isinstance(e, BinaryOp) and e.op == "OR":
            rec(e.left)
            rec(e.right)
        else:
            out.append(e)

    rec(expr)
    return out


# ---------------------------------------------------------------------------
# 索引改写
# ---------------------------------------------------------------------------

_MISSING: Any = object()


def _is_column(e: Expr, column: str) -> bool:
    return isinstance(e, ColumnRef) and e.name == column


def _scalar_literal(e: Expr) -> Any:
    """取出"可做索引键"的常量。

    ``None``（SQL NULL）和列表都**不算** —— 前者因为 ``col = NULL``
    在 SQL 里永远不成立（不是"等于空"，是"未知"），把它翻译成索引区间
    会得到一个语义完全不同的结果；后者因为标量索引不索引数组值
    （数组要用 ``LABEL_LIST`` 索引，走的是另一条路）。
    """

    ok, value = literal_value(e)
    if ok and value is not None and not isinstance(value, (list, tuple, dict)):
        return value
    return _MISSING


def _range_for(op: str, value: Any) -> ScalarIndexExpr:
    if op == "=":
        return RangeQuery(value, value, True, True)
    if op == "!=":
        return IndexNot(RangeQuery(value, value, True, True))
    if op == "<":
        return RangeQuery(None, value, True, False)
    if op == "<=":
        return RangeQuery(None, value, True, True)
    if op == ">":
        return RangeQuery(value, None, False, True)
    if op == ">=":
        return RangeQuery(value, None, True, True)
    raise PlanError(f"无法翻译成索引区间的运算符 {op!r}")  # pragma: no cover


def to_index_expr(expr: Expr, column: str) -> Optional[ScalarIndexExpr]:
    """把谓词翻译成"某个标量索引能回答的问题"。

    返回 ``None`` 表示**这个子树索引答不了**，调用方必须回退到全表扫描。

    支持翻译的形状（其余一律 ``None``）：

    =====================================  ==================================
    SQL 谓词                               索引能回答的形式
    =====================================  ==================================
    ``col = 5``                            ``RangeQuery(5, 5)``
    ``col != 5``                           ``Not(RangeQuery(5, 5))``
    ``col < 5``                            ``RangeQuery(None, 5)``
    ``col <= 5`` / ``> 5`` / ``>= 5``      对应的半开区间
    ``col IN (1, 2, 3)``                   ``IsIn([1, 2, 3])``
    ``col NOT IN (...)``                   ``Not(IsIn([...]))``
    ``col BETWEEN a AND b``                ``RangeQuery(a, b)``
    ``col LIKE 'abc%'``                    ``RangeQuery('abc', 'abc\\uffff')``
    ``a AND b``                            各自翻译后取交集（可部分下推）
    ``a OR b``                             各自翻译后取并集（必须全部可译）
    ``NOT a``                              取补（子表达式必须可译）
    =====================================  ==================================

    几条刻意的取舍：

    * ``col = 5 AND other = 1`` 只会翻译出 ``col`` 那部分 —— ``AND``
      返回的是**超集**，下游过滤补上剩余条件仍然精确；
    * ``OR`` 只要有一个分支翻译不了就整体返回 ``None``。理由见模块文档：
      漏掉一个分支会让结果**变少**；
    * 比较运算两边的列必须是同一个名字，``a = b``、``a + 1 = 5`` 都不翻译。
      真实 Lance 也不做这类表达式重写（那是 Paper 里"expression
      simplification"的活儿），能识别 ``col op literal`` 已经覆盖了
      绝大多数真实查询；
    * ``LIKE 'abc%'`` 翻译成区间是**本实现比真实 Lance 多走的一步**
      （真实 Lance 的 BTREE 对 ``LIKE`` 前缀的处理在 ngram/fts 侧）。
      它的正确性依赖"字符串比较等价于字节序比较"，对 UTF-8 成立。
    """

    if not isinstance(column, str) or not column:
        raise PlanError("to_index_expr 需要一个列名", got=repr(column))
    return _to_index(expr, column)


def _to_index(expr: Expr, column: str) -> Optional[ScalarIndexExpr]:
    if isinstance(expr, BinaryOp):
        if expr.op == "AND":
            parts: List[ScalarIndexExpr] = []
            for c in split_conjuncts(expr):
                sub = _to_index(c, column)
                if sub is not None:
                    parts.append(sub)
            if not parts:
                return None
            return parts[0] if len(parts) == 1 else IndexAnd(parts)
        if expr.op == "OR":
            parts = []
            for c in split_disjuncts(expr):
                sub = _to_index(c, column)
                if sub is None:
                    return None  # OR 不能部分下推
                parts.append(sub)
            return parts[0] if len(parts) == 1 else IndexOr(parts)
        if expr.op in COMPARISON_OPS:
            return _comparison_index(expr, column)
        return None

    if isinstance(expr, UnaryOp) and expr.op == "NOT":
        inner = _to_index(expr.operand, column)
        return None if inner is None else IndexNot(inner)

    if isinstance(expr, IsIn):
        if not _is_column(expr.child, column):
            return None
        values = expr.literal_values()
        if values is None or any(v is None for v in values):
            return None
        isin: ScalarIndexExpr = IndexIsIn(list(values))
        return IndexNot(isin) if expr.negated else isin

    if isinstance(expr, Between):
        if not _is_column(expr.child, column):
            return None
        low = _scalar_literal(expr.low)
        high = _scalar_literal(expr.high)
        if low is _MISSING or high is _MISSING:
            return None
        rq: ScalarIndexExpr = RangeQuery(low, high, True, True)
        return IndexNot(rq) if expr.negated else rq

    if isinstance(expr, Like):
        if expr.negated or not _is_column(expr.child, column):
            return None
        pattern = expr.literal_pattern()
        if pattern is None:
            return None
        matcher = compile_like(pattern, expr.escape)
        if matcher.kind == "eq":
            return RangeQuery(pattern, pattern, True, True)
        if matcher.kind == "prefix":
            # 所有以 prefix 开头的字符串都落在 [prefix, prefix + ￿] 里。
            # ￿ 是"比任何真实字符都大"的哨兵（Unicode 非字符区）。
            return RangeQuery(matcher.literal, matcher.literal + "￿", True, True)
        return None

    # IS NULL / 函数 / CAST / 裸常量：标量索引都答不了
    return None


def _comparison_index(expr: BinaryOp, column: str) -> Optional[ScalarIndexExpr]:
    left, right = expr.left, expr.right
    if _is_column(left, column):
        value = _scalar_literal(right)
        if value is _MISSING:
            return None
        return _range_for(expr.op, value)
    if _is_column(right, column):
        value = _scalar_literal(left)
        if value is _MISSING:
            return None
        # ``5 < col`` 翻成 ``col > 5`` 再翻译
        return _range_for(FLIP_COMPARISON[expr.op], value)
    return None


# ---------------------------------------------------------------------------
# zone map（min/max）剪枝
# ---------------------------------------------------------------------------


def extract_zone_map_predicate(expr: Expr) -> List[Expr]:
    """挑出能参与 min/max 剪枝的合取项。

    判定标准比索引改写更严，因为剪枝是"页码级"的粗筛，必须**零歧义**：

    * 必须是合取项（``OR`` 里的一项不能单独用来跳过一页 ——
      那一页对另一个分支可能还是要读的）；
    * 必须是"列 op 常量"，且常量非 NULL；
    * ``IN`` 和 ``BETWEEN`` 也可以（等价于若干区间的并/一个区间）。

    返回的是**原样的子表达式**，不是改写后的形式 —— 扫描器拿它去和页的
    ``(min, max)`` 比大小即可，见 :mod:`mini_lancedb.scanner`（后续章节）。
    """

    out: List[Expr] = []
    for c in split_conjuncts(expr):
        if _zone_map_usable(c):
            out.append(c)
    return out


def _zone_map_usable(e: Expr) -> bool:
    if isinstance(e, IsIn):
        if not isinstance(e.child, ColumnRef):
            return False
        values = e.literal_values()
        return values is not None and all(v is not None for v in values)
    if isinstance(e, Between):
        if not isinstance(e.child, ColumnRef):
            return False
        return (
            _scalar_literal(e.low) is not _MISSING
            and _scalar_literal(e.high) is not _MISSING
        )
    if isinstance(e, BinaryOp) and e.op in COMPARISON_OPS:
        for col_side, lit_side in ((e.left, e.right), (e.right, e.left)):
            if isinstance(col_side, ColumnRef) and _scalar_literal(lit_side) is not _MISSING:
                return True
        return False
    return False


# ---------------------------------------------------------------------------
# 化简
# ---------------------------------------------------------------------------


def _is_bool_literal(e: Expr, value: bool) -> bool:
    return isinstance(e, Literal) and e.value is value


def _is_null_literal(e: Expr) -> bool:
    return isinstance(e, Literal) and e.value is None


def simplify(expr: Expr) -> Expr:
    """化简表达式，直到不动点（最多 4 轮）。

    规则集（全部显式列出，因为它们直接决定 ``explain`` 的输出长什么样）：

    1. **常量折叠**：``a > 1 + 2`` → ``a > 3``；``NULL AND x`` 相关子树整体塌缩；
    2. ``x AND TRUE`` → ``x``，``x AND FALSE`` → ``FALSE``（``FALSE`` 是吸收元）；
    3. ``x OR FALSE`` → ``x``，``x OR TRUE`` → ``TRUE``；
    4. **双重否定**：``NOT NOT x`` → ``x``；
    5. ``NOT TRUE`` → ``FALSE``、``NOT FALSE`` → ``TRUE``、``NOT NULL`` → ``NULL``；
    6. **单元素 ``IN`` 退化成等值**：``x IN (5)`` → ``x = 5``
       （这样它才能被索引改写认出来）；
    7. ``AND`` / ``OR`` 链去重（``a = 1 AND a = 1`` → ``a = 1``）；
    8. ``CAST(CAST(x AS t) AS t)`` → ``CAST(x AS t)``（幂等）。

    **刻意不做**的改写：

    * ``NOT (a = 1)`` → ``a <> 1``：虽然对 NULL 的传播恰好等价
      （两边在 ``a`` 为 NULL 时都是 NULL），但把用户的写法改掉会让
      explain 输出难以对回原查询，收益也小；
    * ``NOT (a AND b)`` 的德摩根展开：会让表达式变长，
      而向量化求值对 ``AND``/``OR`` 的代价几乎一样；
    * 常量不等式夹逼（``a > 1 AND a > 5`` → ``a > 5``）：需要类型感知的
      区间合并，属于后续可以加但收益有限的优化。
    """

    current = expr
    for _ in range(4):
        nxt = _simplify_once(current)
        if _same(nxt, current):
            return nxt
        current = nxt
    return current


def _same(a: Expr, b: Expr) -> bool:
    return type(a) is type(b) and a.to_sql() == b.to_sql()


def _simplify_once(expr: Expr) -> Expr:
    expr = fold_constants(expr)

    if isinstance(expr, (Literal, ColumnRef)):
        return expr

    if isinstance(expr, BinaryOp):
        if expr.op == "AND":
            return _simplify_conjunction(split_conjuncts(expr))
        if expr.op == "OR":
            return _simplify_disjunction(split_disjuncts(expr))
        return BinaryOp(
            expr.op,
            _simplify_once(expr.left),
            _simplify_once(expr.right),
            expr.type,
        )

    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            inner = _simplify_once(expr.operand)
            if isinstance(inner, UnaryOp) and inner.op == "NOT":
                return _simplify_once(inner.operand)
            if isinstance(inner, Literal):
                if inner.value is None:
                    return Literal(None, BOOL)
                if isinstance(inner.value, bool):
                    return Literal(not inner.value, BOOL)
            return UnaryOp("NOT", inner)
        return UnaryOp(expr.op, _simplify_once(expr.operand), expr.type)

    if isinstance(expr, IsNull):
        return IsNull(_simplify_once(expr.child), expr.negated)

    if isinstance(expr, IsIn):
        child = _simplify_once(expr.child)
        values = [_simplify_once(v) for v in expr.values]
        if len(values) == 1:
            return BinaryOp("!=" if expr.negated else "=", child, values[0], BOOL)
        return IsIn(child, values, expr.negated)

    if isinstance(expr, Like):
        return Like(
            _simplify_once(expr.child),
            _simplify_once(expr.pattern),
            expr.negated,
            expr.escape,
        )

    if isinstance(expr, Between):
        return Between(
            _simplify_once(expr.child),
            _simplify_once(expr.low),
            _simplify_once(expr.high),
            expr.negated,
        )

    if isinstance(expr, Cast):
        child = _simplify_once(expr.child)
        if isinstance(child, Cast) and child.target == expr.target:
            return child
        return Cast(child, expr.target)

    if isinstance(expr, FunctionCall):
        return FunctionCall(
            expr.name, [_simplify_once(a) for a in expr.args], expr.type
        )

    return expr  # pragma: no cover


def _simplify_conjunction(items: Sequence[Expr]) -> Expr:
    kept: List[Expr] = []
    seen: set = set()
    for raw in items:
        e = _simplify_once(raw)
        if _is_bool_literal(e, True):  # AND TRUE 可以直接丢掉
            continue
        if _is_bool_literal(e, False):  # FALSE 是 AND 的吸收元
            return Literal(False, BOOL)
        key = e.to_sql()
        if key in seen:
            continue
        seen.add(key)
        kept.append(e)
    if not kept:
        return Literal(True, BOOL)
    acc = kept[0]
    for e in kept[1:]:
        acc = BinaryOp("AND", acc, e, BOOL)
    return acc


def _simplify_disjunction(items: Sequence[Expr]) -> Expr:
    kept: List[Expr] = []
    seen: set = set()
    for raw in items:
        e = _simplify_once(raw)
        if _is_bool_literal(e, False):
            continue
        if _is_bool_literal(e, True):
            return Literal(True, BOOL)
        key = e.to_sql()
        if key in seen:
            continue
        seen.add(key)
        kept.append(e)
    if not kept:
        return Literal(False, BOOL)
    acc = kept[0]
    for e in kept[1:]:
        acc = BinaryOp("OR", acc, e, BOOL)
    return acc


# ---------------------------------------------------------------------------
# 选择率估计
# ---------------------------------------------------------------------------

#: 完全没信息时的默认选择率（"一半行能过"）
DEFAULT_SELECTIVITY = 0.5
#: 等值比较的默认选择率。1/10 是教科书里对均匀分布的常见默认假设
DEFAULT_EQ_SELECTIVITY = 0.1
#: 范围比较的默认选择率。按"均匀分布、区间落在值域 30% 处"估
DEFAULT_RANGE_SELECTIVITY = 0.3
#: ``IS NULL`` 的默认选择率
DEFAULT_NULL_FRACTION = 0.1
#: ``LIKE 'prefix%'`` 的默认选择率（前缀能砍掉一大部分）
DEFAULT_PREFIX_SELECTIVITY = 0.25
#: ``LIKE '%x%'`` / ``'%x'`` 的默认选择率 —— 这种模式**几乎筛不掉数据**
DEFAULT_CONTAINS_SELECTIVITY = 0.5

_NUMERIC_KINDS = ("i", "u", "f")


def estimate_selectivity(expr: Expr, stats: Optional[Mapping[str, Any]] = None) -> float:
    """粗糙但**确定且可解释**的选择率估计，返回 ``[0, 1]``。

    扫描器用它决定 prefilter 还是 postfilter：估计值小（条件很挑）
    就先过滤再搜向量，估计值大就先搜向量再过滤。

    估计规则（全部可复现，同输入必同输出）：

    =====================================  ==================================
    形状                                    估计值
    =====================================  ==================================
    ``FALSE`` / ``TRUE``                    0.0 / 1.0
    ``col = 常量``                           ``1 / ndv(col)``，无统计量则 0.1
    ``col != 常量``                          上面那个的补
    ``col <常量`` 等范围                     0.3；有 min/max 时按"常量落在
                                            值域外"直接给出 0.0 或 1.0
    ``col IN (v1..vn)``                      ``min(1, n × 等值选择率)``
    ``col IS NULL``                          ``null_frac``，无统计量则 0.1
    ``LIKE 'abc%'``                          0.25
    ``LIKE '%abc%'`` / ``'%abc'`` / 复杂正则   0.5（这种条件筛不掉行）
    ``a AND b``                              两个估计值相乘
    ``a OR b``                               两个相加，截断到 1
    ``NOT a``                                ``1 - 估计(a)``
    其它（函数、CAST、列比列）                  0.5（不知道）
    =====================================  ==================================

    ``stats`` 是可选列的统计信息，形如::

        {"price": {"ndv": 200, "rows": 100000, "null_frac": 0.02,
                   "min": 0.0, "max": 999.0}}

    缺统计量时一律回退到默认值 —— 宁可不准，也不能因为"没有统计量"
    就给出 0 或 1 这种会误导扫描器的极端值。
    """

    value = _selectivity(expr, stats or {})
    if value != value:  # NaN 防御
        return DEFAULT_SELECTIVITY
    return float(min(1.0, max(0.0, value)))


def _clamp(v: float) -> float:
    return min(1.0, max(0.0, v))


def _eq_selectivity(column: str, stats: Mapping[str, Any]) -> float:
    s = stats.get(column)
    if isinstance(s, Mapping):
        ndv = s.get("ndv")
        if ndv is not None and float(ndv) > 0:
            return _clamp(1.0 / float(ndv))
    return DEFAULT_EQ_SELECTIVITY


def _column_of(e: Expr) -> Optional[str]:
    if isinstance(e, ColumnRef):
        return e.name
    return None


def _range_selectivity(op: str, column: str, value: Any, stats: Mapping[str, Any]) -> float:
    """``col op 常量`` 的选择率。``op`` 已经是"列在左"的规范方向。"""

    s = stats.get(column)
    if isinstance(s, Mapping) and value is not None:
        lo, hi = s.get("min"), s.get("max")
        if _comparable(lo, value) and _comparable(hi, value):
            # 常量完全落在值域外 —— 这是估计器能给出的最有价值的一类答案，
            # 扫描器看到 0.0 会直接把这一列的数据页全跳过。
            if op == "<":
                if value > hi:
                    return 1.0
                if value <= lo:
                    return 0.0
            elif op == "<=":
                if value >= hi:
                    return 1.0
                if value < lo:
                    return 0.0
            elif op == ">":
                if value < lo:
                    return 1.0
                if value >= hi:
                    return 0.0
            elif op == ">=":
                if value <= lo:
                    return 1.0
                if value > hi:
                    return 0.0
    return DEFAULT_RANGE_SELECTIVITY


def _comparable(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    try:
        a < b  # type: ignore[operator]
        return True
    except TypeError:
        return False


def _selectivity(expr: Expr, stats: Mapping[str, Any]) -> float:
    if isinstance(expr, Literal):
        if expr.value is None:
            return DEFAULT_SELECTIVITY
        if isinstance(expr.value, bool):
            return 1.0 if expr.value else 0.0
        return DEFAULT_SELECTIVITY

    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            return _clamp(1.0 - _selectivity(expr.operand, stats))
        return DEFAULT_SELECTIVITY

    if isinstance(expr, BinaryOp):
        if expr.op == "AND":
            out = 1.0
            for c in split_conjuncts(expr):
                out *= _selectivity(c, stats)
            return _clamp(out)
        if expr.op == "OR":
            out = 0.0
            for c in split_disjuncts(expr):
                out += _selectivity(c, stats)
            return _clamp(out)
        if expr.op == "=":
            for col_side, lit_side in ((expr.left, expr.right), (expr.right, expr.left)):
                name = _column_of(col_side)
                if name is not None and _scalar_literal(lit_side) is not _MISSING:
                    return _eq_selectivity(name, stats)
            return DEFAULT_SELECTIVITY
        if expr.op == "!=":
            for col_side, lit_side in ((expr.left, expr.right), (expr.right, expr.left)):
                name = _column_of(col_side)
                if name is not None and _scalar_literal(lit_side) is not _MISSING:
                    return _clamp(1.0 - _eq_selectivity(name, stats))
            return DEFAULT_SELECTIVITY
        if expr.op in ("<", "<=", ">", ">="):
            for col_side, lit_side in ((expr.left, expr.right), (expr.right, expr.left)):
                name = _column_of(col_side)
                value = _scalar_literal(lit_side)
                if name is not None and value is not _MISSING:
                    op = FLIP_COMPARISON[expr.op] if col_side is expr.right else expr.op
                    return _range_selectivity(op, name, value, stats)
            return DEFAULT_SELECTIVITY
        return DEFAULT_SELECTIVITY

    if isinstance(expr, IsNull):
        name = _column_of(expr.child)
        null_frac = DEFAULT_NULL_FRACTION
        s = stats.get(name) if name else None
        if isinstance(s, Mapping) and s.get("null_frac") is not None:
            null_frac = float(s["null_frac"])
        return _clamp(1.0 - null_frac if expr.negated else null_frac)

    if isinstance(expr, IsIn):
        name = _column_of(expr.child)
        values = expr.literal_values()
        if name is None or values is None:
            return DEFAULT_SELECTIVITY
        eq = _eq_selectivity(name, stats)
        total = _clamp(len(values) * eq)
        return _clamp(1.0 - total if expr.negated else total)

    if isinstance(expr, Between):
        name = _column_of(expr.child)
        if name is None:
            return DEFAULT_SELECTIVITY
        low = _scalar_literal(expr.low)
        high = _scalar_literal(expr.high)
        if low is _MISSING or high is _MISSING:
            return DEFAULT_RANGE_SELECTIVITY
        base = min(
            _range_selectivity(">=", name, low, stats),
            _range_selectivity("<=", name, high, stats),
        )
        return _clamp(1.0 - base if expr.negated else base)

    if isinstance(expr, Like):
        pattern = expr.literal_pattern()
        if pattern is None:
            return DEFAULT_SELECTIVITY
        matcher = compile_like(pattern, expr.escape)
        if matcher.kind == "eq":
            name = _column_of(expr.child)
            base = _eq_selectivity(name, stats) if name else DEFAULT_EQ_SELECTIVITY
        elif matcher.kind == "prefix":
            base = DEFAULT_PREFIX_SELECTIVITY
        else:
            base = DEFAULT_CONTAINS_SELECTIVITY
        return _clamp(1.0 - base if expr.negated else base)

    return DEFAULT_SELECTIVITY
