# -*- coding: utf-8 -*-
"""第 9 章实测脚本：删除文件格式、物理/逻辑行号、软删除代价、update、merge_insert。"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# 这些脚本原本在仓库根目录下直接跑；挪进 examples/verify/ 之后
# 需要自己把包的搜索路径补上。
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))



import numpy as np
import mini_lancedb as mldb
from mini_lancedb.format.deletion import (
    serialize_deletions, deserialize_deletions, HEADER_SIZE, MAGIC,
)
from mini_lancedb.format.codec_util import varint_encode_list

print("=" * 70)
print("A. 删除文件的物理格式：排序行号 + 差分 + varint")
print("=" * 70)
for n in (3, 1000, 100_000, 3_000_000):
    sparse = [1, n // 2, n - 2]
    b_sparse = serialize_deletions(7, n, sparse)
    dense = list(range(0, n, 2))
    b_dense = serialize_deletions(7, n, dense)
    bitmap = (n + 7) // 8
    print(f"n={n:>9,}  删3行: {len(b_sparse):>9,} B   删一半: {len(b_dense):>9,} B   "
          f"bitmap 固定: {bitmap:>9,} B")
print()
print("头部 struct<magic:4s, version:u32, fragment_id:u64, num_rows:u64, num_deleted:u64> =",
      HEADER_SIZE, "字节, magic =", MAGIC)
head = serialize_deletions(7, 1000, [1, 500, 998])[:HEADER_SIZE]
print("实测头部 hex:", head.hex())
print("解码回来 (fragment_id, num_rows):", deserialize_deletions(head + varint_encode_list([1, 499, 498]))[0:2])
print("解出的行号:", deserialize_deletions(head + varint_encode_list([1, 499, 498]))[2])

print()
print("=" * 70)
print("B. 物理行号 vs 逻辑行号")
print("=" * 70)
db = mldb.connect("memory://")
t = db.create_table("rows", [{"n": i} for i in range(10)])
ds = t._dataset
fi, offs = ds._row_map()
print("片段 id =", ds.fragments[0].id)
for i in range(10):
    a = ds.row_address(i)
    print(f"  逻辑 {i} -> 物理 {int(offs[i])} -> 地址 0x{a:016x}")
print()
t.delete("n IN (1, 3, 5)")
ds = t._dataset
fi, offs = ds._row_map()
print("  逻辑行数 =", ds.num_rows, " 物理行数 =", ds.physical_rows, " 已删 =", ds.num_deleted_rows)
for i in range(ds.num_rows):
    addr = ds.row_address(i)
    fid, off = ds.fragments[int(fi[i])].address_to_row(addr)
    print(f"  逻辑 {i} -> 物理 {int(offs[i])} -> 地址 0x{addr:016x} -> 解回 (片段, 偏移) = ({fid}, {off})")
print("  数据文件里仍然有", ds.fragments[0].physical_rows, "行")
print("  删除文件引用:", ds.fragments[0].deletion_file.to_json())

print()
print("=" * 70)
print("C. 软删除的读取代价")
print("=" * 70)
db = mldb.connect("memory://")
db.instrument()

def meter(table):
    table._ds.store.stats = db.store.stats
    return table

rng = np.random.default_rng(0)
N = 20000
rows = [{"id": i, "n": int(rng.integers(0, 100000))} for i in range(N)]
for frac, name in ((0.0, "无删除"), (0.5, "删 50%"), (0.99, "删 99%")):
    tt = meter(db.create_table("d", rows, mode="overwrite"))
    if frac:
        tt.delete(f"id < {int(N * frac)}")
    d = tt._dataset
    st = tt.stats()
    db.reset_io()
    sc = d.scanner(columns=["n"]); sc.to_batch()
    print(f"{name:<8} 逻辑行={d.num_rows:>6,} 物理行={d.physical_rows:>6,} "
          f"删除比={st.deleted_rows_ratio:.3f} 读字节={sc.stats.bytes_read:>8,} 耗时={sc.stats.elapsed_ms:.2f} ms")

print()
print("=" * 70)
print("D. update = 删除 + 追加：行序会变")
print("=" * 70)
db = mldb.connect("memory://")
t = db.create_table("u", [{"id": i, "tag": chr(ord("a") + i)} for i in range(6)])
print("更新前:", t.to_list())
print("update(where='id = 2', values={'tag':'Z'}) ->", t.update(where="id = 2", values={"tag": "Z"}))
print("更新后:", t.to_list())

print()
print("--- 分页漏行复现（limit + offset）---")
t2 = db.create_table("p", [{"id": i, "tag": str(i)} for i in range(10)])
page1 = [x["id"] for x in t2.to_list()[:3]]
print("第 1 页 (offset 0):", page1)
# 翻页之间，业务端更新了一行 —— 那一行会从原位置挪到表末尾
t2.update(where="id = 1", values={"tag": "X"})
allrows = [x["id"] for x in t2.to_list()]
page2 = allrows[3:6]
print("更新 id=1 之后全表:", allrows)
print("第 2 页 (offset 3):", page2)
print("两页拼起来:", page1 + page2, " <- id=3 被跳过了，因为更新把 id=1 从头挪到了尾")

print()
print("--- values_sql 引用现有列 ---")
t3 = db.create_table("v", [{"id": i, "n": i * 10} for i in range(5)])
print("前:", t3.to_list())
print(t3.update(where="id >= 3", values_sql={"id": "id + 100", "n": "n * 2"}))
print("后:", t3.to_list())

print()
print("=" * 70)
print("E. merge_insert")
print("=" * 70)
db = mldb.connect("memory://")
t = db.create_table("m", [{"id": i, "v": [float(i), 0.0]} for i in range(5)])
res = (t.merge_insert("id").when_matched_update_all().when_not_matched_insert_all()
       .execute([{"id": 2, "v": [2.5, 0.0]}, {"id": 99, "v": [9.9, 0.0]}]))
print("upsert:", res)
print("结果:", t.to_list())

print()
print("--- 源里有重复键（真实 LanceDB 默认 source_dedupe_behavior = Fail）---")
t2 = db.create_table("m2", [{"id": 1, "v": 1.0}])
try:
    t2.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
        [{"id": 1, "v": 10.0}, {"id": 1, "v": 20.0}])
    print("  结果:", t2.to_list())
except Exception as e:
    print("  报错:", type(e).__name__, e)

print()
print("--- 目标表里有重复键：取最后一行 ---")
t5 = db.create_table("m6", [{"id": 1, "v": 1.0}, {"id": 1, "v": 2.0}])
print("  初始:", t5.to_list())
print("  源 [{'id':1,'v':9.0}]:", t5.merge_insert("id").when_matched_update_all().execute([{"id": 1, "v": 9.0}]))
print("  结果:", t5.to_list())

print()
print("--- when_matched_delete(where=...) ---")
t3 = db.create_table("m3", [{"id": i, "color": "red" if i % 2 else "blue"} for i in range(6)])
print("初始:", t3.to_list())
print("when_matched_delete(color='blue'):",
      t3.merge_insert("id").when_matched_delete(where="color = 'blue'")
        .execute([{"id": i, "color": "x"} for i in range(6)]))
print("结果:", t3.to_list())

print()
print("--- 只 when_not_matched_insert_all（insert-if-not-exists）---")
t4 = db.create_table("m4", [{"id": i} for i in range(3)])
print(t4.merge_insert("id").when_not_matched_insert_all().execute([{"id": 1}, {"id": 2}, {"id": 7}]),
      "->", t4.to_list())

print()
print("--- 一次提交：merge 只产生一个版本 ---")
t6 = db.create_table("m5", [{"id": 0, "x": 0}])
v0 = t6.version
r = t6.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
    [{"id": 0, "x": 1}, {"id": 1, "x": 1}])
print(f"提交前 v{v0} -> 提交后 v{t6.version}（一次 merge 只 +1，num_attempts={r.num_attempts}）")
print("事务日志:", [(v.version, v.operation) for v in t6.list_versions()])

print()
print("=" * 70)
print("F. 删除之后 compact")
print("=" * 70)
db = mldb.connect("memory://")
t6 = db.create_table("c", [{"id": i, "n": i} for i in range(5000)])
t6.add([{"id": 10000 + i, "n": i} for i in range(5000)])
t6.delete("id % 2 = 0")
s1 = t6.stats()
print(f"删除后: 片段={s1.num_fragments} 逻辑行={s1.num_rows} 物理行={s1.physical_rows} "
      f"字节={s1.total_bytes:,} 删除比={s1.deleted_rows_ratio:.3f}")
print(t6.compact_files().report())
s2 = t6.stats()
print(f"压实后: 片段={s2.num_fragments} 逻辑行={s2.num_rows} 物理行={s2.physical_rows} "
      f"字节={s2.total_bytes:,} 删除比={s2.deleted_rows_ratio:.3f}")

print()
print("=" * 70)
print("G. 幽灵行：向量索引里的行号在删除后失效（实测 bug）")
print("=" * 70)
rng = np.random.default_rng(3)
N, D = 500, 8
vecs = rng.standard_normal((N, D)).astype(np.float32)
db = mldb.connect("memory://")
t = db.create_table("r", [{"id": i, "vec": vecs[i].tolist()} for i in range(N)])
t.create_index("vec", config={"index_type": "IVF_FLAT", "num_partitions": 8})
print("建索引后检索:", [x["id"] for x in t.search(vecs[0].tolist()).limit(3).to_list()])
t.delete("id < 400")
print("删掉前 400 行后，存活", t.count_rows(), "行")
try:
    print("纯向量检索:", [x["id"] for x in t.search(vecs[10].tolist()).limit(3).to_list()])
except Exception as e:
    print("纯向量检索 ->", type(e).__name__, e)
try:
    print("带 where（默认 prefilter）:", [x["id"] for x in t.search(vecs[10].tolist()).limit(3).where("id < 1000").to_list()])
except Exception as e:
    print("带 where（默认 prefilter）->", type(e).__name__, e)
print("索引记录的索引行数:", t.list_indices()[0].num_indexed_rows, " 当前存活:", t.count_rows())
print("（delete 之后索引没有被标记失效；只有 compact 会）")
i0 = t.list_indices()[0]
t.compact_files()
print("压实后索引 valid =", t.list_indices()[0].valid,
      " details.invalidated_by_compaction =", t.list_indices()[0].details.get("invalidated_by_compaction"))
