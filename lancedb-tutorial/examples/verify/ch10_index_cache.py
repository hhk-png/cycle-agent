#!/usr/bin/env python3
"""第 10 章 §10.7 的补充实测：索引缓存到底存不存在。

跑法：

    python -X utf8 examples/verify/ch10_index_cache.py

**这个脚本是补写的，起因是发现那一节的结论不准确。**
原来 §10.7 写的是"每次查询都要把索引完整加载一次"，并据此把
"索引要能被缓存"列成真实系统有、本实现没有的三件事之一。
但 `Dataset.load_index` 的签名是 `load_index(name, *, use_cache=True)`，
默认就带一个**进程内、按 (uri, 索引名, 版本, 文件清单) 做 key** 的缓存
（`mini_lancedb/dataset.py`）。

所以那张"索引反而更慢"的表量的是**冷缓存**的第一次查询。
本脚本把冷热两次并排打出来，并额外证明缓存**真的会被用上**
（而不是"代码里写了缓存但查询路径没调"）：热查询的读取量应该
恰好比冷查询少一个索引文件的大小，清空缓存后又应该原样涨回去。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import mini_lancedb as ml  # noqa: E402
from mini_lancedb.dataset import clear_index_cache  # noqa: E402

N = 20000
PRED = "n = 12345"


def main() -> int:
    db = ml.connect("memory://")
    t = db.create_table(
        "t",
        [{"n": i, "txt": f"item-{i:06d}", "pad": "x" * (i % 40)} for i in range(N)],
    )
    # 连接与表各自持有一份 store 引用；把计量计数器指回根 store，
    # 否则表这一侧的读取不会被记上。
    t._ds.store.stats = db.store.stats

    def scan(label: str) -> int:
        db.reset_io()
        sc = t._dataset.scanner(columns=["n"], filter=PRED)
        sc.to_batch()
        read = sc.stats.bytes_read
        print(f"{label:<26} 读={read:>12,}  扫描行={sc.stats.rows_scanned:>6,}")
        return read

    scan("无索引")
    t.create_index("n", config="BTREE")

    # 索引元数据自报的大小，和真正落盘的字节数不是一回事 —— 两个都量一下。
    reported = t.list_indices()[0].size_bytes
    on_disk = 0
    for obj in db.store.list(""):  # 表自己的对象在 <表名>.lance/ 下面
        if "/_indices/" not in obj.path:
            continue
        size = db.store.size(obj.path)
        on_disk += size
        print(f"  {obj.path.split('/')[-1]:<20} {size:>10,}")
    print(f"\n  元数据自报 size_bytes = {reported:,}")
    print(f"  落盘文件合计         = {on_disk:,}")
    print(f"  两者相等             = {reported == on_disk}\n")

    cold = scan("有索引 · 第 1 次（冷）")
    warm1 = scan("有索引 · 第 2 次（热）")
    warm2 = scan("有索引 · 第 3 次（热）")

    evicted = clear_index_cache()
    after = scan("清空缓存之后")

    print()
    print(f"冷 − 热            = {cold - warm1:,} 字节")
    print(f"索引落盘合计        = {on_disk:,} 字节")
    print(f"两者相等           = {cold - warm1 == on_disk}   ← 缓存省的正好是索引本身")
    print(f"热查询两次是否一致   = {warm1 == warm2}")
    print(f"清缓存后是否回到冷值 = {after == cold}   （淘汰了 {evicted} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
