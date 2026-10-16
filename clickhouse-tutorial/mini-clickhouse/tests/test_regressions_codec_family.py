"""两条"功能根本没生效 / 静默算错"的回归测试。

它们都属于最危险的那一类缺陷:**不报错、不崩,只是结果悄悄不对**,
所以教程里的整块内容会变成空头支票。

.. code-block:: text

   ① 列级 ``CODEC(...)`` 过去是**空操作** —— 每一列都被按默认的 LZ4 写出。
      根因:``merge_tree.py`` 的 ``_write_one_part`` / ``_rewrite_block``(以及
      合并路径 ``merger.MergeTreeDataPartWriter.write``)调用 ``make_write_context``
      时都没传 ``codecs=``,而函数内部又只 ``codecs=dict(codecs or {})`` —— 永远是空
      dict,于是 ``part.py`` 的 ``codec_for()`` 永远回退到 ``DEFAULT_CODEC = "LZ4"``。
      实测 ``CREATE TABLE … (id UInt64 CODEC(NONE), seq UInt64 CODEC(ZSTD))`` 写完后
      ``part.codecs`` 是 ``{'id':'LZ4','seq':'LZ4'}`` —— 两列声明都被丢掉。

   ② ``AggregatingMergeTree``(以及 ``SummingMergeTree``)在合并时**完全不合并**
      ``SimpleAggregateFunction`` 列,只是保留第一行的值。根因:``merger.py`` 的
      ``_aggregate_func_of`` 对 ``SimpleAggregateFunction`` 返回 ``None``,于是这些
      列在行折叠时被当成普通列"取第一行"。最讽刺的是教程第 13 章 §13.4 就拿它当
      范例教读者 —— 插入 ``(3, 5)`` 与 ``(7, 9)`` 后 ``OPTIMIZE … FINAL``,``sum`` 列
      读到的是 ``3`` 而不是 ``10``、``max`` 列读到的是 ``5`` 而不是 ``9``。

关于链式 ``CODEC``(:func:`_resolve_codec_name`):本实现的压缩块头只有一个 method
字节,表达不了链,所以**取链尾的通用编解码器**真正压缩(刻意简化,见该函数的
文档),非法链(``CODEC(Delta)``)则**报错**而不是静默吞掉。
"""

from __future__ import annotations

import json

import pytest

from minich.compression import CompressionMethodByte
from minich.errors import ErrorCode, MiniChError
from minich.storages.merge_tree.marks import MarkRange
from minich.storages.merge_tree.part import load_part


# ══════════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════════


def _engine(ctx, name):
    return ctx.catalog.get_table("default", name).engine


def _only_part(ctx, name):
    """取一张表当前的**唯一** part —— 调用方保证只插入了一次。"""
    parts = list(_engine(ctx, name).parts.values())
    assert len(parts) == 1, f"expected exactly 1 part, got {[p.name for p in parts]}"
    return parts[0]


def _part_dir(ctx, name):
    eng = _engine(ctx, name)
    part = _only_part(ctx, name)
    return eng, part, f"{eng.data_path}/{part.name}"


# ══════════════════════════════════════════════════════════════════════════
# ① 列级 CODEC(...) 必须真的生效
# ══════════════════════════════════════════════════════════════════════════


def test_column_codec_declarations_are_applied_and_round_trip(disk_ctx):
    """回归 ①:``CODEC(NONE)`` / ``CODEC(ZSTD)`` 要真的生效,且写→读往返一致。

    修前 ``part.codecs`` 会是 ``{'id': 'LZ4', 'seq': 'LZ4', 'plain': 'LZ4'}``
    ——两列声明都被静默丢掉。
    """
    disk_ctx.execute(
        "CREATE TABLE codec_t ("
        "  id UInt64 CODEC(NONE),"
        "  seq UInt64 CODEC(ZSTD),"
        "  plain UInt64"
        ") ENGINE = MergeTree ORDER BY id"
    )
    disk_ctx.execute(
        "INSERT INTO codec_t SELECT number, number * 7, number FROM numbers(500)"
    )

    eng, part, part_dir = _part_dir(disk_ctx, "codec_t")

    # ①-a 声明被真的记住;只有**没写** CODEC 的列才回退到默认的 LZ4
    assert part.codecs == {"id": "NONE", "seq": "ZSTD", "plain": "LZ4"}

    # ①-b 落盘元数据(serialization.json)里的 codec 必须与 part.codecs 一致 ——
    #      否则读路径会对不上(读端靠块头的 method 字节解压,元数据是给人看的)。
    serialization = json.loads(
        disk_ctx.disk.read_file(f"{part_dir}/serialization.json")
    )
    on_disk = {n: c["codec"] for n, c in serialization["columns"].items()}
    assert on_disk == part.codecs

    # ①-c **真正的证据**:落盘块头的第一个字节就是该列的压缩方法。
    #      NONE 列必须是 0x02(原样存),ZSTD 列必须是 0x90 —— 不是默认的 LZ4(0x82)。
    assert disk_ctx.disk.read_file(f"{part_dir}/id.bin")[0] == CompressionMethodByte.NONE
    assert disk_ctx.disk.read_file(f"{part_dir}/seq.bin")[0] == CompressionMethodByte.ZSTD
    assert disk_ctx.disk.read_file(f"{part_dir}/plain.bin")[0] == CompressionMethodByte.LZ4

    # ①-d 写→读往返:经 SELECT 从磁盘**解压**读回,数据必须完全一致
    rows = disk_ctx.execute("SELECT id, seq, plain FROM codec_t ORDER BY id").rows()
    assert rows == [(i, i * 7, i) for i in range(500)]

    # ①-e 更强一步:用 ``load_part`` 从磁盘**重新**建一个 part 对象再逐列读一遍,
    #      证明落盘的块是自描述的(元数据 + method 字节自洽,不依赖内存里的状态)。
    fresh = load_part(disk_ctx.disk, part_dir, eng.table)
    assert fresh.codecs == part.codecs
    marks = MarkRange(0, fresh.get_marks_count())
    for name, factor in (("id", 1), ("seq", 7), ("plain", 1)):
        col = fresh.read_column(None, name, marks)
        assert [col.get(i) for i in range(col.size())] == [i * factor for i in range(500)]


def test_chain_codec_uses_tail_generic_codec_and_round_trips(disk_ctx):
    """回归 ① 的链式分支:本实现表达不了链,退而取**链尾的通用编解码器**。

    这是**刻意的简化**(见 ``merge_tree._resolve_codec_name`` 的文档):压缩块头只有
    一个 method 字节,前面那些"只改布局不压缩"的前置变换被跳过。关键是它**不是**
    "静默什么都不做"——链尾真的去压了(这里变成 ZSTD),而不是退回默认的 LZ4。
    """
    disk_ctx.execute(
        "CREATE TABLE chain_t (k UInt64, v UInt64 CODEC(Delta, ZSTD(3))) "
        "ENGINE = MergeTree ORDER BY k"
    )
    disk_ctx.execute("INSERT INTO chain_t SELECT number, number FROM numbers(300)")

    _, part, part_dir = _part_dir(disk_ctx, "chain_t")
    # 链尾的通用编解码器是 ZSTD —— 既不是整条文本,也不是默认 LZ4
    assert part.codecs == {"k": "LZ4", "v": "ZSTD"}
    assert disk_ctx.disk.read_file(f"{part_dir}/v.bin")[0] == CompressionMethodByte.ZSTD
    # 数据仍能正确往返(链被简化,但写进去的确实压过又解回来了)
    assert disk_ctx.execute("SELECT k, v FROM chain_t ORDER BY k").rows() == [
        (i, i) for i in range(300)
    ]


def test_invalid_codec_chain_is_rejected_not_silently_ignored(disk_ctx):
    """非法链 ``CODEC(Delta)``(专用编解码器后面没有通用编解码器)必须**报错**。

    不能像修前那样被静默吞掉(那时候整条声明被丢掉、按 LZ4 写,插入照样"成功")。
    """
    disk_ctx.execute(
        "CREATE TABLE bad_codec (k UInt64, v UInt64 CODEC(Delta)) "
        "ENGINE = MergeTree ORDER BY k"
    )
    with pytest.raises(MiniChError) as ei:
        disk_ctx.execute("INSERT INTO bad_codec VALUES (1, 2)")
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_column_codec_survives_merge_and_mutation_rewrite(disk_ctx):
    """回归 ①:codec 在**合并**写路径(``merger.MergeTreeDataPartWriter``)与
    **mutation 重写**路径(``MergeTreeData._rewrite_block``)后都要保持。

    这两条路径原来也调 ``make_write_context`` 时丢声明 —— 修法在 ``make_write_context``
    里统一按列收集,所以三处(INSERT / 合并 / 重写)一起生效。这里逐条钉住。
    """
    disk_ctx.execute(
        "CREATE TABLE codec_merge (id UInt64 CODEC(NONE), seq UInt64 CODEC(ZSTD)) "
        "ENGINE = MergeTree ORDER BY id"
    )
    disk_ctx.execute("INSERT INTO codec_merge VALUES (1, 10)")
    disk_ctx.execute("INSERT INTO codec_merge VALUES (2, 20)")

    # 合并路径:两个 part 合成一个,声明不能丢
    disk_ctx.execute("OPTIMIZE TABLE codec_merge FINAL")
    eng = _engine(disk_ctx, "codec_merge")
    parts = list(eng.parts.values())
    assert len(parts) == 1
    assert parts[0].codecs == {"id": "NONE", "seq": "ZSTD"}
    assert disk_ctx.execute("SELECT id, seq FROM codec_merge ORDER BY id").rows() == [
        (1, 10), (2, 20)
    ]

    # mutation 重写路径:ALTER ADD COLUMN 后新列的 CODEC 要生效,旧列声明也不能丢
    disk_ctx.execute(
        "ALTER TABLE codec_merge ADD COLUMN extra UInt64 CODEC(ZSTD) DEFAULT 0"
    )
    disk_ctx.execute("INSERT INTO codec_merge VALUES (3, 30, 300)")
    disk_ctx.execute("OPTIMIZE TABLE codec_merge FINAL")
    parts = list(eng.parts.values())
    assert len(parts) == 1
    assert parts[0].codecs == {"id": "NONE", "seq": "ZSTD", "extra": "ZSTD"}
    assert disk_ctx.execute("SELECT id, seq, extra FROM codec_merge ORDER BY id").rows() == [
        (1, 10, 0), (2, 20, 0), (3, 30, 300)
    ]


# ══════════════════════════════════════════════════════════════════════════
# ② SimpleAggregateFunction 列必须按声明函数折叠
# ══════════════════════════════════════════════════════════════════════════


def test_simple_aggregate_function_merges_in_aggregating_mergetree(disk_ctx):
    """回归 ②:教程 §13.4 的 ``daily_simple`` —— ``sum`` 列要折叠成 10、``max`` 列成 9。

    修前读原始行会看到 ``(3, 5)``(第一行),因为 ``SimpleAggregateFunction`` 列在
    合并时被当普通列"取第一行"。
    """
    disk_ctx.execute(
        "CREATE TABLE daily_simple ("
        "  d Date,"
        "  req_cnt SimpleAggregateFunction(sum, UInt64),"
        "  max_rt SimpleAggregateFunction(max, UInt32)"
        ") ENGINE = AggregatingMergeTree ORDER BY d"
    )
    disk_ctx.execute("INSERT INTO daily_simple VALUES ('2024-01-01', 3, 5)")
    disk_ctx.execute("INSERT INTO daily_simple VALUES ('2024-01-01', 7, 9)")
    disk_ctx.execute("OPTIMIZE TABLE daily_simple FINAL")

    # 同一 key 的两行应已折叠成一行:sum=3+7=10,max=max(5,9)=9
    assert disk_ctx.execute("SELECT req_cnt, max_rt FROM daily_simple").rows() == [(10, 9)]


@pytest.mark.parametrize(
    "func,v1,v2,expected",
    [
        ("sum", 3, 7, 10),      # 3+7
        ("min", 7, 3, 3),       # 故意让"第一行"是 7,与 min 不同 —— 修前会误取 7
        ("max", 3, 7, 7),       # 第一行是 3,修前会误取 3
        ("any", 3, 7, 3),       # any 语义就是"第一行",与修前一致(锁住行为)
        ("anyLast", 3, 7, 7),   # 第一行是 3,修前会误取 3
    ],
)
def test_simple_aggregate_function_folds_each_supported_func(disk_ctx, func, v1, v2, expected):
    """回归 ②:"状态即值"的那一小撮函数都要按各自语义折叠,不只是 ``sum``。

    组内顺序由合并迭代器决定(旧 part 的行在前),所以 ``any`` 取第一行 ``v1``、
    ``anyLast`` 取 ``v2``。除 ``any`` 外,每个用例的期望值都**不等于**修前那种
    "误取第一行"的结果 —— 保证测试能真正区分修复前后。
    """
    table = f"sagg_{func.lower()}"
    disk_ctx.execute(
        f"CREATE TABLE {table} ("
        f"  d UInt64, v SimpleAggregateFunction({func}, UInt64)"
        ") ENGINE = AggregatingMergeTree ORDER BY d"
    )
    disk_ctx.execute(f"INSERT INTO {table} VALUES (1, {v1})")
    disk_ctx.execute(f"INSERT INTO {table} VALUES (1, {v2})")
    disk_ctx.execute(f"OPTIMIZE TABLE {table} FINAL")

    assert disk_ctx.execute(f"SELECT v FROM {table}").rows() == [(expected,)]


def test_simple_aggregate_function_merges_in_summing_mergetree(disk_ctx):
    """回归 ②(``SummingMergeTree`` 一侧):``SimpleAggregateFunction`` 列按声明函数
    折叠,而**自动求和**的普通数值列行为不变(``v`` 照旧 10+20=30)。"""
    disk_ctx.execute(
        "CREATE TABLE sum_sagg ("
        "  d UInt64,"
        "  s SimpleAggregateFunction(sum, UInt64),"
        "  v UInt64"
        ") ENGINE = SummingMergeTree ORDER BY d"
    )
    disk_ctx.execute("INSERT INTO sum_sagg VALUES (1, 3, 10)")
    disk_ctx.execute("INSERT INTO sum_sagg VALUES (1, 7, 20)")
    disk_ctx.execute("OPTIMIZE TABLE sum_sagg FINAL")

    # s 按 SAgF(sum) 折叠成 10;v 是自动求和列,照旧 30
    assert disk_ctx.execute("SELECT s, v FROM sum_sagg").rows() == [(10, 30)]
