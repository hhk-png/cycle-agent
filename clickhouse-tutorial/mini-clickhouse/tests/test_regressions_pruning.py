"""回归测试:两条"静默返回错误结果"的裁剪 bug。

这两个 bug 都**不崩溃、不报错** —— 它们只是让查询**悄悄少返回行**
(读者会拿到缺行/空结果却以为是对的)。所以本文件一律用**差分**来锁定:
"开裁剪"的结果必须和"不裁剪的全扫"逐行一致。

* **bug ①** —— ``KeyCondition._compute_mark_ranges`` 对**逆序(``DESC``)**
  排序键把"自然下界/上界"映射到了**错误的索引端**。稀疏索引是"按索引序"
  存的,对 ``DESC`` 列而言索引序与自然序相反,于是整段 granule 被跳过、
  丢行。(``minich/storages/merge_tree/key_condition.py``)

* **bug ②** —— 跳数索引的条件抽取 :func:`_const_value` 把"折不出来"
  (``evaluate_constant`` 返回 ``None``)的可折叠右值当成了**真实常量
  ``None``**;``minmax`` 的 :func:`_cmp` 会把 ``None`` 当成一个极值参与
  比较,于是判成"没有任何 mark 匹配",把**整表跳空**。违反的是那条
  "比较不了就放行"的纪律。(``minich/storages/merge_tree/skip_index.py``)

差分怎么构造:

* bug ① 没有"关掉主键裁剪"的开关,所以拿**同数据的 Memory 引擎表**当
  参照物 —— Memory 不裁剪,它给出的就是"全表扫"答案;
* bug ② 有现成的开关 ``SETTINGS use_skip_indexes = 0``,直接同一张表
  开/关各跑一遍即可。

.. note::

   谓词里**刻意不出现负数字面量**。``WHERE k > -1``(``k`` 是 ``UInt64``)
   在本实现里会被主键的归一化折成一个巨大的无符号数,从而漏行 —— 这是
   另一条与排序方向无关的**已知、未修**的问题,不在本文件的判定范围内。
"""

from __future__ import annotations

import pytest

from minich.sql import parse_expression_text


#: 所有建表都关掉自适应粒度,让"一个 granule 恰好 N 行"成为硬保证。
GRAN = "index_granularity = 2, index_granularity_bytes = 0"


# ── 工具 ────────────────────────────────────────────────────────────────


def _rows(ctx, sql):
    """跑一条 SQL,返回**排序后**的元组列表 —— 差分比较只看行集合。"""
    return sorted(tuple(r) for r in ctx.execute(sql).rows())


def _mt(ctx, name, cols, order_by, *, partition_by=None, settings=GRAN):
    """建一张 MergeTree 表并立刻停掉后台合并(让 part / mark 数可预测)。"""
    ddl = f"CREATE TABLE {name} ({cols}) ENGINE = MergeTree"
    if partition_by:
        ddl += f" PARTITION BY {partition_by}"
    ddl += f" ORDER BY {order_by} SETTINGS {settings}"
    ctx.execute(ddl)
    eng = ctx.catalog.get_table("default", name).engine
    eng.stop_merges()
    return eng


def _mem(ctx, name, cols, rows):
    """建一张**同数据的 Memory 表**当"不裁剪"的参照物。"""
    ctx.execute(f"CREATE TABLE {name} ({cols}) ENGINE = Memory")
    ctx.insert(name, rows)


def _marks(ctx, eng, where_sql=None):
    """直接调存储层 ``read``,返回 ``(行数, 实际解压的 granule 数)``。

    这是度量"裁剪到底有没有发生"的唯一诚实方式 —— 走 SQL 只能看结果行,
    看不到"读了多少 mark"。
    """
    where = parse_expression_text(where_sql) if where_sql else None
    total = 0
    for chunk in eng.read(ctx, where=where):
        total += chunk.num_rows
    return total, eng.last_read_events.get("SelectedMarks")


# ── 用到的键型与谓词 ────────────────────────────────────────────────────


#: ASC + 各种 DESC 组合。``k`` 低基数(0..5)、``n`` 更细(0..3)。
KEY_CONFIGS = [
    "k",
    "k DESC",
    "(k, n)",
    "(k DESC, n)",
    "(k, n DESC)",
    "(k DESC, n DESC)",
]

#: 数据集:24 行,``k`` 每个取值 4 行 —— 正好制造"重复首键""跨多个 granule"。
DATA = [(k, n) for k in range(6) for n in range(4)]

#: 覆盖 ``< <= > >= = != BETWEEN IN NOT IN`` 以及 ``AND``/``OR`` 组合。
PREDICATES = [
    "k < 3", "k <= 3", "k > 3", "k >= 3", "k = 3", "k != 3",
    "k BETWEEN 2 AND 5", "k IN (1, 3, 5)", "k NOT IN (1, 3, 5)",
    "n < 2", "n <= 2", "n > 2", "n >= 2", "n = 2", "n != 2",
    "n BETWEEN 1 AND 3", "n IN (0, 2, 3)",
    "k = 3 AND n < 3", "k = 3 AND n >= 1",
    "k >= 2 AND k <= 4 AND n BETWEEN 1 AND 2",
    "k < 2 OR n = 3", "k = 0 OR (k = 5 AND n = 3)", "k != 3 AND n != 1",
    "k IN (1, 3) AND n > 1",
]


# ══════════════════════════════════════════════════════════════════════════
# bug ①:DESC 排序键的稀疏主键裁剪丢行
# ══════════════════════════════════════════════════════════════════════════


def test_desc_single_key_reported_repro(make_context, tmp_path):
    """bug ① 的**最小复现**:``ORDER BY k DESC`` 上几条谓词的精确期望值。

    修之前 ``k < 5`` 返回 ``[]`` / ``k BETWEEN 3 AND 7`` 返回 ``[]``……
    这里把"修之前是错的"那几条断言固定下来。
    """
    ctx = make_context(data_path=str(tmp_path / "data"))
    eng = _mt(ctx, "t", "k UInt64, n UInt64", "k DESC")
    ctx.insert("t", [(k, k) for k in range(10)])   # 每个 k 一行,k=0..9
    assert len(eng.get_parts()) == 1

    assert _rows(ctx, "SELECT k FROM t WHERE k < 5") == [(i,) for i in range(5)]
    assert _rows(ctx, "SELECT k FROM t WHERE k <= 5") == [(i,) for i in range(6)]
    assert _rows(ctx, "SELECT k FROM t WHERE k > 5") == [(i,) for i in range(6, 10)]
    assert _rows(ctx, "SELECT k FROM t WHERE k >= 5") == [(i,) for i in range(5, 10)]
    assert _rows(ctx, "SELECT k FROM t WHERE k = 5") == [(5,)]
    assert _rows(ctx, "SELECT k FROM t WHERE k BETWEEN 3 AND 7") == \
        [(i,) for i in range(3, 8)]
    assert _rows(ctx, "SELECT k FROM t WHERE k >= 2 AND k <= 4") == \
        [(i,) for i in range(2, 5)]
    assert _rows(ctx, "SELECT k FROM t WHERE k IN (1, 3, 5)") == [(1,), (3,), (5,)]


@pytest.mark.parametrize("order_by", KEY_CONFIGS)
@pytest.mark.parametrize("granularity", [2, 3])
def test_key_pruning_matches_full_scan(make_context, tmp_path, order_by, granularity):
    """bug ① 差分:任意排序键(ASC / DESC / 混合)的裁剪必须等于全表扫。

    MergeTree(开裁剪)与同数据的 Memory(不裁剪)跑同一批谓词,
    逐条比较结果集 —— 只要裁剪丢了一段 granule,这里就会报差异。
    """
    ctx = make_context(data_path=str(tmp_path / "data"))
    _mem(ctx, "ref", "k UInt64, n UInt64", DATA)
    settings = f"index_granularity = {granularity}, index_granularity_bytes = 0"
    _mt(ctx, "t", "k UInt64, n UInt64", order_by, settings=settings)
    ctx.insert("t", DATA)

    for pred in PREDICATES:
        want = _rows(ctx, f"SELECT k, n FROM ref WHERE {pred}")
        got = _rows(ctx, f"SELECT k, n FROM t WHERE {pred}")
        assert got == want, f"ORDER BY {order_by} (g={granularity}): {pred} 丢行/多行"


@pytest.mark.parametrize("order_by", ["k DESC", "(k, n DESC)", "(k DESC, n DESC)"])
def test_desc_key_pruning_across_parts(make_context, tmp_path, order_by):
    """bug ① 差分:低基数重复键 + **多个 part** 下的 DESC 裁剪。

    低基数排序键会让相邻 granule 的**首键相同**(重复首键),多 part 又会
    让"part 内裁剪"重复发生 —— 这正是最容易把边界算错的情形。
    """
    ctx = make_context(data_path=str(tmp_path / "data"))
    data = [(k % 3, (k * 7) % 4) for k in range(30)]     # 大量重复
    _mem(ctx, "ref", "k UInt64, n UInt64", data)
    eng = _mt(ctx, "t", "k UInt64, n UInt64", order_by)
    for chunk in (data[:10], data[10:17], data[17:]):     # 3 个 part
        ctx.insert("t", chunk)
    assert len(eng.get_parts()) == 3

    preds = ["k < 2", "k <= 2", "k > 1", "k >= 1", "k = 1", "k != 1",
             "k BETWEEN 0 AND 2", "k IN (0, 2)", "k = 1 AND n >= 2",
             "n < 2", "n >= 1", "n BETWEEN 1 AND 2", "k = 2 OR n = 3"]
    for pred in preds:
        want = _rows(ctx, f"SELECT k, n FROM ref WHERE {pred}")
        got = _rows(ctx, f"SELECT k, n FROM t WHERE {pred}")
        assert got == want, f"ORDER BY {order_by} 多 part: {pred} 丢行/多行"


def test_desc_order_key_with_partition_pruning(make_context, tmp_path):
    """bug ① 差分:逆序排序键 + ``PARTITION BY`` 同时裁剪。

    分区裁剪(``PartitionPruner``)与主键裁剪共用 ``KeyCondition``,
    两者叠加时结果仍必须与全扫一致。
    """
    ctx = make_context(data_path=str(tmp_path / "data"))
    data = [(k, k % 3, f"s{k % 4}") for k in range(24)]
    _mem(ctx, "ref", "k UInt64, p UInt64, s String", data)
    _mt(ctx, "t", "k UInt64, p UInt64, s String", "(k DESC, p)", partition_by="p")
    for chunk in (data[:8], data[8:16], data[16:]):
        ctx.insert("t", chunk)

    preds = ["p = 1", "p < 2", "p >= 1", "p != 0", "p BETWEEN 0 AND 1",
             "p IN (0, 2)", "k >= 10", "k = 13 AND p = 1",
             "k > 5 AND p != 2", "s = 's1'", "k != 4 OR p = 2"]
    for pred in preds:
        want = _rows(ctx, f"SELECT k, p, s FROM ref WHERE {pred}")
        got = _rows(ctx, f"SELECT k, p, s FROM t WHERE {pred}")
        assert got == want, f"PARTITION BY p ORDER BY (k DESC, p): {pred} 丢行/多行"


def test_desc_pruning_is_effective_not_disabled(make_context, tmp_path):
    """bug ① 的**防退化**检查:修好之后裁剪仍然**真的在裁**,不是退化成全扫。

    ``ORDER BY k DESC`` 上 ``k = 3`` 只该读一小段 granule,而不是全部
    (若有人用"干脆不裁"来绕过这个 bug,这里会失败)。
    """
    ctx = make_context(data_path=str(tmp_path / "data"))
    eng = _mt(ctx, "t", "k UInt64, n UInt64", "k DESC")
    ctx.insert("t", [(k, k) for k in range(12)])          # 12 行 / 粒度 2 → 6 mark

    full_rows, full_marks = _marks(ctx, eng)
    eq_rows, eq_marks = _marks(ctx, eng, "k = 3")

    assert full_rows == 12 and full_marks == 6
    assert eq_rows == 1
    assert 0 < eq_marks < full_marks      # 裁掉了,但没裁空


# ══════════════════════════════════════════════════════════════════════════
# bug ②:跳数索引在"可常量折叠的右值"上产生假阴性
# ══════════════════════════════════════════════════════════════════════════


def _skip_index_table(ctx, name, index_decl, rows, cols="k UInt64, v Int32"):
    eng = _mt(ctx, name, f"{cols}, {index_decl}", "k")
    ctx.insert(name, rows)
    return eng


def test_minmax_skip_index_folded_rhs_reported_repro(make_context, tmp_path):
    """bug ② 的**最小复现**:``abs(-10)`` / ``toFloat64(10)`` 当右值。

    修之前这两条会被 minmax 判成"没有任何 mark 匹配" → 开索引返回 ``[]``,
    而关索引(全扫)返回匹配行。这里把修好后的行为固定住。
    """
    ctx = make_context(data_path=str(tmp_path / "data"))
    rows = [(k, v) for k, v in enumerate([1, 5, 20])]
    _skip_index_table(ctx, "t", "INDEX ix v TYPE minmax", rows)

    assert _rows(ctx, "SELECT k FROM t WHERE v > abs(-10)") == [(2,)]
    assert _rows(ctx, "SELECT k FROM t WHERE v > toFloat64(10)") == [(2,)]
    # 关掉跳数索引的对照:必须完全一致
    assert _rows(ctx, "SELECT k FROM t WHERE v > abs(-10) "
                      "SETTINGS use_skip_indexes = 0") == [(2,)]


#: 右值表达式 —— 纯字面量、``evaluate_constant`` 折不出来的函数、列引用。
#: 折不出来的那一批就是 bug ② 的触发点(``None`` → 必须保守放行)。
RHS_VALUES = [
    "10",                       # 纯字面量(原本就是好的,用来防回归)
    "5",
    "abs(-10)",                 # ↓ 以下 evaluate_constant 返回 None
    "toFloat64(10)",
    "toFloat32(3.5)",
    "round(1.6)",
    "length('abc')",
    "toInt8(10)",
    "toInt32(10)",              # ↑ 折不出来 vs 折得出来各不相同,都要覆盖
    "toUInt32(10)",             # ← 折得出来
    "toInt64(8)",               # ← 折得出来
]

INDEX_DECLS = [
    "INDEX ix v TYPE minmax",
    "INDEX ix v TYPE set(100)",
    "INDEX ix v TYPE bloom_filter",
]


@pytest.mark.parametrize("index_decl", INDEX_DECLS)
@pytest.mark.parametrize("rhs", RHS_VALUES)
def test_skip_index_differential_on_folded_rhs(make_context, tmp_path, index_decl, rhs):
    """bug ② 差分:各种右值表达式下,开/关跳数索引的结果必须逐行一致。

    覆盖 ``minmax`` / ``set`` / ``bloom_filter`` 三种索引类型 —— 假阴性
    不是 minmax 独有的,条件抽取一旦把 ``None`` 当成真实值,所有索引类型
    都会误判。
    """
    ctx = make_context(data_path=str(tmp_path / "data"))
    rows = [(k, v) for k, v in enumerate([1, 5, 9, 9, 20, 3, 7, 7, 12, 0])]
    _skip_index_table(ctx, "t", index_decl, rows)

    for op in ["<", "<=", ">", ">=", "=", "!="]:
        pred = f"v {op} {rhs}"
        on = _rows(ctx, f"SELECT k FROM t WHERE {pred}")
        off = _rows(ctx, f"SELECT k FROM t WHERE {pred} SETTINGS use_skip_indexes = 0")
        assert on == off, f"{index_decl}: {pred} 假阴性(开索引少返回)"

    # ``IN`` 列表里混一个折不出来的元素,同样必须放行
    on = _rows(ctx, f"SELECT k FROM t WHERE v IN (1, 5, {rhs})")
    off = _rows(ctx, f"SELECT k FROM t WHERE v IN (1, 5, {rhs}) "
                     f"SETTINGS use_skip_indexes = 0")
    assert on == off, f"{index_decl}: v IN (1, 5, {rhs}) 假阴性"


@pytest.mark.parametrize("index_decl", INDEX_DECLS)
def test_skip_index_differential_on_column_reference(make_context, tmp_path, index_decl):
    """bug ② 差分:右值是**列引用**(天然折不出来)时也必须保守放行。

    ``v > k`` 这类条件对索引来说"什么都不知道" —— 收集它会把整表跳空。
    """
    ctx = make_context(data_path=str(tmp_path / "data"))
    rows = [(k, v) for k, v in enumerate([1, 5, 9, 9, 20, 3, 7, 7, 12, 0])]
    _skip_index_table(ctx, "t", index_decl, rows)

    for pred in ["v > k", "v < k", "v = k", "v != k", "v > k OR v = 0",
                 "v > k AND k > 3"]:
        on = _rows(ctx, f"SELECT k FROM t WHERE {pred}")
        off = _rows(ctx, f"SELECT k FROM t WHERE {pred} SETTINGS use_skip_indexes = 0")
        assert on == off, f"{index_decl}: {pred} 假阴性"


def test_minmax_skip_index_still_prunes_pure_literals(make_context, tmp_path):
    """bug ② 的**防退化**检查:纯字面量右值的裁剪**依然有效**。

    "折不出来的就不裁"是对的,但不能顺手把能折的也一起放行 ——
    这里断言 ``v = 99`` 仍然只读 1 个 mark(而不是全扫)。
    """
    ctx = make_context(data_path=str(tmp_path / "data"))
    eng = _mt(ctx, "t", "k UInt64, v UInt64, INDEX ix_minmax v TYPE minmax GRANULARITY 1", "k",
              settings="index_granularity = 4, index_granularity_bytes = 0")
    ctx.insert("t", [(i, 99 if i >= 8 else 1) for i in range(12)])   # 3 个 granule

    _, full_marks = _marks(ctx, eng)
    rows_eq, eq_marks = _marks(ctx, eng, "v = 99")
    assert full_marks == 3
    assert rows_eq == 4
    assert eq_marks == 1        # 只读最后一个 granule —— 裁剪没被弄坏
