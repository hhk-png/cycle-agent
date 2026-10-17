"""例 6：版本、时间旅行与维护 —— 对应第 7、8、9、23 章。

这个脚本演示三条容易反直觉的结论：

1. **每一次写操作都是一个新版本**，包括删除和建索引；
2. **`overwrite` 不删任何数据**，老版本照样能读，空间靠 GC 回收；
3. **压实不一定让查询变快** —— 它会合并统计信息，
   而统计信息本身就是一种索引。

最后一条是这份教程里最值得记住的实测结果之一。

跑法：  python -X utf8 examples/06_versioning_and_maintenance.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mini_lancedb as ldb  # noqa: E402
from mini_lancedb.dataset import Dataset, WriteParams  # noqa: E402
from mini_lancedb.record import RecordBatch  # noqa: E402
from mini_lancedb.storage.base import InMemoryStore, InstrumentedStore  # noqa: E402

DIM = 16


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="mini_lancedb_versions_"))
    try:
        versions_and_time_travel(workdir)
        delete_is_metadata_only()
        compaction_experiment()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def versions_and_time_travel(workdir: Path) -> None:
    section("一、每一次写操作都是一个新版本")
    db = ldb.connect(str(workdir / "db"))
    rng = np.random.default_rng(0)
    rows = [
        {"id": i, "v": rng.standard_normal(DIM).astype("float32").tolist()}
        for i in range(100)
    ]
    table = db.create_table("t", data=rows)
    print(f"  create_table -> v{table.version}  rows={table.count_rows()}")

    table.tags.create("baseline")
    print(f"  tags.create('baseline') 之后仍是 v{table.version}（打标签不改数据）")

    table.add([{"id": 100, "v": [0.0] * DIM}])
    print(f"  add          -> v{table.version}  rows={table.count_rows()}")

    table.delete("id < 50")
    print(f"  delete       -> v{table.version}  rows={table.count_rows()}")

    table.create_index("v", config="IVF_FLAT", name="vi", num_partitions=4)
    print(f"  create_index -> v{table.version}")

    section("二、版本历史")
    for info in table.list_versions():
        print("  ", info)

    section("三、时间旅行是零成本的")
    print(f"  回滚前：v{table.version}，{table.count_rows()} 行")
    table.checkout("baseline")
    print(f"  checkout('baseline') -> v{table.version}，{table.count_rows()} 行")
    table.restore()
    print(f"  restore()            -> v{table.version}，{table.count_rows()} 行")
    print()
    print("  注意 restore 是把旧内容**作为一个新版本**提交回来，不是回退指针：")
    print("  v3/v4/v5 全都还在，所以「回滚」这个动作本身也可以被回滚。")
    print("  这也是为什么 checkout 是就地切换、而 restore 会推高版本号。")


def delete_is_metadata_only() -> None:
    section("四、删除的代价只正比于「删了多少行」")
    store = InstrumentedStore(InMemoryStore())
    n = 200_000
    batch = RecordBatch.from_pylist([{"id": i, "n": i} for i in range(n)])
    ds = Dataset.create(store, "mem://del", batch.schema)
    ds.write([batch], params=WriteParams(target_values_per_page=16384))

    table_bytes = ds.stats()["data_bytes"]
    print(f"  参照物：这个表的数据文件总共 {table_bytes:,} 字节")
    print()
    before = store.stats.bytes_written
    for label, count in (("删 3 行", 3), ("删一半", n // 2)):
        victim = Dataset.open(store, "mem://del")
        victim.delete(f"id < {count}")
        delta = store.stats.bytes_written - before
        before = store.stats.bytes_written
        share = delta / table_bytes
        print(
            f"  {label:<10} 整次提交写入 {delta:>9,} 字节"
            f"  = 数据文件的 {share:>6.2%}"
        )
        # 复位，为下一轮准备
        Dataset.open(store, "mem://del").restore(2)
    print()
    print("  两次删除都**没有重写数据文件**。第一次的 1.4 KB 里，")
    print("  删除文件本身只有几十字节，剩下的是被整体重写的新 manifest")
    print("  （每个版本都是一份全量快照，这是它的固有成本）。")
    print("  第二次的 100 KB 里，绝大部分是删除文件本身 ——")
    print("  10 万个行号做差分 + varint 之后，差不多就是每行 1 字节。")


def compaction_experiment() -> None:
    section("五、反直觉的实测：压实可能让查询变慢")
    store = InstrumentedStore(InMemoryStore())
    rng = np.random.default_rng(3)
    n = 50_000
    rows = [
        {"id": i, "n": i, "v": rng.standard_normal(DIM).astype("float32").tolist()}
        for i in range(n)
    ]
    batch = RecordBatch.from_pylist(rows)
    ds = Dataset.create(store, "mem://compact", batch.schema)
    # 模拟"每 5 分钟增量写一批"的真实管道
    for start in range(0, n, 500):
        ds.write([batch.slice(start, 500)], params=WriteParams(target_values_per_page=8192))

    print(f"  写入后: 片段 {len(ds.fragments)} 个，{ds.num_rows:,} 行，"
          f"{ds.stats()['data_bytes']:,} 字节")

    def measure(label: str) -> None:
        scanner = ds.scanner(columns=["id"], filter="n > 49900")
        scanner.to_batch()
        s = scanner.stats.summary()
        print(
            f"  {label:<12} 片段 {s['片段总数']:>4}  剪掉 {s['被 zone map 剪掉的片段']:>4}  "
            f"扫描 {s['扫描行数']:>7,} 行  读 {s['读取字节']:>8,} 字节  "
            f"{s['耗时(ms)']:>7.2f} ms"
        )

    measure("压实前")
    result = ds.compact()
    print(f"  compact -> 合并 {result['fragments_merged']} 个片段成 "
          f"{result['fragments_created']} 个，重写 {result['rows_rewritten']:,} 行")
    measure("压实后")
    print()
    print("  压实之后这个查询**读得更多、也更慢**。原因不是合并本身，")
    print("  而是 100 组 min/max 统计被合并成了一组 —— 剪枝粒度变粗了。")
    print()
    print("  正确的表述不是「定期压实」，而是：")
    print("  **片段大小是在元数据开销和剪枝粒度之间的权衡**，")
    print("  太大太小都有代价，目标是让它落在一个区间里。")

    section("六、清理旧版本")
    dry = ds.cleanup_old_versions(older_than=0)
    print(f"  dry run: 可回收 {dry['bytes_reclaimable']:,} 字节，"
          f"涉及 {len(dry['removable_versions'])} 个版本、{dry['removable_files']} 个文件")
    print(f"           保留版本 {dry['kept_versions']}")
    print()
    print("  真实 LanceDB 的 cleanup_old_versions **没有 dry_run 参数**，")
    print("  我们加它纯粹是为了教学方便：先看会删多少，再决定删不删。")
    print("  另外注意：被标签引用的版本不会被删 —— 标签是一把锁，不只是个名字。")


if __name__ == "__main__":
    main()
