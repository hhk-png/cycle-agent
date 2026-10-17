"""例 4：向量索引的召回率 / 延迟 / 体积实测 —— 对应第 12、13、14、15、16、24 章。

这个脚本会：

1. 生成一批有簇结构的合成向量（比纯随机数据更接近真实 embedding）；
2. 用暴力检索算出 ground truth 和延迟基线；
3. 逐种索引、逐档参数跑一遍，报出 recall@10、延迟、索引体积。

三个结论值得记住：

* **小数据上暴力检索赢。** 2 万行 64 维的暴力检索只要零点几毫秒。
* **IVF_PQ 的召回是一条平台**，加 nprobes 没用，要加大 refine_factor。
* **建索引的代价差两个数量级**，HNSW 比 IVF_FLAT 慢 25 倍。

跑法：  python -X utf8 examples/04_index_benchmark.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_lancedb.dataset import Dataset, WriteParams  # noqa: E402
from mini_lancedb.index.metric import pairwise_distances, top_k  # noqa: E402
from mini_lancedb.record import RecordBatch  # noqa: E402
from mini_lancedb.storage.base import InMemoryStore, InstrumentedStore  # noqa: E402

N = 20_000
DIM = 64
N_QUERIES = 20
K = 10
CLUSTERS = 50


def section(title: str) -> None:
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def build_dataset(seed: int = 7):
    rng = np.random.default_rng(seed)
    centers = rng.standard_normal((CLUSTERS, DIM)).astype(np.float32)
    assign = rng.integers(0, CLUSTERS, N)
    # 簇内噪声小一点，让数据真的"有结构"——纯随机数据上 ANN 表现会难看得多
    vectors = (centers[assign] + 0.35 * rng.standard_normal((N, DIM))).astype(np.float32)
    rows = [
        {"id": i, "vector": vectors[i].tolist(), "n": i} for i in range(N)
    ]
    store = InstrumentedStore(InMemoryStore())
    ds = Dataset.create(store, "mem://bench", RecordBatch.from_pylist(rows).schema)
    ds.write([RecordBatch.from_pylist(rows)], params=WriteParams(target_values_per_page=8192))
    queries = (vectors[:N_QUERIES] + 0.05 * rng.standard_normal((N_QUERIES, DIM))).astype(
        np.float32
    )
    return ds, vectors, queries


def ground_truth(vectors: np.ndarray, queries: np.ndarray):
    results = []
    started = time.perf_counter()
    for i in range(len(queries)):
        d = pairwise_distances(vectors, queries[i : i + 1], "l2")[0]
        results.append(set(int(x) for x in top_k(d, K)[1]))
    elapsed = (time.perf_counter() - started) / len(queries) * 1000
    return results, elapsed


def measure(index, queries, truth, param_name: str, values) -> list:
    rows = []
    for value in values:
        started = time.perf_counter()
        recall = 0.0
        for i in range(len(queries)):
            res = index.search(
                queries[i : i + 1], K, params={param_name: value, "refine_factor": 1}
            )
            recall += len(set(int(x) for x in res.row_ids[0]) & truth[i]) / K
        elapsed = (time.perf_counter() - started) / len(queries) * 1000
        rows.append((value, recall / len(queries), elapsed))
    return rows


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="mini_lancedb_bench_"))
    try:
        run()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run() -> None:
    section(f"数据集：{N:,} 行 × {DIM} 维，{CLUSTERS} 个簇，{N_QUERIES} 条查询，recall@{K}")
    ds, vectors, queries = build_dataset()
    truth, brute_ms = ground_truth(vectors, queries)
    print(f"  暴力检索基线：{brute_ms:.3f} ms/查询")
    print("  （这就是所有索引要打败的对手。小数据上它很难被超过。）")

    section("各索引类型的默认档位")
    plans = [
        ("IVF_FLAT", {"num_partitions": 64}, "nprobes", [1, 4, 16, 64]),
        ("IVF_SQ", {"num_partitions": 64}, "nprobes", [1, 4, 16, 64]),
        (
            "IVF_PQ",
            {"num_partitions": 64, "num_sub_vectors": 8, "num_bits": 8},
            "nprobes",
            [1, 4, 16, 64],
        ),
        ("HNSW", {"m": 16, "ef_construction": 100}, "ef", [10, 50, 200]),
        (
            "IVF_HNSW_SQ",
            {"num_partitions": 64, "m": 16, "ef_construction": 100},
            "nprobes",
            [1, 4, 16],
        ),
        ("BQ", {}, "refine_factor", [1, 10, 40]),
    ]

    for index_type, params, param_name, values in plans:
        name = index_type.lower()
        try:
            started = time.perf_counter()
            meta = ds.create_index(
                "vector", index_type=index_type, name=name, metric="l2", **params
            )
            build_s = time.perf_counter() - started
            index = ds.load_index(name)
        except Exception as exc:
            print(f"  {index_type:<14} 跳过: {type(exc).__name__}: {str(exc)[:70]}")
            continue

        # BQ 不支持 refine_factor=1 时的精确重排（它的距离就是汉明距离），
        # 所以单独处理：用它的默认搜索。
        if index_type == "BQ":
            started = time.perf_counter()
            recall = 0.0
            for i in range(len(queries)):
                res = index.search(queries[i : i + 1], K, params={"refine_factor": 0})
                recall += len(set(int(x) for x in res.row_ids[0]) & truth[i]) / K
            ms = (time.perf_counter() - started) / len(queries) * 1000
            print(
                f"  {index_type:<14} 建索引 {build_s:6.2f}s  体积 {meta.size_bytes / 1e6:5.2f} MB  "
                f"纯汉明 recall={recall / len(queries):.2f} 延迟 {ms:.3f} ms"
            )
            rows = measure(index, queries, truth, "refine_factor", [4, 10, 40])
            for value, r, t in rows:
                print(f"       {param_name}={value:<4} recall={r:.2f}  {t:.3f} ms")
            continue

        print(
            f"  {index_type:<14} 建索引 {build_s:6.2f}s  体积 {meta.size_bytes / 1e6:5.2f} MB"
        )
        for value, recall, ms in measure(index, queries, truth, param_name, values):
            print(f"       {param_name}={value:<4} recall={recall:.2f}  {ms:.3f} ms")

    section("refine_factor 与 nprobes 的分工（IVF_PQ）")
    index = ds.load_index("ivf_pq")
    print(f"  {'nprobes':>8}{'refine=1':>14}{'refine=5':>14}{'refine=20':>14}")
    for nprobes in (1, 8, 64):
        cells = []
        for rf in (1, 5, 20):
            recall = 0.0
            for i in range(len(queries)):
                res = index.search(
                    queries[i : i + 1], K, params={"nprobes": nprobes, "refine_factor": rf}
                )
                recall += len(set(int(x) for x in res.row_ids[0]) & truth[i]) / K
            cells.append(recall / len(queries))
        print(f"  {nprobes:>8}{cells[0]:>14.2f}{cells[1]:>14.2f}{cells[2]:>14.2f}")
    print()
    print("  读法：横向是 refine_factor，纵向是 nprobes。")
    print("  纵向几乎不动、横向涨得很快 —— 说明瓶颈是量化误差，不是候选集大小。")

    section("怎么把召回率买回来")
    print("  三条路，代价完全不同：")
    print()
    print(f"  {'方案':<34}{'recall@10':>12}{'延迟':>12}")
    print("  " + "-" * 58)

    from mini_lancedb.index.flat import FlatIndex
    from mini_lancedb.index.base import IndexMetadata

    # 方案一：完全绕过索引，做精确暴力检索
    rows_all = ds.take_rows(range(min(N, 20000)), columns=["vector"])
    matrix = np.asarray(rows_all.to_numpy("vector"), dtype=np.float32)
    flat = FlatIndex.build(
        matrix,
        np.arange(len(matrix), dtype=np.uint64),
        metadata=IndexMetadata(name="flat", index_type="FLAT", columns=["vector"]),
    )
    started = time.perf_counter()
    recall = 0.0
    for i in range(len(queries)):
        res = flat.search(queries[i : i + 1], K)
        recall += len(set(int(x) for x in res.row_ids[0]) & truth[i]) / K
    ms = (time.perf_counter() - started) / len(queries) * 1000
    print(f"  {'FLAT（精确暴力检索）':<34}{recall / len(queries):>12.2f}{ms:>11.3f} ms")

    # 方案二：全探分区 + 大 refine_factor（还是走索引，只是把近似补回来）
    for nprobes, rf in ((64, 20), (64, 50)):
        started = time.perf_counter()
        recall = 0.0
        for i in range(len(queries)):
            res = index.search(
                queries[i : i + 1], K, params={"nprobes": nprobes, "refine_factor": rf}
            )
            recall += len(set(int(x) for x in res.row_ids[0]) & truth[i]) / K
        ms = (time.perf_counter() - started) / len(queries) * 1000
        label = f"IVF_PQ nprobes={nprobes} refine={rf}"
        print(f"  {label:<34}{recall / len(queries):>12.2f}{ms:>11.3f} ms")

    print()
    print("  真实 LanceDB 里对应第一个方案的开关是 .bypass_vector_index()；")
    print("  第二个方案对应 .refine_factor(n)（官方推荐区间 5~50）。")


if __name__ == "__main__":
    main()
