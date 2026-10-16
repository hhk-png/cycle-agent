"""``KeyCondition`` —— 把 ``WHERE`` 变成"该读哪几段数据"。

这是整个 MergeTree 里**最值钱**的一段代码,因为它实现了 ClickHouse
最著名的那条能力:**在海量数据上做实时点查**。理解它,就理解了
"为什么 ClickHouse 快"的一半。

.. code-block:: text

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  一条铁律:                                                          ║
   ║                                                                      ║
   ║    **能用索引加速的过滤列,必须是排序键的"前缀"。**                    ║
   ╚══════════════════════════════════════════════════════════════════════╝

   举一个贯穿全文的例子。表 ``ORDER BY (date, id)``,数据按 ``(date, id)``
   字典序排好,分成若干 granule(每个 8192 行):

   .. code-block:: text

     granule:  0          1          2          3      …     24
     date:  2024-01-01 2024-01-01 2024-01-02 2024-01-02 …
     id:     0..8191   8192..16383  0..8191   8192..16383

   ── 查询 A ───────────────────────────────────────────────────────────────
     ``SELECT count() FROM t WHERE date >= '2024-01-02'``

     ``date`` 是排序键的**第 1 列** → 可以在 ``primary.idx`` 上二分,
     直接定位到 granule 2:

       primary.idx:   date=2024-01-01  date=2024-01-01  date=2024-01-02 …
                            ▲                ▲                ▲
                         mark 0           mark 1           mark 2 ← 二分到这里

     于是只读 granule 2..24 → **读一半数据**。date 上再细分能读到更少。

   ── 查询 B ───────────────────────────────────────────────────────────────
     ``SELECT count() FROM t WHERE id = 5``

     ``id`` 是排序键的**第 2 列**。``id=5`` 可能出现在**任何一个**
     date 分组里(因为每个 date 下都有一批 id),所以:

       granule 0 里 id 有 0..8191  ← 有 5
       granule 1 里 id 有 8192..   ← 没有,但**无法排除**(下一个 date 又有 0..)
       granule 2 里 id 有 0..8191  ← 有 5
       ……

     **没有一个 granule 能被排除 → 全表扫描。**

   这就是"过滤列必须是排序键前缀"的字面含义。mini-clickhouse 的
   :class:`KeyCondition` 忠实地实现了这条规则 —— 见
   :meth:`KeyCondition._usable_node_count`。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  KeyCondition 的内部结构:一条**节点链**,每个节点对应一个排序键表达式  │
   └──────────────────────────────────────────────────────────────────────┘

     ORDER BY (date, id)   →   节点 0: date     节点 1: id
                                    │              │
     WHERE date >= X AND id = 5 ────┘              │
                                    │              │
                              ranges = [X, +∞)      │  ← 节点 0 不是"点"
                                                   │
                              节点 0 不是点 → 节点 1 **不可用**(被截断)
                              最终过滤范围 = (date ∈ [X, +∞), 其余不限制)

     WHERE date = X AND id = 5 ────┬──────────────┘
                                   │
                             ranges = [X, X] ← 是"点"!
                                   │
                              节点 1 可用 → 最终范围 = (date=X, id=5) ← 精确定位

   注意上面第二条:一旦前面的键列被**等值**约束成一个"点",
   后面的键列就"活"了 —— 因为剩下要扫的数据已经限定在
   "date = X" 这一小段里,在这段里 id 是**有序**的。
   这就是为什么 `WHERE a = ? AND b = ?` 能精确命中,而
   `WHERE a > ? AND b = ?` 只能用上 `a`。

   .. note::

      真实 ClickHouse 的 ``KeyCondition`` 还支持:
      ① ``monotonic functions`` —— ``toYYYYMM(date)`` 因为单调,
         对它取范围可以换算回 ``date`` 的范围;
      ② 每个节点最多 7 层函数链;③ ``Field`` 级别的 min/max 缓存。
      mini-clickhouse 实现了 ①(见 :data:`MONOTONIC_KEY_FUNCTIONS`),
      并把链深度限制为 1 层(单个函数套单个列),这一点在
      :func:`describe_key_expression` 里标注了。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ...block import Block
from ...errors import ErrorCode, raise_error
from ...types import DataType, get_data_type
from ...util.keys import normalize_key_value
from .expression import (
    evaluate_function_value,
    function_result_type,
    is_monotonic_column_function,
)
from .marks import MarkRange, RANGE_MAX, merge_ranges
from .part import sorting_key_value

__all__ = [
    "Range",
    "KeyConditionBuildInformation",
    "KeyFunctionSpec",
    "KeyCondition",
    "CheckResult",
    "PartitionPruner",
    "compare_index_values",
    "describe_key_expression",
    "MONOTONIC_KEY_FUNCTIONS",
]


#: 我们认得的"单调键函数" —— 名字来自真实 ClickHouse 的 monotonicity 注册表。
MONOTONIC_KEY_FUNCTIONS: Set[str] = {
    "toDate", "toDate32", "toDateTime",
    "toYYYYMM", "toYYYYMMDD", "toYYYYMMDDhhmmss",
    "toStartOfMonth", "toStartOfYear", "toStartOfWeek", "toMonday",
    "toStartOfDay", "toStartOfHour", "toStartOfMinute",
    "toYear", "toMonth", "toDayOfMonth", "toQuarter", "toHour",
    "intDiv", "toUInt8", "toUInt16", "toUInt32", "toUInt64",
    "toInt32", "toInt64", "toDate32OrNull", "identity",
}

#: 一个节点上最多保留几个区间 —— 防止 ``a != 1 AND b != 2 AND …`` 组合出
#: 指数级的区间(真实 ClickHouse 同样会限制 ``max_rows_in_set``/区间数)。
MAX_RANGES_PER_NODE = 16


class _Sentinel:
    """±∞ 哨兵 —— 用来把"只约束了排序键前缀"的边界补齐到完整元数。

    Python 的元组比较是从左到右、遇到第一个不等就返回,所以
    ``(2,)`` 与 ``(2, 0)`` 谁大是**没有意义**的(类型都不是同一个长度)。
    真实 ClickHouse 在 ``Field``/``Tuple`` 比较里同样需要"这一列没有约束"
    这个概念(它用 ``Field::isNull``/``NegativeInfinity`` 之类)。

    实现要点是让哨兵与**任何**真实值都能比较:

    .. code-block:: text

       -∞ < x  对一切 x 成立(x 是真实值)
       +∞ > x  对一切 x 成立
       -∞ == -∞,+∞ == +∞(同一个单例)

    反向比较(``x < -∞``)会走到 Python 的**反射**协议,即调用
    ``_Sentinel.__gt__`` —— 所以四个比较运算都要定义。
    """

    __slots__ = ("lower",)

    def __init__(self, lower: bool) -> None:
        self.lower = lower

    def __eq__(self, other: object) -> bool:
        return other is self

    def __ne__(self, other: object) -> bool:
        return other is not self

    def __lt__(self, other: object) -> bool:
        if other is self:
            return False
        return self.lower          # -∞ 小于一切;+∞ 不小于任何东西

    def __gt__(self, other: object) -> bool:
        if other is self:
            return False
        return not self.lower      # +∞ 大于一切;-∞ 不大于任何东西

    def __le__(self, other: object) -> bool:
        return self is other or self.lower

    def __ge__(self, other: object) -> bool:
        return self is other or not self.lower

    def __hash__(self) -> int:  # pragma: no cover
        return hash(self.lower)

    def __repr__(self) -> str:  # pragma: no cover
        return "-inf" if self.lower else "+inf"


#: 补在"未被约束的键列"上的两个哨兵。
NEG_INF = _Sentinel(True)
POS_INF = _Sentinel(False)


def _key_arity(part: Any) -> int:
    """这个 part 的排序键有多少列(``primary.idx`` 里每个元组的长度)。"""
    idx = getattr(part, "primary_index", None) or []
    if idx:
        return len(idx[0])
    rev = getattr(part, "sorting_key_reverse", None) or []
    return len(rev)


def _pad_bound(part: Any, values: Sequence[Any], arity: int,
               lower: bool) -> Tuple[tuple, int]:
    """把"只约束了前缀"的边界补齐到 ``arity`` 个元素,返回 ``(元组, 有效长度)``。

    ★ 补的哨兵**取决于该列的方向**。因为 ``compare_index_values`` 是
    在**索引序**里比较的(对 DESC 列会翻转),所以"排在最前面的值"
    对不同方向的列是不同的符号:

    ============================  ==============================  ============
    列方向                          该列在索引序里的"起点"        补什么
    ============================  ==============================  ============
    ``ASC``                       值最小                        ``-∞``
    ``DESC``                      值最大                        ``+∞``
    ============================  ==============================  ============

    补错方向不会崩,但会让二分落错位置 —— 与"用前缀比较"是同一类错误
    (要么多读一点,要么**漏读**)。
    """
    vals = list(values)
    reverse = getattr(part, "sorting_key_reverse", None) or []
    for i in range(len(vals), max(arity, len(vals))):
        rev = bool(reverse[i]) if i < len(reverse) else False
        if lower:
            vals.append(POS_INF if rev else NEG_INF)
        else:
            vals.append(NEG_INF if rev else POS_INF)
    return tuple(vals), max(arity, len(values))


# ══════════════════════════════════════════════════════════════════════════
# 值区间
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Range:
    """键值域上的一个区间 —— ``None`` 表示该侧无界。

    ``left_inclusive``/``right_inclusive`` 只在 **同侧有界** 时有意义。
    """

    left: Any = None
    right: Any = None
    left_inclusive: bool = True
    right_inclusive: bool = True

    def is_point(self) -> bool:
        """是否是一个"点"(等值约束)—— 这决定了下一个节点能不能用。"""
        if self.left is None or self.right is None:
            return False
        if not (self.left_inclusive and self.right_inclusive):
            return False
        return _key_eq(self.left, self.right)

    def contains_value(self, value: Any) -> bool:
        """值是否落在区间内 —— 分区裁剪就是靠它逐个判定。"""
        if self.left is not None:
            c = _key_cmp(value, self.left)
            if c < 0 or (c == 0 and not self.left_inclusive):
                return False
        if self.right is not None:
            c = _key_cmp(value, self.right)
            if c > 0 or (c == 0 and not self.right_inclusive):
                return False
        return True

    def intersect_with(self, other: "Range") -> Optional["Range"]:
        left, li = _max_left(self, other)
        right, ri = _min_right(self, other)
        r = Range(left, right, li, ri)
        if left is not None and right is not None:
            c = _key_cmp(left, right)
            if c > 0 or (c == 0 and not (li and ri)):
                return None
        return r

    def describe(self) -> str:  # pragma: no cover - 调试用
        l = ("-inf" if self.left is None else repr(self.left)) + \
            ("" if self.left is None else ("" if self.left_inclusive else "("))
        r = ("" if self.right is None else ("" if self.right_inclusive else ")")) + \
            ("+inf" if self.right is None else repr(self.right))
        return f"[{l}, {r}]"

    def __repr__(self) -> str:  # pragma: no cover
        return f"Range{self.describe()}"


#: 一个节点上的区间集合(析取 —— 任一成立即可)。
RangeSet = List[Range]


def _max_left(a: Range, b: Range) -> Tuple[Any, bool]:
    if a.left is None:
        return b.left, b.left_inclusive
    if b.left is None:
        return a.left, a.left_inclusive
    c = _key_cmp(a.left, b.left)
    if c > 0:
        return a.left, a.left_inclusive
    if c < 0:
        return b.left, b.left_inclusive
    return a.left, a.left_inclusive and b.left_inclusive


def _min_right(a: Range, b: Range) -> Tuple[Any, bool]:
    if a.right is None:
        return b.right, b.right_inclusive
    if b.right is None:
        return a.right, a.right_inclusive
    c = _key_cmp(a.right, b.right)
    if c < 0:
        return a.right, a.right_inclusive
    if c > 0:
        return b.right, b.right_inclusive
    return a.right, a.right_inclusive and b.right_inclusive


def _key_cmp(a: Any, b: Any) -> int:
    """比较两个键值 —— 处理 None/NaN,且**不抛异常**。"""
    na, nb = normalize_key_value(a), normalize_key_value(b)
    if na == nb:
        return 0
    try:
        return -1 if na < nb else 1
    except TypeError:  # pragma: no cover
        return -1 if repr(na) < repr(nb) else 1


def _key_eq(a: Any, b: Any) -> bool:
    return _key_cmp(a, b) == 0


def _union_range_sets(a: RangeSet, b: RangeSet) -> RangeSet:
    """两个区间集合求并(析取)。

    实现遵循"**结果必须是并集的超集**"这一条:合并相邻区间时,
    即使两个区间之间只差一个点(比如 ``(.., 5)`` 与 ``(5, ..)``),
    也合并成一个连续区间 —— 多读一个点不会出错,漏一个点才会。

    区间数超过上限时返回"全范围"(``[Range()]``),等于放弃优化 ——
    这是安全的方向。
    """
    if len(a) + len(b) > MAX_RANGES_PER_NODE:
        return [Range()]
    out = sorted(a + b, key=lambda r: (_sort_key(r.left) if r.left is not None else (-1, 0)))
    merged: RangeSet = []
    for r in out:
        if merged and _mergeable(merged[-1], r):
            prev = merged[-1]
            right, right_inc = _right_union(prev, r)
            merged[-1] = Range(prev.left, right, prev.left_inclusive, right_inc)
        else:
            merged.append(r)
    return [r for r in merged if not _is_empty_range(r)] or [Range()]


def _mergeable(a: Range, b: Range) -> bool:
    if a.right is None or b.left is None:
        return True
    return _key_cmp(a.right, b.left) >= 0


def _right_union(a: Range, b: Range) -> Tuple[Any, bool]:
    if a.right is None or b.right is None:
        return None, True
    c = _key_cmp(a.right, b.right)
    if c > 0:
        return a.right, a.right_inclusive
    if c < 0:
        return b.right, b.right_inclusive
    return a.right, a.right_inclusive or b.right_inclusive


def _intersect_range_sets(a: RangeSet, b: RangeSet) -> RangeSet:
    out: RangeSet = []
    for x in a:
        for y in b:
            r = x.intersect_with(y)
            if r is not None:
                out.append(r)
    if len(out) > MAX_RANGES_PER_NODE:
        return [Range()]
    return out


# ══════════════════════════════════════════════════════════════════════════
# 键描述
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class KeyFunctionSpec:
    """一个"作用在键列上的单调函数" —— 例如 ``toYYYYMM(d)`` 的 ``toYYYYMM``。

    单调函数能参与裁剪的**理由**很简单:

    .. code-block:: text

       如果 f 是非递减的,那么 "f(d) 满足某个条件" 就能换算成
       "d 落在某个区间里":

         f(d) <= c    ⟹    d <= f⁻¹(c) 的上界
         f(d) >= c    ⟹    d >= f⁻¹(c) 的下界

       我们这里做的是**正向**推导(把 d 的区间映射成 f(d) 的区间),
       它总是取一个**保守的超集**:

         d ∈ [lo, hi]  ⟹  f(d) ∈ [f(lo), f(hi)]   (非递减 ⇒ 成立)

       超集意味着"可能多读一点,但绝不会漏" —— 这正是裁剪该有的方向。
    """

    name: str
    #: 函数参数(``intDiv(d, 100)`` 里的 ``100``)
    params: List[Any] = field(default_factory=list)
    #: 内层列的**名字** —— 用来匹配 ``WHERE d ...`` 里的 ``d``
    column: str = ""
    #: 内层列的类型
    column_type: Optional[DataType] = None
    #: 结果的类型
    result_type: Optional[DataType] = None

    def forward_range(self, r: Range) -> Range:
        """把一个"内层列上的区间"放大成"函数结果上的区间"(保守超集)。"""
        lo = None if r.left is None else self._apply(r.left)
        hi = None if r.right is None else self._apply(r.right)
        # 映射后两端都**含** —— 因为非递减函数会把"开"的那一侧也保留下来,
        # 含进去只会让区间更大(更保守),不会漏。
        return Range(lo, hi, True, True)

    def _apply(self, value: Any) -> Any:
        from .expression import MONOTONIC_COLUMN_FUNCTIONS

        if self.name not in MONOTONIC_COLUMN_FUNCTIONS:
            # 没有实现这个函数的**取值语义** → 退化成 identity。
            # 区间不缩小 = 超集 = 安全(只是少了裁剪能力)。
            return value
        try:
            return evaluate_function_value(
                self.name, [value],
                [self.column_type or get_data_type("UInt64")], self.params,
            )
        except Exception:
            return value


@dataclass
class KeyConditionBuildInformation:
    """构造 :class:`KeyCondition` 需要的全部信息 —— "这张表的键长什么样"。

    真实 ClickHouse 里这些信息散在 ``MergeTreeData`` + ``SortDescription`` +
    ``KeyCondition`` 的构造参数里;我们把它们收进一个 dataclass,
    这样 :class:`KeyCondition` 可以独立被构造与测试。
    """

    #: 每个键表达式的 SQL 文本(用于和 ``WHERE`` 里的表达式比对)
    key_exprs: List[str] = field(default_factory=list)
    #: 每个键表达式的**值类型**
    key_types: List[DataType] = field(default_factory=list)
    #: 每个键表达式的方向(``ORDER BY a DESC`` → True)
    reverse: List[bool] = field(default_factory=list)
    #: 键表达式是"函数"时,内层列的名字(不是函数时为空串)
    key_columns: List[str] = field(default_factory=list)
    #: 键表达式是"函数"时的函数描述(不是函数时为空)
    key_functions: List[Optional[KeyFunctionSpec]] = field(default_factory=list)
    #: 供调试与 ``EXPLAIN`` 展示
    description: str = ""

    def node_count(self) -> int:
        return len(self.key_exprs)

    def ensure_consistent(self) -> None:
        n = len(self.key_exprs)
        if len(self.key_types) != n or len(self.reverse) != n:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                "KeyConditionBuildInformation: key_exprs/key_types/reverse "
                "must have the same length",
            )
        if not self.key_columns:
            self.key_columns = [""] * n
        if not self.key_functions:
            self.key_functions = [None] * n


def describe_key_expression(
    expr: Any,
    header: Block,
    index_granularity: int = 8192,
) -> Tuple[str, str, Optional[KeyFunctionSpec], DataType]:
    """把一个排序键表达式拆成"它是什么"。

    支持两种形态(与真实 ClickHouse 的能力差距写在下面):

    .. code-block:: text

       ①  纯列        d            → (expr_sql='d', column='d', func=None, type=Date)
       ②  单层函数    toYYYYMM(d)   → (expr_sql='toYYYYMM(d)', column='d',
                                      func=KeyFunctionSpec('toYYYYMM'), type=UInt32)

    .. warning::

       真实 ClickHouse 支持**任意深度**的单调函数链
       (``toStartOfDay(toDate(ts))`` 这类),因为它给每个函数都注册了
       monotonicity 描述并能逐层推导。mini-clickhouse 只支持**一层**
       (一个函数套一个列);更深的表达式会被当作"不可用"—— 即
       "这个键列不参与裁剪",结果是**退化成多读一点**,而不是错误。
    """
    from ...sql import ast_ as A

    if isinstance(expr, A.Identifier):
        col = header.try_get_by_name(expr.name)
        if col is None:
            raise_error(
                ErrorCode.UNKNOWN_IDENTIFIER,
                f"Sorting key column {expr.name!r} does not exist in the table",
            )
        return expr.to_sql(), expr.name, None, col.type

    if isinstance(expr, A.CastExpr):
        inner_sql, col_name, func, _ = describe_key_expression(expr.expr, header,
                                                              index_granularity)
        target = get_data_type(expr.type_name)
        return expr.to_sql(), col_name, func, target

    if isinstance(expr, A.FunctionCall):
        if len(expr.args) != 1:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Sorting key expression {expr.to_sql()} is not a single-argument "
                f"monotonic function; mini-clickhouse only supports one level",
            )
        arg = expr.args[0]
        if not isinstance(arg, A.Identifier):
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Sorting key expression {expr.to_sql()} must wrap a plain column",
            )
        col = header.try_get_by_name(arg.name)
        if col is None:
            raise_error(
                ErrorCode.UNKNOWN_IDENTIFIER,
                f"Sorting key column {arg.name!r} does not exist in the table",
            )
        if not is_monotonic_column_function(expr.name):
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Function {expr.name} in the sorting key is not registered as "
                f"monotonic; mini-clickhouse cannot use it for index lookups. "
                f"Known monotonic functions: "
                f"{', '.join(sorted(MONOTONIC_KEY_FUNCTIONS))}",
            )
        params = [_literal_of(p) for p in expr.params]
        result_type = function_result_type(expr.name, [col.type], params)
        spec = KeyFunctionSpec(expr.name, params, arg.name, col.type, result_type)
        return expr.to_sql(), arg.name, spec, result_type

    raise_error(
        ErrorCode.NOT_IMPLEMENTED,
        f"Unsupported sorting key expression: {expr.to_sql()}",
    )


def _literal_of(node: Any) -> Any:
    from ...sql import ast_ as A

    if isinstance(node, A.Literal):
        return node.value
    return None


# ══════════════════════════════════════════════════════════════════════════
# CheckResult
# ══════════════════════════════════════════════════════════════════════════


class CheckResult:
    """``KeyCondition`` 对一个 mark 区间给出的三种答案。

    真实 ClickHouse 的 ``KeyCondition::checkInRange`` 正是这三种:

    .. code-block:: text

       FALSE            这个 mark 区间里**一定**没有匹配的行 → 整段跳过
                        (计数到 ``SelectedMarks`` **之前**就被砍掉)

       PARTIALLY_TRUE   可能有 → 读出来,再逐行过滤
                        (这才是 ``SelectedMarks`` 真正计入的那些)

       TRUE             这个 mark 区间里**每一行**都匹配 → 读出来,
                        **可以跳过 WHERE 过滤**(省掉一次表达式求值)

    ``mayBeTrueInRange`` 与 ``alwaysTrueInRange`` 的区别就是
    ``TRUE`` 与 ``PARTIALLY_TRUE`` 的差别 —— ``EXPLAIN`` 里看到的
    "能省的谓词"与 ``SelectedMarks`` 的计数都取决于这两个函数。
    """

    FALSE = "False"
    PARTIALLY_TRUE = "PartiallyTrue"
    TRUE = "True"


# ══════════════════════════════════════════════════════════════════════════
# KeyCondition
# ══════════════════════════════════════════════════════════════════════════


class KeyCondition:
    """把 ``WHERE`` 累积成一条"键的区间链"。

    用法::

        kc = KeyCondition(build_info)
        kc.add(where_expr)                 # 可以多次 add(等价于 AND)
        kc.attach_part(part)               # 绑定一个 part 才能算 mark range
        for mr in kc.get_ranges():         # ← 该读的 mark 区间
            ...

    也可以不绑定 part,只用 :meth:`check_key_value` 在**值域**上判断
    (分区裁剪走的就是这条路)。
    """

    def __init__(self, build_information: KeyConditionBuildInformation) -> None:
        self.info = build_information
        self.info.ensure_consistent()
        #: 每个节点上的区间集合;``None`` 表示"这个节点还没有条件"
        self._node_ranges: List[Optional[RangeSet]] = [None] * self.info.node_count()
        self._part = None
        self._cached_usable = None
        self._cached_ranges: Optional[List[MarkRange]] = None

    # ── 构造 ────────────────────────────────────────────────────────────

    def add(self, expr: Any, is_prewhere: bool = False) -> None:
        """把一棵 ``WHERE`` 表达式并进来。

        ``is_prewhere`` 在真实 ClickHouse 里用于 ``PREWHERE`` 优化
        (先过滤再读其它列);对 ``KeyCondition`` 而言,它只是记录了
        "这个条件来自 PREWHERE",区间计算完全一样 —— 所以我们只保留参数。
        """
        if expr is None:
            return
        self._part = None
        self._cached_usable = None
        self._cached_ranges = None
        self._add(expr)

    def add_and(self, exprs: Sequence[Any]) -> None:
        for e in exprs:
            self.add(e)

    # ── 内部:遍历表达式 ─────────────────────────────────────────────────

    def _add(self, node: Any) -> None:
        from ...sql import ast_ as A

        if isinstance(node, A.BinaryOp) and node.op.upper() == "AND":
            self._add(node.left)
            self._add(node.right)
            return
        if isinstance(node, A.BinaryOp) and node.op.upper() == "OR":
            self._add_or(node)
            return
        if isinstance(node, A.UnaryOp) and node.op.upper() == "NOT":
            # NOT 会把语义整体翻转 —— 我们无法安全地从中抽区间(与跳过索引
            # 遇到 OR 同理),直接放弃这一支,退化成"全扫"。
            return
        self._add_comparison(node)

    def _add_or(self, node: Any) -> None:
        """``A OR B`` —— 两边都能建出**同一组**节点的区间时,做并集。

        如果两边约束的节点不一致(比如一边约束节点 0、另一边约束节点 1),
        或者任一边根本建不出区间,我们**整支放弃** —— 保证不产生 false negative。
        """
        left = self._sub_condition(node.left)
        right = self._sub_condition(node.right)
        if left is None or right is None:
            return
        for i in range(self.info.node_count()):
            lr, rr = left[i], right[i]
            if lr is None or rr is None:
                # 任一侧这一节点没有条件 → 说明这一支可以"任意取值",
                # 并集就是"全范围",等于没有条件 → 该节点放弃
                if lr is None and rr is None:
                    continue
                self._node_ranges[i] = None
                continue
            self._merge_or(i, _union_range_sets(lr, rr))

    def _sub_condition(self, node: Any) -> Optional[List[Optional[RangeSet]]]:
        """在一个干净的 KeyCondition 上跑一遍,拿到它的节点区间(用于 OR)。"""
        sub = KeyCondition(self.info)
        sub._add(node)
        result = sub._node_ranges
        if all(r is None for r in result):
            return None
        return result

    def _merge_or(self, node_index: int, ranges: RangeSet) -> None:
        """把一组区间与节点已有的区间求**并**(OR 语义)。"""
        existing = self._node_ranges[node_index]
        if existing is None:
            self._node_ranges[node_index] = ranges
        else:
            self._node_ranges[node_index] = _union_range_sets(existing, ranges)

    def _merge_and(self, node_index: int, ranges: RangeSet) -> None:
        """把一组区间与节点已有的区间求**交**(AND 语义)。"""
        existing = self._node_ranges[node_index]
        if existing is None:
            self._node_ranges[node_index] = ranges
        else:
            self._node_ranges[node_index] = _intersect_range_sets(existing, ranges)

    # ── 内部:单条条件 ───────────────────────────────────────────────────

    def _add_comparison(self, node: Any) -> None:
        from ...sql import ast_ as A

        if isinstance(node, A.BinaryOp) and node.op in ("=", "==", "!=", "<>", "<", "<=", ">", ">="):
            self._add_binary_compare(node)
            return
        if isinstance(node, A.InExpr):
            self._add_in(node)
            return
        if isinstance(node, A.BetweenExpr) and not node.negated:
            self._add_between(node)
            return
        # 其它条件(LIKE、函数、IS NULL…)对键的区间没有直接贡献 —— 忽略
        return

    def _match_node(self, side: Any) -> Optional[Tuple[int, bool]]:
        """判断一个表达式是不是某个键表达式的"左操作数"。

        返回 ``(节点号, 是否走了函数正向映射)``。
        匹配分三级:

        1. 与 ``key_exprs[i]`` **完全相同** → 节点 i,**不需要**映射;
        2. 与 ``key_columns[i]`` 相同(且该键是个函数) → 节点 i,需要映射;
        3. 都不匹配 → ``None``。
        """
        if side is None:
            return None
        try:
            sql = side.to_sql()
        except Exception:  # pragma: no cover
            return None
        for i, ksql in enumerate(self.info.key_exprs):
            if sql == ksql:
                return (i, False)
        for i, col in enumerate(self.info.key_columns):
            if col and sql == col and self.info.key_functions[i] is not None:
                return (i, True)
        return None

    def _bound_value(self, node_index: int, mapped: bool, value: Any) -> Any:
        """把字面量规范化到"区间所在的域"。"""
        dtype = (self.info.key_functions[node_index].column_type
                 if mapped and self.info.key_functions[node_index] is not None
                 else self.info.key_types[node_index])
        if value is None or dtype is None:
            return value
        try:
            return dtype.normalize(value)
        except Exception:
            return value

    def _add_binary_compare(self, node: Any) -> None:
        op = node.op
        left_match = self._match_node(node.left)
        right_match = self._match_node(node.right)
        if left_match is not None and right_match is not None:
            return  # 比较两个键列 —— 抽不出区间
        if left_match is not None:
            idx, mapped = left_match
            value = _const_of(node.right)
            if value is _NO:
                return
        elif right_match is not None:
            idx, mapped = right_match
            value = _const_of(node.left)
            if value is _NO:
                return
            op = _SWAP_OPS.get(op, op)
        else:
            return
        v = self._bound_value(idx, mapped, value)
        ranges = _ranges_for_operator(op, v)
        if ranges is None:
            return
        self._apply_node_ranges(idx, mapped, ranges)

    def _add_in(self, node: Any) -> None:
        m = self._match_node(node.left)
        if m is None:
            return
        idx, mapped = m
        values = _const_list_of(node.right)
        if values is None:
            return
        norm = [self._bound_value(idx, mapped, v) for v in values]
        if node.negated:
            # NOT IN → 补集(多个区间)。区间太多就直接放弃。
            ranges = _complement_ranges(norm)
            if ranges is None:
                return
        else:
            ranges = [Range(v, v, True, True) for v in norm]
        self._apply_node_ranges(idx, mapped, ranges)

    def _add_between(self, node: Any) -> None:
        m = self._match_node(node.expr)
        if m is None:
            return
        idx, mapped = m
        lo = _const_of(node.low)
        hi = _const_of(node.high)
        if lo is _NO or hi is _NO:
            return
        lo = self._bound_value(idx, mapped, lo)
        hi = self._bound_value(idx, mapped, hi)
        self._apply_node_ranges(idx, mapped, [Range(lo, hi, True, True)])

    def _apply_node_ranges(self, node_index: int, mapped: bool, ranges: RangeSet) -> None:
        spec = self.info.key_functions[node_index]
        if mapped:
            if spec is None:
                return
            ranges = [spec.forward_range(r) for r in ranges]
        self._merge_and(node_index, ranges)

    # ── 节点可用性:排序键前缀规则 ───────────────────────────────────────

    def _usable_node_count(self) -> int:
        """多少**前缀**节点可以被用来做裁剪。

        规则(与真实 ClickHouse 的 ``KeyCondition`` 语义一致):

        .. code-block:: text

           node 0 有条件吗?
             否 → 返回 0(整表扫描)
             是 → node 0 可用
                  node 0 是"点"(等值)吗?
                    否 → 返回 1(只能裁到 node 0 的粒度)
                    是 → 看 node 1,递归
        """
        if self._cached_usable is not None:
            return self._cached_usable
        usable = 0
        for i in range(self.info.node_count()):
            rs = self._node_ranges[i]
            if rs is None:
                break
            usable = i + 1
            if not _range_set_is_point(rs):
                break
        self._cached_usable = usable
        return usable

    def has_usable_ranges(self) -> bool:
        return self._usable_node_count() > 0

    # ── 值域判定(分区裁剪用)────────────────────────────────────────────

    def check_key_value(self, value: Any) -> CheckResult:
        """一个"键值"(单列时是标量,多列时是元组)是否落在过滤范围内。"""
        usable = self._usable_node_count()
        if usable == 0:
            return CheckResult.TRUE
        values = value if isinstance(value, tuple) else (value,)
        for i in range(usable):
            rs = self._node_ranges[i]
            v = values[i] if i < len(values) else None
            if not any(r.contains_value(v) for r in rs):
                return CheckResult.FALSE
        # 是否"必定匹配":每个节点都是点,且值正好等于那个点
        if all(_range_set_is_point(self._node_ranges[i]) for i in range(usable)):
            return CheckResult.TRUE
        return CheckResult.PARTIALLY_TRUE

    # ── mark range ──────────────────────────────────────────────────────

    def attach_part(self, part: Any) -> None:
        """绑定一个 part(拿它的 ``primary.idx`` 与行数)。"""
        self._part = part
        self._cached_ranges = None

    def get_ranges(self) -> List[MarkRange]:
        """返回该读的 mark 区间(必须已经 :meth:`attach_part`)。

        这是 ``EXPLAIN`` 与 ``SelectedMarks`` 的最终来源。
        """
        if self._part is None:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                "KeyCondition.get_ranges() requires a data part to be attached "
                "(call attach_part(part) first)",
            )
        if self._cached_ranges is not None:
            return self._cached_ranges
        self._cached_ranges = self._compute_mark_ranges(self._part)
        return self._cached_ranges

    def _tuple_ranges(self) -> List[Tuple[Range, ...]]:
        """把可用的前缀节点展开成"元组区间"的列表(笛卡尔积)。"""
        usable = self._usable_node_count()
        if usable == 0:
            return []
        combos: List[Tuple[Range, ...]] = [()]
        for i in range(usable):
            rs = self._node_ranges[i]
            new_combos: List[Tuple[Range, ...]] = []
            for c in combos:
                for r in rs:
                    new_combos.append(c + (r,))
            combos = new_combos
            if len(combos) > MAX_RANGES_PER_NODE * 4:
                # 区间组合太多 —— 放弃(返回一个"不限制后面的列"的宽区间)
                combos = [c + (Range(),) for c in combos[:MAX_RANGES_PER_NODE]]
                break
        return combos

    def _compute_mark_ranges(self, part: Any) -> List[MarkRange]:
        """把"元组区间"翻译成"mark 区间"。

        翻译的关键是**两段式**:

        .. code-block:: text

           [lo, hi]  (键值域)
                │  在 primary.idx 上二分(稀疏索引 —— 每个 granule 一条记录)
                ▼
           [begin, end)  (mark 号)

           begin = 第一个"可能含有 ≥ lo 的行"的 granule
                 = (第一个 index_value >= lo 的 mark) − 1
                     ↑ mark 里存的是每个 granule 的**首键**,所以
                       "首键 >= lo" 的那个 granule 的前一个 granule
                       也可能含 lo(它的首键 < lo,但行一直排到首键 >= lo 为止);
                       减 1 把那个"边界 granule"算进来
           end   = 第一个 index_value > hi 的 mark
                     ↑ 这个 granule 的第一行已经 > hi → 它以及之后的都不必读

        .. warning::

           **边界元组必须补齐到排序键的完整元数,缺的列用 -∞ / +∞ 垫。**

           这是本项目里踩过的一个真实 bug。考虑 ``ORDER BY (date, id)``、
           ``index_granularity = 2``、数据 ``(1,0)…(1,5),(2,0)…(2,5)``,
           查询 ``WHERE date = 2``:

           .. code-block:: text

              granule:   0        1        2        3        4        5
              首行键:  (1,0)    (1,2)    (1,4)    (2,0)    (2,2)    (2,4)

              lo 只约束了第 0 列 → 裸元组是 (2,)
              只比第 0 列的话,(2,0)、(2,2)、(2,4) 与 (2,) 的**前缀相等**
                 → 无法区分"date == 2 的第一个 granule"与"date > 2 的 granule"
              → 必须把 lo 补成 (2,−∞) 再比,让"date 的第一段"与
                "date 之后"在比较里区分开

           补齐之后:

           .. code-block:: text

              (2,0) > (2,−∞)  → 成立 → 第一个 >= lo 的 mark 是 3
              begin = 3 − 1 = 2 → 读 granule 2..5
              (granule 2 是 (1,4)/(1,5),读多了但**不会漏** —— 多余的行由谓词过滤掉)

           这就是"**低基数排序键 + 等值查询**"必须处理的情形 ——
           而低基数排序键(按天、按月分桶)恰恰是最常见的用法。

        .. warning::

           **mark 里存的是每个 granule 的"首键",低基数排序键会有重复首键
           —— 下界的二分必须用 ``>=``,不能用 ``>``。**

           数据 ``0,0,0,0,0,0,1,1,2,2``(粒度 4)切出三个 granule,
           首键是 ``[0, 0, 2]`` —— granule 0 与 granule 1 的首键**都是 0**。

           用"第一个 index_value **>** lo 的 mark 再减一"当下界:
           ``lo = 0`` 时"第一个 > 0"的 mark 是 2,于是 ``begin = 1``,
           首键同为 0、里面还有一堆 0 的 granule 1 被整个跳过 ——
           ``WHERE g = 0`` 只读 granule 1 → 只返回 2 行(应为 6 行)。

           换用 **>=**:第一个首键 >= lo 的 mark 是 0,``begin = max(0, 0−1) = 0``,
           整段都不丢。代价是 lo 恰好等于某个 mark 的首键时会**多读**一个
           granule(前缀比较无法知道它里面有没有 >= lo 的行)—— 多读只会慢,
           不会错。上界不受影响:它要的是"第一行就已经 > hi 的 granule",
           重复首键只会让上界更保守(多读),同样不会漏。
        """
        usable = self._usable_node_count()
        marks = part.get_marks_count()
        if usable == 0:
            return [MarkRange(0, marks)]
        arity = _key_arity(part)
        reverse = getattr(part, "sorting_key_reverse", None) or []
        out: List[MarkRange] = []
        for combo in self._tuple_ranges():
            # 下界 / 上界都在**索引序**里取,而"索引序的下界"对逆序(DESC)列
            # 来说对应的**不是**自然下界,而是自然上界(反之亦然)—— 见
            # compare_index_values 里"按列翻转方向"的说明。
            # 所以逆序列必须取另一端:lo 取 r.right,hi 取 r.left。
            # 取错端的后果是"整段 granule 被跳过" → **丢行**(静默错结果)。
            # 下界:取"索引序里有下边界的最长前缀",缺的列补索引序的 -∞
            lo_vals: List[Any] = []
            for i, r in enumerate(combo):
                is_rev = bool(reverse[i]) if i < len(reverse) else False
                v = r.right if is_rev else r.left
                if v is None:
                    break
                lo_vals.append(v)
            # 上界:取"索引序里有上边界的最长前缀",缺的列补索引序的 +∞
            hi_vals: List[Any] = []
            for i, r in enumerate(combo):
                is_rev = bool(reverse[i]) if i < len(reverse) else False
                v = r.left if is_rev else r.right
                if v is None:
                    break
                hi_vals.append(v)

            if lo_vals:
                lo_target, lo_len = _pad_bound(part, lo_vals, arity, lower=True)
                # 用 >= :mark 存的是 granule 的**首键**,重复首键很常见,
                # 用 ">" 会落到一串相等首键的最后一个,把前面的 granule 全丢掉。
                first_ge = _first_mark(part, lo_target, ">=", lo_len)
                begin = max(0, first_ge - 1)
            else:
                begin = 0
            if hi_vals:
                hi_target, hi_len = _pad_bound(part, hi_vals, arity, lower=False)
                end = _first_mark(part, hi_target, ">", hi_len)
            else:
                end = marks
            if end > begin:
                out.append(MarkRange(begin, end))
        if not out:
            return []
        return merge_ranges(out)

    # ── 运行时过滤 ──────────────────────────────────────────────────────

    def check_in_range(self, part: Any, mark_range: MarkRange) -> str:
        """某个 mark 区间"要不要读" —— 返回 :class:`CheckResult` 的三个值之一。"""
        if self._part is not part:
            self.attach_part(part)
        ranges = self.get_ranges()
        if not ranges:
            return CheckResult.FALSE
        for r in ranges:
            if r.intersects(mark_range):
                if self.always_true_in_range(part, mark_range):
                    return CheckResult.TRUE
                return CheckResult.PARTIALLY_TRUE
        return CheckResult.FALSE

    def may_be_true_in_range(self, part: Any, mark_range: MarkRange) -> bool:
        """``checkInRange`` 的布尔版 —— ``EXPLAIN``/``SelectedMarks`` 看的就是它。"""
        return self.check_in_range(part, mark_range) != CheckResult.FALSE

    def always_true_in_range(self, part: Any, mark_range: MarkRange) -> bool:
        """这个 mark 区间里是不是**每一行**都匹配(可以跳过 WHERE 过滤)。

        .. warning::

           返回 True 意味着上层**可以不做逐行过滤** —— 如果这里判错,
           会直接产生**错误结果**。所以本实现**宁可返回 False**:
           只有"恰好一个区间、且该区间在上下界某些边无界"的平凡情形
           才可能判 True。真实 ClickHouse 会用相邻 granule 的索引值给出
           更精确的证明(``alwaysTrueInRange`` 里那套
           ``mark Left``/``mark Right`` 的推导),我们明确放弃那部分精度,
           只保证正确。
        """
        usable = self._usable_node_count()
        if usable == 0:
            return True   # 键上没有条件 → 对键而言恒真
        combos = self._tuple_ranges()
        if len(combos) != 1:
            return False
        # 只有当"被约束的那几个键列在本区间内一定落在同一侧"时才可能恒真;
        # 我们用最保守的判定:不声明任何非平凡情形为恒真。
        return False

    # ── 展示 ────────────────────────────────────────────────────────────

    def describe(self) -> str:  # pragma: no cover - 给 EXPLAIN 用
        usable = self._usable_node_count()
        parts = []
        for i in range(usable):
            rs = self._node_ranges[i]
            parts.append(f"{self.info.key_exprs[i]} ∈ " +
                         " ∪ ".join(r.describe() for r in rs))
        if not parts:
            return "KeyCondition(no usable ranges → full scan)"
        return "KeyCondition(" + "; ".join(parts) + ")"

    def __repr__(self) -> str:  # pragma: no cover
        return self.describe()


# ── 运算符 → 区间 ───────────────────────────────────────────────────────


_SWAP_OPS = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "==": "=",
             "!=": "!=", "<>": "!="}


def _ranges_for_operator(op: str, v: Any) -> Optional[RangeSet]:
    """把 ``col OP v`` 变成一组区间。

    ``!=`` 返回**两个**区间 —— 这正是"不等于"为什么会让索引效率下降:
    它把一次顺序读拆成两次,而且"点"的判定失败,后面的键列也就用不上了。
    """
    if op in ("=", "=="):
        return [Range(v, v, True, True)]
    if op == "<":
        return [Range(None, v, True, False)]
    if op == "<=":
        return [Range(None, v, True, True)]
    if op == ">":
        return [Range(v, None, False, True)]
    if op == ">=":
        return [Range(v, None, True, True)]
    if op in ("!=", "<>"):
        return [Range(None, v, True, False), Range(v, None, False, True)]
    return None


def _complement_ranges(values: Sequence[Any]) -> Optional[RangeSet]:
    """``NOT IN (v1, v2, …)`` → 补集区间。值太多就放弃优化。"""
    if len(values) > MAX_RANGES_PER_NODE:
        return None
    ordered = sorted(values, key=_sort_key)
    out: RangeSet = []
    prev: Optional[Any] = None
    for v in ordered:
        if prev is None:
            out.append(Range(None, v, True, False))
        else:
            out.append(Range(prev, v, False, False))
        prev = v
    if prev is None:
        return [Range()]
    out.append(Range(prev, None, False, True))
    return [r for r in out if not _is_empty_range(r)]


def _is_empty_range(r: Range) -> bool:
    if r.left is None or r.right is None:
        return False
    c = _key_cmp(r.left, r.right)
    if c > 0:
        return True
    if c == 0 and not (r.left_inclusive and r.right_inclusive):
        return True
    return False


def _sort_key(v: Any) -> Any:
    """给区间排序用的稳定键 —— 不依赖值本身的可比较性。

    直接 ``sorted`` 会在混类型(数值与字符串)或 ``None`` 上抛异常;
    用 ``(类型名, repr)`` 排序永远不会崩,而且对**同类型**的值
    仍然给出与真实比较一致的顺序(数值的 repr 长度不同会有点偏差,
    但区间排序只影响合并效率,不影响正确性)。
    """
    if v is None:
        return (-1, 0)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, v)
    return (1, repr(v))


def _range_set_is_point(rs: RangeSet) -> bool:
    return len(rs) == 1 and rs[0].is_point()


# ── 字面量提取 ──────────────────────────────────────────────────────────


class _No:
    __slots__ = ()


_NO = _No()


def _const_of(node: Any) -> Any:
    from ...sql import ast_ as A

    if isinstance(node, A.Literal):
        return node.value
    if isinstance(node, A.UnaryOp) and node.op == "-":
        v = _const_of(node.operand)
        if v is _NO:
            return _NO
        try:
            return -v
        except Exception:  # pragma: no cover
            return _NO
    if isinstance(node, (A.CastExpr, A.FunctionCall)):
        try:
            from .expression import evaluate_constant

            return evaluate_constant(node)
        except Exception:
            return _NO
    return _NO


def _const_list_of(node: Any) -> Optional[List[Any]]:
    from ...sql import ast_ as A

    if node is None:
        return None
    if isinstance(node, A.ArrayLiteral):
        out = []
        for i in node.items:
            v = _const_of(i)
            if v is _NO:
                return None
            out.append(v)
        return out
    if isinstance(node, A.TupleLiteral):
        return _const_list_of(A.ArrayLiteral(items=node.items))
    if isinstance(node, A.Literal) and isinstance(node.value, (list, tuple)):
        return list(node.value)
    if isinstance(node, A.FunctionCall) and node.name.lower() in ("array", "tuple"):
        return _const_list_of(A.ArrayLiteral(items=node.args))
    return None


# ── 稀疏索引上的二分 ────────────────────────────────────────────────────


def compare_index_values(a: Sequence[Any], b: Sequence[Any], reverse: Sequence[bool],
                         length: int) -> int:
    """比较两个**索引值元组**(可能不等长)。

    这是"在 ``primary.idx`` 上二分"的核心比较函数。三个细节必须处理对:

    1. **方向**:``ORDER BY a DESC`` 的键列,索引值是按降序存的,
       所以比较时要**翻转**该列的方向 —— 否则二分会在降序段上乱走;
    2. **长度**:元组可能不等长(比如只约束了前两列,索引值有三列),
       此时只比较前 ``length`` 列 —— 语义是"前缀比较",
       这正是"只用排序键前缀裁剪"在比较层面的体现;
    3. **NULL/NaN**:走 :func:`minich.storages.merge_tree.part.sorting_key_value`
       (``normalize_key_value`` + "NULL 视为最大值"的哨兵),保证 NULL 是
       "最大值"、所有 NaN 相等 —— 且 NULL 与数字/字符串比较不会抛裸
       ``TypeError``(抛了就会退化成按 ``repr`` 比大小,而那套顺序与
       真实 ClickHouse 不一致,会让二分落错位置、**漏掉整段 granule**)。
    """
    for i in range(length):
        av = sorting_key_value(a[i]) if i < len(a) else None
        bv = sorting_key_value(b[i]) if i < len(b) else None
        if av == bv:
            continue
        try:
            c = -1 if av < bv else 1
        except TypeError:  # pragma: no cover
            c = -1 if repr(av) < repr(bv) else 1
        if i < len(reverse) and reverse[i]:
            c = -c
        return c
    return 0


def _first_mark(part: Any, target: Sequence[Any], op: str, length: int) -> int:
    """在 part 的稀疏索引上二分,找第一个满足 ``cmp(index_value, target) OP 0`` 的 mark。

    ``op`` 取 ``">="`` 或 ``">"``。part 必须提供:

    * ``get_marks_count() -> int``
    * ``get_index_value(mark) -> tuple``(越界返回"正无穷"哨兵)
    * ``sorting_key_reverse -> List[bool]``
    """
    lo, hi = 0, part.get_marks_count()
    reverse = getattr(part, "sorting_key_reverse", None) or [False] * length
    while lo < hi:
        mid = (lo + hi) // 2
        kv = part.get_index_value(mid)
        if kv is None:
            hi = mid
            continue
        c = compare_index_values(kv, target, reverse, length)
        ok = (c > 0) if op == ">" else (c >= 0)
        if ok:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _first_mark_op(part: Any, target: Sequence[Any], op: str, length: int) -> int:
    """``_first_mark`` 的别名 —— 保留给文档/测试引用。"""
    return _first_mark(part, target, op, length)


# ══════════════════════════════════════════════════════════════════════════
# 分区裁剪
# ══════════════════════════════════════════════════════════════════════════


class PartitionPruner:
    """``PARTITION BY`` 的分区裁剪器。

    .. code-block:: text

       ┌── 分区键是"虚拟排序键"──────────────────────────────────────────┐
       │                                                                  │
       │  ORDER BY 决定**part 内部**的顺序;PARTITION BY 决定            │
       │  **part 之间**的划分。但对"裁剪"而言,两者的用法完全一样:      │
       │  都是"一个有序的键",都能用同一套区间推导。                      │
       │                                                                  │
       │  所以真实 ClickHouse 用的是**同一个** KeyCondition 类:          │
       │      index_granularity = 无穷大 的分区键 KeyCondition           │
       │  只不过它的"索引"不是 primary.idx,而是"每个分区的键值"列表。   │
       └──────────────────────────────────────────────────────────────────┘

    支持的 ``PARTITION BY`` 表达式(与真实 ClickHouse 的常用写法对齐):

    ==============================  ==========================================
    表达式                            说明
    ==============================  ==========================================
    ``toYYYYMM(d)``                  按月分区(**最常用**)
    ``toYYYYMMDD(d)``                按天(整数形式)
    ``toDate(d)`` / ``toDate32(d)``  按天
    ``toStartOfMonth(d)``            按月的"起点日期"
    ``toMonday(d)``                  按周(周一起)
    ``toStartOfYear(d)``             按年
    ``toStartOfDay(ts)``             按天(时间戳)
    ``toStartOfHour(ts)``            按小时
    ``intDiv(x, k)``                 按任意整数的整除
    ``toUInt32(x)`` / ``toInt64(x)`` 取整(常见于"把 ID 折成桶")
    ``d``(裸列)                 直接用列值
    ==============================  ==========================================

    查询侧支持**两种**写法,两种都能裁剪:

    .. code-block:: text

       WHERE toYYYYMM(d) >= 202403      ← 直接约束分区表达式
       WHERE d >= '2024-03-01'          ← 约束内层列 → **正向映射**到分区键

    第二种能工作的原因就是 :class:`KeyFunctionSpec.forward_range`:
    单调函数把"内层列的区间"放大成"分区键的区间"。
    """

    def __init__(self, build_information: KeyConditionBuildInformation) -> None:
        self.condition = KeyCondition(build_information)
        self.info = build_information

    def add(self, expr: Any) -> None:
        self.condition.add(expr)

    def prune(self, partitions: Dict[str, Any]) -> Set[str]:
        """``{partition_id: 分区键值}`` → 匹配的 partition_id 集合。"""
        if not self.condition.has_usable_ranges():
            return set(partitions)
        out: Set[str] = set()
        for pid, value in partitions.items():
            if self.condition.check_key_value(value) != CheckResult.FALSE:
                out.add(pid)
        return out

    def describe(self) -> str:  # pragma: no cover
        return "PartitionPruner(" + self.condition.describe() + ")"
