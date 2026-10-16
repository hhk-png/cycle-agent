"""表达式分析 —— 把一条 ``SELECT`` 拆成"聚合前 / 聚合 / 聚合后"三段。

这是整个引擎里**最像编译器**的一层。它要回答的问题只有一句话:

    一条 ``SELECT`` 里的每个表达式,应该在**哪个阶段**求值?

.. code-block:: text

   SELECT toDate(ts) AS d, uniqExact(u) AS uu, uu * 2
   FROM hits
   WHERE status = 200
   GROUP BY d
   HAVING uu > 100
   ORDER BY uu DESC
   LIMIT 10

   ┌── 阶段 1:WHERE ─────────────────────────────────────────────┐
   │  在**原始行**上求值:  status = 200                          │
   │  它看不到 d、uu —— 因为分组还没发生                          │
   └─────────────────────────────────────────────────────────────┘
   ┌── 阶段 2:聚合 ──────────────────────────────────────────────┐
   │  GROUP BY 的键:       toDate(ts)      → __key_0             │
   │  聚合的参数:           u              → __agg_arg_0         │
   │  聚合函数:             uniqExact(u)   → 状态列 __agg_0      │
   └─────────────────────────────────────────────────────────────┘
   ┌── 阶段 3:聚合后 ────────────────────────────────────────────┐
   │  把 __agg_0 最终化:    uniqExact(u)   → 一个 UInt64 列      │
   │  SELECT 的其它项:      __agg_0 * 2                          │
   │  HAVING:              __agg_0 > 100                         │
   │  ORDER BY:            __agg_0 DESC                          │
   └─────────────────────────────────────────────────────────────┘

**别名为什么能在 WHERE 里用?** 因为 ClickHouse 在分析期把别名**展开**
(``d`` → ``toDate(ts)``),而不是像 SQL 标准那样禁止。这带来方便
(少写重复表达式)也带来坑:别名与真实列**同名**时,谁优先?
ClickHouse 的行为是"别名优先",于是 ``SELECT a + 1 AS a FROM t``
会让 ``WHERE a > 1`` 引用到那个表达式 —— 一个经典的踩坑点(第 07 章)。

.. note::

   真实 ClickHouse 的 ``ExpressionAnalyzer`` 干的事情比这里多得多:
   它会构建 ``ActionsDAG``、做子表达式消除、做常量折叠、
   做类型推导与隐式转换、处理 ``PREWHERE`` 的自动下推……
   mini-clickhouse 只做"分阶段"这件事,其余交给优化器与求值器。

   **一个重要的结构差异**:真实 ClickHouse 会把聚合函数在 AST 里
   **替换成对聚合结果的引用**(``ASTFunction`` → 具名节点),
   然后对整棵树做一次"聚合后求值"。mini-clickhouse 不改树,
   而是给求值器一张"表达式文本 → 列"的查找表
   (:attr:`ExpressionLookup`),遇到匹配的表达式直接取列。
   语义一样,但**要求两个 AST 的 `to_sql()` 完全一致**才能匹配上 ——
   所以这里的"规范化"必须小心处理括号与空格。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..aggregates import aggregate_function_exists, get_aggregate_function, split_combinators
from ..block import Block, ColumnWithTypeAndName
from ..errors import ErrorCode, MiniChError, raise_error
from ..functions.base import function_exists
from ..sql import ast_ as A
from ..types import DataType

__all__ = [
    "ExpressionLookup",
    "QueryAnalysis",
    "ExpressionAnalyzer",
    "extract_aliases",
    "expand_stars",
    "normalize_sql",
]


#: 表达式文本的规范化 —— 匹配用的键。
#:
#: 只做**空格与大小写**的规整(不动括号),因为我们的 ``to_sql()``
#: 输出本来就是规范形式。真实 ClickHouse 用 AST 的结构化比较
#: (``ASTPtr`` 的 ``getTreeHash``),不依赖文本 —— 更可靠但更复杂。
_WS_RE = re.compile(r"\s+")


def normalize_sql(text: str) -> str:
    """把表达式文本规范化成匹配键。

    .. warning::

       **这个函数是 mini-clickhouse 里最脆弱的一处。** 它靠字符串比较
       来判断"两个表达式是不是同一个",所以

       * ``a + b`` 与 ``b + a`` **匹配不上**(数学上相等但结构不同);
       * ``sum(x)`` 与 ``SUM(x)`` 能匹配上(大小写被规整);
       * 用户写的多余括号 ``(a)`` 与 ``a`` 匹配不上。

       真实 ClickHouse 没有这个问题,因为它比较的是 AST 结构。
       我们的选择是"宁可少匹配(报错),不可错匹配(静默出错)", 所以
       匹配失败时的错误消息会**把两个表达式都打出来**,方便定位。
    """
    return _WS_RE.sub(" ", text.strip()).lower()


# ══════════════════════════════════════════════════════════════════════════
# 查找表
# ══════════════════════════════════════════════════════════════════════════


class ExpressionLookup:
    """表达式文本 → 已算好的列。

    这是"分阶段求值"的粘合剂:聚合后的阶段里,``sum(x)`` 不该再去
    算一遍(它根本算不了),而是去查这张表拿到"已经算好的那一列"。
    """

    def __init__(self) -> None:
        self._columns: Dict[str, ColumnWithTypeAndName] = {}
        #: 记录每个键对应的"人类可读的原始表达式",用于报错
        self._display: Dict[str, str] = {}

    def add(self, expr: Any, column: ColumnWithTypeAndName) -> None:
        key = normalize_sql(expr.to_sql() if hasattr(expr, "to_sql") else str(expr))
        self._columns[key] = column
        self._display[key] = expr.to_sql() if hasattr(expr, "to_sql") else str(expr)

    def add_sql(self, sql: str, column: ColumnWithTypeAndName) -> None:
        key = normalize_sql(sql)
        self._columns[key] = column
        self._display[key] = sql

    def get(self, node: Any) -> Optional[ColumnWithTypeAndName]:
        key = normalize_sql(node.to_sql() if hasattr(node, "to_sql") else str(node))
        return self._columns.get(key)

    def __contains__(self, node: Any) -> bool:
        return self.get(node) is not None

    def keys(self) -> List[str]:
        return list(self._columns.keys())

    def __len__(self) -> int:
        return len(self._columns)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExpressionLookup {sorted(self._display.values())}>"


# ══════════════════════════════════════════════════════════════════════════
# 分析结果
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class AggregateInfo:
    """一个待计算的聚合(分析期的形态,还没有绑定具体实现)。"""

    node: A.FunctionCall
    base_name: str
    params: List[Any]
    arg_exprs: List[A.Node]
    combinator: Optional[str]
    is_state: bool
    is_merge: bool
    #: 输出列名(聚合后阶段的引用名)
    output_name: str
    #: 状态列名(聚合阶段的产出名)
    state_name: str


@dataclass
class QueryAnalysis:
    """一条查询的完整分析结果。"""

    #: SELECT 列表(**已展开 ``*``**,每个元素是 AliasExpr 或裸表达式)
    select_exprs: List[A.Node] = field(default_factory=list)
    #: 每个 SELECT 项的输出名
    output_names: List[str] = field(default_factory=list)
    #: GROUP BY 的键表达式(别名已展开)
    key_exprs: List[A.Node] = field(default_factory=list)
    #: 键的输出列名
    key_names: List[str] = field(default_factory=list)
    #: 聚合函数
    aggregates: List[AggregateInfo] = field(default_factory=list)
    #: 别名表:名字 → 表达式(**已递归展开**)
    aliases: Dict[str, A.Node] = field(default_factory=dict)
    #: 是否聚合查询
    has_aggregation: bool = False
    #: 是否有窗口函数
    has_window: bool = False
    #: 窗口函数(节点 → 输出列名)
    window_names: List[Tuple[A.WindowFunction, str]] = field(default_factory=list)
    #: 用到的源列(列裁剪的依据)
    source_columns: List[str] = field(default_factory=list)
    #: 是否有 ARRAY JOIN
    has_array_join: bool = False
    #: HAVING(聚合后求值)
    having: Optional[A.Node] = None
    #: ORDER BY(已展开别名)
    order_by: List[A.OrderByElement] = field(default_factory=list)
    #: DISTINCT
    distinct: bool = False
    #: WITH TOTALS
    with_totals: bool = False
    #: 展开别名之后的 PREWHERE / WHERE(过滤器要用展开后的版本)
    prewhere: Optional[A.Node] = None
    where: Optional[A.Node] = None
    #: 需要"聚合后"求值的表达式(select + having + order by)
    post_expressions: List[A.Node] = field(default_factory=list)

    def describe(self) -> str:  # pragma: no cover
        lines = ["QueryAnalysis:"]
        lines.append(f"  aggregation: {self.has_aggregation}  window: {self.has_window}")
        lines.append(f"  source columns: {', '.join(self.source_columns)}")
        if self.key_exprs:
            lines.append("  group by: " + ", ".join(
                f"{e.to_sql()} AS {n}" for e, n in zip(self.key_exprs, self.key_names)
            ))
        if self.aggregates:
            lines.append("  aggregates: " + ", ".join(
                f"{a.node.to_sql()} → {a.state_name}" for a in self.aggregates
            ))
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# 工具:别名与星号展开
# ══════════════════════════════════════════════════════════════════════════


def extract_aliases(query: A.SelectQuery) -> Dict[str, A.Node]:
    """收集别名:WITH 里的、SELECT 里的。

    **顺序很重要**:先收集 WITH,再收集 SELECT —— 后收集的会覆盖
    同名的 —— 与真实 ClickHouse 的"后定义者胜"一致。
    """
    aliases: Dict[str, A.Node] = {}
    for w in query.with_exprs:
        if w.expr is not None and not w.is_subquery:
            aliases[w.name] = w.expr
    for item in query.selects:
        if isinstance(item, A.AliasExpr) and item.alias:
            aliases[item.alias] = item.expr
    return aliases


def expand_aliases(node: A.Node, aliases: Dict[str, A.Node], depth: int = 0) -> A.Node:
    """把表达式里的别名**递归展开** —— ``d`` → ``toDate(ts)``。

    真实 ClickHouse 在 ``ExpressionAnalyzer::appendSelect`` 里做同一件事
    (``analyzeAliases`` + 别名替换)。

    .. warning::

       别名**可以**形成环:``SELECT a + 1 AS b, b + 1 AS a``。
       真实 ClickHouse 会报 ``CYCLIC_ALIASES``;我们用一个深度上限,
       超了就报一个明确的错(而不是栈溢出)。
    """
    if depth > 100:
        raise_error(
            ErrorCode.LOGICAL_ERROR,
            "Cyclic aliases detected while expanding "
            f"{node.to_sql() if hasattr(node, 'to_sql') else node}",
        )
    if isinstance(node, A.Identifier) and node.name in aliases:
        return expand_aliases(aliases[node.name], aliases, depth + 1)
    return _map_children(node, lambda c: expand_aliases(c, aliases, depth + 1))


def _map_children(node: A.Node, fn) -> A.Node:
    """对节点的每个子节点调用 ``fn`` 并就地更新 —— 一棵通用的"改写树"。

    真实 ClickHouse 的做法是"每个 AST 类各自实现
    ``clone`` + 子节点替换"(靠宏生成)。我们用反射:遍历 dataclass
    的字段,凡是 ``Node``(或 ``Node`` 列表)就替换。少数字段
    (如 ``OrderByElement.expr``、``AliasExpr.expr``)由字段名判断 ——
    因为它们的字段名不叫 "children" 但语义上是子节点。
    """
    import dataclasses

    if not dataclasses.is_dataclass(node):
        return node
    for f in dataclasses.fields(node):
        if f.name == "pos":
            continue
        value = getattr(node, f.name)
        if isinstance(value, A.Node):
            setattr(node, f.name, fn(value))
        elif isinstance(value, list):
            if value and isinstance(value[0], A.Node):
                setattr(node, f.name, [fn(v) for v in value])
            elif value and isinstance(value[0], tuple):
                new_list = []
                for item in value:
                    new_list.append(tuple(fn(x) if isinstance(x, A.Node) else x for x in item))
                setattr(node, f.name, new_list)
        elif value is not None and dataclasses.is_dataclass(value) and isinstance(value, A.Node):
            setattr(node, f.name, fn(value))
    return node


def _rewrite_implicit_array_join(
    selects: Sequence[A.Node], q: A.SelectQuery
) -> List[A.Node]:
    """把 ``SELECT arrayJoin(expr) …`` 提升成隐式 ``ARRAY JOIN``。

    对每个 ``SELECT arrayJoin(e) [AS a]``:

    * 往 ``q.array_joins`` 里追加一条 ``ArrayJoinExpr(expr=e, alias=a 或文本)``;
    * 在 SELECT 列表的**原位置**放一个对该展开列(列名 = 别名或 ``a.to_sql()``)
      的 ``Identifier`` 引用。

    这与真实 ClickHouse 的 ``SyntaxAnalyzer`` 一致 —— 它把 ``arrayJoin``
    从表达式里"提"到 ``ARRAY JOIN`` 子句,并让表达式读展开后的列。
    只处理 SELECT 列表里、且是**顶层**的 ``arrayJoin``(裸调用或带别名);
    嵌套在别的表达式里的情况(-- ``length(arrayJoin(a))`` --)留待以后,
    真实 CH 也会把它们提到同一个 ARRAY JOIN 里。
    """
    out: List[A.Node] = []
    for s in selects:
        alias: Optional[str] = s.alias if isinstance(s, A.AliasExpr) else None
        inner: A.Node = s.expr if isinstance(s, A.AliasExpr) else s
        if isinstance(inner, A.FunctionCall) and inner.name.lower() in ("arrayjoin", "array_join"):
            if len(inner.args) != 1:
                raise_error(
                    ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                    f"arrayJoin requires exactly one argument, got {len(inner.args)}",
                )
            col_name = alias or s.to_sql()
            q.array_joins.append(
                A.ArrayJoinExpr(expr=inner.args[0], left=False, alias=col_name)
            )
            out.append(A.Identifier(name=col_name, pos=getattr(s, "pos", None)))
        else:
            out.append(s)
    return out


def expand_stars(
    selects: Sequence[A.Node],
    source_header: Optional[Block],
) -> List[A.Node]:
    """把 ``*`` 展开成具体列 —— ``SELECT *`` / ``t.*`` / ``EXCEPT`` / ``REPLACE``。

    三个细节(都与真实 ClickHouse 一致):

    1. ``*`` 展开的是**表的全部列**(含 ``ALIAS`` 列,因为它们在
       查询视角下是存在的),而不是"落盘的列";
    2. 有 ``JOIN`` 时,``*`` 展开的是**左表 + 右表**的全部列,
       同名列会被**自动去重**(保留第一个)—— 这是 ClickHouse
       为 `SELECT * FROM a JOIN b USING (k)` 做的特例;
    3. ``EXCEPT``/``REPLACE`` 在展开**之后**应用。
    """
    if source_header is None:
        for s in selects:
            if isinstance(s, A.StarExpr):
                raise_error(
                    ErrorCode.EMPTY_LIST_OF_COLUMNS_QUERIED,
                    "SELECT * requires a table (no FROM clause was given)",
                )
        return list(selects)

    out: List[A.Node] = []
    available = source_header.get_names()
    for item in selects:
        if not isinstance(item, A.StarExpr):
            out.append(item)
            continue
        names = list(available)
        if item.qualified:
            # ``t.*`` —— 只取带去重前缀的那些列。
            # 我们的 source_header 里,join 之后的列名形如 ``t.a``;
            # 没有 join 时就是裸列名。
            prefixed = [n for n in names if n.startswith(item.qualified + ".")]
            names = prefixed if prefixed else names
        except_set = set(item.except_columns)
        replaced = {name: expr for expr, name in item.replace_columns}
        for name in names:
            base = name.split(".")[-1]
            if base in except_set:
                continue
            if base in replaced:
                out.append(A.AliasExpr(expr=replaced[base], alias=base))
            else:
                out.append(A.Identifier(name=name, quoted=False))
    return out


# ══════════════════════════════════════════════════════════════════════════
# 分析器
# ══════════════════════════════════════════════════════════════════════════


class ExpressionAnalyzer:
    """把一个 ``SelectQuery`` 分析成 :class:`QueryAnalysis`。

    用法::

        analyzer = ExpressionAnalyzer(ctx, query, source_header)
        analysis = analyzer.analyze()
    """

    def __init__(
        self,
        ctx: Any,
        query: A.SelectQuery,
        source_header: Optional[Block] = None,
        extra_aliases: Optional[Dict[str, A.Node]] = None,
    ) -> None:
        self.ctx = ctx
        self.query = query
        self.source_header = source_header
        self.extra_aliases = dict(extra_aliases or {})

    # ── 主入口 ──────────────────────────────────────────────────────────

    def analyze(self) -> QueryAnalysis:
        q = self.query
        result = QueryAnalysis()
        result.distinct = q.distinct
        result.with_totals = bool(q.group_by and q.group_by.with_totals)
        result.has_array_join = bool(q.array_joins)

        aliases = dict(self.extra_aliases)
        aliases.update(extract_aliases(q))
        result.aliases = aliases

        # ① 展开 *
        selects = expand_stars(q.selects, self.source_header)
        result.select_exprs = selects
        result.output_names = [_output_name(s, i) for i, s in enumerate(selects)]

        # ② 展开别名(把 ``d`` 换成 ``toDate(ts)``)
        selects = [expand_aliases(s, aliases) for s in selects]
        result.select_exprs = selects

        # ②.5 把 ``SELECT arrayJoin(expr)`` 提升成隐式 ``ARRAY JOIN``。
        #
        #     真实 ClickHouse 的语法分析器就是这么做的:出现在表达式里的
        #     ``arrayJoin`` 会被改写成一个 ``ARRAY JOIN`` 子句 + 对展开列的
        #     引用 —— 所以 ``SELECT arrayJoin([1,2,3])`` 与
        #     ``SELECT x FROM … ARRAY JOIN [1,2,3] AS x`` 是同一回事。
        #     mini-clickhouse 里 ``arrayJoin`` 只注册了一个"执行时报错"的壳
        #     (它改变行数,不可能是标量函数),所以这个改写不做的话,
        #     ``SELECT arrayJoin(a) FROM t`` 会直接撞 NOT_IMPLEMENTED。
        selects = _rewrite_implicit_array_join(selects, q)
        result.select_exprs = selects

        # ③ 找出窗口函数(它们不能参与聚合分阶段,单独处理)
        result.window_names = self._collect_window_functions(selects, aliases)
        result.has_window = bool(result.window_names)

        # ④ 收集聚合函数
        if q.group_by is not None:
            result.key_exprs = [
                expand_aliases(e, aliases) for e in q.group_by.elements
            ]
            result.has_aggregation = True
        else:
            result.has_aggregation = any(
                _has_aggregate(s) for s in selects
            )
        # ``HAVING``/``ORDER BY`` 里出现聚合也算聚合查询
        if q.having is not None and _has_aggregate(expand_aliases(q.having, aliases)):
            result.has_aggregation = True
        for e in q.order_by:
            if _has_aggregate(expand_aliases(e.expr, aliases)):
                result.has_aggregation = True
                break

        if result.has_aggregation:
            self._collect_aggregates(selects, q, aliases, result)
            result.key_names = [
                _key_column_name(e, i) for i, e in enumerate(result.key_exprs)
            ]
            self._validate_group_by(result)
        else:
            # 非聚合查询:键不存在
            if q.group_by is not None and q.group_by.elements:
                result.has_aggregation = True

        # ④.5 PREWHERE / WHERE —— **别名必须在这里展开**
        #
        # ``SELECT amount * 2 AS d FROM ev WHERE d > 15`` 是合法的
        # ClickHouse 写法:别名在 WHERE 里可用。求值时 WHERE 先于
        # SELECT 发生,所以那里的 ``d`` 必须已经换成 ``amount * 2``。
        # 真实 ClickHouse 在 ``ExpressionAnalyzer::appendWhere`` 里
        # 做同一件事(它改的是 AST,我们是另存一份展开后的副本)。
        if q.prewhere is not None:
            result.prewhere = expand_aliases(q.prewhere, aliases)
        if q.where is not None:
            result.where = expand_aliases(q.where, aliases)

        # ⑤ HAVING(python 条件下必须在聚合后求值)
        if q.having is not None:
            having = expand_aliases(q.having, aliases)
            if not result.has_aggregation and not result.has_window:
                raise_error(
                    ErrorCode.ILLEGAL_AGGREGATION,
                    "HAVING requires GROUP BY or an aggregate function; "
                    "use WHERE for row-level filtering",
                )
            result.having = having

        # ⑥ ORDER BY(展开别名;聚合查询里的排序键要在聚合后求值)
        result.order_by = [
            A.OrderByElement(
                expr=expand_aliases(e.expr, aliases),
                direction=e.direction,
                nulls=e.nulls,
                collate=e.collate,
            )
            for e in q.order_by
        ]

        # ⑦ 源列(列裁剪)
        result.source_columns = self._collect_source_columns(q, selects, result)

        # ⑧ 聚合后需要求值的表达式
        if result.has_aggregation:
            post: List[A.Node] = list(selects)
            if result.having is not None:
                post.append(result.having)
            post.extend(e.expr for e in result.order_by)
            result.post_expressions = post

        return result

    # ── 聚合收集 ────────────────────────────────────────────────────────

    def _collect_aggregates(
        self,
        selects: Sequence[A.Node],
        q: A.SelectQuery,
        aliases: Dict[str, A.Node],
        result: QueryAnalysis,
    ) -> None:
        """遍历所有"聚合后求值"的表达式,把聚合函数调用抽出来。

        每个聚合函数会分到:

        * 一个**状态列名**(``__agg_0``)—— 聚合阶段产出;
        * 一个**输出名**(就是它自己的 SQL 文本)—— 聚合后阶段按它引用。

        用 SQL 文本当引用名,是为了让"聚合后求值"能用
        :class:`ExpressionLookup` 做文本匹配。真实 ClickHouse
        用 AST 节点替换,不需要这个约定。
        """
        seen: Dict[str, int] = {}
        counter = [0]

        def visit(node: A.Node) -> None:
            if node is None:
                return
            # 子查询与窗口函数里的聚合**不属于本层**
            if isinstance(node, (A.SubqueryExpr, A.WindowFunction)):
                return
            if isinstance(node, A.FunctionCall) and _is_aggregate_call(node):
                key = normalize_sql(node.to_sql())
                if key in seen:
                    return
                idx = counter[0]
                counter[0] += 1
                info = self._make_aggregate_info(node, idx, aliases, result)
                seen[key] = idx
                result.aggregates.append(info)
                # 参数里还可能有嵌套聚合(少见但合法:``sum(uniq(x))`` 不行,
                # 但 ``quantile(0.9)(x)`` 的参数里有别的表达式)—— 继续下钻
                for a in node.args:
                    if not isinstance(a, A.Lambda):
                        visit(a)
                return
            for c in node.children():
                visit(c)

        for s in selects:
            visit(s)
        if q.having is not None:
            visit(expand_aliases(q.having, aliases))
        for e in q.order_by:
            visit(expand_aliases(e.expr, aliases))
        if q.group_by is not None and not result.key_exprs:
            result.key_exprs = [expand_aliases(e, aliases) for e in q.group_by.elements]

    def _make_aggregate_info(
        self, node: A.FunctionCall, idx: int, aliases: Dict[str, A.Node],
        result: QueryAnalysis,
    ) -> AggregateInfo:
        name = node.name
        lowered = name.lower()
        base, combinator, is_state, is_merge = split_combinators(lowered)
        if not aggregate_function_exists(lowered) and not aggregate_function_exists(base):
            raise_error(
                ErrorCode.UNKNOWN_AGGREGATE_FUNCTION,
                f"Unknown aggregate function {name}",
            )
        params = [p.value if isinstance(p, A.Literal) else p for p in node.params]
        # 聚合函数的**参数**要展开别名(``sum(d)`` 里的 d 是别名)
        args = [expand_aliases(a, aliases) for a in node.args]
        return AggregateInfo(
            node=node,
            base_name=base,
            params=params,
            arg_exprs=args,
            combinator=combinator,
            is_state=is_state,
            is_merge=is_merge,
            output_name=node.to_sql(),
            state_name=f"__agg_{idx}",
        )

    def _validate_group_by(self, result: QueryAnalysis) -> None:
        """检查"SELECT 里的每个非聚合部分是否都被 GROUP BY 覆盖"。

        规则(与真实 ClickHouse 的语义一致):

        .. code-block:: text

           把 SELECT 的每一项**自顶向下**走一遍:
             遇到一个**整棵子树**能在 GROUP BY 里找到 → 停,这一支算被覆盖
             遇到聚合函数 → 停,聚合函数内部不需要覆盖
             遇到子查询 / 窗口函数 → 停(它们各有自己的作用域)
             走到**裸标识符**还没被覆盖 → 报错

        为什么"整棵子树匹配"就够了? 因为 ``GROUP BY toStartOfMonth(p)``
        已经保证了这个表达式在每个分组里是**同一个值**,所以
        ``SELECT toStartOfMonth(p)`` 合法;而它一旦被覆盖,
        底下的 ``p`` 就不必再单独出现在 GROUP BY 里。

        .. warning::

           早期版本只做了一层粗判:把 SELECT 里的**所有**裸标识符
           拉出来和 GROUP BY 的裸标识符比对。于是

               SELECT toStartOfMonth(p) AS m, count() FROM t GROUP BY m

           会报 ``Column `p` is not under aggregate function and not in
           GROUP BY`` —— 一条**完全合法、而且是最常见写法**的查询被拒。
           (``GROUP BY m`` 里的 ``m`` 是别名,展开之后是
           ``toStartOfMonth(p)``,而校验只看到了里面的 ``p``。)

        真实 ClickHouse 的做法更精细(它有函数依赖推导:如果 ``a`` 是主键,
        ``SELECT a, b`` 可以不写 GROUP BY)。mini-clickhouse **不做**那个推导,
        所以"``a`` 唯一决定 ``b``"的写法仍然要显式写进 GROUP BY ——
        更严格,但绝不会放过错误。
        """
        key_sqls = {normalize_sql(e.to_sql()) for e in result.key_exprs}
        source_names = set()
        if self.source_header is not None:
            source_names = set(self.source_header.get_names())
        for item in result.select_exprs:
            for ident in _uncovered_identifiers(item, key_sqls):
                if ident.name.startswith("__"):
                    continue
                if source_names and ident.name not in source_names:
                    continue
                raise_error(
                    ErrorCode.ILLEGAL_AGGREGATION,
                    f"Column `{ident.name}` is not under aggregate function "
                    f"and not in GROUP BY",
                )

    # ── 窗口函数 ────────────────────────────────────────────────────────

    def _collect_window_functions(
        self, selects: Sequence[A.Node], aliases: Dict[str, A.Node]
    ) -> List[Tuple[A.WindowFunction, str]]:
        out: List[Tuple[A.WindowFunction, str]] = []
        counter = [0]

        def visit(node: A.Node) -> None:
            if node is None:
                return
            if isinstance(node, A.WindowFunction):
                out.append((node, f"__window_{counter[0]}"))
                counter[0] += 1
                return
            for c in node.children():
                visit(c)

        for s in selects:
            visit(s)
        return out

    # ── 源列 ────────────────────────────────────────────────────────────

    def _collect_source_columns(
        self, q: A.SelectQuery, selects: Sequence[A.Node], result: QueryAnalysis
    ) -> List[str]:
        """收集查询用到的**表列** —— 列裁剪的依据。

        这一步的收益很直接:``SELECT count() FROM wide_table`` 不该去读
        100 个列文件。真实 ClickHouse 在 ``InterpreterSelectQuery``
        里做同一件事(``required_columns``),它同时会记录"这一列
        是被 WHERE 用的还是被 SELECT 用的",后者在聚合查询里可以
        **完全不读**(因为聚合之后就不需要它了)。
        """
        if self.source_header is None:
            return []
        available = set(self.source_header.get_names())
        used: Set[str] = set()

        def visit(node: A.Node) -> None:
            if node is None:
                return
            # 子查询有自己的源,它的列不该算进本表的列裁剪 ——
            # 否则 ``SELECT (SELECT max(x) FROM other) FROM t`` 会去
            # ``t`` 里找一个叫 x 的列。
            if isinstance(node, A.SubqueryExpr):
                return
            if isinstance(node, A.Identifier) and node.name in available:
                used.add(node.name)
                return
            for c in node.children():
                visit(c)

        for s in selects:
            visit(s)
        for e in q.array_joins:
            visit(e.expr)
        for e in (q.prewhere, q.where, q.having):
            visit(e)
        if q.group_by is not None:
            for e in q.group_by.elements:
                visit(e)
        for e in q.order_by:
            visit(e.expr)
        if q.limit_by is not None:
            for e in q.limit_by.elements:
                visit(e)
        return [n for n in self.source_header.get_names() if n in used]


# ══════════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════════


def _output_name(node: A.Node, index: int) -> str:
    if isinstance(node, A.AliasExpr) and node.alias:
        return node.alias
    if isinstance(node, A.Identifier):
        return node.name
    return node.to_sql() if hasattr(node, "to_sql") else f"column_{index}"


def _key_column_name(expr: A.Node, index: int) -> str:
    """GROUP BY 键在聚合结果里的列名。

    用**表达式文本**当列名(而不是 ``__key_0``),这样聚合后阶段
    靠文本匹配就能找到它 —— 与真实 ClickHouse 用
    ``__group_by_...`` 内部名不同,但对教学更直观:
    ``EXPLAIN`` 里能看到 ``toDate(ts)`` 这个键。
    """
    return expr.to_sql() if hasattr(expr, "to_sql") else f"__key_{index}"


def _is_aggregate_call(node: A.FunctionCall) -> bool:
    lowered = node.name.lower()
    if aggregate_function_exists(lowered):
        return True
    base, _, _, _ = split_combinators(lowered)
    return aggregate_function_exists(base)


def _has_aggregate(node: Optional[A.Node]) -> bool:
    """这个表达式里是否有**属于本查询**的聚合函数?

    .. warning::

       必须**在子查询处停止下钻**。``SELECT (SELECT max(x) FROM t) AS m``
       是一条**非聚合**查询 —— 那个 ``max(x)`` 属于内层查询。
       下钻会让它被当成外层的聚合,于是外层会去解析 ``x``,
       报出 "Unknown identifier: 'x'. Available: dummy" 这种
       完全指错方向的错误。(这个 bug 在实现这一层时出现过。)

       同理,窗口聚合(``sum(x) OVER ()``)也不是本层 GROUP BY 的聚合,
       它由 WindowTransform 处理。
    """
    if node is None:
        return False
    if isinstance(node, (A.SubqueryExpr, A.WindowFunction)):
        return False
    if isinstance(node, A.FunctionCall) and _is_aggregate_call(node):
        return True
    return any(_has_aggregate(c) for c in node.children())


def _uncovered_identifiers(node: A.Node, key_sqls: set) -> List[A.Identifier]:
    """列出**没有被 GROUP BY 覆盖**的裸标识符。

    见 :meth:`ExpressionAnalyzer._validate_group_by` 里的规则说明。
    """
    out: List[A.Identifier] = []

    def visit(n: A.Node) -> None:
        if n is None:
            return
        # 子查询与窗口函数有自己的作用域,不属于本层的分组校验
        if isinstance(n, (A.SubqueryExpr, A.WindowFunction)):
            return
        # 聚合函数的参数不需要覆盖 —— 这正是聚合的意义
        if isinstance(n, A.FunctionCall) and _is_aggregate_call(n):
            return
        # 整棵子树被 GROUP BY 覆盖 → 停,不再往下看
        if normalize_sql(n.to_sql()) in key_sqls:
            return
        if isinstance(n, A.Identifier):
            out.append(n)
            return
        for c in n.children():
            visit(c)

    visit(node)
    return out


def _bare_identifiers(node: A.Node) -> List[A.Identifier]:
    """收集表达式里的裸标识符(**不进入聚合函数的参数**)。

    为什么不下钻聚合函数? 因为 ``sum(x)`` 里的 ``x`` 是**允许**
    不出现在 GROUP BY 里的 —— 这正是聚合的意义。
    """
    out: List[A.Identifier] = []

    def visit(n: A.Node) -> None:
        if n is None:
            return
        if isinstance(n, (A.SubqueryExpr, A.WindowFunction)):
            return
        if isinstance(n, A.FunctionCall) and _is_aggregate_call(n):
            return
        if isinstance(n, A.Identifier):
            out.append(n)
            return
        for c in n.children():
            visit(c)

    visit(node)
    return out
