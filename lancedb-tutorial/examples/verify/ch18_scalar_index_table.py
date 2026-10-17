# -*- coding: utf-8 -*-
"""统一数据集上的合并测量表。N=100,000，同一份数据、同一台机器。"""
import time, random, numpy as np

import sys as _sys
from pathlib import Path as _Path

# 这个脚本原本在仓库根目录下直接跑；挪进 examples/verify/ 之后
# 需要自己把包的搜索路径补上。
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from mini_lancedb.arrays import array_from_pylist
from mini_lancedb.index.base import IndexMetadata, DictIndexStore, RangeQuery, IsIn, And
from mini_lancedb.index.btree import BTreeIndex
from mini_lancedb.index.bitmap import BitmapIndex
from mini_lancedb.index.label_list import LabelListIndex, ArrayContains
from mini_lancedb.index.ngram import NGramIndex, Contains
from mini_lancedb.index.zonemap import ZoneMapIndex
from mini_lancedb.index.inverted import InvertedIndex
from mini_lancedb.types import INT64, UTF8, List as ListType

N=100_000
rng=np.random.default_rng(42); random.seed(42)
ids=np.arange(N)
cat=np.array(["active","pending","closed","archived"],dtype=object)[rng.integers(0,4,N)]
tags=[[f"t{random.randint(0,49)}" for _ in range(random.randint(0,3))] for _ in range(N)]
text=[f"item-{i:06d}-abcdef" for i in range(N)]
def meta(t,c,**p): return IndexMetadata(name=t.lower()+"_idx",index_type=t,columns=list(c),params=dict(p))
def A(v,ty=INT64): return array_from_pylist(ty,list(v))

jobs=[
 ("BTREE","id",     lambda: BTreeIndex.build(A(ids), ids, metadata=meta("BTREE",["id"])),
                    lambda x: x.search(RangeQuery(50_000,50_100)), "range 100 值"),
 ("BITMAP","cat",   lambda: BitmapIndex.build(A(cat,UTF8), ids, metadata=meta("BITMAP",["cat"])),
                    lambda x: x.search(IsIn(["pending"])), "IN 1 值"),
 ("LABEL_LIST","tags", lambda: LabelListIndex.build(A(tags,ListType(UTF8)), ids, metadata=meta("LABEL_LIST",["tags"])),
                    lambda x: x.search(ArrayContains("t7")), "array_contains"),
 ("NGRAM","text",   lambda: NGramIndex.build(A(text,UTF8), ids, metadata=meta("NGRAM",["text"])),
                    lambda x: x.search(Contains("item-000042-")), "contains 12 字符"),
 ("FTS","text",     lambda: InvertedIndex.build(A(text,UTF8), ids, metadata=meta("FTS",["text"])),
                    lambda x: x.search_ranked("abcdef", k=10), "term k=10"),
 ("ZONEMAP","id",   lambda: ZoneMapIndex.build(A(ids), ids, metadata=meta("ZONEMAP",["id"])),
                    lambda x: x.prune_expr(RangeQuery(0,2000)), "prune [0,2000]"),
]
print(f"N = {N:,}   每一行同一份数据，单线程，取 3 次最快")
print(f"{'index':<11}{'col':<6}{'build ms':>10}{'query ms':>10}{'disk B':>12}{'x原列':>8}  {'query':<16}{'notes'}")
raw={"id":N*8,"cat":N*8,"tags":N*8,"text":N*22}
for name,col,mk,qy,label in jobs:
    t0=time.perf_counter(); ix=mk(); bt=time.perf_counter()-t0
    best=None
    for _ in range(3):
        t=time.perf_counter(); qy(ix); dt=time.perf_counter()-t
        best=dt if best is None else min(best,dt)
    st=DictIndexStore(); ix.save(st); nb=st.total_bytes()
    st_=ix.stats()
    note=f"blocks={st_.get('num_blocks','-')} depth={st_.get('depth','-')} bits/row={st_.get('bits_per_row',float('nan')):.2f}" if 'bits_per_row' in st_ else \
         (f"blocks={st_.get('num_blocks','-')} bits/row={st_.get('bits_per_row','-')}" if 'num_blocks' in st_ else \
          (f"blocks={st_.get('num_blocks','-')}" if 'num_blocks' in st_ else \
           f"terms={st_.get('num_unique_terms', st_.get('num_grams', st_.get('num_labels', st_.get('num_keys','-'))))}"))
    if name=="ZONEMAP":
        keep, ps = ix.prune_expr(RangeQuery(0,2000))
        note=f"blocks={ps.blocks_total} pruned={ps.blocks_pruned} ({ps.prune_ratio:.1%}) rows_skipped={ps.rows_skipped:,}"
    if name=="BTREE": note=f"keys={st_['num_keys']:,} depth={st_['depth']} nodes={st_['num_nodes']:,} card={st_['cardinality']:.3f}"
    if name=="NGRAM": note=f"grams={st_['num_grams']:,} postings={st_['num_postings']:,} max_rows/gram={st_['max_rows_per_gram']:,}"
    if name=="BITMAP": note=f"values={st_['num_values']} bits/row={st_['bits_per_row']:.1f}"
    if name=="LABEL_LIST": note=f"labels={st_['num_labels']} labels/row={st_['avg_labels_per_row']:.2f} bits/row={st_['bits_per_row']:.1f}"
    if name=="FTS": note=f"terms={st_['num_unique_terms']:,} postings={st_['num_postings']:,} tok={st_['tokenizer']}"
    print(f"{name:<11}{col:<6}{bt*1000:>10.1f}{best*1000:>10.3f}{nb:>12,}{nb/raw[col]:>8.2f}  {label:<16}{note}")

print("\nZONEMAP 随排布变化的剪枝（同一份 id 列，两种排布）")
for lbl,vals in [("已排序", np.sort(rng.integers(0,N,N))), ("随机", rng.integers(0,N,N))]:
    z=ZoneMapIndex.build(A(vals), ids, metadata=meta("ZONEMAP",["id"]))
    _,ps=z.prune_expr(RangeQuery(0,2000))
    print(f"  {lbl:<5} blocks={ps.blocks_total} pruned={ps.blocks_pruned} ({ps.prune_ratio:5.1%}) rows_skipped={ps.rows_skipped:,} bytes_skipped={ps.bytes_skipped_estimate:,}")
    print(f"        as_dict={ps.as_dict()}")
