"""窗口函数回归测试 —— 每个测试钉住一个**已复现确认**的窗口实现 bug。

这些 bug 由一次独立审计逐条复现,涉及 ``minich/execution/window.py``:

* ① 窗口 ``sum()`` 对**有符号整型**返回 ``UInt64``,负结果被回绕成垃圾值;
* ② ``lagInFrame``/``leadInFrame``(及 ``lag``/``lead``)**无视窗口帧**,
  永远按整分区取"前/后一行";
* ③ ``nth_value`` 取的是**整分区第 n 行**而非**帧内第 n 个**;
* ④ ``cume_dist()`` 在 ORDER BY 列含 **NULL** 时抛 Python 原生 ``TypeError``;
* ⑤ ``first_value_respect_nulls``/``last_value_respect_nulls`` 的结果类型
  被错判成 ``Float64`` —— 整型返回 ``5.0``、字符串列直接抛 ``ValueError``。

真实 ClickHouse 的语义是基准;每条期望值都与教程第 16 章(§16.9~§16.10)的
描述一致。文件名带 ``_window`` 后缀,与通用的 ``test_regressions.py`` 区分。
"""

from __future__ import annotations

from conftest import assert_rows


# ══════════════════════════════════════════════════════════════════════════
# ① 窗口 sum() 对 Int32 返回 UInt64 —— 负值回绕
# ══════════════════════════════════════════════════════════════════════════


def test_window_sum_on_signed_int_keeps_signed_type(memory_ctx):
    """**bug ①**:窗口 ``sum()`` 对 ``Int32`` 曾返回 ``UInt64``,把负结果回绕。

    症状:``sum(Int32) OVER ()`` 里 -6 被打印成 ``18446744073709551610``
    (UInt64 表示下的 -6);累计帧上同理。根因是 ``_infer_result_type`` 对
    "非浮点、非 Decimal"一律返回 ``UInt64``。**普通聚合**的 ``sum()`` 走
    另一条路径,本来就是对的 —— 这里顺带钉住,避免连带改坏。
    """
    memory_ctx.create_table("t", "k String, v Int32", engine_sql="Memory")
    memory_ctx.insert("t", [("a", -5), ("b", -3), ("c", 0), ("d", 2)])

    # 普通聚合:行为不变(修窗口路径时绝不能碰这条)
    assert_rows(memory_ctx.execute("SELECT sum(v) FROM t"), [(-6,)])

    # 整分区窗口:每行都是 -6,且结果列类型是 Int64(不是 UInt64)
    result = memory_ctx.execute("SELECT v, sum(v) OVER () AS s FROM t ORDER BY v")
    assert_rows(result, [(-5, -6), (-3, -6), (0, -6), (2, -6)])
    assert result.column_types()[-1] == "Int64"

    # 累计帧:前缀和 -5, -8, -8, -6
    assert_rows(
        memory_ctx.execute(
            "SELECT v, sum(v) OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING "
            "AND CURRENT ROW) AS s FROM t ORDER BY v"
        ),
        [(-5, -5), (-3, -8), (0, -8), (2, -6)],
    )


# ══════════════════════════════════════════════════════════════════════════
# ② lagInFrame / leadInFrame 无视窗口帧
# ══════════════════════════════════════════════════════════════════════════


def test_lead_in_frame_respects_default_frame(memory_ctx):
    """**bug ②**:``leadInFrame`` 曾无视窗口帧,直接按**整分区**取"下一行"。

    有 ``ORDER BY`` 时默认帧是 ``[0, slot]``(右边界就是当前行),所以
    "下一行"永远在帧外 —— 应**静默返回默认值**,这正是教程 §16.10 的范例。
    早先的实现 ``target = slot + 1`` 只 clamp 到分区长度,于是每行都拿到了
    下一行的值,与教材自相矛盾。

    顺带钉住两点:``lagInFrame`` 在默认帧下仍取得到上一行(上一行在帧内);
    显式写成整帧后 ``leadInFrame`` 才能看到下一行。
    """
    memory_ctx.create_table("ev", "ts UInt32", engine_sql="Memory")
    memory_ctx.insert(
        "ev", [(1704067200,), (1704067201,), (1704067202,), (1704067203,)]
    )

    # 默认帧:下一行在帧外 → 全部返回默认值 0
    assert_rows(
        memory_ctx.execute(
            "SELECT ts, leadInFrame(ts, 1, toUInt32(0)) OVER (ORDER BY ts) AS nx "
            "FROM ev ORDER BY ts"
        ),
        [(1704067200, 0), (1704067201, 0), (1704067202, 0), (1704067203, 0)],
    )

    # lagInFrame 的"上一行"在默认帧内,行为照旧
    assert_rows(
        memory_ctx.execute(
            "SELECT ts, lagInFrame(ts, 1, toUInt32(0)) OVER (ORDER BY ts) AS pv "
            "FROM ev ORDER BY ts"
        ),
        [
            (1704067200, 0),
            (1704067201, 1704067200),
            (1704067202, 1704067201),
            (1704067203, 1704067202),
        ],
    )

    # 显式整帧后 leadInFrame 才能取到下一行(末行仍给默认值)
    assert_rows(
        memory_ctx.execute(
            "SELECT ts, leadInFrame(ts, 1, toUInt32(0)) OVER (ORDER BY ts "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS nx "
            "FROM ev ORDER BY ts"
        ),
        [
            (1704067200, 1704067201),
            (1704067201, 1704067202),
            (1704067202, 1704067203),
            (1704067203, 0),
        ],
    )


# ══════════════════════════════════════════════════════════════════════════
# ③ nth_value 无视窗口帧
# ══════════════════════════════════════════════════════════════════════════


def test_nth_value_respects_frame(memory_ctx):
    """**bug ③**:``nth_value`` 曾返回 ``part[n-1]``(整分区第 n 行)而非**帧内**第 n 个。

    默认帧下第 1 行的帧只有它自己,``nth_value(v, 2)`` 应给 NULL;之后各行
    帧扩到含前两行,都取第 2 行。用 ``Nullable`` 列让 NULL 显形 —— 非
    Nullable 的整数列会把 NULL 落成默认值 0(与真实值难区分)。教程第 16 章
    把 ``nth_value`` 明确定义为"帧内第 n 个"。
    """
    memory_ctx.create_table("w", "v Nullable(Int32)", engine_sql="Memory")
    memory_ctx.insert("w", [(1,), (2,), (3,), (4,), (5,)])

    # 默认帧:第 1 行帧内不足 2 行 → NULL;其余行取帧内第 2 个
    assert_rows(
        memory_ctx.execute(
            "SELECT v, nth_value(v, 2) OVER (ORDER BY v) AS n FROM w ORDER BY v"
        ),
        [(1, None), (2, 2), (3, 2), (4, 2), (5, 2)],
    )

    # 整帧:每一行的帧都是整个分区 → 全部取第 2 行(=2)
    assert_rows(
        memory_ctx.execute(
            "SELECT v, nth_value(v, 2) OVER (ORDER BY v "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS n "
            "FROM w ORDER BY v"
        ),
        [(1, 2), (2, 2), (3, 2), (4, 2), (5, 2)],
    )


# ══════════════════════════════════════════════════════════════════════════
# ④ cume_dist() 遇 NULL 崩
# ══════════════════════════════════════════════════════════════════════════


def test_cume_dist_with_null_order_key_does_not_crash(memory_ctx):
    """**bug ④**:``cume_dist()`` 在 ORDER BY 列含 NULL 时曾抛原生 ``TypeError``。

    根因是用 ``_order_tuple(...) <= cur`` 直接比较排序键,出现 ``None`` 就
    ``'<=' not supported between instances of 'NoneType' and 'int'``。应像其它
    排名函数一样按既定规则"**NULL 当作最大值**"处理:并列的 NULL 自成一组、
    排在末尾,于是它的 ``cume_dist`` 是 ``1.0``。
    """
    memory_ctx.create_table("cn", "x Nullable(Int32)", engine_sql="Memory")
    memory_ctx.insert("cn", [(1,), (2,), (None,), (2,)])

    assert_rows(
        memory_ctx.execute(
            "SELECT x, cume_dist() OVER (ORDER BY x) AS cd FROM cn ORDER BY x"
        ),
        [(1, 0.25), (2, 0.75), (2, 0.75), (None, 1.0)],
    )


# ══════════════════════════════════════════════════════════════════════════
# ⑤ first/last_value_respect_nulls 类型被错判
# ══════════════════════════════════════════════════════════════════════════


def test_respect_nulls_variants_infer_input_type(memory_ctx):
    """**bug ⑤**:``first_value_respect_nulls``/``last_value_respect_nulls``
    的结果类型曾落到 ``Float64`` 兜底。

    症状:整型列返回 ``5.0``(而不是 ``5``);字符串列直接抛
    ``ValueError: could not convert string to float: 'x'``。根因是
    ``_infer_result_type`` 的名字元组漏了这两个名字。修好后它们与
    ``first_value``/``last_value`` 走同一条类型推断路径,保持输入类型。
    """
    memory_ctx.create_table("i", "v Int32", engine_sql="Memory")
    memory_ctx.insert("i", [(5,), (6,), (7,)])
    result = memory_ctx.execute(
        "SELECT v, first_value_respect_nulls(v) OVER (ORDER BY v) AS fv, "
        "last_value_respect_nulls(v) OVER (ORDER BY v) AS lv FROM i ORDER BY v"
    )
    assert result.column_types()[1:] == ["Int32", "Int32"]
    assert_rows(result, [(5, 5, 5), (6, 5, 6), (7, 5, 7)])

    # 字符串列不再抛 ValueError —— 类型是 String
    memory_ctx.create_table("s", "v String", engine_sql="Memory")
    memory_ctx.insert("s", [("x",), ("y",)])
    result = memory_ctx.execute(
        "SELECT v, first_value_respect_nulls(v) OVER (ORDER BY v) AS fv, "
        "last_value_respect_nulls(v) OVER (ORDER BY v) AS lv FROM s ORDER BY v"
    )
    assert result.column_types()[1:] == ["String", "String"]
    assert_rows(result, [("x", "x", "x"), ("y", "x", "y")])
