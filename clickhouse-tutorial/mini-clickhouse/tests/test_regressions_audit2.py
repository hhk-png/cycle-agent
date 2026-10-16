"""第二轮独立审计确认的 bug 回归测试。

这些 bug 由一次独立审计逐条复现,横跨 ``minich/functions``、
``minich/execution`` 与 ``minich/aggregates``。每条测试钉住一个**已确认的
不正确行为**,并与真实 ClickHouse 的语义对齐:

* **E1** 常量 NULL(``ColumnConst`` 包着 ``ColumnNullable``)的空位图被整段
  丢掉 —— ``lower(NULL)`` 返回 ``'none'``、``NULL = NULL`` 返回 ``1``;
* **E2** 普通 ``lag``/``lead`` 被错误地夹到窗口帧里(真实 CH:整分区取行);
* **E3** ``any``/``anyLast`` 与窗口 ``first_value``/``last_value`` 把 NULL
  也当成一个值(真实 CH:默认 IGNORE NULLS);
* **E4** ``uniq*`` 家族与 ``COUNT(DISTINCT)`` 把 NULL 当成一个独立的不同值;
* **E5** ``GROUP BY … WITH ROLLUP/WITH CUBE`` 被解析后**静默丢弃**;
* **E6** ``UNION ALL`` 分支合并时向常量列追加不同的值直接报 Code 49;
* **E7** ``toStartOfDay`` 返回 ``Date``(真实 CH:``DateTime``);
* **E8** ``SELECT arrayJoin(...)`` 没有隐式 ``ARRAY JOIN`` 改写,直接报 Code 48;
* **S1** ``INSERT`` NULL 进不可空列漏出**裸的 Python ``TypeError``**;
* **S2** ``extract('a','b')`` 二参函数写法被当语法错误拒绝。

真实 ClickHouse 的语义是基准;每条期望值都直接对应它的文档 / 官方 bug fix。
"""

from __future__ import annotations

import pytest

from conftest import assert_rows
from minich.errors import MiniChError


def _nullable_ints(ctx) -> None:
    """``x Nullable(Int32)`` = 1, 1, 2, NULL, NULL。"""
    ctx.create_table("n", "x Nullable(Int32)", engine_sql="Memory")
    ctx.insert("n", [(1,), (1,), (2,), (None,), (None,)])


# ══════════════════════════════════════════════════════════════════════════
# E1 —— 常量 NULL 的传播(ColumnConst 里的空位图不能被丢掉)
# ══════════════════════════════════════════════════════════════════════════


def test_constant_null_propagates_through_scalar_functions(memory_ctx):
    """``NULL`` 字面量是 ``ColumnConst`` 包着 ``ColumnNullable``。

    修好之前空位图被丢掉,裸的 ``None`` 被当成普通值传给函数 —— 于是
    ``lower(NULL)`` → ``'none'``、``length(concat(NULL))`` → ``4``。
    真实 ClickHouse 里这些**全是 NULL**(``IFunction::defaultImplementationForNulls``
    先 ``convertToFullColumnIfConst``,再合并各实参的空位图)。
    """
    assert_rows(memory_ctx.execute("SELECT lower(NULL)"), [(None,)])
    assert_rows(memory_ctx.execute("SELECT toString(NULL)"), [(None,)])
    assert_rows(memory_ctx.execute("SELECT concat('x', toString(NULL))"), [(None,)])
    assert_rows(memory_ctx.execute("SELECT length(concat(NULL))"), [(None,)])
    assert_rows(memory_ctx.execute("SELECT lower(nullIf('a', 'a'))"), [(None,)])
    assert_rows(memory_ctx.execute("SELECT toInt32(NULL)"), [(None,)])


def test_constant_null_three_valued_comparison(memory_ctx):
    """``NULL = NULL`` 是 **NULL**,不是 1;``WHERE NULL = NULL`` 匹配 0 行。"""
    assert_rows(memory_ctx.execute("SELECT lower(NULL) IS NULL"), [(1,)])
    assert_rows(
        memory_ctx.execute("SELECT NULL = NULL, NULL <= NULL, NULL < NULL"),
        [(None, None, None)],
    )
    memory_ctx.create_table("t", "x Int32", engine_sql="Memory")
    memory_ctx.insert("t", [(1,), (2,), (3,)])
    assert_rows(memory_ctx.execute("SELECT count() FROM t WHERE NULL = NULL"), [(0,)])


def test_all_null_coalesce_and_ifnull_return_null(memory_ctx):
    """``coalesce(NULL, NULL)`` / ``ifNull(NULL, NULL)`` 是**一行 NULL**
    (类型 ``Nullable(Nothing)``),不是 Code 8 "produced 0 rows"。"""
    assert_rows(memory_ctx.execute("SELECT coalesce(NULL, NULL)"), [(None,)])
    assert_rows(memory_ctx.execute("SELECT ifNull(NULL, NULL)"), [(None,)])
    assert_rows(
        memory_ctx.execute("SELECT toTypeName(coalesce(NULL, NULL))"),
        [("Nullable(Nothing)",)],
    )


# ══════════════════════════════════════════════════════════════════════════
# E2 —— 普通 lag/lead 用整个分区;只有 *InFrame 尊重窗口帧
# ══════════════════════════════════════════════════════════════════════════


def _four_rows(ctx) -> None:
    ctx.create_table("w", "x Int32", engine_sql="Memory")
    ctx.insert("w", [(1,), (2,), (3,), (4,)])


def test_plain_lead_uses_full_partition(memory_ctx):
    """``lead(x) OVER (ORDER BY x)`` 看得到**后面的行**(真实 CH 整分区取行)。

    默认帧 ``[0, current]`` 下,如果 lay/lead 也去夹帧,``lead`` 会永远拿到
    默认值 —— 那正是修之前的结果 ``(1,0) (2,0) (3,0) (4,0)``。
    """
    _four_rows(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT x, lead(x) OVER (ORDER BY x) FROM w"),
        [(1, 2), (2, 3), (3, 4), (4, 0)],
    )
    assert_rows(
        memory_ctx.execute("SELECT x, lag(x) OVER (ORDER BY x) FROM w"),
        [(1, 0), (2, 1), (3, 2), (4, 3)],
    )


def test_lead_in_frame_respects_the_frame(memory_ctx):
    """``leadInFrame`` **尊重窗口帧**:默认帧 ``[0, current]`` 下永远拿不到
    下一行,于是每行都是默认值(这正是它和普通 ``lead`` 的区别)。"""
    _four_rows(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT x, leadInFrame(x) OVER (ORDER BY x) FROM w"),
        [(1, 0), (2, 0), (3, 0), (4, 0)],
    )


# ══════════════════════════════════════════════════════════════════════════
# E3 —— any/anyLast 与窗口 first_value/last_value 默认忽略 NULL
# ══════════════════════════════════════════════════════════════════════════


def test_any_and_anylast_skip_nulls(memory_ctx):
    """``any``/``anyLast`` 默认跳过 NULL(它们是 ``first_value``/``last_value``
    的别名,默认 IGNORE NULLS)。修之前第一个 NULL 会被原样返回。"""
    memory_ctx.create_table("c", "city Nullable(String)", engine_sql="Memory")
    memory_ctx.insert("c", [(None,), ("Amsterdam",), ("New York",)])
    assert_rows(
        memory_ctx.execute("SELECT any(city), anyLast(city) FROM c"),
        [("Amsterdam", "New York")],
    )


def test_window_first_and_last_value_skip_nulls(memory_ctx):
    """窗口 ``first_value``/``last_value`` 默认忽略 NULL;``*_respect_nulls``
    变体保留 NULL。修之前两者被当成同一个函数。"""
    memory_ctx.create_table("v", "x Int32, city Nullable(String)", engine_sql="Memory")
    memory_ctx.insert("v", [(1, None), (2, "a"), (3, "b")])
    assert_rows(
        memory_ctx.execute("SELECT x, first_value(city) OVER (ORDER BY x) FROM v"),
        [(1, None), (2, "a"), (3, "a")],
    )
    assert_rows(
        memory_ctx.execute(
            "SELECT x, first_value_respect_nulls(city) OVER (ORDER BY x) FROM v"
        ),
        [(1, None), (2, None), (3, None)],
    )

    memory_ctx.create_table("v2", "x Int32, city Nullable(String)", engine_sql="Memory")
    memory_ctx.insert("v2", [(1, "a"), (2, "b"), (3, None)])
    assert_rows(
        memory_ctx.execute("SELECT x, last_value(city) OVER (ORDER BY x) FROM v2"),
        [(1, "a"), (2, "b"), (3, "b")],
    )
    assert_rows(
        memory_ctx.execute(
            "SELECT x, last_value_respect_nulls(city) OVER (ORDER BY x) FROM v2"
        ),
        [(1, "a"), (2, "b"), (3, None)],
    )


# ══════════════════════════════════════════════════════════════════════════
# E4 —— uniq 家族与 COUNT(DISTINCT) 跳过 NULL
# ══════════════════════════════════════════════════════════════════════════


def test_uniq_family_and_count_distinct_skip_nulls(memory_ctx):
    """``{1, 1, 2, NULL, NULL}`` 的不同值只有 2 个 —— NULL 不算一个值。"""
    _nullable_ints(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT count(DISTINCT x) FROM n"), [(2,)])
    assert_rows(memory_ctx.execute("SELECT uniqExact(x) FROM n"), [(2,)])
    assert_rows(memory_ctx.execute("SELECT uniq(x) FROM n"), [(2,)])
    assert_rows(memory_ctx.execute("SELECT uniqCombined(x) FROM n"), [(2,)])
    assert_rows(memory_ctx.execute("SELECT uniqUpTo(10)(x) FROM n"), [(2,)])


# ══════════════════════════════════════════════════════════════════════════
# E5 —— GROUP BY … WITH ROLLUP / WITH CUBE
# ══════════════════════════════════════════════════════════════════════════


def test_group_by_with_rollup_adds_subtotals(memory_ctx):
    """``GROUP BY k WITH ROLLUP`` 在小计行之上再加一行**总计**;
    被"去掉"的键列填类型默认值(``''``),不是 NULL。"""
    memory_ctx.create_table("g", "k String, v Int32", engine_sql="Memory")
    memory_ctx.insert("g", [("a", 1), ("a", 2), ("b", 3)])
    assert_rows(
        memory_ctx.execute("SELECT k, sum(v) FROM g GROUP BY k WITH ROLLUP"),
        [("a", 3), ("b", 3), ("", 6)],
    )


def test_group_by_with_cube_enumerates_all_key_subsets(memory_ctx):
    """``GROUP BY a, b WITH CUBE`` 展开成全部 4 个子集:
    ``(a,b) (a) (b) ()``。"""
    memory_ctx.create_table("g2", "a String, b String, v Int32", engine_sql="Memory")
    memory_ctx.insert("g2", [("x", "p", 1), ("x", "q", 2), ("y", "p", 4)])
    assert_rows(
        memory_ctx.execute("SELECT a, b, sum(v) FROM g2 GROUP BY a, b WITH CUBE"),
        [
            ("x", "p", 1),
            ("x", "q", 2),
            ("y", "p", 4),
            ("x", "", 3),
            ("y", "", 4),
            ("", "p", 5),
            ("", "q", 2),
            ("", "", 7),
        ],
    )


# ══════════════════════════════════════════════════════════════════════════
# E6 —— UNION ALL 分支合并时把常量列展开
# ══════════════════════════════════════════════════════════════════════════


def test_union_all_derived_table_materializes_constant_columns(memory_ctx):
    """``SELECT * FROM (SELECT 1 AS x UNION ALL SELECT 2)`` 曾报
    ``Code: 49 Cannot insert a different value into a constant column``。"""
    assert_rows(
        memory_ctx.execute("SELECT * FROM (SELECT 1 AS x UNION ALL SELECT 2)"),
        [(1,), (2,)],
    )


# ══════════════════════════════════════════════════════════════════════════
# E7 —— toStartOfDay 返回 DateTime(不是 Date)
# ══════════════════════════════════════════════════════════════════════════


def test_to_start_of_day_returns_datetime(memory_ctx):
    """``toStartOfDay(DateTime)`` 的类型是 ``DateTime``,并且渲染出时刻。"""
    assert_rows(
        memory_ctx.execute(
            "SELECT toTypeName(toStartOfDay(toDateTime('2020-01-02 03:04:05')))"
        ),
        [("DateTime",)],
    )
    r = memory_ctx.execute("SELECT toStartOfDay(toDateTime('2020-01-02 03:04:05')) AS d")
    assert "2020-01-02 00:00:00" in r.text("TSV")


# ══════════════════════════════════════════════════════════════════════════
# E8 —— SELECT 列表里的 arrayJoin 隐式改写为 ARRAY JOIN
# ══════════════════════════════════════════════════════════════════════════


def test_array_join_function_in_select(memory_ctx):
    """``SELECT arrayJoin([1,2,3])`` 曾报 Code 48 NOT_IMPLEMENTED ——
    引擎必须像真实 CH 那样把它改写成 ``ARRAY JOIN``。"""
    assert_rows(memory_ctx.execute("SELECT arrayJoin([1,2,3])"), [(1,), (2,), (3,)])

    memory_ctx.create_table("at", "id Int32, a Array(Int32)", engine_sql="Memory")
    memory_ctx.insert("at", [(1, [10, 20]), (2, [30])])
    assert_rows(
        memory_ctx.execute("SELECT id, arrayJoin(a) AS el FROM at"),
        [(1, 10), (1, 20), (2, 30)],
    )


# ══════════════════════════════════════════════════════════════════════════
# 小问题:S1 INSERT NULL / S2 extract 二参写法
# ══════════════════════════════════════════════════════════════════════════


def test_insert_null_into_non_nullable_raises_minich_error(memory_ctx):
    """``INSERT INTO t(x Int32) VALUES (NULL)`` 应抛 ``MiniChError``
    (Code 70),而不是漏出裸的 ``TypeError``。"""
    memory_ctx.create_table("p", "x Int32", engine_sql="Memory")
    with pytest.raises(MiniChError) as ei:
        memory_ctx.execute("INSERT INTO p VALUES (NULL)")
    assert ei.value.code == 70

    # 可空列仍然可以写 NULL
    memory_ctx.create_table("q", "x Nullable(Int32)", engine_sql="Memory")
    memory_ctx.execute("INSERT INTO q VALUES (NULL), (5)")
    assert_rows(memory_ctx.execute("SELECT x FROM q"), [(None,), (5,)])


def test_extract_two_argument_function_form(memory_ctx):
    """``extract(haystack, pattern)`` 是 ``extractAll`` 的别名,二参写法合法;
    ``EXTRACT(part FROM expr)`` 的老写法不能因此坏掉。"""
    assert_rows(memory_ctx.execute("SELECT extract('hello', 'l+')"), [("ll",)])
    assert_rows(
        memory_ctx.execute("SELECT EXTRACT(YEAR FROM toDate('2020-05-04'))"),
        [(2020,)],
    )
