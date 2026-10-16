"""端到端 SQL 执行测试 —— 把 ``Context.execute()`` 当成一个真的 SQL 引擎来跑。

这份文件关心的是**"从一条 SQL 语句到一组结果行"这条完整链路**,也就是
真实 ClickHouse 里 ``InterpreterSelectQuery`` 那条路:解析 → 分析 → 执行 →
投影。断言一律用**精确的期望行**(``assert_rows``),因为一个"行数对了但内容
错了"的 bug(比如 ``ORDER BY`` 与 ``LIMIT BY`` 顺序颠倒、``HAVING`` 根本没生效)
用计数是抓不到的。

覆盖的范围:

* 算术/比较/NULL 三值逻辑 —— 类型提升与显式窄化时的回绕;
* ``ORDER BY``(含 NULLS FIRST/LAST)、``LIMIT``/``OFFSET``/``LIMIT BY``;
* ``DISTINCT``、``GROUP BY``、``HAVING``、``WITH TOTALS``;
* ``WHERE`` 里用别名、``IN``(字面量元组 / 子查询)、``CASE``/``if``/``multiIf``;
* ``UNION ALL``、标量子查询、``ARRAY JOIN``、窗口函数;
* ``JOIN`` 的各种 strictness,(可选)``join_use_nulls``;
* ``MergeTree`` / ``ReplacingMergeTree`` 上的 ``FINAL`` 与 ``OPTIMIZE``。

.. note::

  凡是与真实 ClickHouse 语义**不一致**的地方,都单独写成一个
  带 ``xfail(raises=...)`` 的测试 —— 这是"把 bug 记录在案"
   而不是"绕过它"。绿色代表当前行为符合预期;一旦实现被修好,
   ``strict`` 会让对应的 ``XPASS`` 变成失败,提醒来更新文档。
"""

from __future__ import annotations

import math

import pytest

from conftest import assert_rows
from minich.errors import MiniChError


# ══════════════════════════════════════════════════════════════════════════
# 小工具:按需建样本表
# ══════════════════════════════════════════════════════════════════════════


def _t(ctx, name="t"):
    """(a Int32, b String) —— 有重复值,方便测 GROUP BY / DISTINCT / JOIN 左表。"""
    ctx.create_table(name, "a Int32, b String", engine_sql="Memory")
    ctx.insert(name, [(3, "x"), (1, "y"), (2, "x"), (1, "z"), (3, "y")])
    return name


def _nn(ctx, name="nn"):
    """x Nullable(Int32) —— 带两个 NULL,专门测 NULLS FIRST/LAST。"""
    ctx.create_table(name, "x Nullable(Int32)", engine_sql="Memory")
    ctx.insert(name, [(None,), (2,), (1,), (None,)])
    return name


def _win(ctx, name="w"):
    """(g String, v Int32) —— 两个分组、组内有序,窗口函数的标配样本。"""
    ctx.create_table(name, "g String, v Int32", engine_sql="Memory")
    ctx.insert(name, [("a", 1), ("a", 2), ("a", 3), ("b", 10), ("b", 20)])
    return name


def _lr(ctx):
    """建一对 (左表 l, 右表 r),键有多对多关系,覆盖所有 JOIN 分支。"""
    ctx.create_table("l", "k Int32, lv String", engine_sql="Memory")
    ctx.insert("l", [(1, "L1"), (2, "L2"), (2, "L2b"), (3, "L3")])
    ctx.create_table("r", "k Int32, rv String", engine_sql="Memory")
    ctx.insert("r", [(2, "R2"), (2, "R2b"), (4, "R4")])


# ══════════════════════════════════════════════════════════════════════════
# 1. 算术与比较语义
# ══════════════════════════════════════════════════════════════════════════


def test_constant_arithmetic(memory_ctx):
    """没有 FROM 的常量表达式 —— 走"恰好一行"的伪源。"""
    assert_rows(
        memory_ctx.execute("SELECT 1 + 1, 7 / 2, 7 % 3, 2 * 3, 10 - 4"),
        [(2, 3.5, 1, 6, 6)],
    )


def test_division_returns_float():
    """``/`` 是**浮点除法** —— 整数相除也得到 Float64,和 C 的 ``/`` 不同。

    (这里不用 fixture:一条常量查询不需要任何状态。)
    """
    from minich.execution.context import Context

    ctx = Context()
    try:
        result = ctx.execute("SELECT 8 / 2")
        assert_rows(result, [(4.0,)])
        assert result.column_types() == ["Float64"]
    finally:
        ctx.shutdown()


def test_division_by_zero_follows_ieee():
    """除零按 IEEE 754 得到 ``inf``/``-inf`` —— ClickHouse 不抛异常。"""
    from minich.execution.context import Context

    ctx = Context()
    try:
        rowa = ctx.execute("SELECT 1 / 0").rows()[0][0]
        rowb = ctx.execute("SELECT -1 / 0").rows()[0][0]
        assert rowa == math.inf
        assert rowb == -math.inf
    finally:
        ctx.shutdown()


def test_comparison_yields_0_or_1(memory_ctx):
    """比较运算的返回类型是 ``UInt8``,真为 1、假为 0。"""
    assert_rows(
        memory_ctx.execute("SELECT 2 > 1, 1 > 2, 1 = 1, 1 != 1, 2 >= 2, 2 <= 1"),
        [(1, 0, 1, 0, 1, 0)],
    )


def test_intdiv_truncates_toward_zero(memory_ctx):
    """``intDiv`` 向零截断:``intDiv(-7, 2) == -3``(不是 -4)。"""
    assert_rows(
        memory_ctx.execute("SELECT intDiv(7, 2), intDiv(-7, 2), intDiv(9, 2)"),
        [(3, -3, 4)],
    )


def test_intdiv_or_zero_hides_division_by_zero(memory_ctx):
    """``intDivOrZero(x, 0)`` 返回 0 —— 整数世界里"安全的除法"。"""
    assert_rows(memory_ctx.execute("SELECT intDivOrZero(9, 0)"), [(0,)])


def test_modulo_keeps_sign_of_dividend(memory_ctx):
    """取模结果的符号跟随**被除数**:``-5 % 3 == -2``。"""
    assert_rows(memory_ctx.execute("SELECT 5 % 3, -5 % 3"), [(2, -2)])


def test_float_promotion(memory_ctx):
    """整数 + 浮点 → 浮点;结果是精确的 3.5。"""
    assert_rows(memory_ctx.execute("SELECT 1 + 2.5"), [(3.5,)])


def test_integer_arithmetic_widens_beyond_uint8_type(memory_ctx):
    """整数加法的结果类型**被拓宽**,而不是在 UInt8 里回绕。

    这是 mini-clickhouse 与真实 ClickHouse 的一处**公开偏离**:
    真 CH 里 ``toUInt8(255) + toUInt8(1)`` 会回绕成 0(结果仍是 UInt8),
    这里则提升成 ``UInt16`` 得到 256。断言实际行为 + 记录类型。
    """
    result = memory_ctx.execute("SELECT toUInt8(255) + toUInt8(1)")
    assert_rows(result, [(256,)])
    assert result.column_types() == ["UInt16"]


@pytest.mark.xfail(
    raises=MiniChError,
    reason="已知缺陷:``LEFT ARRAY JOIN`` 对空数组的行没有补元素默认值,而是试图把 None 写进非空"
           "String 列,抛 ILLEGAL_TYPE_OF_ARGUMENT(24)。正确语义是保留该行、``tag`` 取 ''。",
)
def test_left_array_join_keeps_empty_rows(memory_ctx):
    """``LEFT ARRAY JOIN`` —— 空数组的行应当保留(``tag`` 补元素类型的默认值)。"""
    memory_ctx.execute("CREATE TABLE arr (id UInt32, tags Array(String)) ENGINE = Memory")
    memory_ctx.insert("arr", [(1, ["a"]), (2, []), (3, ["c"])])
    assert_rows(
        memory_ctx.execute(
            "SELECT id, tag FROM arr LEFT ARRAY JOIN tags AS tag ORDER BY id, tag"
        ),
        [(1, "a"), (2, ""), (3, "c")],
    )


# ══════════════════════════════════════════════════════════════════════════
# 13. 窗口函数
# ══════════════════════════════════════════════════════════════════════════
#
# 注意:窗口函数必须写成 ``... OVER (...) AS 别名``。裸写(顶层没有别名)
# 当前会报 NOT_IMPLEMENTED —— 见本段末尾的 xfail。
#


def test_row_number_partition(memory_ctx):
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT g, v, row_number() OVER (PARTITION BY g ORDER BY v) AS rn "
            "FROM w ORDER BY g, v"
        ),
        [("a", 1, 1), ("a", 2, 2), ("a", 3, 3), ("b", 10, 1), ("b", 20, 2)],
    )


def test_sum_over_partition_is_constant_per_group(memory_ctx):
    """``PARTITION BY`` 没有 ``ORDER BY`` ⇒ 整个分区一个值(窗口是"全分区")。"""
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT g, v, sum(v) OVER (PARTITION BY g) AS s FROM w ORDER BY g, v"
        ),
        [("a", 1, 6), ("a", 2, 6), ("a", 3, 6), ("b", 10, 30), ("b", 20, 30)],
    )


def test_running_sum_with_order_by(memory_ctx):
    """``ORDER BY`` 把窗口变成"到当前行为止" ⇒ 前缀和(默认累积帧)。"""
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT g, v, sum(v) OVER (PARTITION BY g ORDER BY v) AS s "
            "FROM w ORDER BY g, v"
        ),
        [("a", 1, 1), ("a", 2, 3), ("a", 3, 6), ("b", 10, 10), ("b", 20, 30)],
    )


def test_running_sum_with_explicit_rows_frame(memory_ctx):
    """显式写 ``ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`` —— 与默认一致。"""
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT g, v, sum(v) OVER (PARTITION BY g ORDER BY v "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s "
            "FROM w ORDER BY g, v"
        ),
        [("a", 1, 1), ("a", 2, 3), ("a", 3, 6), ("b", 10, 10), ("b", 20, 30)],
    )


def test_rank_and_dense_rank(memory_ctx):
    """这里没有并列,所以 rank 与 dense_rank 都退化成 1,2,3。"""
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT g, v, rank() OVER (PARTITION BY g ORDER BY v) AS r FROM w ORDER BY g, v"
        ),
        [("a", 1, 1), ("a", 2, 2), ("a", 3, 3), ("b", 10, 1), ("b", 20, 2)],
    )
    assert_rows(
        memory_ctx.execute(
            "SELECT g, v, dense_rank() OVER (PARTITION BY g ORDER BY v) AS r "
            "FROM w ORDER BY g, v"
        ),
        [("a", 1, 1), ("a", 2, 2), ("a", 3, 3), ("b", 10, 1), ("b", 20, 2)],
    )


def test_avg_over_partition_returns_float(memory_ctx):
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT g, v, avg(v) OVER (PARTITION BY g) AS m FROM w ORDER BY g, v"
        ),
        [("a", 1, 2.0), ("a", 2, 2.0), ("a", 3, 2.0), ("b", 10, 15.0), ("b", 20, 15.0)],
    )


def test_count_over_partition(memory_ctx):
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT g, v, count() OVER (PARTITION BY g) AS c FROM w ORDER BY g, v"
        ),
        [("a", 1, 3), ("a", 2, 3), ("a", 3, 3), ("b", 10, 2), ("b", 20, 2)],
    )


def test_lag_in_frame(memory_ctx):
    """``lagInFrame``:分区内取前一行;每分区第一行取类型默认值(这里是 0)。"""
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT g, v, lagInFrame(v) OVER (PARTITION BY g ORDER BY v) AS p "
            "FROM w ORDER BY g, v"
        ),
        [("a", 1, 0), ("a", 2, 1), ("a", 3, 2), ("b", 10, 0), ("b", 20, 10)],
    )


def test_first_and_last_value(memory_ctx):
    """``first_value`` / ``last_value`` 取窗口帧的首尾。"""
    memory_ctx.create_table("fv", "v Int32", engine_sql="Memory")
    memory_ctx.insert("fv", [(1,), (2,), (3,)])
    assert_rows(
        memory_ctx.execute("SELECT v, first_value(v) OVER (ORDER BY v) AS f FROM fv ORDER BY v"),
        [(1, 1), (2, 1), (3, 1)],
    )
    assert_rows(
        memory_ctx.execute(
            "SELECT v, last_value(v) OVER (ORDER BY v "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS l "
            "FROM fv ORDER BY v"
        ),
        [(1, 3), (2, 3), (3, 3)],
    )


def test_settings_clause_on_select(memory_ctx):
    """``... SETTINGS join_use_nulls = 1`` 应当和会话级设置一样生效。"""
    _lr(memory_ctx)
    result = memory_ctx.execute(
        "SELECT l.k, r.rv FROM l LEFT JOIN r ON l.k = r.k ORDER BY l.k, r.rv "
        "SETTINGS join_use_nulls = 1"
    )
    assert_rows(
        result,
        [(1, None), (2, "R2"), (2, "R2"), (2, "R2b"), (2, "R2b"), (3, None)],
    )


# ══════════════════════════════════════════════════════════════════════════
# 15. MergeTree / FINAL(落盘,用 disk_ctx)
# ══════════════════════════════════════════════════════════════════════════


def test_mergetree_roundtrip_sorted(disk_ctx):
    """MergeTree 的 part 落盘、读回来仍能按 ORDER BY 排序 —— 一次基本往返。"""
    disk_ctx.create_table("mt", "a Int32, b String", engine_sql="MergeTree ORDER BY a")
    disk_ctx.insert("mt", [(2, "x"), (1, "y"), (3, "z")])
    assert_rows(disk_ctx.execute("SELECT * FROM mt ORDER BY a"), [(1, "y"), (2, "x"), (3, "z")])


def test_mergetree_final_is_noop_for_distinct_rows(disk_ctx):
    """普通 MergeTree 没有去重家族逻辑,FINAL 对互不相同的行是**空操作**。"""
    disk_ctx.create_table("mt", "a Int32, b String", engine_sql="MergeTree ORDER BY a")
    disk_ctx.insert("mt", [(2, "x"), (1, "y"), (3, "z")])
    assert_rows(
        disk_ctx.execute("SELECT * FROM mt FINAL ORDER BY a"),
        [(1, "y"), (2, "x"), (3, "z")],
    )


def test_replacing_final_collapses_identical_rows(disk_ctx):
    """``FINAL`` 至少能把**逐列完全相同**的重复行折叠成一行。"""
    disk_ctx.create_table("rmt", "id Int32, ver Int32, v String", engine_sql="ReplacingMergeTree(ver) ORDER BY id")
    disk_ctx.insert("rmt", [(1, 1, "dup"), (1, 1, "dup"), (2, 1, "a"), (2, 1, "a")])
    assert_rows(disk_ctx.execute("SELECT * FROM rmt FINAL ORDER BY id"), [(1, 1, "dup"), (2, 1, "a")])


def test_optimize_final_applies_version_replacement(disk_ctx):
    """``OPTIMIZE ... FINAL`` 真的按 **ReplacingMergeTree 的版本列**收敛:
    同键只留版本最大的那一行。"""
    disk_ctx.create_table("rmt", "id Int32, ver Int32, v String", engine_sql="ReplacingMergeTree(ver) ORDER BY id")
    disk_ctx.insert("rmt", [(1, 1, "old"), (1, 2, "new"), (2, 1, "a")])
    disk_ctx.execute("OPTIMIZE TABLE rmt FINAL")
    assert_rows(disk_ctx.execute("SELECT * FROM rmt ORDER BY id"), [(1, 2, "new"), (2, 1, "a")])


def test_replacing_final_version_dedup(disk_ctx):
    """``SELECT ... FROM rmt FINAL`` 本就该做版本收敛(不依赖 OPTIMIZE)。"""
    disk_ctx.create_table("rmt", "id Int32, ver Int32, v String", engine_sql="ReplacingMergeTree(ver) ORDER BY id")
    disk_ctx.insert("rmt", [(1, 1, "old"), (1, 2, "new"), (2, 1, "a")])
    assert_rows(disk_ctx.execute("SELECT * FROM rmt FINAL ORDER BY id"), [(1, 2, "new"), (2, 1, "a")])


# ══════════════════════════════════════════════════════════════════════════
# 16. ev 夹具:DateTime 存的是 epoch 秒
# ══════════════════════════════════════════════════════════════════════════


def test_ev_select_ts_is_epoch_seconds(ev):
    """``DateTime`` 列读出来是 **epoch 秒整数**(不是字符串,也不是 datetime 对象)。"""
    assert_rows(
        ev.execute("SELECT ts FROM ev ORDER BY ts"),
        [(1704067200,), (1704067201,), (1704067202,), (1704067203,), (1704067204,)],
    )


def test_ev_group_by_uid(ev):
    assert_rows(
        ev.execute("SELECT uid, count() FROM ev GROUP BY uid ORDER BY uid"),
        [(1, 2), (2, 2), (3, 1)],
    )


def test_ev_group_by_url_with_sum(ev):
    assert_rows(
        ev.execute("SELECT url, count(), sum(status) FROM ev GROUP BY url ORDER BY url"),
        [("/a", 3, 1001), ("/b", 1, 404), ("/c", 1, 200)],
    )


def test_ev_sum_of_floats(ev):
    """``dur`` 是 Float64,``sum`` 保留小数(不是整数截断)。"""
    assert_rows(ev.execute("SELECT sum(dur) FROM ev"), [(8.0,)])


# ══════════════════════════════════════════════════════════════════════════
# 17. ORDER BY 的 NULL 位置与多键排序
# ══════════════════════════════════════════════════════════════════════════
#
# 关键语义(与真实 ClickHouse 一致):**NULL 被当作"最大"的值**。
# 因此不写修饰词时,``ASC`` 把 NULL 排在最后(等于 NULLS LAST),
# ``DESC`` 把 NULL 排在最前(等于 NULLS FIRST)。这与很多把 NULL 当
# "最小"的数据库(SQLite、MySQL 的 ASC)正好相反 —— 是个常踩的坑。
#
# 样本 ``_nn``:``x Nullable(Int32)`` = (NULL, 2, 1, NULL)。
#


def test_order_by_asc_default_puts_nulls_last(memory_ctx):
    """``ASC`` 不写修饰词 ⇒ NULL 在最后(NULL 最大)。"""
    _nn(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT x FROM nn ORDER BY x ASC"), [(1,), (2,), (None,), (None,)])


def test_order_by_desc_default_puts_nulls_first(memory_ctx):
    """``DESC`` 不写修饰词 ⇒ NULL 在最前(还是"NULL 最大"在起作用)。"""
    _nn(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT x FROM nn ORDER BY x DESC"), [(None,), (None,), (2,), (1,)])


def test_order_by_explicit_nulls_first(memory_ctx):
    """``ASC NULLS FIRST`` 显式把 NULL 顶到最前 —— 覆盖默认排序。"""
    _nn(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT x FROM nn ORDER BY x ASC NULLS FIRST"),
        [(None,), (None,), (1,), (2,)],
    )


def test_order_by_explicit_nulls_last(memory_ctx):
    """``ASC NULLS LAST``(与默认一致)与 ``DESC NULLS LAST``(覆盖默认)都要能写。"""
    _nn(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT x FROM nn ORDER BY x ASC NULLS LAST"),
        [(1,), (2,), (None,), (None,)],
    )
    assert_rows(
        memory_ctx.execute("SELECT x FROM nn ORDER BY x DESC NULLS LAST"),
        [(2,), (1,), (None,), (None,)],
    )


def test_order_by_multi_key_mixed_directions(memory_ctx):
    """多键排序:第一键升序,第二键降序 —— 每个方向独立生效。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a, b FROM t ORDER BY a, b DESC"),
        [(1, "z"), (1, "y"), (2, "x"), (3, "y"), (3, "x")],
    )


def test_order_by_first_key_desc_then_second_asc(memory_ctx):
    """把方向换到第一键:``a DESC, b`` —— 组间降序、组内升序。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a, b FROM t ORDER BY a DESC, b"),
        [(3, "x"), (3, "y"), (2, "x"), (1, "y"), (1, "z")],
    )


# ══════════════════════════════════════════════════════════════════════════
# 18. LIMIT / OFFSET / ``LIMIT n, m`` / LIMIT BY
# ══════════════════════════════════════════════════════════════════════════
#
# 样本 ``_t``(a Int32, b String):a = (3, 1, 2, 1, 3),b = (x, y, x, z, y)。
# 一律配 ``ORDER BY`` 让"取第几行"确定下来。
#


def test_limit_takes_first_n_after_order(memory_ctx):
    """``LIMIT`` 作用在**排好序之后**的结果上。"""
    _t(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT a FROM t ORDER BY a LIMIT 2"), [(1,), (1,)])


def test_limit_with_offset_skips_rows(memory_ctx):
    """``LIMIT n OFFSET k`` —— 先跳过 k 行再取 n 行。"""
    _t(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT a FROM t ORDER BY a LIMIT 2 OFFSET 1"), [(1,), (2,)])


def test_limit_comma_is_offset_count(memory_ctx):
    """``LIMIT k, n``(MySQL 风格)里 **k 是 OFFSET、n 是数量**,不是"从 k 取到 n"。"""
    _t(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT a FROM t ORDER BY a LIMIT 1, 2"), [(1,), (2,)])
    # 与 ``LIMIT 2 OFFSET 1`` 结果等同 —— 两种写法互相印证。
    assert_rows(memory_ctx.execute("SELECT a FROM t ORDER BY a LIMIT 2 OFFSET 1"), [(1,), (2,)])


def test_limit_by_keeps_first_row_per_group(memory_ctx):
    """``LIMIT n BY expr`` —— 每个 ``b`` 分组只保留 n 行。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT b, a FROM t ORDER BY a LIMIT 1 BY b"),
        [("y", 1), ("z", 1), ("x", 2)],
    )


def test_limit_by_runs_after_order_by(memory_ctx):
    """LIMIT BY 在 **ORDER BY 之后**取每组的第一行 —— 换个排序方向,取到的行也变。

    这正验证了执行顺序:先全局排序 ``a``,再按 ``b`` 分组取第一行。
    若顺序颠倒(先 LIMIT BY 再排序),两种方向会取到同一批行。
    """
    _t(memory_ctx)
    # 升序:每个 b 里 a 最小的那一行 → y=1, z=1, x=2
    assert_rows(
        memory_ctx.execute("SELECT b, a FROM t ORDER BY a LIMIT 1 BY b"),
        [("y", 1), ("z", 1), ("x", 2)],
    )
    # 降序:每个 b 里 a 最大的那一行 → x=3, y=3, z=1
    assert_rows(
        memory_ctx.execute("SELECT b, a FROM t ORDER BY a DESC LIMIT 1 BY b"),
        [("x", 3), ("y", 3), ("z", 1)],
    )


# ══════════════════════════════════════════════════════════════════════════
# 19. DISTINCT
# ══════════════════════════════════════════════════════════════════════════


def test_distinct_single_column(memory_ctx):
    _t(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT DISTINCT b FROM t ORDER BY b"), [("x",), ("y",), ("z",)])


def test_distinct_multiple_columns(memory_ctx):
    """``DISTINCT`` 对**整行元组**去重,不是对某一列。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT DISTINCT a, b FROM t ORDER BY a, b"),
        [(1, "y"), (1, "z"), (2, "x"), (3, "x"), (3, "y")],
    )


def test_distinct_keeps_duplicate_values_in_one_column_when_tuples_differ(memory_ctx):
    """``DISTINCT a`` 只去重 a,所以 (3) 只出现一次;而上面那种情况下 a=1 会有两行。"""
    _t(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT DISTINCT a FROM t ORDER BY a"), [(1,), (2,), (3,)])


# ══════════════════════════════════════════════════════════════════════════
# 20. GROUP BY / HAVING / WITH TOTALS
# ══════════════════════════════════════════════════════════════════════════


def test_group_by_single_key_with_count(memory_ctx):
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT b, count() AS c FROM t GROUP BY b ORDER BY b"),
        [("x", 2), ("y", 2), ("z", 1)],
    )


def test_group_by_multiple_keys(memory_ctx):
    """多列 ``GROUP BY`` —— 每个 (b, a) 组合一行。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT b, a, count() FROM t GROUP BY b, a ORDER BY b, a"),
        [("x", 2, 1), ("x", 3, 1), ("y", 1, 1), ("y", 3, 1), ("z", 1, 1)],
    )


@pytest.mark.xfail(
    raises=AssertionError,
    reason="已知缺陷:``HAVING`` 子句被**整体忽略** —— 连 ``HAVING 0`` 都返回全部组。"
           "真实 ClickHouse 会在聚合后按该谓词过滤掉不满足的组,这里一行都不过滤。",
)
def test_having_filters_groups(memory_ctx):
    """``HAVING`` 在聚合**之后**过滤 —— 只留 count>=2 的组。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT b, count() AS c FROM t GROUP BY b HAVING count() >= 2 ORDER BY b"),
        [("x", 2), ("y", 2)],
    )


@pytest.mark.xfail(
    raises=AssertionError,
    reason="已知缺陷:同 ``HAVING`` 整体被忽略 —— 引用 SELECT 别名 ``c`` 的谓词照样不生效。",
)
def test_having_can_reference_select_alias(memory_ctx):
    """``HAVING`` 里能直接用 SELECT 起的别名 ``c`` —— 和 WHERE 用别名一样的便利。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT b, count() AS c FROM t GROUP BY b HAVING c >= 2 ORDER BY b"),
        [("x", 2), ("y", 2)],
    )


def test_having_true_predicate_keeps_all_groups(memory_ctx):
    """``HAVING 1``(恒真)返回全部组 —— 记录当前真实行为,与上面被忽略的 xfail 对照。

    注意:这个测试即使 HAVING 完全被忽略也会"通过",所以它**不**能证明 HAVING
    生效,只是用来固定住"恒真谓词不该改变结果"这个下界。
    """
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT b, count() AS c FROM t GROUP BY b HAVING 1 ORDER BY b"),
        [("x", 2), ("y", 2), ("z", 1)],
    )


def test_group_by_with_totals_appends_total_row(memory_ctx):
    """``WITH TOTALS`` 追加一行"全表汇总":``('', 5)``。

    .. note::

       与真实 ClickHouse 的差异:那边 TOTALS 是**单独一个 Block** 发出来的,
       客户端能把它和普通结果行分开读;这里把 TOTALS 行**并进了同一个 Block**,
       ``result.rows()`` 里它和普通行混在一起(所以未分列的行取到类型默认值 ``''``)。
       未写 ``ORDER BY`` 时它出现在末尾,内容是对的。写着 ``ORDER BY`` 时
       TOTALS 行会被排序打乱位置 —— 见下面那个 xfail。
    """
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT b, count() AS c FROM t GROUP BY b WITH TOTALS"),
        [("x", 2), ("y", 2), ("z", 1), ("", 5)],
    )


@pytest.mark.xfail(
    raises=AssertionError,
    reason="已知缺陷:``GROUP BY ... WITH TOTALS`` 的 TOTALS 行被并进同一个 Block 后"
           "也参与了 ``ORDER BY`` 排序,于是 ``''`` 被排到最前。真实 ClickHouse 的 TOTALS "
           "在独立 Block 里,不受 ORDER BY 影响,应始终排在普通结果行之后。",
)
def test_with_totals_row_is_not_reordered_by_order_by(memory_ctx):
    """正确的语义:TOTALS 行固定排在普通行**之后**,不受 ORDER BY 影响。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT b, count() AS c FROM t GROUP BY b WITH TOTALS ORDER BY b"),
        [("x", 2), ("y", 2), ("z", 1), ("", 5)],
    )


# ══════════════════════════════════════════════════════════════════════════
# 21. WHERE 用别名 / IN / CASE
# ══════════════════════════════════════════════════════════════════════════


def test_where_can_use_select_alias(memory_ctx):
    """``SELECT a*2 AS d ... WHERE d > 3`` —— WHERE 里能用 SELECT 的别名。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a * 2 AS d FROM t WHERE d > 3 ORDER BY d"),
        [(4,), (6,), (6,)],
    )


def test_in_with_literal_tuple(memory_ctx):
    """``IN (1, 3)`` —— 字面量列表,命中任意一个即留下。"""
    _t(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT a FROM t WHERE a IN (1, 3) ORDER BY a"), [(1,), (1,), (3,), (3,)])


def test_not_in_with_literal_tuple(memory_ctx):
    _t(memory_ctx)
    assert_rows(memory_ctx.execute("SELECT a FROM t WHERE a NOT IN (1, 3) ORDER BY a"), [(2,)])


def test_in_with_subquery(memory_ctx):
    """``IN (SELECT ...)`` —— 子查询当集合用。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a FROM t WHERE a IN (SELECT a FROM t WHERE a > 2) ORDER BY a"),
        [(3,), (3,)],
    )


def test_not_in_with_subquery(memory_ctx):
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a FROM t WHERE a NOT IN (SELECT a FROM t WHERE a > 1) ORDER BY a"),
        [(1,), (1,)],
    )


def test_case_searched_form(memory_ctx):
    """``CASE WHEN ... THEN ... ELSE ... END`` —— 逐条件求值。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT a, CASE WHEN a = 1 THEN 'one' WHEN a = 2 THEN 'two' ELSE 'other' END "
            "FROM t ORDER BY a"
        ),
        [(1, "one"), (1, "one"), (2, "two"), (3, "other"), (3, "other")],
    )


def test_if_function(memory_ctx):
    """``if(cond, then, else)`` —— CASE 的函数形态。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a, if(a > 1, 'big', 'small') FROM t ORDER BY a"),
        [(1, "small"), (1, "small"), (2, "big"), (3, "big"), (3, "big")],
    )


def test_multiif_function(memory_ctx):
    """``multiIf`` —— 多路 if,最后那个参数是兜底。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT a, multiIf(a = 1, 'one', a = 2, 'two', 'other') FROM t ORDER BY a"
        ),
        [(1, "one"), (1, "one"), (2, "two"), (3, "other"), (3, "other")],
    )


# ══════════════════════════════════════════════════════════════════════════
# 22. UNION
# ══════════════════════════════════════════════════════════════════════════


def test_union_all_concatenates_with_duplicates(memory_ctx):
    """``UNION ALL`` **不去重**,按分支顺序拼接:5 + 5 = 10 行。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a FROM t UNION ALL SELECT a FROM t"),
        [(3,), (1,), (2,), (1,), (3,), (3,), (1,), (2,), (1,), (3,)],
    )


def test_union_distinct_removes_duplicates(memory_ctx):
    """``UNION DISTINCT``(等价于裸 ``UNION``)对整体结果去重 → 3 个不同值。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a FROM t UNION DISTINCT SELECT a FROM t"),
        [(3,), (1,), (2,)],
    )


@pytest.mark.xfail(
    raises=AssertionError,
    reason="已知缺陷:写在 ``UNION ALL`` **末尾**的 ``ORDER BY`` 只作用到最后一个分支,"
           "没有把整个并集排序(结果仍是拼接顺序)。真实 ClickHouse 里它会排全部结果。",
)
def test_order_by_applies_to_whole_union_all(memory_ctx):
    """正确语义:``A UNION ALL B ORDER BY a`` 排的是**并集整体**。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a FROM t UNION ALL SELECT a FROM t ORDER BY a"),
        [(1,), (1,), (1,), (1,), (2,), (2,), (3,), (3,), (3,), (3,)],
    )


def test_order_by_after_union_all_only_sorts_last_branch(memory_ctx):
    """记录当前真实行为:末尾的 ``ORDER BY`` 只排了第二个分支。

    第一分支保持来源顺序 ``(3,1,2,1,3)``,第二分支被排成 ``(1,1,2,3,3)`` ——
    与上面那个"应当排并集整体"的 xfail 正好互相印证。
    """
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a FROM t UNION ALL SELECT a FROM t ORDER BY a"),
        [(3,), (1,), (2,), (1,), (3,), (1,), (1,), (2,), (3,), (3,)],
    )


# ══════════════════════════════════════════════════════════════════════════
# 23. 标量子查询
# ══════════════════════════════════════════════════════════════════════════


def test_scalar_subquery_in_select(memory_ctx):
    """``SELECT (SELECT max(a) FROM t) AS mx`` —— 标量子查询在每行都带上同一个值。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a, (SELECT max(a) FROM t) AS mx FROM t ORDER BY a"),
        [(1, 3), (1, 3), (2, 3), (3, 3), (3, 3)],
    )


def test_scalar_subquery_in_where(memory_ctx):
    """标量子查询也能出现在 ``WHERE`` 的比较里。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a FROM t WHERE a = (SELECT max(a) FROM t) ORDER BY a"),
        [(3,), (3,)],
    )


def test_scalar_subquery_with_no_rows_yields_type_default(memory_ctx):
    """子查询**一行都没有**时,标量取该类型的默认值(Int32 → 0),而不是 NULL。"""
    _t(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT a, (SELECT max(a) FROM t WHERE a > 100) AS m FROM t WHERE a = 1"),
        [(1, 0), (1, 0)],
    )


# ══════════════════════════════════════════════════════════════════════════
# 24. ARRAY JOIN(非 LEFT)
# ══════════════════════════════════════════════════════════════════════════
#
# ``_arrne`` 刻意**不含空数组** —— 空数组会触发一个已知缺陷(见文件末尾的
# xfail):非 LEFT 的 ARRAY JOIN 假设每个数组至少一个元素。
#


def _arrne(ctx, name="arrne"):
    """(id UInt32, tags Array(String)) —— 数组都非空,行数 = 数组长度之和。"""
    ctx.create_table(name, "id UInt32, tags Array(String)", engine_sql="Memory")
    ctx.insert(name, [(1, ["a", "b"]), (2, ["c"]), (3, ["d", "e", "f"])])
    return name


def test_array_join_expands_rows(memory_ctx):
    """``ARRAY JOIN`` 把每个数组元素摊成一行 —— 3 行变 6 行。"""
    _arrne(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT id, tag FROM arrne ARRAY JOIN tags AS tag ORDER BY id, tag"),
        [(1, "a"), (1, "b"), (2, "c"), (3, "d"), (3, "e"), (3, "f")],
    )


def test_array_join_on_int_array(memory_ctx):
    """数组元素是整数也一样摊开。"""
    memory_ctx.create_table("ai", "id UInt32, xs Array(Int32)", engine_sql="Memory")
    memory_ctx.insert("ai", [(1, [1, 2]), (2, [3])])
    assert_rows(
        memory_ctx.execute("SELECT id, x FROM ai ARRAY JOIN xs AS x ORDER BY id, x"),
        [(1, 1), (1, 2), (2, 3)],
    )


@pytest.mark.xfail(
    raises=IndexError,
    reason="已知缺陷:非 LEFT 的 ``ARRAY JOIN`` 遇到空数组时索引越界抛 IndexError"
           "(与 LEFT ARRAY JOIN 那个 MiniChError 同源,都是没处理空数组)。"
           "真实 ClickHouse 里非 LEFT 的 ARRAY JOIN 会**丢弃**空数组那一行,保留其余行。",
)
def test_array_join_drops_rows_with_empty_array(memory_ctx):
    """非 LEFT 的 ARRAY JOIN:空数组的行应当被**丢弃**,其余照常摊开。"""
    memory_ctx.create_table("arr", "id UInt32, tags Array(String)", engine_sql="Memory")
    memory_ctx.insert("arr", [(1, ["a"]), (2, []), (3, ["c"])])
    assert_rows(
        memory_ctx.execute("SELECT id, tag FROM arr ARRAY JOIN tags AS tag ORDER BY id, tag"),
        [(1, "a"), (3, "c")],
    )


# ══════════════════════════════════════════════════════════════════════════
# 25. JOIN 的各种类型 / USING / ON / ANY / join_use_nulls
# ══════════════════════════════════════════════════════════════════════════
#
# 样本 ``_lr``:
#   l: (1,L1) (2,L2) (2,L2b) (3,L3)
#   r: (2,R2) (2,R2b) (4,R4)
# 键 k=2 是**多对多**(l 两行 × r 两行 = 4 行)。
#


def test_inner_join_on(memory_ctx):
    """INNER JOIN + ON:只有两边都匹配的 k 留下,多对多产生笛卡尔积。"""
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, l.lv, r.rv FROM l INNER JOIN r ON l.k = r.k ORDER BY l.lv, r.rv"
        ),
        [(2, "L2", "R2"), (2, "L2", "R2b"), (2, "L2b", "R2"), (2, "L2b", "R2b")],
    )


def test_inner_join_using_equals_on(memory_ctx):
    """``USING (k)`` 与 ``ON l.k = r.k`` 结果一致。"""
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, l.lv, r.rv FROM l INNER JOIN r USING (k) ORDER BY l.lv, r.rv"
        ),
        [(2, "L2", "R2"), (2, "L2", "R2b"), (2, "L2b", "R2"), (2, "L2b", "R2b")],
    )


def test_left_join_fills_missing_with_type_default(memory_ctx):
    """LEFT JOIN 未匹配的右表列填**类型默认值**(String → ``''``),而不是 NULL
    —— 因为 ``join_use_nulls`` 默认是 0(真实 ClickHouse 的默认)。"""
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, l.lv, r.rv FROM l LEFT JOIN r ON l.k = r.k ORDER BY l.lv, r.rv"
        ),
        [(1, "L1", ""), (2, "L2", "R2"), (2, "L2", "R2b"), (2, "L2b", "R2"), (2, "L2b", "R2b"), (3, "L3", "")],
    )


def test_right_join(memory_ctx):
    """RIGHT JOIN:未匹配的左表键填 0(Int32 默认)。"""
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, r.k, r.rv FROM l RIGHT JOIN r ON l.k = r.k ORDER BY r.rv, l.k"
        ),
        [(2, 2, "R2"), (2, 2, "R2"), (2, 2, "R2b"), (2, 2, "R2b"), (0, 4, "R4")],
    )


def test_full_join(memory_ctx):
    """FULL JOIN:两边都保留,各自缺失的键填 0。"""
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT l.k, r.k FROM l FULL JOIN r ON l.k = r.k ORDER BY l.k, r.k"),
        [(0, 4), (1, 0), (2, 2), (2, 2), (2, 2), (2, 2), (3, 0)],
    )


def test_cross_join(memory_ctx):
    """CROSS JOIN:4 × 3 = 12 行,没有 ON 条件。

    这里断言的是 ``r.k`` —— 也就是**右表**的键 ``{2, 2, 4}``。
    早期版本把它写成了 ``l.k`` 的值(``(1,1),(2,2),(3,3)``),那是假绿:
    当时右表的同名列被误绑定到了左表,``r.k`` 读出来其实是 ``l.k``,
    于是这个测试恰好"通过"了 —— 它锁住的正是那个 bug。修好绑定之后,
    期望值必须按真正的笛卡尔积来写。
    """
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT l.k, r.k FROM l CROSS JOIN r ORDER BY l.k, r.k"),
        [(1, 2), (1, 2), (1, 4),
         (2, 2), (2, 2), (2, 2), (2, 2), (2, 4), (2, 4),
         (3, 2), (3, 2), (3, 4)],
    )


def test_inner_any_join_takes_first_match(memory_ctx):
    """``INNER ANY JOIN``:每个左行**只取一个**匹配的右行(不再多对多展开)。"""
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, l.lv, r.rv FROM l INNER ANY JOIN r ON l.k = r.k ORDER BY l.lv"
        ),
        [(2, "L2", "R2"), (2, "L2b", "R2")],
    )


def test_left_any_join_keeps_unmatched_left(memory_ctx):
    """``LEFT ANY JOIN``:每个左行最多一个匹配,未匹配的左行照旧保留。"""
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, l.lv, r.rv FROM l LEFT ANY JOIN r ON l.k = r.k ORDER BY l.lv"
        ),
        [(1, "L1", ""), (2, "L2", "R2"), (2, "L2b", "R2"), (3, "L3", "")],
    )


def test_any_is_a_prefix_modifier(memory_ctx):
    """``ANY`` 是**前缀**修饰词:``ANY LEFT JOIN`` 与 ``LEFT ANY JOIN`` 等价。"""
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, l.lv, r.rv FROM l ANY LEFT JOIN r ON l.k = r.k ORDER BY l.lv"
        ),
        [(1, "L1", ""), (2, "L2", "R2"), (2, "L2b", "R2"), (3, "L3", "")],
    )


def test_session_set_join_use_nulls_turns_missing_into_null(memory_ctx):
    """会话级 ``SET join_use_nulls = 1`` 之后,未匹配的列填 NULL 且类型变 Nullable。"""
    _lr(memory_ctx)
    memory_ctx.execute("SET join_use_nulls = 1")
    result = memory_ctx.execute(
        "SELECT l.k, r.k AS rk, r.rv FROM l LEFT JOIN r ON l.k = r.k ORDER BY l.k, r.rv"
    )
    assert_rows(
        result,
        [(1, None, None), (2, 2, "R2"), (2, 2, "R2"), (2, 2, "R2b"), (2, 2, "R2b"), (3, None, None)],
    )
    # 未匹配列的类型确实被提升成 Nullable。
    assert result.column_types() == ["Int32", "Nullable(Int32)", "Nullable(String)"]


def test_session_set_join_use_nulls_does_not_change_inner_join(memory_ctx):
    """``join_use_nulls`` 只影响**外连接**的补齐方式;INNER JOIN 没有补齐,结果不变。"""
    _lr(memory_ctx)
    memory_ctx.execute("SET join_use_nulls = 1")
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, l.lv, r.rv FROM l INNER JOIN r ON l.k = r.k ORDER BY l.lv, r.rv"
        ),
        [(2, "L2", "R2"), (2, "L2", "R2b"), (2, "L2b", "R2"), (2, "L2b", "R2b")],
    )


# ══════════════════════════════════════════════════════════════════════════
# 26. FINAL 再补几个(含 Memory / 落盘两种引擎)
# ══════════════════════════════════════════════════════════════════════════


def test_final_on_memory_engine_is_noop_for_distinct_rows(memory_ctx):
    """``Memory`` 表没有 part 概念,``FINAL`` 对互不相同的行是空操作。"""
    memory_ctx.create_table("mt", "a Int32, b String", engine_sql="Memory")
    memory_ctx.insert("mt", [(2, "x"), (1, "y"), (3, "z")])
    assert_rows(memory_ctx.execute("SELECT * FROM mt FINAL ORDER BY a"), [(1, "y"), (2, "x"), (3, "z")])


def test_mergetree_final_keeps_identical_rows(disk_ctx):
    """普通 ``MergeTree`` 的 ``FINAL`` 是 **no-op**:逐列完全相同的重复行**原样保留**。

    ``FINAL`` 的"同键折叠"只对**特殊家族**(Replacing/Summing/...)有意义;
    MergeTree 自己不折叠任何东西,所以两行 ``(1,'y')`` 必须原样出现两次。
    """
    disk_ctx.create_table("mt", "a Int32, b String", engine_sql="MergeTree ORDER BY a")
    disk_ctx.insert("mt", [(1, "y"), (1, "y"), (2, "x")])
    assert_rows(
        disk_ctx.execute("SELECT * FROM mt FINAL ORDER BY a"),
        [(1, "y"), (1, "y"), (2, "x")],
    )


def test_replacing_final_no_version_keeps_last_inserted(disk_ctx):
    """``ReplacingMergeTree`` 不带版本列时,同键保留**最后插入**的那一行。"""
    disk_ctx.create_table("rmtn", "id Int32, v String", engine_sql="ReplacingMergeTree ORDER BY id")
    disk_ctx.insert("rmtn", [(1, "first"), (1, "second"), (2, "x")])
    assert_rows(disk_ctx.execute("SELECT * FROM rmtn FINAL ORDER BY id"), [(1, "second"), (2, "x")])


def test_replacing_final_single_column_dedup(disk_ctx):
    """单列 ReplacingMergeTree:重复键折叠成一行。"""
    disk_ctx.create_table("rt", "id Int32", engine_sql="ReplacingMergeTree ORDER BY id")
    disk_ctx.insert("rt", [(1,), (1,), (1,), (2,)])
    assert_rows(disk_ctx.execute("SELECT * FROM rt FINAL ORDER BY id"), [(1,), (2,)])


# ══════════════════════════════════════════════════════════════════════════
# 27. 回归测试(每个修复一条;先失败、修好后通过)
# ══════════════════════════════════════════════════════════════════════════


# ── #3 普通 MergeTree 的 FINAL 必须是 no-op ──────────────────────────────
def test_mergetree_final_is_noop_for_identical_rows(disk_ctx):
    """``FINAL`` 只对**特殊家族**(Replacing/Summing/...)折叠;普通 MergeTree 是 no-op。

    复现:``(1,'x'),(1,'x')`` 两行逐列相同 —— ``SELECT ... FINAL`` 必须
    原样返回**两行**,而不是按整行去重合成一行。
    """
    disk_ctx.create_table("t", "a UInt32, b String", engine_sql="MergeTree ORDER BY a")
    disk_ctx.insert("t", [(1, "x"), (1, "x")])
    assert_rows(
        disk_ctx.execute("SELECT a, b FROM t FINAL"),
        [(1, "x"), (1, "x")],
    )


# ── #8 平凡 count 不能被 LIMIT/OFFSET/FINAL 绕过 ────────────────────────
def test_trivial_count_disabled_by_limit_and_offset(memory_ctx):
    """``count()`` 的元数据捷径在没有 WHERE 时生效,但**有 LIMIT/OFFSET 时必须失效**。

    ``LIMIT 0`` 返回 **0 行**;``OFFSET 1`` 把唯一那一行也跳过 —— 都不是 ``[(N,)]``。
    """
    _t(memory_ctx)                                   # 5 行
    assert_rows(memory_ctx.execute("SELECT count() FROM t LIMIT 0"), [])
    assert_rows(memory_ctx.execute("SELECT count() FROM t OFFSET 1"), [])
    # 没有修饰符时捷径仍然生效(答案不变,只是更快)。
    assert_rows(memory_ctx.execute("SELECT count() FROM t"), [(5,)])
    assert_rows(memory_ctx.execute("SELECT count() FROM t LIMIT 1"), [(5,)])


def test_trivial_count_disabled_by_final(disk_ctx):
    """``count() FROM t FINAL`` 必须走真正的 FINAL 折叠,而不是 part 元数据的行数。"""
    disk_ctx.create_table("rt", "id Int32, ver Int32",
                          engine_sql="ReplacingMergeTree(ver) ORDER BY id")
    disk_ctx.insert("rt", [(1, 1), (1, 2), (2, 1)])
    # 不带 FINAL:元数据捷径 → 物理行数 3。
    assert_rows(disk_ctx.execute("SELECT count() FROM rt"), [(3,)])
    # 带 FINAL:版本折叠后 id=1 只剩一行 → 2 行。
    assert_rows(disk_ctx.execute("SELECT count() FROM rt FINAL"), [(2,)])


# ── #9 ON 里的残余谓词必须参与"匹配判定" ────────────────────────────────
def _lr_residual(ctx):
    """l:(1,a),(2,b),(3,c)  r:(1,99),(2,12) —— 专门测 ``ON ... AND r.w = 12``。"""
    ctx.create_table("l", "k Int32, lv String", engine_sql="Memory")
    ctx.insert("l", [(1, "a"), (2, "b"), (3, "c")])
    ctx.create_table("r", "k Int32, w Int32", engine_sql="Memory")
    ctx.insert("r", [(1, 99), (2, 12)])


def test_left_join_residual_predicate_keeps_unmatched_left(memory_ctx):
    """``LEFT JOIN ... ON l.k=r.k AND r.w=12``:没通过残余谓词的左行走"补默认值"分支。

    旧实现把残余谓词放到**拼行之后**过滤,于是 ``(1,'a')`` 与 ``(3,'c')``
    这两个"未匹配"的左行被误杀 —— 结果只剩一行。
    """
    _lr_residual(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, r.k, r.w FROM l LEFT JOIN r ON l.k = r.k AND r.w = 12 "
            "ORDER BY l.k"
        ),
        [(1, 0, 0), (2, 2, 12), (3, 0, 0)],
    )


def test_right_join_residual_predicate_keeps_unmatched_right(memory_ctx):
    """``RIGHT JOIN`` 同理:残余谓词不成立的右行走"左侧补默认值"分支。"""
    _lr_residual(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT l.k, r.k, r.w FROM l RIGHT JOIN r ON l.k = r.k AND r.w = 12 "
            "ORDER BY r.k"
        ),
        [(0, 1, 99), (2, 2, 12)],
    )


# ── #16 join_use_nulls=1 对外连接的**两侧**一致生效 ─────────────────────
def test_right_join_join_use_nulls_nulls_the_left_side(memory_ctx):
    """``RIGHT JOIN`` 未匹配的右行,其**左表**列必须是 NULL(而不是 0/'' )。

    旧实现只把右半边升级成 Nullable,左半边仍填类型默认值 —— 同一行里
    半边 NULL、半边 0,自相矛盾。
    """
    memory_ctx.create_table("l", "k Int32, lv String", engine_sql="Memory")
    memory_ctx.insert("l", [(1, "L1"), (2, "L2")])
    memory_ctx.create_table("r", "k Int32, rv String", engine_sql="Memory")
    memory_ctx.insert("r", [(2, "R2"), (3, "R3")])
    result = memory_ctx.execute(
        "SELECT l.k, l.lv, r.k, r.rv FROM l RIGHT JOIN r ON l.k = r.k "
        "ORDER BY r.k SETTINGS join_use_nulls = 1"
    )
    assert_rows(result, [(2, "L2", 2, "R2"), (None, None, 3, "R3")])
    # 左侧(会出现未匹配行)变 Nullable;右侧(每个右行都有值)保持非 Nullable。
    assert result.column_types() == ["Nullable(Int32)", "Nullable(String)", "Int32", "String"]


# ── #14 窗口函数的三处语义修正 ───────────────────────────────────────────
def test_window_order_by_places_nulls_like_engine_order_by(memory_ctx):
    """窗口的 ``ORDER BY v`` 里,NULL 的位置必须和引擎自己的 ``ORDER BY v`` 一致。

    ClickHouse 把 NULL 当"最大的值":``ASC`` 时 NULL 在最**后**。
    旧实现把 ASC 的 NULL 排到了最前,于是 ``row_number`` 给 NULL 排了第 1。
    """
    memory_ctx.create_table("wn", "v Nullable(Int32)", engine_sql="Memory")
    memory_ctx.insert("wn", [(3,), (None,), (1,)])
    assert_rows(
        memory_ctx.execute(
            "SELECT v, row_number() OVER (ORDER BY v) AS rn FROM wn ORDER BY v"
        ),
        [(1, 1), (3, 2), (None, 3)],
    )


def test_window_empty_frame_returns_identity(memory_ctx):
    """空帧(``end < start``)必须退化成"零个元素",而不是"只剩当前行"。

    第 1 行没有前两行,``ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING`` 是空帧:
    ``sum`` 的单位元是 **0**;旧实现把它错当"只有当前行",返回了当前行的值。
    """
    memory_ctx.create_table("wn", "o Int32, v Nullable(Int32)", engine_sql="Memory")
    memory_ctx.insert("wn", [(1, 3), (2, None), (3, 1)])
    assert_rows(
        memory_ctx.execute(
            "SELECT o, sum(v) OVER (ORDER BY o ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING) AS s "
            "FROM wn ORDER BY o"
        ),
        [(1, 0), (2, 3), (3, 3)],
    )


def test_window_percent_rank_uses_rank_not_row_position(memory_ctx):
    """``percent_rank`` = ``(rank - 1) / (n - 1)`` —— 并列行拿到**相同**的百分比。

    ``ORDER BY x``,x = 1,1,2 → rank = 1,1,3 → (0.0, 0.0, 1.0)。
    旧实现用行位置 ``slot/(n-1)``,把中间那行算成了 0.5。
    """
    memory_ctx.create_table("pr", "x Int32", engine_sql="Memory")
    memory_ctx.insert("pr", [(1,), (1,), (2,)])
    assert_rows(
        memory_ctx.execute(
            "SELECT x, percent_rank() OVER (ORDER BY x) AS p FROM pr ORDER BY x"
        ),
        [(1, 0.0), (1, 0.0), (2, 1.0)],
    )


# ── #15 窗口函数写在 ORDER BY(别名)/ 裸写 ──────────────────────────────
def test_window_in_order_by_by_alias(memory_ctx):
    """``ORDER BY rn`` 引用窗口函数的别名 —— ClickHouse 的常规写法,必须支持。"""
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT v, row_number() OVER (ORDER BY v) AS rn FROM w ORDER BY rn"
        ),
        [(1, 1), (2, 2), (3, 3), (10, 4), (20, 5)],
    )


def test_bare_window_function_in_select(memory_ctx):
    """裸写窗口函数(顶层没有别名)也要能被替换成窗口结果列,而不是 NOT_IMPLEMENTED。"""
    _win(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT row_number() OVER (ORDER BY v) FROM w ORDER BY v"),
        [(1,), (2,), (3,), (4,), (5,)],
    )


# ── #19 ADD COLUMN DEFAULT / INSERT 缺列都要按 DEFAULT 回填 ─────────────
def test_insert_missing_default_column_uses_expression(memory_ctx):
    """``INSERT`` 省略一个带 ``DEFAULT`` 的列时,写入的是**表达式的结果**(7),不是 0。"""
    memory_ctx.create_table("t", "a UInt8, b UInt8 DEFAULT 7", engine_sql="Memory")
    memory_ctx.insert("t", [(1, 7)])                     # 显式给了 7
    memory_ctx.execute("INSERT INTO t (a) VALUES (2)")   # 省略 b → DEFAULT 7
    assert_rows(memory_ctx.execute("SELECT a, b FROM t ORDER BY a"), [(1, 7), (2, 7)])


def test_memory_alter_add_column_default_backfills(memory_ctx):
    """``Memory`` 表 ``ADD COLUMN ... DEFAULT 7`` 之后,老行读出来必须是 7(与 MergeTree 一致)。"""
    memory_ctx.create_table("mm", "a UInt8", engine_sql="Memory")
    memory_ctx.insert("mm", [(1,), (2,)])
    memory_ctx.execute("ALTER TABLE mm ADD COLUMN b UInt8 DEFAULT 7")
    assert_rows(memory_ctx.execute("SELECT a, b FROM mm ORDER BY a"), [(1, 7), (2, 7)])


def test_memory_alter_add_column_without_default_stays_zero(memory_ctx):
    """没有 ``DEFAULT`` 的新列,老行仍然读到类型零值(不因为这次修改而改变)。"""
    memory_ctx.create_table("mm", "a UInt8", engine_sql="Memory")
    memory_ctx.insert("mm", [(1,)])
    memory_ctx.execute("ALTER TABLE mm ADD COLUMN c UInt8")
    assert_rows(memory_ctx.execute("SELECT a, c FROM mm"), [(1, 0)])


# ── RIGHT ANY:每个右行至多一行,有匹配就用真实左行(而非默认值) ─────────
def test_right_any_join_keeps_right_rows_matched(memory_ctx):
    """``RIGHT ANY JOIN`` 以右表为驱动:同键的每个右行都应与真实左行配对。

    等价于两侧对调后的 ``LEFT ANY``(``any_join_distinct_right_table_keys = 0``):
    ``la RIGHT ANY JOIN ra`` == ``ra LEFT ANY JOIN la``。旧实现按右键截断候选,
    把第二个右行当成未匹配,错误地补上左表默认值。
    """
    memory_ctx.create_table("la", "k Int32, lv String", engine_sql="Memory")
    memory_ctx.insert("la", [(1, "a"), (2, "b")])
    memory_ctx.create_table("ra", "k Int32, rv String", engine_sql="Memory")
    memory_ctx.insert("ra", [(2, "R2"), (2, "R2b"), (4, "R4")])
    assert_rows(
        memory_ctx.execute(
            "SELECT la.k, ra.k, ra.rv FROM la RIGHT ANY JOIN ra ON la.k = ra.k "
            "ORDER BY ra.k, ra.rv"
        ),
        [(2, 2, "R2"), (2, 2, "R2b"), (0, 4, "R4")],
    )
