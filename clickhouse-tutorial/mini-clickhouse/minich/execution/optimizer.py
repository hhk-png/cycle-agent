"""查询优化 —— 把"用户写的 SQL"变成"最省事的等价 SQL"。

优化器在 ClickHouse 里是一条**规则链**:每条规则尝试改写查询,
改得动就用新版本,改不动就跳过。这跟"基于代价的优化器(CBO)"
是两种哲学:

.. code-block:: text

   基于规则(RBO,ClickHouse 主要这么做)   基于代价(CBO,PostgreSQL 等)
   ────────────────────────────────────   ──────────────────────────────────
   在源码里写"这类模式这么改"             用统计信息估算每种计划的代价
   改写结果**确定、可解释**               结果依赖统计信息是否新鲜
   不需要统计信息(所以不需要 ANALYZE)    统计信息过期就会选错计划
   代价是可能错过更优的计划               代价是实现复杂、维护成本高

ClickHouse 的官方立场是"**你来决定怎么写,我负责把它跑快**"——
所以它有一批**确定性的**改写规则,而在"选哪种 JOIN 顺序"这类
真正需要代价模型的地方,它选择**不猜**(把右表交给你决定)。

mini-clickhouse 实现四条最常用、最容易讲清楚的规则:

===================================================================  ==========================================
规则(对应的真实设置)                                                 在 mini-clickhouse 里的效果
===================================================================  ==========================================
``optimize_trivial_count_query``                                     ``SELECT count() FROM t`` 直接读 part 元数据
``optimize_move_to_prewhere``                                        WHERE 里的主键条件下推到读取阶段
``optimize_read_in_order``(部分)                                     排序键与 ORDER BY 一致时跳过排序
``limit_push_down``                                                  把 LIMIT 传给存储引擎以提前停止
===================================================================  ==========================================

.. warning::

   真实 ClickHouse 有**上百条**优化规则(见
   ``src/Interpreters/QueryPlanOptimizers.cpp`` 与
   ``QueryPlanOptimizer`` 家族)。mini-clickhouse 的四条规则远不能覆盖它,
   而且我们**不做**下面两件真实 ClickHouse 会做的事:

   * **谓词下推到子查询/JOIN 内部**(``enable_optimize_predicate_expression``);
   * **无用列消除**(``optimize_functions_to_subcolumns``)。

   所以 ``SELECT count() FROM (SELECT ... )`` 这类写法在 mini-clickhouse 里
   会老老实实执行子查询 —— 而真实 ClickHouse 可能把它整个消掉。
   第 22 章会讲"为什么读 EXPLAIN 是排查性能问题的第一步"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import ErrorCode, MiniChError, raise_error
from ..sql import ast_ as A

__all__ = ["OptimizationResult", "optimize_select", "OPTIMIZATION_RULES"]


#: 规则的元信息 —— ``EXPLAIN`` 与 ``system.query_log`` 会用到。
OPTIMIZATION_RULES: Dict[str, str] = {
    "trivial_count_query": "没有 WHERE 的 count() 直接读 part 元数据,不扫数据",
    "move_to_prewhere": "把 WHERE 里只依赖排序键/分区键的条件提到 PREWHERE",
    "read_in_order": "ORDER BY 与表排序键一致时跳过排序算子",
    "limit_push_down": "把 LIMIT 传给存储引擎,让它提前停止读取",
    "aggregation_in_order": "GROUP BY 的键与排序键一致时可以边读边聚合",
}


@dataclass
class OptimizationResult:
    """优化结果 —— 除了改写后的查询,还包括"做了什么"的记录。

    记录这些的意义在于 ``EXPLAIN``:真实 ClickHouse 的
    ``EXPLAIN`` 输出里能看出"哪些优化生效了",
    而不仅仅是一个最终计划。
    """

    applied: List[str] = field(default_factory=list)
    #: 可以完全用元数据回答的 count(避免扫数据)
    trivial_count: bool = False
    #: 下推到存储引擎的主键/分区条件
    pushed_filters: List[Any] = field(default_factory=list)
    #: 分组键是否与排序键一致(可以"边读边聚合")
    aggregation_in_order: bool = False
    #: 排序键是否与 ORDER BY 一致(可以跳过排序)
    sorted_in_order: bool = False
    #: 传给存储的 LIMIT(提前停止)
    limit: Optional[int] = None
    #: 实际要读的列(列裁剪的结果)
    required_columns: Optional[List[str]] = None

    def note(self, rule: str) -> None:
        if rule not in self.applied:
            self.applied.append(rule)

    def describe(self) -> str:  # pragma: no cover
        if not self.applied:
            return "  (no optimization applied)"
        lines = []
        for rule in self.applied:
            lines.append(f"  {rule}: {OPTIMIZATION_RULES.get(rule, '')}")
        if self.trivial_count:
            lines.append("  → count() will be answered from part metadata")
        if self.limit is not None:
            lines.append(f"  → storage limit push-down: {self.limit}")
        return "\n".join(lines)


def optimize_select(
    ctx: Any,
    query: A.SelectQuery,
    table: Any = None,
    source_columns: Optional[Sequence[str]] = None,
) -> OptimizationResult:
    """对一条 ``SELECT`` 跑一遍规则链。

    ``table`` 是源表(可能为 None —— 子查询/表函数没有表)。
    规则需要知道表的排序键、主键、分区键才能判断"能不能下推"。
    """
    result = OptimizationResult()

    # ── 规则 1:平凡 count ────────────────────────────────────────────────
    if _is_trivial_count(query) and table is not None:
        result.trivial_count = True
        result.note("trivial_count_query")

    # ── 规则 2:下推条件下的 PREWHERE ─────────────────────────────────────
    if table is not None and query.where is not None:
        moved, kept = _split_for_prewhere(query.where, table)
        if moved is not None:
            query.prewhere = moved if query.prewhere is None else \
                A.BinaryOp(op="AND", left=query.prewhere, right=moved)
            query.where = kept
            result.pushed_filters.append(moved)
            result.note("move_to_prewhere")

    # ── 规则 3:LIMIT 下推 ────────────────────────────────────────────────
    if query.limit is not None and query.limit_by is None:
        limit = _const_int(query.limit)
        if limit is not None:
            result.limit = limit
            result.note("limit_push_down")

    # ── 规则 4:有序读 ────────────────────────────────────────────────────
    if table is not None and query.order_by:
        sorting_key = _sorting_key_columns(table)
        order_cols = [
            e.expr.name for e in query.order_by
            if isinstance(e.expr, A.Identifier)
        ]
        if sorting_key and order_cols and order_cols == sorting_key[: len(order_cols)]:
            result.sorted_in_order = True
            result.note("read_in_order")

    if source_columns is not None:
        result.required_columns = list(source_columns)

    return result


# ══════════════════════════════════════════════════════════════════════════
# 各规则
# ══════════════════════════════════════════════════════════════════════════


def _is_trivial_count(query: A.SelectQuery) -> bool:
    """``SELECT count() FROM t``(没有 WHERE/GROUP BY/HAVING)。

    这个优化看着小,实际上**极其重要**:
    ``SELECT count(*) FROM huge`` 在 ClickHouse 里是**毫秒级**的,
    因为它读的是 ``count.txt`` 而不是数据。而如果没有这个优化,
    它要扫完所有 part 的**一列**(通常是主键列)。

    .. warning::

       有 ``WHERE`` 时这个优化**不生效** —— 因为元数据里没有
       "满足这个条件的行数"。真实 ClickHouse 需要走 skip index 或
       真扫一遍。很多人以为 ``count()`` 永远很快,从而写出
       ``SELECT count() FROM t WHERE ...`` 的报表查询然后抱怨慢 ——
       第 12 章有一节专门讲"怎么用 ``system.parts`` 把行数统计做成 O(1)"。
    """
    if query.where is not None or query.prewhere is not None:
        return False
    if query.group_by is not None or query.having is not None:
        return False
    if query.distinct or query.unions:
        return False
    # 任何**会改变结果行数**的修饰符都必须让这条捷径失效 ——
    # 因为它回答的是"表里有多少行"(part 元数据),而不是"这条查询
    # 会返回多少行"。漏检的后果是**静默的错误结果**(不是慢):
    #
    #   * ``LIMIT`` / ``OFFSET`` / ``LIMIT BY`` —— 裁剪输出行;
    #   * ``FINAL`` —— Replacing/Summing 家族会折叠掉重复键的行;
    #   * ``ARRAY JOIN`` —— 一个数组元素一行,行数会被**放大**;
    #   * ``SAMPLE`` —— 只读一部分数据(在 TableReference 上)。
    #
    # ``SELECT count() FROM t LIMIT 0`` 必须返回 **0 行**,而不是 ``[(N,)]``。
    if (query.limit is not None or query.offset is not None
            or query.limit_by is not None):
        return False
    if query.final or query.array_joins:
        return False
    ref = query.from_
    if isinstance(ref, A.TableReference) and (ref.final or ref.sample is not None):
        return False
    if len(query.selects) != 1:
        return False
    item = query.selects[0]
    if isinstance(item, A.AliasExpr):
        item = item.expr
    if not isinstance(item, A.FunctionCall):
        return False
    return item.name.lower() in ("count",) and not item.args


def _split_for_prewhere(
    cond: A.Node, table: Any
) -> Tuple[Optional[A.Node], Optional[A.Node]]:
    """把 ``WHERE a = 1 AND b = 2`` 拆成 ``PREWHERE a = 1`` + ``WHERE b = 2``。

    判据只有一个:**这个条件只引用排序键的前缀或分区键吗?**
    如果是,放到 PREWHERE 就能让 MergeTree 少解压很多数据。

    真实的 ``optimize_move_to_prewhere`` 判据更细(它看列的大小、
    看是否有子列可以只读一部分),但核心就是这一条。
    """
    conjuncts = _split_conjunction(cond)
    key_columns = set(_sorting_key_columns(table)) | set(_partition_key_columns(table))
    if not key_columns:
        return None, cond
    movable: List[A.Node] = []
    rest: List[A.Node] = []
    for c in conjuncts:
        cols = _referenced_columns(c)
        if cols and cols <= key_columns:
            movable.append(c)
        else:
            rest.append(c)
    if not movable:
        return None, cond
    moved = _join_conjunction(movable)
    kept = _join_conjunction(rest) if rest else None
    return moved, kept


def _split_conjunction(node: A.Node) -> List[A.Node]:
    if isinstance(node, A.BinaryOp) and node.op == "AND":
        return _split_conjunction(node.left) + _split_conjunction(node.right)
    return [node]


def _join_conjunction(nodes: Sequence[A.Node]) -> Optional[A.Node]:
    if not nodes:
        return None
    out = nodes[0]
    for n in nodes[1:]:
        out = A.BinaryOp(op="AND", left=out, right=n)
    return out


def _referenced_columns(node: A.Node) -> set:
    out: set = set()

    def visit(n: A.Node) -> None:
        if n is None:
            return
        if isinstance(n, A.Identifier):
            out.add(n.name)
            return
        for c in n.children():
            visit(c)

    visit(node)
    return out


def _sorting_key_columns(table: Any) -> List[str]:
    engine = getattr(table, "engine", None)
    if engine is None:
        return []
    desc = getattr(engine, "order_by_sql", None) or getattr(engine, "sorting_key_sql", None)
    if desc is None:
        cfg = getattr(engine, "config", None)
        desc = getattr(cfg, "order_by_sql", None) if cfg is not None else None
    if not desc:
        return []
    return [c.strip().strip("`") for c in str(desc).split(",")]


def _partition_key_columns(table: Any) -> List[str]:
    engine = getattr(table, "engine", None)
    if engine is None:
        return []
    desc = getattr(engine, "partition_by_sql", None)
    if desc is None:
        cfg = getattr(engine, "config", None)
        desc = getattr(cfg, "partition_by_sql", None) if cfg is not None else None
    if not desc:
        return []
    # 分区键通常是 toYYYYMM(d) 这种函数 —— 把里面的列名抠出来
    return sorted(_referenced_columns_sql(str(desc)))


def _referenced_columns_sql(text: str) -> set:
    import re

    out = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", text):
        word = m.group(0)
        if word.lower() in ("toyyyymm", "todate", "tostartofmonth", "toyyyymmdd",
                            "tomonday", "toyyyy", "intdiv", "touint32", "tohour"):
            continue
        out.add(word)
    return out


def _const_int(node: Optional[A.Node]) -> Optional[int]:
    if isinstance(node, A.Literal) and isinstance(node.value, int):
        return node.value
    return None
