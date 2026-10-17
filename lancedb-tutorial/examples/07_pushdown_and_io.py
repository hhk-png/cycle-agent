"""例 7：四层下推与 I/O 记账 —— 对应第 10、19、24 章。

"下推"这个词听起来抽象，但它只回答一个问题：
**这个算子能不能让下游少读一点字节？**

这个脚本把四层下推各自的收益量出来，并且演示一个实用的方法论：
**先建立基线，一次只改一个旋钮，改完再量。**

四层是：
1. 列投影下推（只读需要的列）
2. 片段级统计剪枝（min/max 直接跳过整段）
3. 索引下推（把"扫描"变成"取行"）
4. 延迟物化（先算掩码，再只取存活行的投影列）

跑法：  python -X utf8 examples/07_pushdown_and_io.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_lancedb.dataset import Dataset, WriteParams  # noqa: E402
from mini_lancedb.record import RecordBatch  # noqa: E402
from mini_lancedb.scanner import Scanner  # noqa: E402
from mini_lancedb.storage.base import InMemoryStore, InstrumentedStore  # noqa: E402

N = 50_000
DIM = 64
FRAGMENTS = 25


def section(title: str) -> None:
    print()
    print("=" * 84)
    print(title)
    print("=" * 84)


def build() -> tuple[Dataset, InstrumentedStore]:
    store = InstrumentedStore(InMemoryStore())
    rng = np.random.default_rng(4)
    vectors = rng.standard_normal((N, DIM)).astype("float32")
    rows = [
        {
            "id": i,
            "n": i,
            "cat": f"c{i % 40}",
            "payload": f"record {i:06d} " + "x" * 40,
            "vector": vectors[i].tolist(),
        }
        for i in range(N)
    ]
    batch = RecordBatch.from_pylist(rows)
    ds = Dataset.create(store, "mem://io", batch.schema)
    per = N // FRAGMENTS
    for start in range(0, N, per):
        ds.write([batch.slice(start, per)], params=WriteParams(target_values_per_page=8192))
    return ds, store


def measure(
    store: InstrumentedStore, ds: Dataset, label: str, columns, filter_sql=None, **kw
):
    before = store.stats.snapshot()
    scanner = Scanner(ds, columns=columns, filter=filter_sql, **kw)
    batch = scanner.to_batch()
    after = store.stats.snapshot()
    s = scanner.stats
    print(
        f"  {label:<30}{batch.num_rows:>8,} 行"
        f"{after['bytes_read'] - before['bytes_read']:>11,} B"
        f"{after['gets'] - before['gets']:>7}"
        f"{s.fragments_scanned:>5}/{s.fragments_total:<4}"
        f"{s.pages_skipped:>6}"
    )
    return batch.num_rows, after["bytes_read"] - before["bytes_read"]


def main() -> None:
    ds, store = build()
    table_bytes = ds.stats()["data_bytes"]
    section(f"数据集：{N:,} 行 × {DIM} 维，切成 {len(ds.fragments)} 个片段，"
            f"数据文件共 {table_bytes:,} 字节")
    print("  每行都是：id / n / cat / payload（约 50 字节文本）/ vector（64 维）")
    print("  vector 列占了文件 95% 以上的空间 —— 这条事实决定了下面的每一个数字。")

    section("第一层：列投影下推")
    print(f"  {'查询':<30}{'行数':>8}{'读取字节':>12}{'请求':>7}{'片段':>9}{'跳页':>6}")
    print("  " + "-" * 78)
    measure(store, ds, "全列（SELECT *）", None)
    measure(store, ds, "只读 id", ["id"])
    measure(store, ds, "id + n", ["id", "n"])
    measure(store, ds, "只读 vector", ["vector"])
    print()
    print("  「只读 id」和「只读 vector」的差距就是列存最直接的收益：")
    print("  同一个表、同一批行，一个读几十 KB，一个读几十 MB。")

    section("第二层：片段级统计剪枝（零数据 I/O）")
    print(f"  {'查询':<30}{'行数':>8}{'读取字节':>12}{'请求':>7}{'片段':>9}{'跳页':>6}")
    print("  " + "-" * 78)
    measure(store, ds, "无谓词（全扫）", ["id"])
    measure(store, ds, "WHERE n > 49000", ["id"], "n > 49000")
    measure(store, ds, "同上，但关掉统计剪枝", ["id"], "n > 49000", use_zone_maps=False)
    print()
    scanner = Scanner(ds, columns=["id"], filter="n > 49000")
    scanner.to_batch()
    print(f"  被统计剪掉的片段：{scanner.stats.fragments_pruned_by_stats} / "
          f"{scanner.stats.fragments_total}")
    print()
    print("  这一步**没有读任何一个数据页**：列的 min/max 是抄在 manifest 里的，")
    print("  所以「这个片段有没有可能命中」可以在打开任何文件之前回答。")

    section("第三层：索引下推")
    ds.create_index("n", index_type="BTREE", name="n_idx")
    idx_meta = next(i for i in ds.list_indices() if i.name == "n_idx")
    print(f"  在 n 上建了一棵 B 树：{idx_meta.size_bytes:,} 字节 "
          f"（这一列本身只有 {N * 8:,} 字节）")

    # 冷启动：第一次查询要把索引文件读进内存
    from mini_lancedb.dataset import clear_index_cache

    clear_index_cache()
    before = store.stats.snapshot()
    Scanner(ds, columns=["id"], filter="n BETWEEN 100 AND 200").to_batch()
    cold = store.stats.snapshot()["bytes_read"] - before["bytes_read"]
    print(f"  第一次查询（冷启动，含加载索引）读 {cold:,} 字节")
    print()

    print(f"  {'查询':<34}{'行数':>8}{'读取字节':>12}{'请求':>7}{'片段':>9}")
    print("  " + "-" * 74)
    measure(store, ds, "WHERE cat = 'c7'（无索引可用）", ["id"], "cat = 'c7'")
    measure(store, ds, "WHERE n BETWEEN 100 AND 200（走索引）", ["id"], "n BETWEEN 100 AND 200")
    print()
    print("  第二行才是索引的真实收益：**1/25 个片段**，读几十 KB。")
    print("  注意它是靠两件事做到的 —— 索引给出精确的行号集合，")
    print("  然后扫描器用这个集合**跳过整个片段**。只给行号不跳片段的话，")
    print("  25 个片段还是要全打开，读取量会比统计剪枝还差。")
    print()
    print("  第一行的 1.5 MB 里几乎全是**冷启动读索引文件**的成本。")
    print("  这也是为什么索引必须缓存：不缓存的话，")
    print("  「建了索引反而更慢」就不是段子，是我们自己量出来的。")
    print(f"  索引大小 {idx_meta.size_bytes:,} 字节 vs 它索引的那一列 {N * 8:,} 字节 ——")
    print("  在小表上建索引是一笔**净亏**的买卖：索引比数据还大。")
    print("  建索引之前先问：这列的数据有多大？这个查询有多频繁？")

    section("第四层：延迟物化")
    print("  用 cat = 'c7'（没有索引、也剪不掉片段）对比开启/关闭：")
    for flag in (True, False):
        scanner = Scanner(
            ds, columns=["id", "payload"], filter="cat = 'c7'",
            late_materialization=flag,
        )
        batch = scanner.to_batch()
        saved = scanner.stats.late_materialization_saved_rows
        label = "开启" if flag else "关闭"
        print(
            f"    {label}  命中 {batch.num_rows:>6,} 行  "
            f"读 {scanner.stats.bytes_read:>10,} B，"
            f"扫描 {scanner.stats.rows_scanned:>7,} 行，"
            f"延迟物化省下 {saved:>8,} 行"
        )
    print()
    print("  延迟物化的意思是：先只读过滤列算出掩码，")
    print("  **再只把活下来的行的投影列读回来**。")
    print("  过滤命中 1% 时，投影列的读取量就只有 1%。")

    section("把它们叠起来")
    print("  三个查询，从最贵到最便宜：")
    for label, cols, flt, kw in (
        ("全列 + 无谓词", None, None, {}),
        ("全列 + 有谓词", None, "n > 49000", {}),
        ("两列 + 有谓词 + 延迟物化", ["id", "payload"], "n > 49000", {}),
    ):
        measure(store, ds, label, cols, flt, **kw)
    print()
    print("  最后一行和第一行的差距，就是「列存 + 下推」的全部价值。")
    print("  没有一层是新算法 —— 每一层都只是在回答同一个问题：")
    print("  **我能不能让下游少读一点字节？**")

    section("方法论")
    print("  调优的顺序：")
    print("    1. 建立基线（延迟、读取字节、范围读次数、召回率）")
    print("    2. 判断瓶颈在哪一层：读太多字节？算太多次距离？还是写太慢？")
    print("    3. **一次只改一个旋钮** —— 否则得不出因果结论")
    print("    4. 验收要看召回率，不能只看延迟")
    print("    5. 把「在什么数据上测出什么结论」写下来，换数据就得重量")


if __name__ == "__main__":
    main()
