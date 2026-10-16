"""Nullable 与 part 生命周期的回归测试(S1–S6)。

这一份钉住六条"**不报错、只是悄悄错/悄悄崩**"的缺陷。它们的共同点是:
正确性只能靠"和真实 ClickHouse 的语义对齐"来判断,套件里原来的测试
一条都没覆盖到(或者恰好断言了错误的行为)。

.. code-block:: text

   S1  Nullable(DateTime) / Nullable(Date) 的 TTL 把整张表删光
       —— DataTypeNullable 不透传 is_date() / is_date_time(),
          于是 ``d + INTERVAL 30 DAY`` 被当成 Date 语义:给"秒"的基数
          加了 30 **秒**而不是 30 天 → 立刻"过期" → OPTIMIZE 全删。
   S2  SummingMergeTree()(不给列清单)漏掉所有 Nullable 数值列
       —— ``Nullable(Int32).is_numeric()`` 是 False,自动挑列把它排除;
          显式 SummingMergeTree(v) 却对 —— 同引擎两种写法行为不一致。
   S3  part 目录在"在飞的读"眼皮底下被合并删掉
       —— ``read()`` 快照 part 之后惰性读,而合并一替换就 remove_dir;
          前台 OPTIMIZE 与后台合并还会挑中同一批 part(同名 os.replace 撞权限)。
   S4  ``ORDER BY CAST(id AS UInt64)`` 每次 INSERT 都 RecursionError
       —— ``_key_column_name`` 对 CastExpr 用同样的参数递归了自己。
   S5  存储层求值器把 ``NULL + 1`` 当错误、把 ``Nullable(Int32)+1`` 推成 UInt64
       —— 与解释器(正确的)不一致,于是 mutation/TTL/DEFAULT 路径崩。
   S6  ``ttl.txt`` 永远写成 ``{}``
       —— 编码器读的字段名(``delete``/``recompress``…)与 TTLInfo 上
          真正的字段名(``delete_at``/``delete_min``…)对不上。

每条测试都断言**真实 ClickHouse 的语义**,而不是"实现当前的行为"。
"""

from __future__ import annotations

import json
import os

import pytest

from minich.block import Block, ColumnWithTypeAndName
from minich.columns import ColumnNullable, ColumnVector
from minich.errors import ErrorCode, MiniChError
from minich.storages.merge_tree.expression import evaluate_expression
from minich.storages.merge_tree.marks import MarkRange
from minich.storages.merge_tree.merge_tree import _ttl_info_of
from minich.storages.merge_tree.part import load_part
from minich.storages.merge_tree.ttl import ttl_value_in_seconds
from minich.sql import parse_expression_text
from minich.types import DataTypeNullable, get_data_type

import numpy as np


# ══════════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════════

#: 关掉自适应粒度,让"一个 granule 恰好 4 行"成为硬保证(与 test_mergetree 一致)。
GRAN = "index_granularity = 4, index_granularity_bytes = 0"


def _engine(ctx, name):
    return ctx.catalog.get_table("default", name).engine


def _create(ctx, name, columns, order_by="id", *, engine="MergeTree", ttl=""):
    ddl = f"CREATE TABLE {name} ({columns}) ENGINE = {engine} ORDER BY {order_by}"
    ddl += f" SETTINGS {GRAN}"
    if ttl:
        ddl += f" TTL {ttl}"
    ctx.execute(ddl)
    eng = _engine(ctx, name)
    eng.stop_merges()          # part 数完全由测试掌控
    return eng


def _rows(ctx, sql):
    return [tuple(r) for r in ctx.execute(sql).rows()]


# ══════════════════════════════════════════════════════════════════════════
# S1 —— Nullable 的 is_date / is_date_time 必须透传
# ══════════════════════════════════════════════════════════════════════════


def test_nullable_delegates_date_predicates():
    """根因断言:``Nullable(T)`` 的值域判定必须等于 ``T``。"""
    assert DataTypeNullable(get_data_type("DateTime")).is_date_time() is True
    assert DataTypeNullable(get_data_type("DateTime")).is_date() is False
    assert DataTypeNullable(get_data_type("Date")).is_date() is True
    assert DataTypeNullable(get_data_type("Date")).is_date_time() is False
    assert DataTypeNullable(get_data_type("Int32")).is_date() is False
    assert DataTypeNullable(get_data_type("Int32")).is_date_time() is False


def test_ttl_value_in_seconds_scales_nullable_date():
    """``Nullable(Date)`` 的内部值是**天数**,TTL 判定必须 × 86400。

    不换算的话 19766(天数)会和 ``now()`` 的 17 亿(秒)比大小 ——
    永远"已过期",TTL 变成"全删"。
    """
    days = get_data_type("Nullable(Date)")
    assert ttl_value_in_seconds(19766, days) == 19766 * 86400
    dt = get_data_type("Nullable(DateTime)")
    assert ttl_value_in_seconds(1_700_000_000, dt) == 1_700_000_000
    # NULL 是"永不过期",不能当成 0(否则 NULL 行会被 TTL 删掉)
    assert ttl_value_in_seconds(None, dt) == 253402300799


@pytest.mark.parametrize("coltype", ["DateTime", "Nullable(DateTime)"])
def test_nullable_datetime_ttl_keeps_fresh_rows(disk_ctx, coltype):
    """60 秒前的行在 ``TTL d + INTERVAL 30 DAY`` 下必须**活着**。"""
    _create(disk_ctx, "n", f"id Int32, d {coltype}",
            ttl="d + INTERVAL 30 DAY DELETE")
    disk_ctx.execute("INSERT INTO n VALUES (1, now() - 60), (2, now() - 70)")
    disk_ctx.execute("OPTIMIZE TABLE n FINAL")
    assert _rows(disk_ctx, "SELECT id FROM n ORDER BY id") == [(1,), (2,)]


@pytest.mark.parametrize("coltype", ["Date", "Nullable(Date)"])
def test_nullable_date_ttl_keeps_today_rows(disk_ctx, coltype):
    """今天的行在 ``TTL d + INTERVAL 30 DAY`` 下必须活着(日期是"天数")。"""
    _create(disk_ctx, "n", f"id Int32, d {coltype}",
            ttl="d + INTERVAL 30 DAY DELETE")
    disk_ctx.execute("INSERT INTO n VALUES (1, today()), (2, today())")
    disk_ctx.execute("OPTIMIZE TABLE n FINAL")
    assert _rows(disk_ctx, "SELECT id FROM n ORDER BY id") == [(1,), (2,)]


# ══════════════════════════════════════════════════════════════════════════
# S2 —— SummingMergeTree() 的自动列清单必须包含 Nullable 数值列
# ══════════════════════════════════════════════════════════════════════════


def test_summing_merge_tree_auto_column_list_includes_nullable(disk_ctx):
    """``SummingMergeTree()`` 与 ``SummingMergeTree(v)`` 必须给出同一个答案。"""
    for i, engine in enumerate(("SummingMergeTree()", "SummingMergeTree(v)")):
        name = f"s{i}"
        _create(disk_ctx, name, "k Int32, v Nullable(Int32)", order_by="k", engine=engine)
        disk_ctx.execute(f"INSERT INTO {name} VALUES (1, 10), (1, 20)")
        disk_ctx.execute(f"OPTIMIZE TABLE {name} FINAL")
        assert _rows(disk_ctx, f"SELECT k, v FROM {name}") == [(1, 30)], engine


def test_summing_merge_tree_nullable_skips_nulls_and_wraps(disk_ctx):
    """NULL 不参与求和(全是 NULL 时保持 NULL);窄类型照常回绕。"""
    _create(disk_ctx, "s", "k Int32, v Nullable(Int32)", order_by="k",
            engine="SummingMergeTree()")
    disk_ctx.execute("INSERT INTO s VALUES (1, 10), (1, NULL), (1, 20)")
    disk_ctx.execute("INSERT INTO s VALUES (2, NULL), (2, NULL)")
    disk_ctx.execute("OPTIMIZE TABLE s FINAL")
    assert _rows(disk_ctx, "SELECT k, v FROM s ORDER BY k") == [(1, 30), (2, None)]

    _create(disk_ctx, "s2", "k Int32, v Nullable(UInt8)", order_by="k",
            engine="SummingMergeTree()")
    disk_ctx.execute("INSERT INTO s2 VALUES (1, 250), (1, 10)")
    disk_ctx.execute("OPTIMIZE TABLE s2 FINAL")
    # 真实 ClickHouse 的整数溢出语义:250 + 10 回绕成 4(不是 260,也不报错)
    assert _rows(disk_ctx, "SELECT k, v FROM s2") == [(1, 4)]


# ══════════════════════════════════════════════════════════════════════════
# S4 —— ORDER BY CAST(...) 不能递归爆栈
# ══════════════════════════════════════════════════════════════════════════


def test_order_by_cast_does_not_recurse(disk_ctx):
    """``ORDER BY CAST(id AS UInt64)`` 建表 + 插入都必须成功。"""
    _create(disk_ctx, "c", "id Int32, a String", order_by="CAST(id AS UInt64)")
    disk_ctx.execute("INSERT INTO c VALUES (3, 'x'), (1, 'y'), (2, 'z')")
    assert _rows(disk_ctx, "SELECT id FROM c ORDER BY id") == [(1,), (2,), (3,)]


# ══════════════════════════════════════════════════════════════════════════
# S5 —— 存储层求值器:NULL 传播 + Nullable 结果类型
# ══════════════════════════════════════════════════════════════════════════


def _nullable_block(values):
    col = ColumnNullable(ColumnVector(np.int32))
    for v in values:
        col.append(v)
    block = Block()
    block.add_column(ColumnWithTypeAndName(
        "v", get_data_type("Nullable(Int32)"), col))
    return block


def test_storage_evaluator_propagates_null_in_binary_arithmetic():
    """``NULL + 1`` 是 ``NULL``,不是错误;结果类型保持可空。"""
    block = _nullable_block([5, None, 7])
    out = evaluate_expression(parse_expression_text("v + 1"), block)
    assert out.type.is_nullable(), out.type.get_name()
    assert [out.column.get(i) for i in range(3)] == [6, None, 8]


def test_storage_evaluator_propagates_null_in_unary_minus():
    """``-NULL`` 是 ``NULL``;以前这里抛裸 ``TypeError``。"""
    block = _nullable_block([5, None])
    out = evaluate_expression(parse_expression_text("-v"), block)
    assert out.type.get_name() == "Nullable(Int32)"
    assert [out.column.get(i) for i in range(2)] == [-5, None]


def test_alter_update_over_nullable_matches_interpreter(disk_ctx):
    """``ALTER ... UPDATE v = v + 1`` 在可空列上必须与解释器同答案。"""
    _create(disk_ctx, "m", "id Int32, v Nullable(Int32)")
    disk_ctx.execute("INSERT INTO m VALUES (1, 5), (2, NULL)")
    disk_ctx.execute("ALTER TABLE m UPDATE v = v + 1 WHERE 1")
    disk_ctx.execute("OPTIMIZE TABLE m FINAL")
    assert _rows(disk_ctx, "SELECT id, v FROM m ORDER BY id") == [(1, 6), (2, None)]


# ══════════════════════════════════════════════════════════════════════════
# S6 —— ttl.txt 的形状(编码器与读取器必须一致)
# ══════════════════════════════════════════════════════════════════════════


def test_ttl_txt_is_not_empty_and_round_trips(disk_ctx):
    """``ttl.txt`` 必须真的写进极值,而且重新加载后仍能读出同一个 TTLInfo。"""
    eng = _create(disk_ctx, "t6", "id Int32, d DateTime",
                  ttl="d + INTERVAL 30 DAY DELETE")
    disk_ctx.execute("INSERT INTO t6 VALUES (1, now())")
    part = eng.get_data_parts()[0]

    raw = eng.disk.read_file(f"{part.path}/ttl.txt")
    stored = json.loads(raw.decode("utf-8"))
    assert stored != {}, "ttl.txt 写成了空对象 —— 快路径永远不会生效"
    # 嵌套形状:每个动作一个 {min, max}(见 TTLInfo.to_ttl_txt)
    assert isinstance(stored["delete"], dict)
    assert stored["delete"]["min"] and stored["delete"]["max"]

    # 读取器(从磁盘重新加载)必须解析出同一个 delete_at / delete_min
    fresh = load_part(disk_ctx.disk, part.path, eng.table)
    info = _ttl_info_of(fresh, eng.ttls, eng, None)
    assert info is not None
    assert info.delete_at is not None and info.delete_min is not None
    assert info.delete_at == info.delete_min   # 单行 part:两个极值相同


def test_ttl_info_of_does_not_fall_back_to_full_read(disk_ctx):
    """``ttl.txt`` 有效时,``_ttl_info_of`` 不能退回"整份读 part"。

    这是这个文件存在的**全部意义**:一次 JSON 比较 vs 扫全 part。
    """
    eng = _create(disk_ctx, "t6", "id Int32, d DateTime",
                  ttl="d + INTERVAL 30 DAY DELETE")
    disk_ctx.execute("INSERT INTO t6 VALUES (1, now())")
    part = eng.get_data_parts()[0]

    def _explode(*_a, **_k):  # pragma: no cover - 走到这里就是回归
        raise AssertionError("_ttl_info_of 退回 _read_part_fully —— ttl.txt 没生效")

    eng._read_part_fully = _explode  # type: ignore[method-assign]
    info = _ttl_info_of(part, eng.ttls, eng, None)
    assert info is not None and info.delete_at is not None


# ══════════════════════════════════════════════════════════════════════════
# S3 —— part 的生命周期:在飞的读不会被抽走目录 / 候选必须预约
# ══════════════════════════════════════════════════════════════════════════


def _two_parts(disk_ctx, name="t"):
    """建一张有两个 part 的表(每个 part 4 行),返回 (engine, parts)。"""
    eng = _create(disk_ctx, name, "a Int32, b String", order_by="a")
    disk_ctx.execute(f"INSERT INTO {name} VALUES (1,'a'),(2,'b'),(3,'c'),(4,'d')")
    disk_ctx.execute(f"INSERT INTO {name} VALUES (5,'e'),(6,'f'),(7,'g'),(8,'h')")
    parts = eng.get_data_parts()
    assert len(parts) == 2
    return eng, parts


def test_pinned_part_directory_survives_a_merge(disk_ctx):
    """被 pin 的 part:目录推迟到引用归还才删,读它不会 FileNotFoundError。"""
    eng, parts = _two_parts(disk_ctx)
    victim = parts[0]
    old_dir = f"{eng.data_path}/{victim.get_name()}"

    eng.pin_parts(parts)                    # 模拟"一次在飞的读"
    eng.optimize()                          # 合并会替换并"删除"这两个 part
    try:
        assert eng.disk.exists_directory(old_dir), "pin 期间目录被删了"
        col = victim.read_column(None, "a", MarkRange(0, len(victim.marks["a"])))
        assert col.to_list() == [1, 2, 3, 4]
    finally:
        eng.unpin_parts(parts)
    # 引用归零 → 目录才真正回收
    assert not eng.disk.exists_directory(old_dir)
    assert len(eng.get_data_parts()) == 1


def test_streaming_read_survives_interleaved_optimize(disk_ctx):
    """``read()`` 生成的 chunk 之间插入一次 OPTIMIZE,读必须照常读完。"""
    eng, _ = _two_parts(disk_ctx)
    it = eng.read(disk_ctx)
    first = next(it)                        # 读第 1 个 part 期间 pin 生效
    assert eng._part_pins, "read() 没有借出 part —— 目录随时可能被删"
    eng.optimize()                          # 后台/前台合并同时发生
    total = first.num_rows + sum(c.num_rows for c in it)
    assert total == 8
    assert not eng._part_pins, "读完之后引用计数没有归还"


def test_reading_a_removed_part_raises_minich_error(disk_ctx):
    """绕过引擎的读取撞上已删 part 时,必须是带错误码的 ``MiniChError``。"""
    eng, parts = _two_parts(disk_ctx)
    victim = parts[0]
    eng.optimize()                          # 没有任何 pin → 目录立刻回收
    marks = len(victim.marks["a"])
    with pytest.raises(MiniChError) as ei:
        victim.read_column(None, "a", MarkRange(0, marks))
    assert ei.value.code == ErrorCode.CANNOT_READ_FROM_ISTREAM


def test_merge_candidate_reservation_is_exclusive(disk_ctx):
    """选中即占用:预约过的 part 不会再被选中,重复预约失败。"""
    eng, _ = _two_parts(disk_ctx)
    cand = eng._select_merge_candidate()
    assert cand is not None and len(cand.parts) == 2
    assert eng._reserve_merge_candidate(cand) is True
    assert eng._select_merge_candidate() is None, "已预约的 part 仍被重复选中"
    assert eng._reserve_merge_candidate(cand) is False


def test_duplicate_merge_candidate_is_rejected(disk_ctx):
    """候选里有"已经不在活跃清单里"的 part 时必须拒绝(防止同名 os.replace)。"""
    eng, parts = _two_parts(disk_ctx)
    cand = eng._select_merge_candidate()
    assert eng._reserve_merge_candidate(cand) is True
    # 把其中一个输入 part 从活跃清单里摘掉(模拟被别的合并抢先替换)
    eng._obsolete_part(parts[0])
    assert eng._reserve_merge_candidate(cand) is False
