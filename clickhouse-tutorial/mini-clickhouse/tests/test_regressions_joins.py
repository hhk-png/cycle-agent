"""已确认的 JOIN 正确性 bug 的回归测试。

本文件里的每一个测试都对应一条**独立审计复现确认**的 bug(见 docstring 开头的
编号)。这些 bug 的共同点是**结果行数/内容错了却不报错** —— 所以断言一律用
**精确的期望行**(``assert_rows``),行数对不上、内容对不上都算失败。

源码侧的唯一改动落在 ``minich/execution/joins.py``(哈希连接);本文件只加测试,
不改任何已有测试。
"""

from __future__ import annotations

import pytest

from conftest import assert_rows


# ══════════════════════════════════════════════════════════════════════════
# 样本表
# ══════════════════════════════════════════════════════════════════════════


def _lr(ctx):
    """``L(id): 1,2,3``  ``R(id): 2`` —— 只有 ``id = 2`` 有匹配(bug ① 的样本)。"""
    ctx.create_table("L", "id Int32", engine_sql="Memory")
    ctx.insert("L", [(1,), (2,), (3,)])
    ctx.create_table("R", "id Int32", engine_sql="Memory")
    ctx.insert("R", [(2,)])


def _ab(ctx):
    """``A(k,x): (1,a1),(2,a2)``  ``B(k,y): (1,b1),(2,b2)`` —— bug ②/④/⑤ 的样本。"""
    ctx.create_table("A", "k Int32, x String", engine_sql="Memory")
    ctx.insert("A", [(1, "a1"), (2, "a2")])
    ctx.create_table("B", "k Int32, y String", engine_sql="Memory")
    ctx.insert("B", [(1, "b1"), (2, "b2")])


def _ab_distinct(ctx):
    """同 ``_ab``,但右表 ``k`` 取 (10, 20) —— 左右取值不同,``b.k`` 绑到哪张表一眼可辨。"""
    ctx.create_table("A", "k Int32, x String", engine_sql="Memory")
    ctx.insert("A", [(1, "a1"), (2, "a2")])
    ctx.create_table("B", "k Int32, y String", engine_sql="Memory")
    ctx.insert("B", [(10, "b10"), (20, "b20")])


def _null(ctx):
    """``LN``/``RN`` 各有一行 ``k = NULL`` —— 验证 NULL 连接键永不匹配(bug ③)。"""
    ctx.create_table("LN", "k Nullable(Int32), v String", engine_sql="Memory")
    ctx.insert("LN", [(1, "l1"), (None, "ln")])
    ctx.create_table("RN", "k Nullable(Int32), w String", engine_sql="Memory")
    ctx.insert("RN", [(1, "r1"), (None, "rn")])


# ══════════════════════════════════════════════════════════════════════════
# bug ①:LEFT SEMI JOIN 返回了全部左表行(应只返回有匹配的左表行)
# ══════════════════════════════════════════════════════════════════════════


def test_left_semi_join_keeps_only_matched_left_rows(memory_ctx):
    """bug ①:``LEFT SEMI JOIN`` 只保留**有匹配**的左表行。

    修复前 ``joins.py`` 的"无候选"分支对 ``kind in ('LEFT','FULL')`` 追加未匹配
    左行时漏了排除 ``SEMI``,于是把左表**全部**行都补了出来(``[1,2,3]``)。
    期望:``[2]`` —— 只有 ``id = 2`` 在右表里有匹配。
    """
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT id FROM L AS l LEFT SEMI JOIN R AS r ON l.id = r.id ORDER BY id"
        ),
        [(2,)],
    )


def test_inner_semi_join_keeps_only_matched_left_rows(memory_ctx):
    """bug ①(同类):``INNER SEMI JOIN`` 与 ``LEFT SEMI`` 一样,只留有匹配的左行。"""
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT id FROM L AS l INNER SEMI JOIN R AS r ON l.id = r.id ORDER BY id"
        ),
        [(2,)],
    )


def test_left_anti_join_still_keeps_only_unmatched_left_rows(memory_ctx):
    """回归保护:修 bug ① 时**不能弄坏 ANTI** —— 只留没有匹配的左行。

    (同一个"无候选"分支里 ``ANTI`` 本来就是对的;期望 ``[1,3]``。)
    """
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT id FROM L AS l LEFT ANTI JOIN R AS r ON l.id = r.id ORDER BY id"
        ),
        [(1,), (3,)],
    )


def test_right_semi_and_anti_join_do_not_emit_unmatched_right_rows(memory_ctx):
    """bug ① 的镜像:``RIGHT SEMI``/``RIGHT ANTI`` 不该把右行当成"未匹配"补出来。

    修复前"未匹配的右表行"分支没有排除 ``SEMI``/``ANTI``,于是**每个右行**都被
    当成未匹配补空。mini-clickhouse 的 ``SEMI``/``ANTI`` 是**左驱动过滤**:
    ``RIGHT SEMI`` 保留左表中有匹配的行、``RIGHT ANTI`` 保留左表中无匹配的行。
    """
    _lr(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT id FROM L AS l RIGHT SEMI JOIN R AS r ON l.id = r.id ORDER BY id"
        ),
        [(2,)],
    )
    assert_rows(
        memory_ctx.execute(
            "SELECT id FROM L AS l RIGHT ANTI JOIN R AS r ON l.id = r.id ORDER BY id"
        ),
        [(1,), (3,)],
    )


# ══════════════════════════════════════════════════════════════════════════
# bug ②:逗号连接 / CROSS JOIN 里右表的限定列名静默绑到左表列
# ══════════════════════════════════════════════════════════════════════════


def test_comma_join_where_equality_filters_instead_of_cartesian(memory_ctx):
    """bug ②:``FROM A AS a, B AS b WHERE a.k = b.k`` 必须真的按等值过滤。

    修复前 ``_cross_join`` 现场构造了一个没有 ``left``/``right`` 的合成 ``JoinExpr``,
    右表的 ``k`` 被命名成 ``right.k`` 而不是 ``b.k``;查询里的 ``b.k`` 精确匹配不到,
    静默回退(去掉前缀)绑到**左表**的 ``k``,于是 ``a.k = b.k`` 退化成 ``k = k``
    恒真,吐出整个笛卡尔积。期望:``[('a1','b1'),('a2','b2')]``。
    """
    _ab(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT a.x, b.y FROM A AS a, B AS b WHERE a.k = b.k ORDER BY a.x"
        ),
        [("a1", "b1"), ("a2", "b2")],
    )


def test_cross_join_qualified_right_column_binds_to_right_table(memory_ctx):
    """bug ②:``CROSS JOIN`` 里 ``b.k`` 必须绑到**右表**的 ``k``。

    左表 ``k`` 取值 (1,2),右表 ``k`` 取值 (10,20) —— 直方图一眼可辨:
    期望 ``10,10,20,20``(右表的 k 按 2×2 笛卡尔积重复),而不是 ``1,1,2,2``(左表的 k)。
    """
    _ab_distinct(memory_ctx)
    assert_rows(
        memory_ctx.execute("SELECT b.k FROM A AS a CROSS JOIN B AS b ORDER BY b.k"),
        [(10,), (10,), (20,), (20,)],
    )


# ══════════════════════════════════════════════════════════════════════════
# bug ③:JOIN 会匹配 NULL 键(真实 ClickHouse 永不匹配 NULL 连接键)
# ══════════════════════════════════════════════════════════════════════════


def test_inner_join_does_not_match_null_keys(memory_ctx):
    """bug ③:含 NULL 的连接键**永不匹配**(``NULL = NULL`` 在三值逻辑里是 UNKNOWN)。

    修复前 ``LN``/``RN`` 里那两个 ``k = NULL`` 会被连到一起;期望只有 ``k = 1`` 那一对。
    """
    _null(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT ln.v, rn.w FROM LN AS ln JOIN RN AS rn ON ln.k = rn.k ORDER BY ln.v"
        ),
        [("l1", "r1")],
    )


def test_left_join_null_key_row_is_treated_as_unmatched(memory_ctx):
    """bug ③(外连接侧):``LEFT JOIN`` 里键为 NULL 的左行"没有匹配" → 右侧补默认值。"""
    _null(memory_ctx)
    assert_rows(
        memory_ctx.execute(
            "SELECT ln.v, rn.w FROM LN AS ln LEFT JOIN RN AS rn ON ln.k = rn.k ORDER BY ln.v"
        ),
        [("l1", "r1"), ("ln", "")],
    )


def test_join_composite_key_never_matches_when_any_column_is_null(memory_ctx):
    """bug ③(复合键):多个等值键里**只要任一列**为 NULL 就不匹配。"""
    memory_ctx.create_table(
        "X", "a Nullable(Int32), b Nullable(Int32), v String", engine_sql="Memory"
    )
    memory_ctx.insert("X", [(1, None, "x1"), (1, 2, "x2")])
    memory_ctx.create_table(
        "Y", "a Nullable(Int32), b Nullable(Int32), w String", engine_sql="Memory"
    )
    memory_ctx.insert("Y", [(1, None, "y1"), (1, 2, "y2")])
    assert_rows(
        memory_ctx.execute(
            "SELECT x.v, y.w FROM X AS x JOIN Y AS y "
            "ON x.a = y.a AND x.b = y.b ORDER BY x.v"
        ),
        [("x2", "y2")],
    )


# ══════════════════════════════════════════════════════════════════════════
# bug ④:三张及以上表的链式 ON JOIN 报错(错误信息误导)
# ══════════════════════════════════════════════════════════════════════════


def test_chained_join_with_on_equalities_works(memory_ctx):
    """bug ④:三张表的链式 ``ON`` 连接必须能跑,并真正按等值过滤。

    修复前 ``_alias_of`` 只认"子树根别名",而链式连接的左侧是内层 ``JoinExpr``,
    拿到的别名是 ``None``,于是外层 ``ON a.k = c.k`` 被判成"没有等值条件",报出
    误导性的 ``NOT_IMPLEMENTED: ...requires at least one equality condition in ON``
    (其实有等值条件)。期望:``[('a1','c1'),('a2','c2')]``。
    """
    _ab(memory_ctx)
    memory_ctx.create_table("C", "k Int32, z String", engine_sql="Memory")
    memory_ctx.insert("C", [(1, "c1"), (2, "c2")])
    assert_rows(
        memory_ctx.execute(
            "SELECT a.x, c.z FROM A AS a JOIN B AS b ON a.k = b.k "
            "JOIN C AS c ON a.k = c.k ORDER BY a.x"
        ),
        [("a1", "c1"), ("a2", "c2")],
    )


# ══════════════════════════════════════════════════════════════════════════
# bug ⑤:SELECT t.* 在连接上展开出错误的列集(未修:根因在 analyzer.py)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug ⑤ 的根因在 analyzer.py:333-338 的 `t.*` 展开逻辑(按"
        "『列名以 t. 开头』筛选),要修必须改该文件;超出本次可改范围"
        "(只准改 joins.py)。这里用 xfail 把正确行为记录在案。"
    ),
)
def test_qualified_star_expands_only_that_tables_columns(memory_ctx):
    """bug ⑤:``SELECT a.*`` 只应展开 ``a`` 的列(``k, x``)。

    修复前会展开成整个连接结果的 4 列 ``k, x, b.k, y``。根因是
    ``analyzer.py`` 的 ``t.*`` 展开依赖"列名以 ``t.`` 开头"这个前缀约定,而
    joins.py 只给**右表**的冲突列加了前缀、左表列仍是裸名 —— 于是 ``a.*``
    筛出空集、回退成"全部列"。正确修法是在 analyzer 里建立"列 → 来源表"的
    映射,**必须动 analyzer.py**,不在本次允许改动范围内。
    """
    _ab(memory_ctx)
    result = memory_ctx.execute("SELECT a.* FROM A AS a JOIN B AS b ON a.k = b.k")
    assert_rows(result, [(1, "a1"), (2, "a2")])
