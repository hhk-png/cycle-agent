"""MergeTree 引擎测试 —— part 布局、granule 裁剪、跳数索引、引擎家族、mutation、TTL、校验。

这一份测试覆盖教程里"MergeTree 三根支柱"那一章的全部断言点。写的时候守着
两条规矩:

* **只用确定性的事实**:每个 part 名、每个 mark 数都来自"固定输入 → 固定输出"。
  为了让 granule 划分完全可预测,所有建表都用
  ``SETTINGS index_granularity = <小值>, index_granularity_bytes = 0``
  —— 后者把**自适应粒度**关掉,于是"一个 granule 恰好 N 行"成为硬保证
  (见 :mod:`minich.storages.merge_tree.marks` 里 :class:`IndexGranularity` 的文档)。
* **后台合并必须停掉**:``CREATE TABLE`` 会 ``startup()`` 起一个后台合并线程,
  它在两次 INSERT 之间可能就把 part 合掉了,导致"part 数"这类断言随机失败。
  所以每个用例都用 :func:`_create` 建表并立刻 ``stop_merges()``
  —— 想合并时再显式 ``OPTIMIZE ... FINAL``(同步、可预测)。

``WHERE`` 的下推要单独说一句:**解释器不会把 WHERE 推进存储层**
(它在拿到全部行之后自己过滤,见 ``interpreter._apply_filter``)。所以
"mark 裁剪""分区裁剪"这类断言是**直接调用引擎的 ``read``** 来验证的
—— 这正是存储层的接口,也是这一章真正想讲的东西。
"""

from __future__ import annotations

import math
import os

import pytest

from minich.errors import ErrorCode, MiniChError
from minich.sql import parse_expression_text
from minich.storages.merge_tree.skip_index import IndexDescription
from minich.storages.merge_tree.marks import (
    RANGE_MAX,
    IndexGranularity,
    MarkRange,
    mark_range_for_rows,
    merge_ranges,
    round_down_to_multiple,
    round_up_to_multiple,
)
from minich.storages.merge_tree.part import (
    format_part_name,
    format_partition_id,
    parse_part_name,
)


# ══════════════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════════════

#: 所有建表都用这一组 settings —— ``index_granularity_bytes = 0`` 关掉自适应粒度,
#: 于是 "10 行 / 粒度 4" 必然切出 ``[4, 4, 2]`` 三个 granule,断言才能写死。
GRAN = "index_granularity = 4, index_granularity_bytes = 0"


def _create(ctx, name, columns, order_by, *, engine="MergeTree",
            partition_by=None, indexes="", ttl="", settings=GRAN):
    """建一张 MergeTree 表并**立刻停掉后台合并** —— 让 part 数完全由测试掌控。

    ``indexes`` / ``ttl`` 是可选片段(列定义里的 ``INDEX ...``、表级的 ``TTL ...``)。
    """
    cols = columns
    if indexes:
        cols = f"{columns}, {indexes}"
    ddl = f"CREATE TABLE {name} ({cols}) ENGINE = {engine}"
    if partition_by:
        ddl += f" PARTITION BY {partition_by}"
    ddl += f" ORDER BY {order_by}"
    ddl += f" SETTINGS {settings}"
    if ttl:
        ddl += f" TTL {ttl}"
    ctx.execute(ddl)
    eng = ctx.catalog.get_table("default", name).engine
    # 后台合并线程会打破"几次 INSERT = 几个 part"的断言,这里直接关掉。
    eng.stop_merges()
    return eng


def _read(ctx, engine, where_sql=None):
    """直接调存储层 ``read``,返回 ``(行数, ProfileEvents)``。

    这是验证"mark/分区裁剪"的唯一诚实方式 —— 走 SQL 的话解释器会把
    WHERE 留到最后过滤,存储层读的仍是全部 granule。

    每次 ``read`` 都会把 ``last_read_events`` 换成一个**新的** ProfileEvents
    (语义与 ``system.query_log`` 的"查询前后取快照相减"一致),所以这里
    **不要**去 reset 它 —— 那会把上一次读取返回的对象清空。
    """
    where = parse_expression_text(where_sql) if where_sql else None
    total = 0
    for chunk in engine.read(ctx, where=where):
        total += chunk.num_rows
    return total, engine.last_read_events


def _marks(events):
    """一次读取实际解压的 granule(mark)个数 —— 裁剪效果的直接度量。"""
    return events.get("SelectedMarks")


# ══════════════════════════════════════════════════════════════════════════
# 1. part 名:``<partition_id>_<min_block>_<max_block>_<level>``
# ══════════════════════════════════════════════════════════════════════════


def test_format_part_name_shape():
    """part 名就是四段拼起来的字符串 —— 这是磁盘上的目录名。"""
    assert format_part_name("202401", 1, 2, 1) == "202401_1_2_1"
    assert format_part_name("all", 3, 3, 0) == "all_3_3_0"


def test_parse_part_name_roundtrip():
    pn = parse_part_name("202401_1_2_1")
    assert (pn.partition_id, pn.min_block, pn.max_block, pn.level) == ("202401", 1, 2, 1)
    # 回打必须与 parse 前完全一致 —— 否则 part 名会在磁性与内存之间漂移
    assert pn.format() == "202401_1_2_1"


def test_parse_part_name_rejects_garbage():
    """名字坏掉要报 ``BAD_DATA_PART_NAME``(246),而不是静默解析成别的 part。"""
    with pytest.raises(MiniChError) as ei:
        parse_part_name("abc")
    assert ei.value.code == ErrorCode.BAD_DATA_PART_NAME


def test_format_partition_id_scalar_and_tuple():
    """单列分区键直接用文本形式;多列用 ``-`` 连接;非目录安全字符被转义。"""
    assert format_partition_id(202401) == "202401"
    assert format_partition_id(("a", "b")) == "a-b"
    # ``/`` 不能出现在目录名里,被转义成 ``_2f``
    assert format_partition_id("2024/01") == "2024_2f01"


def test_first_insert_produces_block_number_one_part(disk_ctx):
    """第一次 INSERT 拿到 block 号 1,于是 part 名是 ``all_1_1_0``。"""
    eng = _create(disk_ctx, "t", "id UInt64, name String", "id")
    disk_ctx.insert("t", [(i, f"n{i}") for i in range(1, 11)])
    assert [p.get_name() for p in eng.get_parts()] == ["all_1_1_0"]


def test_sequential_inserts_get_increasing_block_numbers(disk_ctx):
    """每次 INSERT 递增 block 号 —— 于是 "min_block..max_block" 唯一标识这批数据。"""
    eng = _create(disk_ctx, "t", "id UInt64", "id")
    for _ in range(3):
        disk_ctx.insert("t", [(1,), (2,), (3,)])
    # 合并已停:三次 INSERT = 三个独立 part,block 号 1/2/3
    assert sorted(p.get_name() for p in eng.get_parts()) == [
        "all_1_1_0", "all_2_2_0", "all_3_3_0",
    ]


# ══════════════════════════════════════════════════════════════════════════
# 2. granule 与 mark 数:``ceil(rows / index_granularity)``
# ══════════════════════════════════════════════════════════════════════════


def test_mark_count_is_ceil_of_rows_over_granularity(disk_ctx):
    """10 行 / 粒度 4 → 3 个 granule ``[4, 4, 2]``(最后一个不满也算一个 mark)。"""
    eng = _create(disk_ctx, "t", "id UInt64", "id")
    disk_ctx.insert("t", [(i,) for i in range(10)])
    part = eng.get_parts()[0]
    assert part.rows_count() == 10
    assert part.get_marks_count() == 3
    assert part.get_index_granularity().granularities == [4, 4, 2]


def test_mark_count_exact_multiple_has_no_partial_granule(disk_ctx):
    """8 行 / 粒度 4 → 恰好 2 个满 granule(没有尾巴)。"""
    eng = _create(disk_ctx, "t", "id UInt64", "id")
    disk_ctx.insert("t", [(i,) for i in range(8)])
    part = eng.get_parts()[0]
    assert part.get_marks_count() == 2
    assert part.get_index_granularity().granularities == [4, 4]


def test_single_row_part_has_exactly_one_mark(disk_ctx):
    """1 行 → 1 个 granule(边界情形:granule 数向上取整)。"""
    eng = _create(disk_ctx, "t", "id UInt64", "id")
    disk_ctx.insert("t", [(1,)])
    assert eng.get_parts()[0].get_marks_count() == 1


def test_system_parts_reports_deterministic_name_rows_marks(disk_ctx):
    """``system.parts`` 暴露的就是 part 清单本身 —— 名字/行数/mark 数一字不差。"""
    _create(disk_ctx, "t", "id UInt64, name String", "id")
    disk_ctx.insert("t", [(i, f"n{i}") for i in range(1, 11)])
    r = disk_ctx.execute(
        "SELECT name, partition, rows, marks FROM system.parts "
        "WHERE table = 't' ORDER BY name"
    )
    assert r.rows() == [("all_1_1_0", "all", 10, 3)]


def test_alter_add_column_reads_default_without_rewriting_parts(disk_ctx):
    """``ALTER ADD COLUMN`` 是**纯元数据**操作:老 part 里没有这一列,读时补默认值。

    这是"给一张大表加一列"能做到毫秒级的原因 —— 一个 part 都不重写。
    """
    eng = _create(disk_ctx, "t", "id UInt64", "id")
    disk_ctx.insert("t", [(i,) for i in range(1, 6)])
    assert eng.part_count() == 1
    part = eng.get_parts()[0]

    disk_ctx.execute("ALTER TABLE t ADD COLUMN extra UInt64 DEFAULT 7")
    # 没有产生新 part,老 part 里也**没有**这个列文件
    assert eng.part_count() == 1
    assert not part.has_column("extra")
    # 读的时候现补默认值
    assert disk_ctx.execute("SELECT id, extra FROM t ORDER BY id").rows() == [
        (1, 7), (2, 7), (3, 7), (4, 7), (5, 7),
    ]


# ══════════════════════════════════════════════════════════════════════════
# 3. granule / mark 的纯逻辑(``marks.py``)
# ══════════════════════════════════════════════════════════════════════════


def test_index_granularity_fixed_split():
    """固定粒度(``index_granularity_bytes = 0``)就是"每 N 行一刀"。"""
    g = IndexGranularity(index_granularity=4, index_granularity_bytes=0)
    assert g.compute_granularity(10, 100) == [4, 4, 2]
    assert g.get_mark_count() == 3
    assert g.get_rows_before_mark(2) == 8


def test_exceeding_parts_to_throw_insert(disk_ctx):
    """超过 ``parts_to_throw_insert`` 应抛 ``TOO_MANY_PARTS``(232) 的 MiniChError。"""
    ctx = disk_ctx
    eng = _create(ctx, "t", "id UInt64", "id",
                  settings="index_granularity = 4, index_granularity_bytes = 0, "
                           "parts_to_throw_insert = 1")
    eng.stop_merges()
    ctx.insert("t", [(1,)])
    with pytest.raises(MiniChError) as ei:
        ctx.insert("t", [(2,)])   # 第二个 part 会越过阈值
    assert ei.value.code == 232


@pytest.mark.xfail(
    raises=AssertionError,
    reason=(
        "minich bug: MergeTreeData._configure_family 处理 SummingMergeTree 的引擎参数时,"
        "只取 engine_args[0](`cols = [first]`),后面列出来的列被静默忽略。"
        "于是 'SummingMergeTree(v, w)' 只会相加 v,w 悄悄取第一行的值 ——"
        " 用户以为两列都在汇总,实际只有一列。"
    ),
)
def test_summing_multiple_explicit_columns(disk_ctx):
    """显式列出多列时应当都相加 —— 目前只加第一列。"""
    _create(disk_ctx, "s", "k UInt64, v UInt64, w UInt64", "k",
            engine="SummingMergeTree(v, w)")
    disk_ctx.insert("s", [(1, 10, 100)])
    disk_ctx.insert("s", [(1, 5, 7)])
    disk_ctx.execute("OPTIMIZE TABLE s FINAL")
    # 正确结果应为 (1, 15, 107);实际是 (1, 15, 100)
    assert disk_ctx.execute("SELECT k, v, w FROM s ORDER BY k").rows() == [(1, 15, 107)]


# ══════════════════════════════════════════════════════════════════════════
# 4. part 名 / 行数 / mark 数 —— 跨多次 INSERT 的完整映射
# ══════════════════════════════════════════════════════════════════════════


def test_part_name_rows_and_marks_across_two_inserts(disk_ctx):
    """固定输入 → 每个 part 的 ``<partition>_<min>_<max>_<level>``、行数、mark 数一字不差。

    这里把"part 名 = 四段拼接"与"mark 数 = ``ceil(rows / index_granularity)``"
    两条事实钉在**同一个**例子上:10 行 → ``all_1_1_0``(granule ``[4,4,2]``,3 个 mark),
    4 行 → ``all_2_2_0``(granule ``[4]``,1 个 mark)。合并已停,所以两次 INSERT
    必然留下两个独立 part。
    """
    eng = _create(disk_ctx, "t", "id UInt64, name String", "id")
    disk_ctx.insert("t", [(i, f"n{i}") for i in range(10)])
    disk_ctx.insert("t", [(i, f"n{i}") for i in range(10, 14)])

    # 名字直接由 format_part_name 生成,这里反过来验证"名字就是这四段"
    assert sorted(p.get_name() for p in eng.get_parts()) == ["all_1_1_0", "all_2_2_0"]
    assert format_part_name("all", 1, 1, 0) == "all_1_1_0"
    assert format_part_name("all", 2, 2, 0) == "all_2_2_0"

    p1 = eng.get_part("all_1_1_0")
    p2 = eng.get_part("all_2_2_0")
    assert (p1.rows_count(), p1.get_marks_count(), p1.get_index_granularity().granularities) \
        == (10, 3, [4, 4, 2])
    assert (p2.rows_count(), p2.get_marks_count(), p2.get_index_granularity().granularities) \
        == (4, 1, [4])
    # mark 数 = ceil(rows / index_granularity)
    assert p1.get_marks_count() == math.ceil(10 / 4)
    assert p2.get_marks_count() == math.ceil(4 / 4)


# ══════════════════════════════════════════════════════════════════════════
# 5. WHERE 与 mark 裁剪 —— 直接驱动存储层(解释器不下推 WHERE)
# ══════════════════════════════════════════════════════════════════════════


def test_sort_key_prefix_selects_fewer_marks_than_full_scan(disk_ctx):
    """排序键前缀上的谓词让 ``SelectedMarks`` 下降;对照是全扫。

    12 行 / 粒度 4 → 3 个 granule,主键索引里三段的第一个键值是 ``0、4、8``。

    * 全扫(=3 个 mark)是基准;
    * ``id >= 8`` 读 2 个 mark —— 头一个 mark 是**边界 granule**
      (granule 1 的首键是 4 < 8,但它有可能一直排到 8,必须读进来;
      前缀比较无法证明它里面没有 8),第二个 mark 才是首键 >= 8 的那段;
    * ``id < 4`` 保守地读 2 个 mark(仍然少于全扫)。

    这些数字来自 ``KeyCondition`` 在 ``primary.idx`` 上的二分,不是猜的。
    **注意下界是"第一个首键 >= lo 的 mark 减一"** —— 别再优化成"只读
    首键 >= lo 的那一段":那样在低基数排序键(重复首键)上会漏掉整段
    granule(见 ``test_duplicate_sort_key_keeps_all_matching_granules``)。
    """
    eng = _create(disk_ctx, "t", "id UInt64, name String", "id")
    disk_ctx.insert("t", [(i, f"n{i}") for i in range(12)])

    full_rows, full_ev = _read(disk_ctx, eng)
    assert (full_rows, _marks(full_ev)) == (12, 3)

    rows_hi, ev_hi = _read(disk_ctx, eng, "id >= 8")
    assert (rows_hi, _marks(ev_hi)) == (4, 2)

    rows_lo, ev_lo = _read(disk_ctx, eng, "id < 4")
    assert (rows_lo, _marks(ev_lo)) == (4, 2)
    # 前缀裁剪的实质:读的 granule 严格少于全扫
    assert _marks(ev_hi) < _marks(full_ev)
    assert _marks(ev_lo) < _marks(full_ev)


def test_where_on_non_key_column_selects_all_marks(disk_ctx):
    """非排序键列上的谓词**裁不掉任何 mark** —— 只能全扫。

    ``name`` 不参与排序键,所以主键索引对它一无所知;即便只有 1 行匹配,
    存储层仍然读满 3 个 granule(行级过滤发生在读完之后的 ``_filter_block``)。
    """
    eng = _create(disk_ctx, "t", "id UInt64, name String", "id")
    disk_ctx.insert("t", [(i, f"n{i}") for i in range(12)])

    _, full_ev = _read(disk_ctx, eng)
    rows, ev = _read(disk_ctx, eng, "name = 'n5'")
    assert rows == 1                       # 只匹配一行
    assert _marks(ev) == _marks(full_ev) == 3   # 但读的是全部 granule


# ══════════════════════════════════════════════════════════════════════════
# 6. 分区裁剪 —— ``SelectedParts`` / ``SelectedRanges`` 下降
# ══════════════════════════════════════════════════════════════════════════


def test_partition_predicate_prunes_parts_and_ranges(disk_ctx):
    """按分区键过滤时,part 级裁剪直接把整个分区排除掉。

    一次 INSERT 按分区键切成两个 part(``1_1_1_0`` / ``2_2_2_0``):

    * 全扫选中 2 个 part;
    * ``pid = 1`` 只选中 1 个 part,并让 ``SelectedRanges`` 记下"砍掉 1 段"。
    """
    eng = _create(disk_ctx, "t", "pid UInt64, id UInt64", "id", partition_by="pid")
    disk_ctx.insert("t", [(1, 1), (1, 2), (1, 3), (2, 4), (2, 5), (2, 6)])

    assert sorted(p.get_name() for p in eng.get_parts()) == ["1_1_1_0", "2_2_2_0"]

    full_rows, full_ev = _read(disk_ctx, eng)
    assert full_rows == 6
    assert full_ev.get("SelectedParts") == 2
    assert full_ev.get("SelectedRanges") == 0

    rows, ev = _read(disk_ctx, eng, "pid = 1")
    assert rows == 3
    assert ev.get("SelectedParts") == 1      # 只剩 pid=1 那个 part
    assert ev.get("SelectedRanges") == 1     # 2 个分区取值里砍掉 1 个


# ══════════════════════════════════════════════════════════════════════════
# 7. 跳数索引 —— minmax / set / bloom_filter 让 mark 数下降
# ══════════════════════════════════════════════════════════════════════════


def test_minmax_skip_index_prunes_marks(disk_ctx):
    """``minmax`` 索引按 granule 记 (min,max);选择性的 ``=`` 只留一个 mark。

    12 行,granule ``[4,4,4]``,``v`` 前 8 行是 1、后 4 行是 99。
    ``WHERE v = 99``:前两个 granule 的 max 都是 1 → 砍掉;只读第 3 个 granule。
    """
    eng = _create(disk_ctx, "t", "id UInt64, v UInt64", "id",
                  indexes="INDEX ix_minmax v TYPE minmax GRANULARITY 1")
    disk_ctx.insert("t", [(i, 99 if i >= 8 else 1) for i in range(12)])

    _, full_ev = _read(disk_ctx, eng)
    assert _marks(full_ev) == 3

    rows, ev = _read(disk_ctx, eng, "v = 99")
    assert rows == 4
    assert _marks(ev) == 1
    # 对照:同一索引对 ``v = 1`` 只能裁掉最后一个 granule(2 个 mark)
    rows1, ev1 = _read(disk_ctx, eng, "v = 1")
    assert (rows1, _marks(ev1)) == (8, 2)


def test_set_skip_index_prunes_marks(disk_ctx):
    """``set(N)`` 索引记每个 granule 里出现过的字符串;``s = 'z'`` 只读第 3 个 granule。"""
    eng = _create(disk_ctx, "t", "id UInt64, s String", "id",
                  indexes="INDEX ix_set s TYPE set(100) GRANULARITY 1")
    disk_ctx.insert("t", [(i, "z" if i >= 8 else "a") for i in range(12)])

    _, full_ev = _read(disk_ctx, eng)
    assert _marks(full_ev) == 3

    rows, ev = _read(disk_ctx, eng, "s = 'z'")
    assert rows == 4
    assert _marks(ev) == 1

    rows_a, ev_a = _read(disk_ctx, eng, "s = 'a'")
    assert (rows_a, _marks(ev_a)) == (8, 2)


def test_bloom_filter_skip_index_prunes_marks(disk_ctx):
    """``bloom_filter`` 对数值列的等值查询同样只留一个 granule。"""
    eng = _create(disk_ctx, "t", "id UInt64, v UInt64", "id",
                  indexes="INDEX ix_bf v TYPE bloom_filter GRANULARITY 1")
    disk_ctx.insert("t", [(i, 99 if i >= 8 else 1) for i in range(12)])

    part = eng.get_parts()[0]
    assert "ix_bf" in part.index_readers      # 索引真的落到了磁盘上

    _, full_ev = _read(disk_ctx, eng)
    assert _marks(full_ev) == 3

    rows, ev = _read(disk_ctx, eng, "v = 99")
    assert rows == 4
    assert _marks(ev) == 1


def test_add_index_via_storage_api_then_insert_prunes_marks(disk_ctx):
    """用存储层 API(``engine.add_index``)声明索引 —— 之后写入的 part 才带索引。

    这演示了 ``ALTER ADD INDEX`` 的真实语义:**只影响之后写出的 part**,
    已有的 part 不会被回填(要回填得 ``MATERIALIZE INDEX``)。这里先建表、
    再 ``add_index``、最后 INSERT,于是新 part 带上了 ``ix_api``。
    """
    eng = _create(disk_ctx, "t", "id UInt64, v UInt64", "id")
    eng.add_index(IndexDescription(
        name="ix_api",
        expr_sql="v",
        expr=parse_expression_text("v"),
        index_type="minmax",
        params=[],
        granularity=1,
    ))
    disk_ctx.insert("t", [(i, 99 if i >= 8 else 1) for i in range(12)])

    assert "ix_api" in eng.get_parts()[0].index_readers

    _, full_ev = _read(disk_ctx, eng)
    assert _marks(full_ev) == 3

    rows, ev = _read(disk_ctx, eng, "v = 99")
    assert rows == 4
    assert _marks(ev) == 1


# ══════════════════════════════════════════════════════════════════════════
# 8. OPTIMIZE FINAL —— N 个 part 收敛成 1 个
# ══════════════════════════════════════════════════════════════════════════


def test_optimize_final_merges_all_parts_into_one(disk_ctx):
    """``OPTIMIZE ... FINAL`` 把 3 个 part 并成 1 个,名字/level/行数都精确。

    输入是 ``all_1_1_0`` / ``all_2_2_0`` / ``all_3_3_0``(每个 3 行)。
    合并结果的 block 区间取 ``[min(min_block), max(max_block)] = [1, 3]``,
    level 取 ``max(levels) + 1 = 1`` → ``all_1_3_1``,共 9 行。
    """
    eng = _create(disk_ctx, "t", "id UInt64", "id")
    for _ in range(3):
        disk_ctx.insert("t", [(1,), (2,), (3,)])
    assert sorted(p.get_name() for p in eng.get_parts()) == [
        "all_1_1_0", "all_2_2_0", "all_3_3_0",
    ]

    disk_ctx.execute("OPTIMIZE TABLE t FINAL")

    assert eng.part_count() == 1
    part = eng.get_parts()[0]
    assert part.get_name() == "all_1_3_1"
    assert part.get_level() == 1
    assert part.rows_count() == 9
    assert disk_ctx.execute("SELECT count() FROM t").rows() == [(9,)]


# ══════════════════════════════════════════════════════════════════════════
# 9. 引擎家族 —— 合并时的折叠语义
# ══════════════════════════════════════════════════════════════════════════


def test_replacing_merge_tree_keeps_max_version(disk_ctx):
    """``ReplacingMergeTree(ver)``:合并后同一排序键只留 ``ver`` 最大的一行。

    合并之前,``k=1`` 的两行**都在**(去重是"合并时"发生的,不是插入时)。
    """
    eng = _create(disk_ctx, "t", "k UInt64, ver UInt64, v String", "k",
                  engine="ReplacingMergeTree(ver)")
    disk_ctx.insert("t", [(1, 1, "old")])
    disk_ctx.insert("t", [(1, 2, "new"), (2, 1, "only")])
    disk_ctx.insert("t", [(3, 1, "third")])

    # 合并前:两行都还在
    assert disk_ctx.execute("SELECT k, ver, v FROM t ORDER BY k, ver").rows() == [
        (1, 1, "old"), (1, 2, "new"), (2, 1, "only"), (3, 1, "third"),
    ]

    disk_ctx.execute("OPTIMIZE TABLE t FINAL")

    assert eng.part_count() == 1
    assert disk_ctx.execute("SELECT k, ver, v FROM t ORDER BY k").rows() == [
        (1, 2, "new"), (2, 1, "only"), (3, 1, "third"),
    ]


def test_replacing_merge_tree_without_version_keeps_last_insert(disk_ctx):
    """没写 version 列时,按"新近程度"(block 号大 = 更新)保留最后插入的那行。"""
    eng = _create(disk_ctx, "t", "k UInt64, v String", "k",
                  engine="ReplacingMergeTree()")
    disk_ctx.insert("t", [(1, "a")])
    disk_ctx.insert("t", [(1, "b")])

    disk_ctx.execute("OPTIMIZE TABLE t FINAL")

    assert eng.part_count() == 1
    assert disk_ctx.execute("SELECT k, v FROM t ORDER BY k").rows() == [(1, "b")]


def test_summing_merge_tree_sums_auto_numeric_columns(disk_ctx):
    """``SummingMergeTree()``:不列列名时**自动**相加"所有不在排序键里的数值列"。

    ``v`` 与 ``w`` 都会被相加:``(1,10,100)+(1,5,7) → (1,15,107)``。
    """
    _create(disk_ctx, "s", "k UInt64, v UInt64, w UInt64", "k",
            engine="SummingMergeTree()")
    disk_ctx.insert("s", [(1, 10, 100), (2, 1, 1)])
    disk_ctx.insert("s", [(1, 5, 7), (2, 2, 2)])

    disk_ctx.execute("OPTIMIZE TABLE s FINAL")

    assert disk_ctx.execute("SELECT k, v, w FROM s ORDER BY k").rows() == [
        (1, 15, 107), (2, 3, 3),
    ]


def test_summing_merge_tree_sums_single_listed_column(disk_ctx):
    """显式列出**一列**时:只加这一列,其余列取组内第一行的值。

    ``SummingMergeTree(v)`` 会把 ``v`` 相加(10+5=15),而 ``w`` 不在清单里,
    于是保留第一行(part 较旧的那个)的值 ``100``。
    """
    _create(disk_ctx, "s", "k UInt64, v UInt64, w UInt64", "k",
            engine="SummingMergeTree(v)")
    disk_ctx.insert("s", [(1, 10, 100)])
    disk_ctx.insert("s", [(1, 5, 7)])

    disk_ctx.execute("OPTIMIZE TABLE s FINAL")

    assert disk_ctx.execute("SELECT k, v, w FROM s ORDER BY k").rows() == [(1, 15, 100)]


@pytest.mark.xfail(
    raises=MiniChError,
    reason=(
        "minich bug: 解析器不接受 ``SummingMergeTree((v, w))`` 这种'元组参数'写法"
        "(真实 ClickHouse 里多列求和的标准写法),建表就抛 SYNTAX_ERROR。"
        "于是多列求和**根本无法表达** —— 退而写字符串形式 ``SummingMergeTree(v, w)``,"
        "又会被 ``_configure_family`` 静默截断成只加第一列(见上一个 xfail)。"
    ),
)
def test_summing_tuple_column_syntax_is_accepted(disk_ctx):
    """``SummingMergeTree((v, w))`` 应当能建表(真实 CH 标准写法)—— 目前被解析器拒绝。"""
    _create(disk_ctx, "s", "k UInt64, v UInt64, w UInt64", "k",
            engine="SummingMergeTree((v, w))")


def test_collapsing_merge_tree_cancels_sign(disk_ctx):
    """``CollapsingMergeTree(sign)``:``+1`` 与 ``-1`` 抵消(``sum(sign) <= 0`` 整组丢弃)。

    ``k=1`` 一组是 ``+1`` 与 ``-1`` → 抵消后为空;``k=2`` 只有 ``+1`` → 保留。
    """
    _create(disk_ctx, "c", "k UInt64, v UInt64, sign Int8", "k",
            engine="CollapsingMergeTree(sign)")
    disk_ctx.insert("c", [(1, 10, 1)])
    disk_ctx.insert("c", [(1, 10, -1)])
    disk_ctx.insert("c", [(2, 20, 1)])

    disk_ctx.execute("OPTIMIZE TABLE c FINAL")

    assert disk_ctx.execute("SELECT k, v, sign FROM c ORDER BY k").rows() == [(2, 20, 1)]


# ══════════════════════════════════════════════════════════════════════════
# 10. Mutation —— ALTER DELETE / UPDATE 登记后由 OPTIMIZE FINAL 落实
# ══════════════════════════════════════════════════════════════════════════


def test_alter_delete_is_registered_then_applied_by_optimize(disk_ctx):
    """``ALTER DELETE`` 只**登记**一条 mutation;数据要等 part 重写才变。

    登记后立刻查:仍然是 6 行,``system`` 侧的 ``parts_to_do = 1``、``is_done = 0``。
    ``OPTIMIZE ... FINAL`` 触发重写,``id=2`` 这才消失,``is_done`` 变成 1。
    """
    eng = _create(disk_ctx, "mm", "id UInt64, v UInt64", "id")
    disk_ctx.insert("mm", [(i, i * 10) for i in range(6)])

    disk_ctx.execute("ALTER TABLE mm DELETE WHERE id = 2")

    # 只是登记:数据还没变
    assert disk_ctx.execute("SELECT count() FROM mm").rows() == [(6,)]
    status = eng.mutation_status()
    assert len(status) == 1
    assert (status[0]["parts_to_do"], status[0]["is_done"]) == (1, 0)

    disk_ctx.execute("OPTIMIZE TABLE mm FINAL")

    assert disk_ctx.execute("SELECT id, v FROM mm ORDER BY id").rows() == [
        (0, 0), (1, 10), (3, 30), (4, 40), (5, 50),
    ]
    assert eng.mutation_status()[0]["is_done"] == 1


def test_alter_update_applied_by_optimize_final(disk_ctx):
    """``ALTER UPDATE`` 同样滞后:``OPTIMIZE ... FINAL`` 之后只有谓词为真的行被改。"""
    eng = _create(disk_ctx, "mm", "id UInt64, v UInt64", "id")
    disk_ctx.insert("mm", [(i, i * 10) for i in range(6)])

    disk_ctx.execute("ALTER TABLE mm UPDATE v = 999 WHERE id = 3")
    # 登记后立即查:还是旧值
    assert disk_ctx.execute("SELECT v FROM mm WHERE id = 3").rows() == [(30,)]

    disk_ctx.execute("OPTIMIZE TABLE mm FINAL")

    assert disk_ctx.execute("SELECT id, v FROM mm ORDER BY id").rows() == [
        (0, 0), (1, 10), (2, 20), (3, 999), (4, 40), (5, 50),
    ]
    assert eng.mutation_status()[0]["is_done"] == 1


# ══════════════════════════════════════════════════════════════════════════
# 11. TTL —— 到点的行在 part 重写(OPTIMIZE FINAL)时被丢弃
# ══════════════════════════════════════════════════════════════════════════


def test_ttl_delete_drops_expired_rows_on_optimize(disk_ctx):
    """``TTL ts + INTERVAL 1 DAY``:2020 那行过期、2099 那行没过期。

    未 OPTIMIZE 前两行都在;``OPTIMIZE ... FINAL`` 触发 TTL 后只剩未过期的那行。
    """
    _create(disk_ctx, "ttl", "id UInt64, ts DateTime", "id",
            ttl="ts + INTERVAL 1 DAY")
    disk_ctx.insert("ttl", [(1, "2020-01-01 00:00:00"), (2, "2099-01-01 00:00:00")])

    assert disk_ctx.execute("SELECT id FROM ttl ORDER BY id").rows() == [(1,), (2,)]

    disk_ctx.execute("OPTIMIZE TABLE ttl FINAL")

    assert disk_ctx.execute("SELECT id FROM ttl ORDER BY id").rows() == [(2,)]
    assert disk_ctx.execute("SELECT count() FROM ttl").rows() == [(1,)]


def test_ttl_drops_fully_expired_part(disk_ctx):
    """整个 part 都过期时直接丢弃(O(1) 快路径):只剩下未过期的那个 part。

    两次 INSERT = 两个 part:``all_1_1_0``(已过期)与 ``all_2_2_0``(未来)。
    OPTIMIZE FINAL 只把过期的那个整块删掉,另一个连名字都不变。
    """
    eng = _create(disk_ctx, "ttl", "id UInt64, ts DateTime", "id",
                  ttl="ts + INTERVAL 1 DAY")
    disk_ctx.insert("ttl", [(1, "2020-01-01 00:00:00")])
    disk_ctx.insert("ttl", [(2, "2099-01-01 00:00:00")])
    assert sorted(p.get_name() for p in eng.get_parts()) == ["all_1_1_0", "all_2_2_0"]

    disk_ctx.execute("OPTIMIZE TABLE ttl FINAL")

    assert [p.get_name() for p in eng.get_parts()] == ["all_2_2_0"]
    assert disk_ctx.execute("SELECT id FROM ttl").rows() == [(2,)]


# ══════════════════════════════════════════════════════════════════════════
# 12. 校验和损坏 —— ``CHECKSUM_DOESNT_MATCH`` (233)
# ══════════════════════════════════════════════════════════════════════════


def _corrupt(path):
    """把文件最后一个字节取反 —— 大小不变、内容变了,CRC32 必然对不上。"""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    data[-1] ^= 0xFF
    with open(path, "wb") as f:
        f.write(bytes(data))


def test_check_reports_checksum_mismatch_for_corrupted_column_bin(disk_ctx):
    """破坏列的 ``.bin`` 后,``engine.check()`` 报告 ``id.bin: checksum mismatch``。

    这正是 ``CHECK TABLE`` 存在的意义:把"静默的数据损坏"变成一条明确的错误。
    """
    eng = _create(disk_ctx, "t", "id UInt64", "id")
    disk_ctx.insert("t", [(i,) for i in range(10)])
    part = eng.get_parts()[0]

    bin_path = os.path.join(disk_ctx.data_path, "default", "t",
                            part.get_name(), "id.bin")
    assert os.path.exists(bin_path)
    _corrupt(bin_path)

    report = eng.check()
    assert "id.bin" in report
    assert "checksum mismatch" in report
    assert str(ErrorCode.CHECKSUM_DOESNT_MATCH) in report  # 233


def test_reading_corrupted_column_bin_raises_checksum_mismatch(disk_ctx):
    """读被破坏的列时,压缩块自己的 CRC 会先炸 —— ``CHECKSUM_DOESNT_MATCH`` (233)。"""
    eng = _create(disk_ctx, "t", "id UInt64", "id")
    disk_ctx.insert("t", [(i,) for i in range(10)])
    part = eng.get_parts()[0]

    bin_path = os.path.join(disk_ctx.data_path, "default", "t",
                            part.get_name(), "id.bin")
    _corrupt(bin_path)

    with pytest.raises(MiniChError) as ei:
        _read(disk_ctx, eng)
    assert ei.value.code == ErrorCode.CHECKSUM_DOESNT_MATCH
    assert ei.value.code == 233


def test_check_reports_checksum_mismatch_for_corrupted_count_txt(disk_ctx):
    """改 ``count.txt`` 也会被 part 级 checksums 抓到(行数与校验和都记在那儿)。"""
    eng = _create(disk_ctx, "t", "id UInt64", "id")
    disk_ctx.insert("t", [(i,) for i in range(10)])
    part = eng.get_parts()[0]

    count_path = os.path.join(disk_ctx.data_path, "default", "t",
                              part.get_name(), "count.txt")
    with open(count_path, "wb") as f:
        f.write(b"99")

    report = eng.check()
    assert "count.txt" in report
    assert "checksum mismatch" in report


# ══════════════════════════════════════════════════════════════════════════
# 13. 回归 —— 稀疏索引下界 / FINAL 读路径 / Nullable 排序键 / 窄类型求和 / TTL
#
# 这一节对应的都是"测试全绿时也照样错"的缺陷:它们的共同点是**只有特定
# 形状的数据**才会暴露(重复首键、多 part 拼接、NULL、窄类型、DateTime64),
# 而原来的测试恰好都躲开了这些形状。
# ══════════════════════════════════════════════════════════════════════════


def test_duplicate_sort_key_keeps_all_matching_granules(disk_ctx):
    """低基数排序键的**重复首键**不能把整段 granule 裁掉(数据丢失)。

    10 行 / 粒度 4、键为 ``0,0,0,0,0,0,1,1,2,2`` → 3 个 granule,
    ``primary.idx`` 里的首键是 ``0、0、2`` —— granule 0 与 granule 1
    **首键相同**。

    如果下界用"第一个首键 **>** lo 的 mark 减一",``WHERE g = 0`` 会从
    mark 1 开始读,granule 0 里那 4 个 0 就永远读不到了(只返回 2 行)。
    下界必须是"第一个首键 **>=** lo 的 mark 减一"。
    """
    eng = _create(disk_ctx, "t", "g UInt64", "g")
    disk_ctx.insert("t", [(0,), (0,), (0,), (0,), (0,), (0,), (1,), (1,), (2,), (2,)])
    part = eng.get_parts()[0]

    # 前提:首键确实重复 —— 否则这个用例什么也证明不了
    assert part.get_marks_count() == 3
    assert [part.get_index_value(m) for m in range(3)] == [(0,), (0,), (2,)]

    rows, ev = _read(disk_ctx, eng, "g = 0")
    assert rows == 6                     # 而不是 2
    assert _marks(ev) == 2               # granule 0 与 granule 1 都要读

    rows, ev = _read(disk_ctx, eng, "g >= 0")
    assert rows == 10
    assert _marks(ev) == 3

    rows, ev = _read(disk_ctx, eng, "g IN (0, 1)")
    assert rows == 8                     # 而不是 4
    assert _marks(ev) == 2

    # 上界没坏:``g > 1`` 没有上界 → 读到最后一个 granule 为止(2 个 mark)
    rows, ev = _read(disk_ctx, eng, "g > 1")
    assert rows == 2
    assert _marks(ev) == 2


def test_final_read_folds_replacing_across_parts(disk_ctx):
    """``SELECT ... FINAL`` 必须先把多个 part 的行**按排序键对齐**再折叠。

    三次 INSERT → 三个 part,彼此之间键不是全局有序的
    (part1 有 k=1,part2 只有 k=2,part3 又有 k=1)。不排序的话
    "同键的行不相邻",ReplacingMergeTree 一行也去不掉。
    """
    _create(disk_ctx, "r", "k UInt64, ver UInt64, v UInt64", "k",
            engine="ReplacingMergeTree(ver)")
    disk_ctx.insert("r", [(1, 10, 1)])
    disk_ctx.insert("r", [(2, 0, 1)])
    disk_ctx.insert("r", [(1, 20, 2)])

    # 不带 FINAL:三行都在(缺陷只在读路径的 FINAL,不动"未合并"的语义)
    assert disk_ctx.execute("SELECT k FROM r ORDER BY k").rows() == [(1,), (1,), (2,)]

    assert disk_ctx.execute("SELECT k, ver, v FROM r FINAL").rows() == [
        (1, 20, 2), (2, 0, 1),
    ]


def test_final_read_folds_summing_across_parts(disk_ctx):
    """``SummingMergeTree`` 的 ``FINAL`` 读路径同样要跨 part 归并。"""
    _create(disk_ctx, "s", "k UInt64, v UInt64", "k", engine="SummingMergeTree")
    disk_ctx.insert("s", [(1, 10)])
    disk_ctx.insert("s", [(2, 100)])
    disk_ctx.insert("s", [(1, 20)])

    assert disk_ctx.execute("SELECT k, v FROM s FINAL").rows() == [(1, 30), (2, 100)]


def test_final_read_folds_collapsing_across_parts(disk_ctx):
    """``CollapsingMergeTree`` 的 ``FINAL``:+1 与 -1 落在**不同 part** 也要抵消。"""
    _create(disk_ctx, "c", "k UInt64, v UInt64, sign Int8", "k",
            engine="CollapsingMergeTree(sign)")
    disk_ctx.insert("c", [(1, 10, 1)])
    disk_ctx.insert("c", [(2, 100, 1)])
    disk_ctx.insert("c", [(1, 10, -1)])

    assert disk_ctx.execute("SELECT k, v, sign FROM c FINAL").rows() == [(2, 100, 1)]


def test_nullable_sort_key_inserts_and_sorts_null_last(disk_ctx):
    """排序键是 ``Nullable`` 且含 NULL 时 INSERT 不能崩,NULL 按"最大值"排。

    老实现直接拿原始值比较,``None < int`` 抛裸 ``TypeError``;
    现在 NULL 会被收成一个"比任何值都大"的哨兵(与真实 ClickHouse 一致:
    ASC 时 NULL 在最后)。
    """
    eng = _create(disk_ctx, "t", "k Nullable(Int32), v String", "k")
    disk_ctx.insert("t", [(None, "a"), (1, "b")])       # 以前这里直接 TypeError
    part = eng.get_parts()[0]

    # part 内部按排序键排好:1 在前、NULL 在后
    assert disk_ctx.execute("SELECT k, v FROM t ORDER BY k").rows() == [
        (1, "b"), (None, "a"),
    ]
    # 首键也必须是这个顺序 —— 否则在 primary.idx 上二分会乱走
    assert part.get_index_value(0) == (1,)

    rows, _ = _read(disk_ctx, eng, "k = 1")
    assert rows == 1


def test_nullable_string_key_condition_does_not_drop_granules(disk_ctx):
    """``Nullable(String)`` 排序键 + NULL:主键索引的二分不能把数据裁掉。

    8 行 / 粒度 4 → 首键是 ``['Zebra', NULL]``。如果比较函数按 ``repr``
    兜底比较,``repr(None) == 'None' < 'Zebra'`` 会让索引看起来是**降序**的,
    二分于是落到"第一个 > 'Zebra' 的 mark = 1",``WHERE k = 'Zebra'``
    读成全是 NULL 的那个 granule → **一行都返回不了**。
    """
    eng = _create(disk_ctx, "t", "k Nullable(String), v UInt64", "k")
    disk_ctx.insert("t", [("Zebra", 1), ("Zebra", 2), ("Zebra", 3), ("Zebra", 4),
                          (None, 5), (None, 6), (None, 7), (None, 8)])
    part = eng.get_parts()[0]
    assert part.get_index_value(0) == ("Zebra",)

    rows, ev = _read(disk_ctx, eng, "k = 'Zebra'")
    assert rows == 4
    assert _marks(ev) == 1

    rows, ev = _read(disk_ctx, eng, "k > 'A'")
    # NULL 是最大值,但 ``NULL > 'A'`` 在 SQL 里是 NULL(不成立)→ 只返回 4 行;
    # 关键是**两个 granule 都被读到了**(索引没把含 'Zebra' 的那段裁掉)
    assert rows == 4
    assert _marks(ev) == 2


def test_summing_merge_tree_wraps_uint8_overflow(disk_ctx):
    """``SummingMergeTree`` 的窄类型求和要**回绕**,不能抛裸 ``OverflowError``。

    docstring 自己写着"``UInt8`` 的 ``250 + 10 = 4``":求和用 Python 任意精度
    算,回写 ``UInt8`` 列时必须按列宽回绕,否则 NumPy 抛
    ``OverflowError: Python integer 260 out of bounds for uint8``。
    """
    _create(disk_ctx, "s", "k UInt64, v UInt8", "k", engine="SummingMergeTree")
    disk_ctx.insert("s", [(1, 250)])
    disk_ctx.insert("s", [(1, 10)])

    disk_ctx.execute("OPTIMIZE TABLE s FINAL")          # 真合并路径
    assert disk_ctx.execute("SELECT k, v FROM s").rows() == [(1, 4)]


def test_summing_merge_tree_wraps_int8_overflow(disk_ctx):
    """有符号窄类型同理:``Int8`` 的 ``127 + 1 = -128``。"""
    _create(disk_ctx, "s", "k UInt64, v Int8", "k", engine="SummingMergeTree")
    disk_ctx.insert("s", [(1, 127)])
    disk_ctx.insert("s", [(1, 1)])

    assert disk_ctx.execute("SELECT k, v FROM s FINAL").rows() == [(1, -128)]


def test_ttl_datetime64_expires_by_precision(disk_ctx):
    """``TTL ts + INTERVAL 1 DAY`` 对 ``DateTime64`` 必须真的生效。

    ``DateTime64`` 内部是**按精度缩放过的整数**(毫秒/微秒),不按精度
    还原就拿 1e12 级的值去和 ``now()`` 的 1e9 级值比,"过期"永远不成立
    → TTL **静默失效**(``DateTime``/``Date`` 却是对的)。
    """
    _create(disk_ctx, "ttl3", "id UInt64, ts DateTime64(3)", "id",
            ttl="ts + INTERVAL 1 DAY")
    disk_ctx.insert("ttl3", [(1, "2000-01-01 00:00:00"), (2, "2099-01-01 00:00:00")])
    disk_ctx.execute("OPTIMIZE TABLE ttl3 FINAL")
    assert disk_ctx.execute("SELECT id FROM ttl3 ORDER BY id").rows() == [(2,)]

    # 精度 6(微秒)是同一回事 —— 别只修 3 位
    _create(disk_ctx, "ttl6", "id UInt64, ts DateTime64(6)", "id",
            ttl="ts + INTERVAL 1 DAY")
    disk_ctx.insert("ttl6", [(1, "2000-01-01 00:00:00"), (2, "2099-01-01 00:00:00")])
    disk_ctx.execute("OPTIMIZE TABLE ttl6 FINAL")
    assert disk_ctx.execute("SELECT id FROM ttl6 ORDER BY id").rows() == [(2,)]


def test_mark_for_row_is_zero_based():
    """``mark_for_row`` 是行号 → granule 下标(0 基),不是"插入位置"。

    ``_prefix`` 是前缀和 ``[0, 4, 8, 10]``,``bisect_right`` 返回的是**下一个**
    granule 的下标 —— 不减一的话整条映射偏移一格(``mark_for_row(0) == 1``),
    ``mark_range_for_rows`` 也跟着偏。
    """
    g = IndexGranularity(4)
    g.set_granularities([4, 4, 2])
    assert [g.mark_for_row(r) for r in range(10)] == [0, 0, 0, 0, 1, 1, 1, 1, 2, 2]
    # 越界行返回"granule 数"这个哨兵(调用方据此知道行不存在)
    assert g.mark_for_row(10) == 3
    # 行区间 → granule 区间:只会变宽,不会变窄
    assert g.mark_range_for_rows(0, 10) == MarkRange(0, 3)
    assert g.mark_range_for_rows(4, 8) == MarkRange(1, 2)
    assert g.mark_range_for_rows(0, 0) == MarkRange(0, 0)
