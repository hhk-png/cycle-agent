"""例 2：把一个 .lance 文件拆开看 —— 对应第 3 章。

这个脚本不读数据页，只读元数据，然后把文件的物理结构打印出来：

* footer 的 40 个字节里装了什么；
* 每一列被结构化解开成了哪些子列（向量列会展开成 ``vector.item``）；
* 每一页用了哪种编码、多大、在文件的哪个偏移；
* "打开一个文件"到底花了多少次范围读、多少字节。

最后会做一次对比实验：同样的数据，页大小从 8192 个值改成 512 个值，
看打开成本涨了多少 —— 这是"页不能太小"最直观的证据。

跑法：  python -X utf8 examples/02_file_format_tour.py
"""

from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_lancedb.format.lance_file import (  # noqa: E402
    FOOTER_SIZE,
    MAGIC,
    LanceFileReader,
    write_lance_file,
)
from mini_lancedb.record import RecordBatch  # noqa: E402
from mini_lancedb.storage.base import (  # noqa: E402
    InMemoryStore,
    InstrumentedStore,
    LocalFileStore,
)


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def make_batch(n: int, dim: int, seed: int = 0) -> RecordBatch:
    rng = np.random.default_rng(seed)
    rows = [
        {
            "id": i,
            "vector": rng.standard_normal(dim).astype("float32").tolist(),
            "title": f"document number {i}",
            "cat": f"c{i % 13}",
            "score": float(i % 1000) / 7.0,
        }
        for i in range(n)
    ]
    return RecordBatch.from_pylist(rows)


def dump_footer(raw: bytes) -> None:
    section("footer：文件最后 40 个字节")
    print("最后 4 字节（魔数）:", raw[-4:], "==", MAGIC, "->", raw[-4:] == MAGIC)
    fields = struct.unpack("<QQQIIHH", raw[-FOOTER_SIZE:-4])
    names = [
        "column_metadata_start        (u64)",
        "column_metadata_offsets_start(u64)",
        "global_buffer_offsets_start  (u64)",
        "num_global_buffers           (u32)",
        "num_columns                  (u32)",
        "major_version                (u16)",
        "minor_version                (u16)",
    ]
    for name, value in zip(names, fields):
        print(f"  {name} = {value}")
    print(f"  footer 总长 = 3*8 + 4 + 4 + 2 + 2 + 4 = {FOOTER_SIZE}")


def tour(store, path: str, label: str) -> None:
    section(f"{label}")
    reader = LanceFileReader(store, path)
    info = reader.explain()
    print(f"  文件大小        : {info['size_bytes']:>12,} B")
    print(f"  行数            : {info['num_rows']:>12,}")
    print(f"  容器版本        : {info['version']['major']}.{info['version']['minor']}")
    print(
        f"  打开文件的开销  : {info['bytes_read_to_open']:>12,} B / "
        f"{info['range_reads_to_open']} 次范围读"
    )
    print()
    print("  列结构（注意向量列被摊平成了 xxx.item 子列）:")
    print(f"    {'idx':>3} {'名称':<18} {'类型':<30} {'行数':>9} {'页':>5} {'编码':<20}")
    for col in info["columns"]:
        enc = col["pages"][0]["encoding"] if col["pages"] else "-"
        print(
            f"    {col['index']:>3} {col['name']:<18} {col['type']:<30} "
            f"{col['rows']:>9} {len(col['pages']):>5} {enc:<20}"
        )
    print()
    for col in info["columns"]:
        if not col["pages"]:
            continue
        page = col["pages"][0]
        print(
            f"  {col['name']} 的第 0 页: encoding={page['encoding']} "
            f"layout={page['layout']} num_values={page['num_values']} "
            f"bytes={page['bytes']} priority={page['priority']}"
        )
        print(f"      buffer_offsets={page['buffer_offsets']}")
        print(f"      buffer_sizes  ={page['buffer_sizes']}")
        if col["zone_map"]:
            print(f"      zone_map      ={col['zone_map']}")


def read_cost_experiment() -> None:
    section("对比实验：页大小如何影响打开成本")
    print("  同样 5 万行，只改 target_values_per_page")
    print()
    print(f"  {'页大小(值)':>12} {'文件字节':>14} {'打开字节':>14} {'范围读':>8} {'占文件比':>10}")
    for values_per_page in (8192, 4096, 1024, 512):
        store = InstrumentedStore(InMemoryStore())
        batch = make_batch(50_000, 32, seed=7)
        write_lance_file(
            store, "t.lance", [batch], target_values_per_page=values_per_page
        )
        reader = LanceFileReader(store, "t.lance")
        info = reader.explain()
        ratio = info["bytes_read_to_open"] / max(1, info["size_bytes"])
        print(
            f"  {values_per_page:>12} {info['size_bytes']:>14,} "
            f"{info['bytes_read_to_open']:>14,} {info['range_reads_to_open']:>8} "
            f"{ratio:>9.2%}"
        )
    print()
    print("  结论：页越小，元数据条数越多。元数据每次都会被读，")
    print("        所以「页太小」的代价不是读数据，而是读元数据。")


def projection_experiment() -> None:
    section("对比实验：投影下推与行范围下推")
    store = InstrumentedStore(InMemoryStore())
    batch = make_batch(20_000, 64, seed=11)
    write_lance_file(store, "t.lance", [batch], target_values_per_page=4096)
    print(f"  {'读法':<28} {'读取字节':>12} {'范围读':>8} {'读页':>6} {'跳过页':>8}")
    for label, kwargs in (
        ("全列（整表）", {}),
        ("只读 id", {"columns": ["id"]}),
        ("只读 vector", {"columns": ["vector"]}),
        ("id 的前 100 行", {"columns": ["id"], "row_range": (0, 100)}),
        ("id 的 15000~15099 行", {"columns": ["id"], "row_range": (15000, 15100)}),
    ):
        reader = LanceFileReader(store, "t.lance")
        reader.read(**kwargs)
        s = reader.stats.summary()
        print(
            f"  {label:<28} {s['bytes_read']:>12,} {s['range_reads']:>8} "
            f"{s['pages_read']:>6} {s['pages_skipped']:>8}"
        )
    print()
    print("  vector 列占文件 96% 以上的空间 —— 选对投影列就是几十倍的差距。")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mini_lancedb_fmt_") as tmp:
        store = InstrumentedStore(LocalFileStore(tmp))
        batch = make_batch(20_000, 64, seed=0)
        write_lance_file(store, "t.lance", [batch], target_values_per_page=4096)
        raw = store.get("t.lance")
        print(f"写出了一个 {len(raw):,} 字节的 .lance 文件，共 {batch.num_rows:,} 行")
        dump_footer(raw)
        tour(store, "t.lance", "物理结构")
        read_cost_experiment()
        projection_experiment()


if __name__ == "__main__":
    main()
