"""``EXPLAIN`` —— 把"引擎打算怎么跑"打出来。

这是排查 ClickHouse 性能问题的**第一件该做的事**。真实 ClickHouse
支持六种 ``EXPLAIN``,每一种回答一个不同的问题:

===============================  ================================================================
写法                              回答什么问题
===============================  ================================================================
``EXPLAIN AST``                   解析出来了吗? 语法树长什么样?
``EXPLAIN SYNTAX``                优化器把查询改写成什么样子了?
``EXPLAIN QUERY TREE``            新的查询树(pass 之后的中间形态)
``EXPLAIN PLAN``                  **逻辑**计划:有哪些步骤、谁先谁后
                                  → 定位"为什么没走索引"
``EXPLAIN PIPELINE``              **物理**流水线:几个 processor、怎么并行
                                  → 定位"为什么只有一个核在跑"
``EXPLAIN ESTIMATE``              每一步的**行数/字节**估计
                                  → 定位"为什么选了个糟糕的 JOIN 顺序"
===============================  ================================================================

.. note::

   真实 ClickHouse 的 ``EXPLAIN PLAN`` 输出的是 ``QueryPlan`` 的树
   (``ReadFromMergeTree`` → ``Expression`` → ``Aggregating`` →
   ``Sorting`` → …),而且每一步都带 ``Indexes`` 明细
   (用了哪个主键区间、哪个跳数索引、选了多少 granule)。
   mini-clickhouse 的输出格式相近但更简略 —— 我们打印的是
   "分段执行"的步骤列表,因为那就是我们的实现。
   教程第 22 章会把真实输出与我们的输出并排展示,讲清差异。
"""

from __future__ import annotations

from typing import Any, List, Optional

from ..errors import ErrorCode, MiniChError, raise_error
from ..sql import ast_ as A
from ..sql import format_ast
from .analyzer import ExpressionAnalyzer

__all__ = ["explain_query"]


def explain_query(ctx: Any, node: A.ExplainQuery) -> List[str]:
    """返回 ``EXPLAIN`` 的文本行(每行是结果集的一行)。"""
    kind = (node.kind or "PLAN").upper()
    statement = node.statement

    if kind == "AST":
        return format_ast(statement).split("\n")

    if kind == "SYNTAX":
        # 真实 ClickHouse 会在这里展示**优化后**的查询
        # (常量折叠、别名展开、谓词下推都会体现在文本上)。
        # mini-clickhouse 的优化器不改语法树(它改的是执行计划),
        # 所以两者通常一样 —— 这个差异如实打印出来。
        return [
            statement.to_sql(),
            "-- mini-clickhouse: the optimizer rewrites the *plan*, not the AST, "
            "so SYNTAX matches the input here",
        ]

    if kind in ("PLAN", "QUERY TREE", "CURRENT"):
        return _explain_plan(ctx, statement)

    if kind == "PIPELINE":
        return _explain_pipeline(ctx, statement)

    if kind == "ESTIMATE":
        return _explain_estimate(ctx, statement)

    raise_error(
        ErrorCode.NOT_IMPLEMENTED,
        f"EXPLAIN {kind} is not implemented in mini-clickhouse "
        f"(supported: AST, SYNTAX, PLAN, PIPELINE, ESTIMATE, QUERY TREE)",
    )


def _explain_plan(ctx: Any, statement: Optional[A.Node]) -> List[str]:
    """逻辑计划 —— 与我们的"分段执行"一一对应。"""
    if not isinstance(statement, A.SelectQuery):
        return [f"{type(statement).__name__}"]
    lines: List[str] = ["Expression ((Projection + Before ORDER BY))"]
    plan: List[str] = []

    table = _resolve_table(ctx, statement)
    header = None
    if table is not None:
        header = table.header()
        engine = table.engine
        engine_name = engine.get_name() if engine is not None else "None"
        plan.append(f"ReadFromStorage ({table.full_name()}, engine = {engine_name})")
        if engine is not None:
            keys = getattr(engine, "order_by_sql", None) or getattr(engine, "sorting_key_sql", None)
            if keys:
                plan.append(f"  Sorting key: {keys}")
            pkey = getattr(engine, "partition_by_sql", None)
            if pkey:
                plan.append(f"  Partition key: {pkey}")
            if hasattr(engine, "get_primary_key_description"):
                pk = engine.get_primary_key_description()
                if pk:
                    plan.append(f"  Primary key: {pk}")
    elif statement.from_ is not None:
        plan.append(f"ReadFromSource ({type(statement.from_).__name__})")
    else:
        plan.append("ReadFromSource (single row)")

    if statement.array_joins:
        plan.append("ArrayJoin")
    if statement.prewhere is not None:
        plan.append(f"Filter (PREWHERE {statement.prewhere.to_sql()})")
    if statement.where is not None:
        plan.append(f"Filter (WHERE {statement.where.to_sql()})")

    analysis = None
    try:
        analysis = ExpressionAnalyzer(ctx, statement, header).analyze()
    except MiniChError:
        analysis = None

    if analysis is not None and analysis.has_aggregation:
        key_text = ", ".join(
            f"{e.to_sql()} AS {n}" for e, n in zip(analysis.key_exprs, analysis.key_names)
        ) or "(no keys)"
        plan.append(f"Aggregating (keys: {key_text})")
        plan.append(f"  Aggregates: " + ", ".join(
            f"{a.node.to_sql()}" for a in analysis.aggregates
        ))
        if analysis.with_totals:
            plan.append("  WITH TOTALS")
    if analysis is not None and analysis.has_window:
        plan.append("Window (" + ", ".join(
            wf.to_sql() for wf, _ in analysis.window_names
        ) + ")")
    if statement.distinct:
        plan.append("Distinct")
    if analysis is not None and analysis.having is not None:
        plan.append(f"Filter (HAVING {analysis.having.to_sql()})")
    if statement.order_by:
        plan.append("Sorting (" + ", ".join(e.to_sql() for e in statement.order_by) + ")")
    if statement.limit_by is not None:
        plan.append("LimitBy (" + ", ".join(e.to_sql() for e in statement.limit_by.elements) + ")")
    if statement.limit is not None:
        plan.append(f"Limit ({statement.limit.to_sql()}"
                    + (f", offset {statement.offset.to_sql()}" if statement.offset else "") + ")")
    if statement.unions:
        plan.append(f"Union (× {len(statement.unions) + 1}"
                    + (", DISTINCT" if statement.union_distinct else ", ALL") + ")")

    lines.extend("  " + p for p in plan)
    return lines


def _explain_pipeline(ctx: Any, statement: Optional[A.Node]) -> List[str]:
    """物理流水线 —— 我们用"分段执行",所以会看到多个 Pipeline。

    .. warning::

       真实 ClickHouse 打印的是**一张**图(processor 之间有端口连接),
       mini-clickhouse 打印的是**一段一段**的列表,因为聚合/排序这些
       阻塞算子在我们的实现里会把中间结果物化。这个差异必须说清楚 ——
       否则读者会以为 ClickHouse 也只是"一段一段跑"。
    """
    if not isinstance(statement, A.SelectQuery):
        return ["(EXPLAIN PIPELINE supports SELECT statements only)"]
    lines = [
        "(mini-clickhouse 分段执行,不是一张完整的 processor 图)",
        "Pipeline 0:",
        "  (ReadFromStorage × 1)",
    ]
    if statement.array_joins:
        lines.append("  (ArrayJoin × 1)")
    if statement.prewhere is not None:
        lines.append("  (Filter(PREWHERE) × 1)")
    if statement.where is not None:
        lines.append("  (Filter(WHERE) × 1)")
    if statement.group_by is not None or _has_aggregate(statement):
        lines.append("Pipeline 1:")
        lines.append("  (Expression × 1)")
        lines.append("  (Aggregating × 1)")
        lines.append("  (MergingAggregated × 1)   ← 阻塞点:必须看到全部输入")
    if statement.order_by:
        lines.append("Pipeline 2:")
        lines.append("  (Expression × 1)")
        lines.append("  (Sorting × 1)             ← 阻塞点:内存峰值 = 结果集大小")
    lines.append(f"Pipeline {3 if statement.order_by else 2}:")
    lines.append("  (Expression(Projection) × 1)")
    lines.append("  (Limit × 1) → (CollectingSink × 1)")
    lines.append("")
    lines.append("max_threads = " + str(_setting(ctx, "max_threads", 0)))
    lines.append("max_block_size = " + str(_setting(ctx, "max_block_size", 65409)))
    return lines


def _explain_estimate(ctx: Any, statement: Optional[A.Node]) -> List[str]:
    """行数/字节估计。

    .. note::

       mini-clickhouse **没有统计信息**(没有 ``ANALYZE``、没有直方图),
       所以这里给的是**精确值**(直接读 part 的 ``count.txt``),
       而不是估计值。真实 ClickHouse 的 ``read_rows`` 也是精确的
       (它也读元数据),但涉及 JOIN 与多表时的 ``result_rows`` 是估算的。
    """
    if not isinstance(statement, A.SelectQuery):
        return ["(no estimate available)"]
    table = _resolve_table(ctx, statement)
    if table is None:
        return [
            "Expression ((Projection + Before ORDER BY))",
            "  ReadFromSource (no table)",
            "  rows: 1",
        ]
    rows = table.row_count()
    # ``row_count()`` 返回 ``None`` 表示"这个引擎不提供行数元数据"
    # (普通视图就是这样:它不存数据,行数只有跑一遍子查询才知道)。
    # 打印成 ``None`` 会让 ``EXPLAIN ESTIMATE`` 看起来像个 bug,所以渲染成 ``?``。
    rows_text = "?" if rows is None else str(rows)
    size = table.size_in_bytes()
    return [
        f"ReadFromStorage ({table.full_name()})",
        f"  rows: {rows_text}",
        f"  bytes: {size}",
        f"  parts: {table.engine.part_count() if table.engine is not None else 0}",
        "Expression ((Projection + Before ORDER BY))",
        f"  rows: {rows_text}",
    ]


def _resolve_table(ctx: Any, statement: A.SelectQuery):
    if statement.from_ is None:
        return None
    from_ = statement.from_
    if isinstance(from_, A.TableReference):
        database = from_.database or ctx.database
        return ctx.catalog.try_get_table(database, from_.table)
    return None


def _has_aggregate(statement: A.SelectQuery) -> bool:
    from .analyzer import _has_aggregate as _h

    return any(_h(s) for s in statement.selects)


def _setting(ctx: Any, name: str, default: Any) -> Any:
    try:
        return ctx.settings.try_get(name, default)
    except Exception:
        return default
