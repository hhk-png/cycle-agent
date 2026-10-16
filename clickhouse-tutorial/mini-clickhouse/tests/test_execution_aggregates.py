"""聚合语义测试 —— ``count``/``sum``/``avg``/``uniq``/``min``/``max`` 的精确结果。

聚合是 mini-clickhouse 里最容易"看着对、其实错"的一块,主要有三类暗坑:

1. **空输入**。``SELECT count() FROM empty`` 的结果是"一行 0",而
   ``... GROUP BY x`` 在没有分组键时是**零行**。两者都合法,绝不能混。
2. **NULL**。``count()`` 数所有行,``count(col)`` 只数非 NULL,``avg`` 跳过
   NULL……每个聚合函数对 NULL 的态度都不同。
3. **两阶段聚合**。``(SELECT ... GROUP BY ...) GROUP BY ...`` 必须和
   一步到位的结果**逐位相等** —— 这正是分布式下 ``sum(sum(x))`` 能对的原因。

最后一段用 ``AggregateFunction`` 状态列 + ``AggregatingMergeTree`` 验证
"状态可合并"这一特性,并记录 ``disk`` 路径上目前坏掉的两个点。

期望值都取自一张写死的样本表,和 ``test_execution.py`` 用同一份数据口径。
"""

from __future__ import annotations

import math

import pytest

from conftest import assert_rows
from minich.errors import MiniChError


# ══════════════════════════════════════════════════════════════════════════
# 数据准备
# ══════════════════════════════════════════════════════════════════════════


def _empty(ctx, name="e"):
    """建一张**空**表(不插任何行)—— 专门测空输入语义。"""
    ctx.create_table(name, "x Int32", engine_sql="Memory")
    return name


def _nullable(ctx, name="n"):
    """x Nullable(Int32) —— 4 行里有 2 个 NULL。"""
    ctx.create_table(name, "x Nullable(Int32)", engine_sql="Memory")
    ctx.insert(name, [(1,), (None,), (3,), (None,)])
    return name


# ══════════════════════════════════════════════════════════════════════════
# 1. 已知数据上的精确结果(用 ev 夹具)
# ══════════════════════════════════════════════════════════════════════════


def test_count_star(ev):
    """``count()`` == 行数。"""
    assert_rows(ev.execute("SELECT count() FROM ev"), [(5,)])


def test_count_of_column_on_non_null_column(ev):
    """非 NULL 列上 ``count(col)`` 与 ``count()`` 相同。"""
    assert_rows(ev.execute("SELECT count(uid) FROM ev"), [(5,)])


def test_sum_of_uint_column(ev):
    """``sum(status)`` = 200+404+500+200+301 = 1605。"""
    assert_rows(ev.execute("SELECT sum(status) FROM ev"), [(1605,)])


def test_avg_of_uint_column(ev):
    """``avg(status)`` = 1605 / 5 = 321.0(浮点)。"""
    assert_rows(ev.execute("SELECT avg(status) FROM ev"), [(321.0,)])


def test_min_max_of_column(ev):
    assert_rows(ev.execute("SELECT min(status), max(status) FROM ev"), [(200, 500)])


def test_uniq_and_uniq_exact(ev):
    """三个不同的 uid ⇒ ``uniq`` 与精确的 ``uniqExact`` 都等于 3。"""
    assert_rows(ev.execute("SELECT uniq(uid), uniqExact(uid) FROM ev"), [(3, 3)])


def test_sum_of_float_column(ev):
    """``sum(dur)`` = 0.5+1.5+2.25+0.0+3.75 = 8.0。"""
    assert_rows(ev.execute("SELECT sum(dur) FROM ev"), [(8.0,)])


def test_avg_of_float_column(ev):
    """``avg(dur)`` = 8.0 / 5 = 1.6。"""
    assert_rows(ev.execute("SELECT avg(dur) FROM ev"), [(1.6,)])


def test_many_aggregates_in_one_query(ev):
    """一条查询里同时算多个聚合 —— 结果顺序与 SELECT 顺序一致。"""
    assert_rows(
        ev.execute(
            "SELECT count(), sum(status), avg(status), min(status), max(status), uniq(uid) FROM ev"
        ),
        [(5, 1605, 321.0, 200, 500, 3)],
    )


def test_group_by_with_multiple_aggregates(ev):
    """按 uid 分组,同时取 count 与 sum(dur)。"""
    assert_rows(
        ev.execute("SELECT uid, count(), sum(dur) FROM ev GROUP BY uid ORDER BY uid"),
        [(1, 2, 2.75), (2, 2, 5.25), (3, 1, 0.0)],
    )


# ══════════════════════════════════════════════════════════════════════════
# 2. 两阶段聚合 == 单阶段
# ══════════════════════════════════════════════════════════════════════════


def test_two_stage_sum_equals_single_stage(ev):
    """``sum(sum(dur))`` 不应等于把 dur 的平方加一遍 —— 分组聚合可加。"""
    two = ev.execute("SELECT sum(s) FROM (SELECT uid, sum(dur) AS s FROM ev GROUP BY uid)")
    one = ev.execute("SELECT sum(dur) FROM ev")
    assert two.rows() == one.rows() == [(8.0,)]


def test_two_stage_sum_of_sums_per_url(ev):
    two = ev.execute("SELECT sum(s) FROM (SELECT url, sum(status) AS s FROM ev GROUP BY url)")
    one = ev.execute("SELECT sum(status) FROM ev")
    assert two.rows() == one.rows() == [(1605,)]


def test_two_stage_count_equals_unique_uid(ev):
    """``count()`` 套在"按 uid 分组"的子查询上 == uid 的去重个数。"""
    assert_rows(
        ev.execute("SELECT count() FROM (SELECT uid FROM ev GROUP BY uid)"),
        [(3,)],
    )


def test_three_level_aggregation_equals_single_stage(ev):
    """三级聚合(uid,url → uid → 全局)与一步到位相等,验证结合律。"""
    three = ev.execute(
        "SELECT sum(s2) FROM ("
        "  SELECT uid, sum(s) AS s2 FROM ("
        "    SELECT uid, url, sum(dur) AS s FROM ev GROUP BY uid, url"
        "  ) GROUP BY uid"
        ")"
    )
    one = ev.execute("SELECT sum(dur) FROM ev")
    assert three.rows() == one.rows() == [(8.0,)]


# ══════════════════════════════════════════════════════════════════════════
# 3. 多键 GROUP BY
# ══════════════════════════════════════════════════════════════════════════


def _g3(ctx):
    ctx.create_table("g3", "k1 Int32, k2 Int32, k3 Int32, v Int32", engine_sql="Memory")
    ctx.insert(
        "g3",
        [(1, 1, 1, 5), (1, 1, 1, 7), (1, 1, 2, 3), (1, 2, 1, 2), (2, 1, 1, 9)],
    )


def test_group_by_three_keys(memory_ctx):
    """三个键联合分组,count 与 sum 逐组精确。"""
    _g3(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT k1, k2, k3, count(), sum(v) FROM g3 GROUP BY k1, k2, k3 "
            "ORDER BY k1, k2, k3"
        ),
        [(1, 1, 1, 2, 12), (1, 1, 2, 1, 3), (1, 2, 1, 1, 2), (2, 1, 1, 1, 9)],
    )


def test_group_by_key_order_does_not_change_partitioning(memory_ctx):
    """``GROUP BY k1,k2`` 与 ``GROUP BY k2,k1`` 划出的分组集合相同。"""
    _g3(memory_ctx)
    a = memory_ctx.execute("SELECT k1, k2, count() FROM g3 GROUP BY k1, k2").rows()
    b = memory_ctx.execute("SELECT k2, k1, count() FROM g3 GROUP BY k2, k1").rows()
    assert sorted(a) == sorted(b)


# ══════════════════════════════════════════════════════════════════════════
# 4. 空输入语义
# ══════════════════════════════════════════════════════════════════════════


def test_empty_count_returns_one_row_with_zero(memory_ctx):
    """空表上 ``SELECT count()`` ⇒ **一行** 0(不是零行)。"""
    _empty(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT count() FROM e"), [(0,)])


def test_empty_sum_returns_zero(memory_ctx):
    """空表上 ``sum`` 返回 **0**(该类型的零元)。"""
    _empty(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT sum(x) FROM e"), [(0,)])


def test_empty_avg_is_nan(memory_ctx):
    """空表上 ``avg`` 是 ``nan``(0/0,不是 NULL)。"""
    _empty(memory_ctx)
    value = memory_ctx.execute("SELECT avg(x) FROM e").scalar()
    assert math.isnan(value)


def test_empty_min_max_return_default_value(memory_ctx):
    """非 Nullable 的 ``min``/``max`` 在空表上返回类型的默认值(0)。"""
    _empty(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT min(x), max(x) FROM e"), [(0, 0)])


def test_empty_uniq_is_zero(memory_ctx):
    _empty(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT uniq(x) FROM e"), [(0,)])


def test_empty_group_by_returns_zero_rows(memory_ctx):
    """空表上 ``GROUP BY`` ⇒ **零行**(没有键就没有分组)。"""
    _empty(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT x, count() FROM e GROUP BY x"), [])


def test_empty_group_by_count_returns_zero_rows(memory_ctx):
    _empty(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT count() FROM e GROUP BY x"), [])


# ══════════════════════════════════════════════════════════════════════════
# 5. 聚合里的 NULL
# ══════════════════════════════════════════════════════════════════════════


def test_count_star_counts_all_rows_including_nulls(memory_ctx):
    _nullable(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT count() FROM n"), [(4,)])


def test_count_of_column_skips_nulls(memory_ctx):
    """``count(x)`` 只数非 NULL —— 4 行里 2 个 NULL ⇒ 2。"""
    _nullable(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT count(x) FROM n"), [(2,)])


def test_avg_skips_nulls(memory_ctx):
    """``avg`` 跳过 NULL:(1 + 3) / 2 = 2.0,而不是 (1+3)/4 = 1.0。"""
    _nullable(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT avg(x) FROM n"), [(2.0,)])


def test_min_max_skip_nulls(memory_ctx):
    """``min``/``max`` 忽略 NULL,得到 1 与 3(而不是 NULL)。"""
    _nullable(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT min(x), max(x) FROM n"), [(1, 3)])


def test_uniq_skips_null(memory_ctx):
    """``uniq`` **跳过 NULL**:值域 {1, NULL, 3} ⇒ 2(不是 3)。

    真实 ClickHouse 的整个 ``uniq*`` 家族(以及 ``count(DISTINCT)``)都把
    NULL 排除在外 —— 可空实参会被套上 ``Null`` 组合器,含 NULL 的行在编码
    之前就被丢掉。参见 ClickHouse 官方 ``uniq``/``uniqExact`` 文档,以及
    "Fix count_distinct_optimization on Nullable columns"(PR #105439)与
    "Fix optimize_uniq_to_count for a NULL-able argument"(PR #117222) ——
    这两个 bug fix 都明确以"``uniq``/``count(DISTINCT)`` 忽略 NULL"为基准。

    (本测试此前断言的是**错误**行为 ``3``,已按真实语义更正。)
    """
    _nullable(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT uniq(x) FROM n"), [(2,)])


@pytest.mark.xfail(
    raises=MiniChError,
    reason="bug:sum 的参数类型检查用 is_numeric(),而 Nullable(Int32) 不通过 —— "
    "真 CH 的 sum 会跳过 NULL,这里却直接报 ILLEGAL_TYPE_OF_ARGUMENT",
)
def test_sum_over_nullable_is_broken(memory_ctx):
    """``sum(Nullable(Int32))`` 应当忽略 NULL 得到 4。"""
    _nullable(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT sum(x) FROM n"), [(4,)])


def test_sum_ignores_null_via_ifNull_workaround(memory_ctx):
    """在 ``sum`` 修好之前,``sum(ifNull(x, 0))`` 是可行的替代写法。"""
    _nullable(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT sum(ifNull(x, 0)) FROM n"), [(4,)])


# ══════════════════════════════════════════════════════════════════════════
# 6. AggregateFunction 状态列(Memory 引擎可用)
# ══════════════════════════════════════════════════════════════════════════


def _state_table(ctx):
    """建一张带 ``AggregateFunction(uniq, ...)`` / ``(sum, ...)`` 状态列的表。"""
    ctx.execute(
        "CREATE TABLE st ("
        "  k String,"
        "  u AggregateFunction(uniq, UInt32),"
        "  s AggregateFunction(sum, UInt64)"
        ") ENGINE = Memory"
    )
    ctx.execute("INSERT INTO st SELECT 'a', uniqState(toUInt32(1)), sumState(toUInt64(10))")
    ctx.execute("INSERT INTO st SELECT 'a', uniqState(toUInt32(2)), sumState(toUInt64(5))")
    ctx.execute("INSERT INTO st SELECT 'b', uniqState(toUInt32(2)), sumState(toUInt64(7))")


def test_uniq_merge_of_states_on_memory_engine(memory_ctx):
    """``uniqMerge(u)`` 把 'a' 上的两个 ``uniqState`` 合并 ⇒ 不同值 {1,2} = 2。"""
    _state_table(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT k, uniqMerge(u) FROM st GROUP BY k ORDER BY k"),
        [("a", 2), ("b", 1)],
    )


def test_sum_merge_of_states_on_memory_engine(memory_ctx):
    """``sumMerge(s)`` 把 'a' 上的 10 与 5 相加 ⇒ 15。"""
    _state_table(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT k, sumMerge(s) FROM st GROUP BY k ORDER BY k"),
        [("a", 15), ("b", 7)],
    )


def test_uniq_merge_of_subquery_states(memory_ctx):
    """``uniqMerge(uniqState(...))`` 的等价写法:状态经子查询再合并。"""
    memory_ctx.create_table("u", "uid UInt32", engine_sql="Memory")
    memory_ctx.insert("u", [(1,), (2,), (1,)])
    assert_rows(
        memory_ctx.execute("SELECT uniqMerge(g) FROM (SELECT uniqState(uid) AS g FROM u)"),
        [(2,)],
    )


def test_sum_merge_of_subquery_states(memory_ctx):
    memory_ctx.create_table("u", "d Float64", engine_sql="Memory")
    memory_ctx.insert("u", [(1.5,), (2.5,), (4.0,)])
    assert_rows(
        memory_ctx.execute("SELECT sumMerge(g) FROM (SELECT sumState(d) AS g FROM u)"),
        [(8.0,)],
    )


@pytest.mark.xfail(
    raises=TypeError,
    reason="bug:往落盘的 AggregatingMergeTree 写 uniqState 时,内部 HyperLogLog 对象"
    "无法序列化成字节(TypeError: cannot convert 'HyperLogLog' object to bytes)",
)
def test_disk_aggregating_mergetree_uniq_state_is_broken(disk_ctx):
    """AggregatingMergeTree(落盘)本应能存 ``uniqState`` 并在读时 ``uniqMerge``。"""
    disk_ctx.execute(
        "CREATE TABLE amt (k String, u AggregateFunction(uniq, UInt32)) "
        "ENGINE = AggregatingMergeTree ORDER BY k"
    )
    disk_ctx.execute("INSERT INTO amt SELECT 'a', uniqState(toUInt32(1))")


@pytest.mark.xfail(
    raises=MiniChError,
    reason="bug:落盘的状态列序列化被截断 —— INSERT sumState 能过,但读时 sumMerge "
    "报 UNEXPECTED_END_OF_FILE(状态字节长度不对)",
)
def test_disk_aggregating_mergetree_sum_state_merge_is_broken(disk_ctx):
    """AggregatingMergeTree(落盘)本应在读时正确 ``sumMerge`` 出 10。"""
    disk_ctx.execute(
        "CREATE TABLE amt (k String, s AggregateFunction(sum, UInt64)) "
        "ENGINE = AggregatingMergeTree ORDER BY k"
    )
    disk_ctx.execute("INSERT INTO amt SELECT 'a', sumState(toUInt64(10))")
    assert_rows(disk_ctx.execute("SELECT k, sumMerge(s) FROM amt GROUP BY k"), [("a", 10)])


@pytest.mark.xfail(
    raises=MiniChError,
    reason="bug:SimpleAggregateFunction(sum, UInt64) 列直接送给 sum() 会被当成非数值类型,"
    "报 ILLEGAL_TYPE_OF_ARGUMENT(真 CH 里 sum 会透明地作用在底层类型上)",
)
def test_simple_aggregate_function_sum_is_broken(disk_ctx):
    """``SimpleAggregateFunction`` 本可直接用普通 ``sum()`` 查询。"""
    disk_ctx.execute(
        "CREATE TABLE sagg (k String, s SimpleAggregateFunction(sum, UInt64)) "
        "ENGINE = AggregatingMergeTree ORDER BY k"
    )
    disk_ctx.execute("INSERT INTO sagg SELECT 'a', toUInt64(10)")
    assert_rows(disk_ctx.execute("SELECT sum(s) FROM sagg"), [(10,)])
