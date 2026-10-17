# -*- coding: utf-8 -*-
"""第 10 章实测脚本：四层过滤手段的收益量化。"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# 这些脚本原本在仓库根目录下直接跑；挪进 examples/verify/ 之后
# 需要自己把包的搜索路径补上。
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))



import numpy as np
import mini_lancedb as mldb
from mini_lancedb.scanner import _stats_can_match
from mini_lancedb.expr import parse
from mini_lancedb.expr.rewrite import to_index_expr, estimate_selectivity


def meter(db, table):
    """_PrefixedStore 不转发 stats，这里手动把它的 stats 指回根 instrumented store。"""
    table._ds.store.stats = db.store.stats
    return table


def scan(db, tbl, **kw):
    db.reset_io()
    sc = tbl._dataset.scanner(**kw)
    b = sc.to_batch()
    return sc, b


def make_rows(f0, f1):
    return [{"n": n, "grp": n % 50, "txt": f"item-{n:06d}", "pad": "x" * (n % 40)}
            for n in range(f0, f1)]


def segmented(db, name, n_frag=10, per=2000):
    t = db.create_table(name, make_rows(0, per))
    for f in range(1, n_frag):
        t.add(make_rows(f * per, (f + 1) * per))
    return meter(db, t)


def flat(db, name, n=20000):
    return meter(db, db.create_table(name, make_rows(0, n)))


db = mldb.connect("memory://")
db.instrument()

print("=" * 72)
print("数据集 A：分 10 次写入 -> 10 个片段，每个 2000 行，n 全局单调递增")
print("=" * 72)
t = segmented(db, "seg")
s = t.stats()
print(f"片段数={s.num_fragments} 行数={s.num_rows:,} 字节={s.total_bytes:,} 每行={s.bytes_per_row} 字节")

print()
print("=" * 72)
print("1. 列投影下推")
print("=" * 72)
for cols in (["n"], ["n", "grp"], ["n", "grp", "txt"], ["n", "grp", "txt", "pad"], None):
    sc, b = scan(db, t, columns=cols)
    label = "全列" if cols is None else f"{len(cols)} 列 {cols}"
    print(f"{label:<28} 读取字节={sc.stats.bytes_read:>9,}  往返={sc.stats.range_reads:>4}  "
          f"扫描行={sc.stats.rows_scanned:>7,}  耗时={sc.stats.elapsed_ms:7.2f} ms")

print()
print("=" * 72)
print("2. 片段级 zone map 剪枝")
print("=" * 72)
for pred in ("n > 19000", "n > 19999", "n > 20000", "n < 0", "n >= 0"):
    sc, b = scan(db, t, columns=["n"], filter=pred)
    print(f"{pred:<14} 剪掉={sc.stats.fragments_pruned_by_stats:>2}/10 扫描片段={sc.stats.fragments_scanned} "
          f"扫描行={sc.stats.rows_scanned:>7,} 读取字节={sc.stats.bytes_read:>9,} 结果={b.num_rows:>6,}")
print()
print("--- 开 / 关 zone map 对照（n > 19000，只读 n 列）---")
sc1, b1 = scan(db, t, columns=["n"], filter="n > 19000")
sc2, b2 = scan(db, t, columns=["n"], filter="n > 19000")
db.reset_io()
sc3 = t._dataset.scanner(columns=["n"], filter="n > 19000").disable_zone_maps()
sc3.to_batch()
print(f"开启剪枝: 剪掉={sc1.stats.fragments_pruned_by_stats} 扫描行={sc1.stats.rows_scanned:>7,} "
      f"读取字节={sc1.stats.bytes_read:>9,} 耗时={sc1.stats.elapsed_ms:7.2f} ms")
print(f"关闭剪枝: 剪掉={sc3.stats.fragments_pruned_by_stats} 扫描行={sc3.stats.rows_scanned:>7,} "
      f"读取字节={sc3.stats.bytes_read:>9,} 耗时={sc3.stats.elapsed_ms:7.2f} ms")

print()
print("=" * 72)
print("3. 索引下推（无索引 vs BTREE）")
print("=" * 72)
for pred in ("n = 12345", "n > 19000"):
    sc, b = scan(db, t, columns=["n"], filter=pred)
    print(f"无索引 {pred:<14} 扫描行={sc.stats.rows_scanned:>7,} 读={sc.stats.bytes_read:>9,} "
          f"索引查询={sc.stats.index_lookups} 结果={b.num_rows}")
t.create_index("n", config="BTREE")
print("已建 BTREE 索引 on n，索引大小 =", t.list_indices()[0].size_bytes, "字节")
for pred in ("n = 12345", "n > 19000"):
    sc, b = scan(db, t, columns=["n"], filter=pred)
    print(f"有索引 {pred:<14} 扫描行={sc.stats.rows_scanned:>7,} 读={sc.stats.bytes_read:>9,} "
          f"索引查询={sc.stats.index_lookups} 结果={b.num_rows}")

print()
print("--- 索引在宽表上：交叉点 ---")
dbx = mldb.connect("memory://"); dbx.instrument()
def wide(a, b):
    return [{"id": i, "payload": "y" * 120, "f": float(i) * 1.5} for i in range(a, b)]
tx = meter(dbx, dbx.create_table("wide", wide(0, 20000)))
for f in range(1, 10):
    tx.add(wide(f * 20000, (f + 1) * 20000))
sx = tx.stats()
print(f"  表: {sx.num_rows:,} 行 {sx.num_fragments} 片段 {sx.total_bytes:,} 字节")
for label in ("无索引", "BTREE"):
    if label == "BTREE":
        tx.create_index("id", config="BTREE")
    dbx.reset_io()
    sc = tx._dataset.scanner(columns=["payload"], filter="id = 123456"); sc.to_batch()
    print(f"  {label:<6} 点查: 扫描行={sc.stats.rows_scanned:>7,} 读={sc.stats.bytes_read:>10,} 耗时={sc.stats.elapsed_ms:8.2f} ms")
print("  BTREE 索引文件大小 =", tx.list_indices()[0].size_bytes, "字节")

print()
print("=" * 72)
print("4. 延迟物化（在没有索引的表上测）")
print("=" * 72)
u = flat(db, "flat")
for pred in ("n >= 0", "n > 15000", "n > 19500"):
    a = u._dataset.scanner(columns=["n", "grp", "txt", "pad"], filter=pred, late_materialization=True)
    db.reset_io(); sa = a.to_batch()
    c = u._dataset.scanner(columns=["n", "grp", "txt", "pad"], filter=pred, late_materialization=False)
    db.reset_io(); scb = c.to_batch()
    print(f"{pred:<11} 延迟物化 读={a.stats.bytes_read:>9,} 省行={a.stats.late_materialization_saved_rows:>7,} | "
          f"关掉 读={c.stats.bytes_read:>9,} | 结果={sa.num_rows}")
a = u._dataset.scanner(columns=["n"], filter="n > 19500")
db.reset_io(); a.to_batch()
print(f"投影列 = 过滤列（只读 n）时：读={a.stats.bytes_read:,} 省行={a.stats.late_materialization_saved_rows}")

print()
print("=" * 72)
print("5. 行范围下推：偏移连续 vs 碎裂")
print("=" * 72)
big = [{"n": i} for i in range(20000)]
db2 = mldb.connect("memory://"); db2.instrument()
for label, dele in (("无删除（偏移连续）", None),
                    ("删掉一半（偏移碎裂）", "n % 2 = 0"),
                    ("只删前段（偏移连续）", "n < 19900")):
    tt = meter(db2, db2.create_table("rr", big, mode="overwrite"))
    if dele:
        tt.delete(dele)
    sc, b = scan(db2, tt, columns=["n"])
    print(f"{label:<24} 读取字节={sc.stats.bytes_read:>8,} 往返={sc.stats.range_reads:>2} "
          f"扫描行={sc.stats.rows_scanned:>7,} 逻辑行={tt.count_rows():>6,}")
bigw = [{"id": i, "payload": "z" * (i % 200 + 50)} for i in range(20000)]
for label, dele in (("窄表 无删除", None), ("窄表 只删前段", "id < 19900")):
    tt = meter(db2, db2.create_table("rrw", bigw, mode="overwrite"))
    if dele:
        tt.delete(dele)
    sc, b = scan(db2, tt, columns=["payload"])
    print(f"{label:<24} 读取字节={sc.stats.bytes_read:>8,} 往返={sc.stats.range_reads:>2} "
          f"扫描行={sc.stats.rows_scanned:>7,} 逻辑行={tt.count_rows():>6,}")

print()
print("=" * 72)
print("6. AND 部分下推 vs OR 全有全无")
print("=" * 72)
schema = t.schema
for pred in ("n = 5 AND grp = 5", "n = 5 OR grp = 5", "n = 5 AND txt LIKE 'item-000005'",
             "n = 5 OR txt LIKE 'item-000005'", "n > 5 AND n < 10", "n > 5 OR n < 10"):
    e = parse(pred, schema)
    ie = to_index_expr(e, "n")
    print(f"{pred:<34} to_index_expr(n) -> {type(ie).__name__ if ie is not None else 'None（退化成全表扫描）'}")
print()
tp = meter(db, db.create_table("part", [{"a": i % 7, "b": i % 3} for i in range(30000)]))
tp.create_index("a", config="BTREE")
print("只给 a 建了 BTREE，谓词 a = 2 AND b = 1")
sc, b = scan(db, tp, columns=["a", "b"], filter="a = 2 AND b = 1")
print(f"  AND: 索引查询={sc.stats.index_lookups} 扫描行={sc.stats.rows_scanned:>7,} 结果={b.num_rows}")
sc, b = scan(db, tp, columns=["a", "b"], filter="a = 2 OR b = 1")
print(f"  OR : 索引查询={sc.stats.index_lookups} 扫描行={sc.stats.rows_scanned:>7,} 结果={b.num_rows}")

print()
print("=" * 72)
print("7. 三态剪枝：只有 False 是安全的")
print("=" * 72)
ty = meter(db, db.create_table("tri", [{"n": i} for i in range(1000)]))
ty.add_columns({"later": "int64"})
ds = ty._dataset
frag = ds.fragments[0]
for pred in ("n > 9999", "later > 5", "later IS NULL", "later IS NOT NULL", "n = 1", "n > 500"):
    e = parse(pred, ds.schema)
    print(f"  {pred:<20} -> {_stats_can_match(e, frag, ds.schema)}")
print("  该片段文件的列:", [f.column_names for f in frag.files], " 物理行数:", frag.physical_rows)
print("  对照：直接给一个「全 null」统计 -> _range_might_match 会返回什么？")
from mini_lancedb.scanner import _range_might_match
print("   _range_might_match('>', 5, {'min': None, 'max': None, 'null_count': 1000}) ->",
      _range_might_match(">", 5, {"min": None, "max": None, "null_count": 1000}))

print()
print("=" * 72)
print("8. 字符串列的 min/max 剪枝")
print("=" * 72)
ti = segmented(db, "noidx")
for col, pred in (("txt", "txt = 'item-015000'"), ("txt", "txt < 'item-002000'"), ("n", "n = 15000")):
    sc, b = scan(db, ti, columns=[col], filter=pred)
    print(f"{pred:<24} 剪掉={sc.stats.fragments_pruned_by_stats}/10 读={sc.stats.bytes_read:>8,} 结果={b.num_rows}")
print("  片段 0 里 txt 的统计:", ti._dataset.fragments[0].files[0].column_stats.get("txt"))
print("  片段 0 里 n 的统计:  ", ti._dataset.fragments[0].files[0].column_stats.get("n"))
print()
print("--- 在别的列上建索引，会关掉 zone map 剪枝 ---")
ti.create_index("n", config="BTREE")
sc, b = scan(db, ti, columns=["txt"], filter="txt = 'item-015000'")
print(f"给 n 建 BTREE 后，同一个 txt 谓词：剪掉={sc.stats.fragments_pruned_by_stats}/10 "
      f"扫描片段={sc.stats.fragments_scanned} 读={sc.stats.bytes_read:,}")

print()
print("=" * 72)
print("9. 优化前后总对比（同一张无索引的 10 片段表）")
print("=" * 72)
print(f"{'方案':<40}{'扫描行':>10}{'读取字节':>12}{'耗时ms':>10}")
u10 = segmented(db, "final")

def line(label, **kw):
    db.reset_io()
    sc = u10._dataset.scanner(**kw)
    sc.to_batch()
    print(f"{label:<40}{sc.stats.rows_scanned:>10,}{sc.stats.bytes_read:>12,}{sc.stats.elapsed_ms:>10.2f}")

line("全表全列（无谓词）")
line("只读 n 列", columns=["n"])
line("n > 19000，只读 n", columns=["n"], filter="n > 19000")
line("n > 19000，只读 n，关 zone map", columns=["n"], filter="n > 19000", use_zone_maps=False)
line("n > 19000，投影 4 列，延迟物化", columns=["n", "grp", "txt", "pad"], filter="n > 19000")
line("n > 19000，投影 4 列，关延迟物化", columns=["n", "grp", "txt", "pad"], filter="n > 19000", late_materialization=False)
line("n = 12345，只读 n（无索引）", columns=["n"], filter="n = 12345")
u10.create_index("n", config="BTREE")
line("n = 12345，只读 n（BTREE）", columns=["n"], filter="n = 12345")

print()
print("=" * 72)
print("10. 选择率估计")
print("=" * 72)
for pred in ("n = 1", "n > 1", "n > 999999", "n IS NULL", "txt LIKE 'item-%'", "txt LIKE '%1%'",
             "n > 1 AND n < 5", "n > 1 OR n < 5", "NOT n = 1", "n IN (1,2,3)"):
    print(f"  {pred:<22} -> {estimate_selectivity(parse(pred, t.schema)):.4f}")
st = {"n": {"ndv": 20000, "min": 0, "max": 19999}}
print(f"  带统计量 n = 1        -> {estimate_selectivity(parse('n = 1', t.schema), st):.6f}")
print(f"  带统计量 n > 999999   -> {estimate_selectivity(parse('n > 999999', t.schema), st):.4f}")
print(f"  带统计量 n < 0        -> {estimate_selectivity(parse('n < 0', t.schema), st):.4f}")

print()
print("=" * 72)
print("11. 一个真实的静默错误：BETWEEN 被统计剪枝整个吃掉")
print("=" * 72)
tz = segmented(db, "between")
for pred in ("n BETWEEN 1500 AND 1510", "n >= 1500 AND n <= 1510"):
    a = scan(db, tz, columns=["n"], filter=pred)[1].num_rows
    b = tz._dataset.scanner(columns=["n"], filter=pred).disable_zone_maps().to_batch().num_rows
    print(f"{pred:<28} 开 zone map={a:>4}  关 zone map={b:>4}")
print("  count_rows('n BETWEEN 1500 AND 1510') =", tz.count_rows("n BETWEEN 1500 AND 1510"))
print("  delete('n BETWEEN 0 AND 499') 删掉的行数 =",
      tz.delete("n BETWEEN 0 AND 499").num_deleted_rows, "（delete 走 _predicate_mask，不受影响）")
print("  单片段表也复现:")
tz2 = meter(db, db.create_table("b2", make_rows(0, 1000)))
print("  n BETWEEN 100 AND 200 ->", tz2._dataset.scanner(columns=["n"], filter="n BETWEEN 100 AND 200").to_batch().num_rows,
      " 应为 101；关剪枝 ->", tz2._dataset.scanner(columns=["n"], filter="n BETWEEN 100 AND 200").disable_zone_maps().to_batch().num_rows)
