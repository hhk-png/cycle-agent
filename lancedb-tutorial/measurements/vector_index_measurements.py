"""第 13~16 章（向量索引：IVF / PQ / HNSW / SQ-BQ）的全部实测数字。

一章一节，输出即章内引用的原始数据。运行方式（Windows 控制台是 GBK，必须带 -X utf8）：

    python -X utf8 measurements/vector_index_measurements.py
    python -X utf8 measurements/vector_index_measurements.py 13     # 只跑第 13 章那节
    python -X utf8 measurements/vector_index_measurements.py 13 14  # 跑多节

关于这些数字怎么读：**召回率、分区大小、候选比例、距离计算次数、
字节数是确定性的**——同一份数据、同一组参数，重跑逐位相同。
**延迟和建图耗时是挂钟时间**，会被机器负载影响，同一台机器两遍能差 2~3 倍。
所以所有毫秒数只当作量级和比值读，不要当作基准。
"""

from __future__ import annotations

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mini_lancedb.index import (
    BQ,
    BinaryQuantizer,
    FlatIndex,
    HnswGraph,
    HnswIndex,
    IndexMetadata,
    IvfHnswPqIndex,
    IvfHnswSqIndex,
    IvfIndex,
    IvfPqIndex,
    IvfSqIndex,
    ProductQuantizer,
    ScalarQuantizer,
    kmeans,
)
from mini_lancedb.index.metric import pairwise_distances, top_k

# ---------------------------------------------------------------------------
# 基准数据：和 tests/test_index_vector.py 完全一致
# ---------------------------------------------------------------------------
N_ROWS, DIM, N_CLUSTERS, N_QUERIES, K = 3000, 32, 20, 40, 10
ROW_ID_BASE = 10_000


def make_bench(seed=1234):
    rng = np.random.default_rng(seed)
    centers = (rng.normal(size=(N_CLUSTERS, DIM)) * 3.0).astype(np.float32)
    labels = rng.integers(0, N_CLUSTERS, N_ROWS)
    data = (centers[labels] + rng.normal(size=(N_ROWS, DIM))).astype(np.float32)
    row_ids = (np.arange(N_ROWS) + ROW_ID_BASE).astype(np.uint64)
    q_labels = rng.integers(0, N_CLUSTERS, N_QUERIES)
    queries = (centers[q_labels] + rng.normal(size=(N_QUERIES, DIM)) * 0.6).astype(np.float32)
    return data, row_ids, queries


def metadata_for(index_type, **params):
    p = {"distance_type": "l2"}
    p.update(params)
    return IndexMetadata(name=f"idx_{index_type.lower()}", index_type=index_type,
                         columns=["vector"], params=p)


def exact_truth(data, queries, k=K, row_ids=None):
    if row_ids is None:
        row_ids = np.arange(data.shape[0])
    out = []
    for i in range(queries.shape[0]):
        d = pairwise_distances(data, queries[i:i + 1], "l2")[0]
        _, idx = top_k(d, k)
        out.append({int(row_ids[j]) for j in idx})
    return out


def recall_of(index, queries, truth, k=K, params=None, mask=None):
    res = index.search(queries, k, params=params, mask=mask)
    hits = total = 0
    for i, expected in enumerate(truth):
        got = {int(r) for r in res.row_ids[i] if r >= 0}
        hits += len(got & expected)
        total += len(expected)
    return hits / max(1, total)


def latency_ms(index, queries, k=K, params=None, mask=None, reps=10):
    t0 = time.perf_counter()
    for _ in range(reps):
        index.search(queries, k, params=params, mask=mask)
    return (time.perf_counter() - t0) / reps / queries.shape[0] * 1000


def gini(counts):
    x = np.sort(np.asarray(counts, dtype=np.float64))
    n = x.size
    if n == 0 or x.sum() == 0:
        return 0.0
    idx = np.arange(1, n + 1)
    return float((2 * (idx * x).sum()) / (n * x.sum()) - (n + 1) / n)


def inertia_on(centroids, X):
    d = pairwise_distances(centroids, X, "l2")
    return float(d.min(axis=0).sum())


def banner(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ===========================================================================
# 第 13 章
# ===========================================================================
def chapter_13():
    data, row_ids, queries = make_bench()
    truth = exact_truth(data, queries, K, row_ids=row_ids)

    banner("13.1 暴力检索的算术成本（1M x 768 维，一次查询）")
    n, d = 1_000_000, 768
    print(f"n={n:,}  d={d}  乘加次数 = n*d = {n*d:,} ({n*d/1e6:.0f} 百万)")
    print(f"float32 存储 = {n*d*4/1e9:.2f} GB")
    print(f"单核 10 GFLOP/s（每乘加 2 FLOP）-> 单次查询约 {2*n*d/10e9*1000:.0f} ms")

    banner("13.3 k-means：迭代次数、inertia、耗时")
    for k in (8, 20, 64, 256):
        t0 = time.perf_counter()
        cents, inertia, iters = kmeans(data, k, max_iters=50, seed=0)
        dt = (time.perf_counter() - t0) * 1000
        print(f"k={k:4d}  iters={iters:3d}  inertia={inertia:12.1f}  耗时 {dt:6.1f} ms")

    print()
    print("-- 采样训练：10% 求质心，拿到全量上评估 --")
    c_full, _, it_full = kmeans(data, 20, max_iters=50, seed=0)
    c_s, _, it_s = kmeans(data, 20, max_iters=50, seed=0, sample_rate=0.1)
    i_full, i_s = inertia_on(c_full, data), inertia_on(c_s, data)
    print(f"全量训练，在全量上评估 inertia = {i_full:.1f}  (iters={it_full})")
    print(f"10% 训练，在全量上评估 inertia = {i_s:.1f}  (iters={it_s})")
    print(f"差 {((i_s - i_full)/i_full*100):+.2f}%")

    banner("13.5 IVF_FLAT：recall@10 vs nprobes（聚簇数据）")
    idx = IvfIndex.build(data, row_ids, metadata=metadata_for("IVF_FLAT", num_partitions=20))
    sizes = np.diff(idx.partition_offsets)
    print(f"分区数={idx.num_partitions}  k-means iters={idx.kmeans_iterations}")
    print(f"桶大小: min={sizes.min()} median={np.median(sizes):.0f} max={sizes.max()} "
          f"mean={sizes.mean():.1f} imbalance={sizes.max()/sizes.mean():.3f} gini={gini(sizes):.4f}")
    print()
    print("nprobes  recall@10   延迟/查询(ms)  候选比例")
    for p in (1, 2, 4, 8, 20):
        lat = latency_ms(idx, queries, params={"nprobes": p}, reps=20)
        r = recall_of(idx, queries, truth, params={"nprobes": p})
        frac = sum(sizes[np.argsort(pairwise_distances(idx.centroids, q[None, :], "l2")[0])[:p]].sum()
                   for q in queries) / (N_QUERIES * N_ROWS)
        print(f"{p:5d}    {r:.4f}      {lat:8.4f}      {frac:.3f}")

    banner("13.8 分区不均衡：Gini / imbalance")
    print(f"聚簇数据: imbalance={sizes.max()/sizes.mean():.3f}  gini={gini(sizes):.4f}")

    banner("13.9 随机（无聚簇）数据上 IVF 的表现")
    rng = np.random.default_rng(99)
    rand_data = rng.normal(size=(N_ROWS, DIM)).astype(np.float32)
    rand_q = rng.normal(size=(N_QUERIES, DIM)).astype(np.float32)
    rand_truth = exact_truth(rand_data, rand_q, K, row_ids=row_ids)
    ri = IvfIndex.build(rand_data, row_ids, metadata=metadata_for("IVF_FLAT", num_partitions=20))
    rs = np.diff(ri.partition_offsets)
    print(f"随机数据桶: min={rs.min()} max={rs.max()} mean={rs.mean():.1f} "
          f"imbalance={rs.max()/rs.mean():.3f} gini={gini(rs):.4f}")
    print("nprobes  recall@10")
    for p in (1, 2, 4, 8, 20):
        print(f"{p:5d}    {recall_of(ri, rand_q, rand_truth, params={'nprobes': p}):.4f}")

    banner("13.4 num_partitions 的影响（nprobes=2）")
    print("num_partitions  imbalance  gini    recall@10   平均桶长")
    for P in (4, 8, 16, 32, 64):
        ix = IvfIndex.build(data, row_ids, metadata=metadata_for("IVF_FLAT", num_partitions=P))
        ss = np.diff(ix.partition_offsets)
        print(f"{P:14d}  {ss.max()/ss.mean():8.3f}  {gini(ss):.4f}   "
              f"{recall_of(ix, queries, truth, params={'nprobes': 2}):.4f}      {ss.mean():.1f}")
    print()
    print("sqrt(N) 经验规则：")
    for N in (1_000, 10_000, 100_000, 1_000_000, 10_000_000, 100_000_000, 1_000_000_000):
        print(f"  N={N:>12,}  sqrt(N)={int(round(math.sqrt(N))):>8,}")

    banner("13.6 残差 vs 原向量（IVF_PQ, nprobes=20）")
    res_idx = IvfPqIndex.build(data, row_ids, metadata=metadata_for(
        "IVF_PQ", num_partitions=20, num_sub_vectors=8, num_bits=8))
    raw_idx = IvfPqIndex.build(data, row_ids, metadata=metadata_for(
        "IVF_PQ", num_partitions=20, num_sub_vectors=8, num_bits=8, residual_vector=False))
    print(f"残差 PQ:   recall@10 = {recall_of(res_idx, queries, truth, params={'nprobes': 20}):.4f}")
    print(f"原向量 PQ: recall@10 = {recall_of(raw_idx, queries, truth, params={'nprobes': 20}):.4f}")
    resid = data - res_idx.centroids[res_idx.assignments]
    print(f"原向量范数均值 = {np.linalg.norm(data, axis=1).mean():.4f}")
    print(f"残差范数均值   = {np.linalg.norm(resid, axis=1).mean():.4f}")
    print(f"比值           = {np.linalg.norm(resid, axis=1).mean()/np.linalg.norm(data, axis=1).mean():.4f}")

    banner("13.10 更大规模（50000 x 64，50 簇）上的真实延迟")
    N, D, C = 50_000, 64, 50
    rng = np.random.default_rng(2024)
    centers = (rng.normal(size=(C, D)) * 3.0).astype(np.float32)
    labels = rng.integers(0, C, N)
    big = (centers[labels] + rng.normal(size=(N, D))).astype(np.float32)
    big_ids = np.arange(N, dtype=np.uint64)
    ql = rng.integers(0, C, 100)
    bq_q = (centers[ql] + rng.normal(size=(100, D)) * 0.6).astype(np.float32)
    big_truth = exact_truth(big, bq_q, K, row_ids=big_ids)

    flat = FlatIndex.build(big, big_ids, metadata=metadata_for("FLAT"))
    flat_lat = latency_ms(flat, bq_q, reps=5)
    print(f"FLAT (暴力)  recall@10=1.0000  延迟={flat_lat:.3f} ms/查询")
    ivf = IvfIndex.build(big, big_ids, metadata=metadata_for("IVF_FLAT", num_partitions=64))
    ss = np.diff(ivf.partition_offsets)
    print(f"IVF_FLAT 桶: min={ss.min()} max={ss.max()} mean={ss.mean():.1f} "
          f"imbalance={ss.max()/ss.mean():.2f}")
    print("  nprobes  recall@10  延迟(ms)  相对 FLAT 加速")
    for p in (1, 4, 16, 64):
        r = recall_of(ivf, bq_q, big_truth, params={"nprobes": p})
        l = latency_ms(ivf, bq_q, params={"nprobes": p}, reps=5)
        print(f"  {p:7d}    {r:.4f}    {l:7.3f}   {flat_lat/l:.2f}x")
    hn = HnswIndex.build(big, big_ids, metadata=metadata_for(
        "HNSW", m=16, ef_construction=100, seed=7))
    print("HNSW (m=16, ef_construction=100):")
    for ef in (20, 50, 100):
        r = recall_of(hn, bq_q, big_truth, params={"ef": ef})
        l = latency_ms(hn, bq_q, params={"ef": ef}, reps=5)
        print(f"  ef={ef:4d}: recall={r:.4f}  延迟={l:7.3f} ms  ({flat_lat/l:.2f}x)")


# ===========================================================================
# 第 14 章
# ===========================================================================
def chapter_14():
    data, row_ids, queries = make_bench()
    truth = exact_truth(data, queries, K, row_ids=row_ids)

    banner("14.1 内存问题：十亿条向量的存放（算术，非实测）")
    for n, d in [(1_000_000_000, 128), (1_000_000_000, 768), (100_000_000, 768)]:
        print(f"n={n:>13,}  d={d:3d}  float32 = {n*d*4/1e9:8.2f} GB   "
              f"PQ(m=8,8bit) = {n*8/1e9:5.2f} GB   PQ(m=16) = {n*16/1e9:5.2f} GB")

    banner("14.2 重建误差 vs (m, num_bits) -- 3000x32 数据")
    print(" m  nbits  子空间宽  K=2^b  码本字节  压缩比  平均相对误差  中位相对误差  训练(ms)")
    for m in (2, 4, 8, 16, 32):
        for bits in (4, 8):
            pq = ProductQuantizer()
            t0 = time.perf_counter()
            pq.train(data, num_sub_vectors=m, num_bits=bits, max_iters=25)
            dt = (time.perf_counter() - t0) * 1000
            e = pq.reconstruction_error(data)
            print(f"{m:3d}  {bits:4d}   {DIM//m:6d}  {1<<bits:5d}  {pq.size_bytes():8d}  "
                  f"{e['compression_ratio']:6.1f}   {e['mean_relative_error']:.5f}      "
                  f"{e['median_relative_error']:.5f}    {dt:7.1f}")

    banner("14.5 IVF_PQ 的 recall@10 vs (m, num_bits)，nprobes=20")
    print(" m  nbits  压缩比   recall@10   recall@10(refine=8)   码本字节")
    for m in (4, 8, 16):
        for bits in (4, 8):
            idx = IvfPqIndex.build(data, row_ids, metadata=metadata_for(
                "IVF_PQ", num_partitions=20, num_sub_vectors=m, num_bits=bits))
            st = idx.stats()
            print(f"{m:3d}  {bits:4d}  {st['compression_ratio']:6.1f}   "
                  f"{recall_of(idx, queries, truth, params={'nprobes': 20}):.4f}     "
                  f"{recall_of(idx, queries, truth, params={'nprobes': 20, 'refine_factor': 8}):.4f}"
                  f"           {st['codebook_bytes']}")

    banner("14.6 残差 PQ vs 朴素 PQ（m=8, 8bit, nprobes=20）")
    res_idx = IvfPqIndex.build(data, row_ids, metadata=metadata_for(
        "IVF_PQ", num_partitions=20, num_sub_vectors=8, num_bits=8))
    raw_idx = IvfPqIndex.build(data, row_ids, metadata=metadata_for(
        "IVF_PQ", num_partitions=20, num_sub_vectors=8, num_bits=8, residual_vector=False))
    er = res_idx.pq.reconstruction_error(res_idx._pq_targets(res_idx.vectors, res_idx.assignments))
    ew = raw_idx.pq.reconstruction_error(raw_idx._pq_targets(raw_idx.vectors, raw_idx.assignments))
    print(f"残差模式：对【学习目标】的重建误差 median={er['median_relative_error']:.4f}  <- 分母是残差的范数")
    print(f"原向量：  对【学习目标】的重建误差 median={ew['median_relative_error']:.4f}  <- 分母是原向量的范数")
    print("（两者不可比！下面是还原回原向量 x 之后的正确比法）")
    for name, dec in (("残差 PQ", res_idx._reconstruct(res_idx.codes)),
                      ("原向量 PQ", raw_idx._reconstruct(raw_idx.codes))):
        err = data - dec
        rel = np.sqrt((err ** 2).sum(1)) / np.maximum(np.linalg.norm(data, axis=1), 1e-12)
        print(f"{name}: 对原向量 x 的重建 中位相对误差 = {np.median(rel):.4f}  均值 = {rel.mean():.4f}")
    print(f"残差 PQ   recall@10(nprobes=20) = {recall_of(res_idx, queries, truth, params={'nprobes': 20}):.4f}")
    print(f"原向量 PQ recall@10(nprobes=20) = {recall_of(raw_idx, queries, truth, params={'nprobes': 20}):.4f}")

    banner("14.3 ADC：查表做距离，不做乘法")
    pq = ProductQuantizer().train(data, num_sub_vectors=8, num_bits=8, max_iters=25)
    codes = pq.encode(data[:500])
    table = pq.compute_distance_table(queries[0])
    print(f"距离表形状 = {table.shape}  (= (num_sub_vectors, 2**num_bits))")
    t0 = time.perf_counter()
    for _ in range(50):
        pq.adc_distances(queries, codes)
    adc_t = (time.perf_counter() - t0) / 50 * 1000
    dec = pq.decode(codes)
    t0 = time.perf_counter()
    for _ in range(50):
        pairwise_distances(dec, queries, "l2")
    dec_t = (time.perf_counter() - t0) / 50 * 1000
    print(f"ADC (500 编码, 40 查询)   = {adc_t:.2f} ms")
    print(f"解码后再算 l2（BLAS）      = {dec_t:.2f} ms")
    print("（这里 ADC 更慢是 Python 解释器开销，不是算法结论）")
    adc = pq.adc_distances(queries[:4], codes)
    ref = pairwise_distances(dec, queries[:4], "l2")
    print(f"ADC 与解码后 l2 的最大绝对差 = {np.abs(adc - ref).max():.6f}")
    for nn in (1_000, 100_000, 1_000_000):
        print(f"n={nn:>9,}: 编码 {nn*8/1e6:7.2f} MB  -> 解码后 {nn*DIM*4/1e6:8.2f} MB "
              f"（膨胀 {DIM*4/8:.0f} 倍）")

    banner("14.4 转置布局：按【子空间】取码 vs 按【行】取码")
    n, M = 1_000_000, 16
    rng = np.random.default_rng(0)
    row_major = rng.integers(0, 256, size=(n, M), dtype=np.uint8)
    transposed = np.ascontiguousarray(row_major.T)
    print(f"row-major codes[:, s] : strides={row_major.strides}")
    print(f"transposed T[s]       : strides={transposed.strides}")
    t0 = time.perf_counter()
    for s in range(M):
        _ = row_major[:, s].astype(np.int64)
    rm = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    for s in range(M):
        _ = transposed[s].astype(np.int64)
    tr = (time.perf_counter() - t0) * 1000
    print(f"row-major 跨列取全部码  = {rm:7.2f} ms")
    print(f"transposed 连续取全部码 = {tr:7.2f} ms   -> 快 {rm/max(tr,1e-9):.1f} 倍")

    banner("14.2 num_bits 4 vs 8")
    for bits in (4, 8):
        pq = ProductQuantizer().train(data, num_sub_vectors=8, num_bits=bits, max_iters=25)
        e = pq.reconstruction_error(data)
        print(f"num_bits={bits}: K={1<<bits:3d}  码本={pq.size_bytes():6d} 字节  "
              f"每条向量 {pq.code_bytes_per_vector} 字节  "
              f"中位相对重建误差 = {e['median_relative_error']:.5f}")


# ===========================================================================
# 第 15 章
# ===========================================================================
def chapter_15():
    data, row_ids, queries = make_bench()
    truth = exact_truth(data, queries, K, row_ids=row_ids)

    banner("15.2 ml = 1/ln(m) 与层高分布")
    for m in (4, 8, 16, 32, 64):
        print(f"m={m:3d}  ml = 1/ln(m) = {1/math.log(m):.4f}   第 0 层占比期望 = {1 - 1/m:.3f}")
    print()
    for m, seed in ((12, 7), (8, 7), (16, 0)):
        g = HnswGraph(dim=DIM, m=m, ef_construction=60, seed=seed).build(data)
        counts = np.bincount(g.levels, minlength=int(g.levels.max()) + 1)
        levels = len(counts)
        # `levels[i]` 是点 i 的“最高层”，所以 bincount 数的是“恰好落在第 l 层”
        # 的点 —— 这是**逐层**口径，对照值必须是 n(m-1)/m^{l+1}。
        # 累积口径 n·m^-l 数的是“最高层 ≥ l”的点，两个口径不能混着比。
        print(f"m={m} 实测层分布（n={N_ROWS}）: 每层点数 = {[int(c) for c in counts]}")
        print(f"        理论(逐层) n(m-1)/m^(l+1) = "
              f"{[round(N_ROWS * (m - 1) / m ** (l + 1), 1) for l in range(levels)]}")
        print(f"        理论(累积) n*m^-l         = "
              f"{[round(N_ROWS / (m ** l), 1) for l in range(levels)]}")

    banner("15.5 HNSW recall@10 vs ef（m=12, ef_construction=60）")
    hnsw = HnswIndex.build(data, row_ids, metadata=metadata_for(
        "HNSW", m=12, ef_construction=60, seed=7))
    print("  ef    recall@10   延迟/查询(ms)")
    for ef in (10, 20, 50, 100, 200):
        print(f"{ef:5d}    {recall_of(hnsw, queries, truth, params={'ef': ef}):.4f}      "
              f"{latency_ms(hnsw, queries, params={'ef': ef}):8.4f}")
    print()
    print("-- ef 远大于 k（k=10）：只线性变慢、不再变准 --")
    for ef in (200, 1000, 3000):
        print(f"ef={ef:5d}: recall={recall_of(hnsw, queries, truth, params={'ef': ef}):.4f}"
              f"  延迟={latency_ms(hnsw, queries, params={'ef': ef}):8.4f} ms  (n={N_ROWS})")

    banner("15.7 建图时间 vs (m, ef_construction)")
    print("  m  ef_constr   建图(ms)   距离计算次数   平均出度(L0)  图字节")
    for m in (8, 16):
        for efc in (20, 60, 200):
            t0 = time.perf_counter()
            ix = HnswIndex.build(data, row_ids, metadata=metadata_for(
                "HNSW", m=m, ef_construction=efc, seed=7))
            dt = (time.perf_counter() - t0) * 1000
            s = ix.stats()
            print(f"{m:3d}  {efc:8d}  {dt:9.1f}  {s['distance_evaluations']:12,d}   "
                  f"{s['avg_degree_level0']:8.2f}     {s['graph_bytes']:8d}")

    banner("15.7b ef_construction vs 查询期召回（m=12, 查询 ef=50）")
    print("  ef_constr   建图(ms)   recall@10(ef=50)")
    for efc in (10, 20, 50, 100, 200):
        t0 = time.perf_counter()
        ix = HnswIndex.build(data, row_ids, metadata=metadata_for(
            "HNSW", m=12, ef_construction=efc, seed=7))
        dt = (time.perf_counter() - t0) * 1000
        print(f"{efc:10d}  {dt:9.1f}   "
              f"{recall_of(ix, queries, truth, params={'ef': 50}):.4f}")

    banner("15.6 内存成本（每节点）")
    s = hnsw.stats()
    print(f"m=12, n={N_ROWS}, 维度={DIM}")
    print(f"图字节 = {s['graph_bytes']}  -> 每节点 {s['graph_bytes']/N_ROWS:.2f} 字节")
    print(f"平均出度 L0 = {s['avg_degree_level0']:.2f}（m_max0 = 2m = 24）")
    print(f"图 + 向量 = {s['memory_bytes']} 字节 -> 每节点 {s['memory_bytes']/N_ROWS:.2f} 字节")
    print(f"其中向量本身 = {N_ROWS*DIM*4} 字节（占 {N_ROWS*DIM*4/s['memory_bytes']*100:.1f}%）")

    banner("15.11 过滤搜索：HNSW vs IVF_PQ（保留 10% 的行）")
    mask = np.zeros(N_ROWS, dtype=bool)
    mask[: N_ROWS // 10] = True
    filt_truth = exact_truth(data[mask], queries, K, row_ids=row_ids[mask])
    print(f"过滤后保留 {mask.sum()} / {N_ROWS} 行")
    print(f"HNSW (ef=100):            recall@10={recall_of(hnsw, queries, filt_truth, params={'ef': 100}, mask=mask):.4f}"
          f"  延迟={latency_ms(hnsw, queries, params={'ef': 100}, mask=mask):.4f} ms")
    ivf_pq = IvfPqIndex.build(data, row_ids, metadata=metadata_for(
        "IVF_PQ", num_partitions=20, num_sub_vectors=8, num_bits=8))
    print(f"IVF_PQ (nprobes=20, M=8): recall@10={recall_of(ivf_pq, queries, filt_truth, params={'nprobes': 20}, mask=mask):.4f}"
          f"  延迟={latency_ms(ivf_pq, queries, params={'nprobes': 20}, mask=mask):.4f} ms")
    print()
    print("-- 同一个 HNSW，无过滤 / 强过滤 --")
    for ef in (50, 100, 200):
        l_none = latency_ms(hnsw, queries, params={"ef": ef})
        l_filt = latency_ms(hnsw, queries, params={"ef": ef}, mask=mask)
        print(f"ef={ef:3d}: 无过滤 latency={l_none:.4f} | 强过滤 latency={l_filt:.4f}"
              f"  倍数={l_filt/l_none:.1f}x")

    banner("15.10 IVF_HNSW_SQ：分区图索引")
    ihs = IvfHnswSqIndex.build(data, row_ids, metadata=metadata_for(
        "IVF_HNSW_SQ", num_partitions=20, num_bits=8, m=8, ef_construction=40, seed=7))
    s2 = ihs.stats()
    print(f"分区数={s2['num_partitions']}  图数={s2['num_graphs']}  "
          f"每分区点数={[g.num_nodes for g in ihs.graphs][:8]}...")
    print(f"图字节={s2['graph_bytes']}  码字节={s2['code_bytes']}  压缩比={s2['compression_ratio']}")
    print()
    print("nprobes   ef   recall@10   延迟/查询(ms)")
    for npb in (1, 4, 20):
        for ef in (20, 50, 100):
            print(f"{npb:7d} {ef:4d}    "
                  f"{recall_of(ihs, queries, truth, params={'nprobes': npb, 'ef': ef}):.4f}      "
                  f"{latency_ms(ihs, queries, params={'nprobes': npb, 'ef': ef}):8.4f}")
    print()
    print(f"纯 HNSW      (ef=50)             recall@10 = {recall_of(hnsw, queries, truth, params={'ef': 50}):.4f}")
    print(f"IVF_HNSW_SQ (nprobes=20, ef=50)  recall@10 = "
          f"{recall_of(ihs, queries, truth, params={'nprobes': 20, 'ef': 50}):.4f}")

    banner("15.10b IVF_HNSW_PQ：分区内图 + 残差乘积量化（20 分区, M=8, 8bit, m=8）")
    ihp = IvfHnswPqIndex.build(data, row_ids, metadata=metadata_for(
        "IVF_HNSW_PQ", num_partitions=20, num_sub_vectors=8, num_bits=8,
        m=8, ef_construction=40, seed=7))
    s3 = ihp.stats()
    print(f"压缩比={s3['compression_ratio']}  码字节={s3['code_bytes']}  "
          f"（原始={N_ROWS*DIM*4}）")
    print()
    print("nprobes   ef   recall@10   recall@10(refine=8)")
    for npb in (1, 4, 20):
        for ef in (20, 50):
            print(f"{npb:7d} {ef:4d}    "
                  f"{recall_of(ihp, queries, truth, params={'nprobes': npb, 'ef': ef}):.4f}      "
                  f"{recall_of(ihp, queries, truth, params={'nprobes': npb, 'ef': ef, 'refine_factor': 8}):.4f}")
    print()
    print(f"纯 HNSW            (ef=50)                 recall@10 = {recall_of(hnsw, queries, truth, params={'ef': 50}):.4f}")
    print(f"IVF_HNSW_PQ (np=20, ef=50)                 recall@10 = "
          f"{recall_of(ihp, queries, truth, params={'nprobes': 20, 'ef': 50}):.4f}")
    print(f"IVF_HNSW_PQ (np=20, ef=50, refine=8)       recall@10 = "
          f"{recall_of(ihp, queries, truth, params={'nprobes': 20, 'ef': 50, 'refine_factor': 8}):.4f}")

    banner("15.4 邻居选择启发式消融：Algorithm 4 vs 朴素最近 m 个")
    print(" m  ef_constr  方式          recall@10(ef=50)  平均出度L0")

    def build_graph(m, efc, seed, heuristic):
        g = HnswGraph(dim=DIM, m=m, ef_construction=efc, seed=seed)
        if not heuristic:
            g._select_neighbors = lambda W, limit: [i for _, i in W][:limit]
        g.build(data)
        return g

    def graph_recall(g, ef=50):
        hits = total = 0
        for i in range(queries.shape[0]):
            local, _ = g.search(queries[i], K, ef=ef)
            hits += len({int(row_ids[j]) for j in local} & truth[i])
            total += K
        return hits / total

    for m in (8, 16):
        for efc in (30, 100):
            for heur in (True, False):
                g = build_graph(m, efc, 7, heur)
                tag = "Algorithm4" if heur else "朴素最近m  "
                print(f"{m:3d}  {efc:8d}  {tag}     {graph_recall(g):.4f}          "
                      f"{g.stats()['avg_degree_level0']:.2f}")


# ===========================================================================
# 第 16 章
# ===========================================================================
def chapter_16():
    data, row_ids, queries = make_bench()
    truth = exact_truth(data, queries, K, row_ids=row_ids)

    banner("16.2 SQ：位数、逐维/全局 bounds、重建误差")
    print("num_bits  per_dim  码值域  中位相对误差  平均相对误差  最大相对误差")
    for bits in (2, 4, 8):
        for per_dim in (True, False):
            sq = ScalarQuantizer(num_bits=bits, per_dimension=per_dim).train(data)
            e = sq.quantization_error(data)
            print(f"{bits:8d}  {str(per_dim):7s}  0..{sq.max_level:<4d}  "
                  f"{e.median_relative_error:.5f}     {e.mean_relative_error:.5f}     "
                  f"{e.max_relative_error:.5f}")
    print()
    lo, hi = data.min(axis=0), data.max(axis=0)
    print(f"逐维区间宽度: 最小 {np.min(hi-lo):.3f}  最大 {np.max(hi-lo):.3f}  "
          f"比值 {np.max(hi-lo)/np.min(hi-lo):.2f}")
    print(f"全局区间: [{data.min():.3f}, {data.max():.3f}]  宽度 {data.max()-data.min():.3f}")

    banner("16.1 SQ 增量：整体平移 vs 中位相对误差（全局 bounds, 8 bit）")
    shift_sq = ScalarQuantizer(num_bits=8, per_dimension=False).train(data)
    print(f"训练区间 = [{float(shift_sq.min[0]):.3f}, {float(shift_sq.max[0]):.3f}]")
    print("平移量   中位相对误差   平均相对误差")
    base = None
    for shift in (0, 6, 12, 25):
        e = shift_sq.quantization_error(data + shift)
        if shift == 0:
            base = e.median_relative_error
        print(f"{shift:+6d}   {e.median_relative_error:.4f}       "
              f"{e.mean_relative_error:.4f}    (相对 +0: {e.median_relative_error / base:.1f}x)")

    banner("16.3 SQ：查询也要量化 -- 对称 vs 非对称")
    for per_dim in (True, False):
        sq = ScalarQuantizer(num_bits=8, per_dimension=per_dim).train(data)
        codes, dec = sq.encode(data), sq.decode(sq.encode(data))
        sym = asym = 0.0
        for sym_flag in (True, False):
            hits = tot = 0
            for i in range(N_QUERIES):
                if sym_flag:
                    qc = sq.encode(queries[i:i + 1])[0].astype(np.int64)
                    d = (((codes.astype(np.int64) - qc) ** 2)
                         * (sq.scale.astype(np.float64) ** 2)).sum(1).astype(np.float32)
                else:
                    d = pairwise_distances(dec, queries[i:i + 1], "l2")[0]
                idx = top_k(d, K)[1]
                hits += len({int(row_ids[j]) for j in idx} & truth[i])
                tot += K
            if sym_flag:
                sym = hits / tot
            else:
                asym = hits / tot
        print(f"per_dimension={per_dim}: 对称(查询编u8)={sym:.4f}  非对称(查询浮点)={asym:.4f}")

    banner("16.1 IVF_SQ：recall 与压缩（8 bit）")
    ivf_sq = IvfSqIndex.build(data, row_ids, metadata=metadata_for(
        "IVF_SQ", num_partitions=20, num_bits=8))
    s = ivf_sq.stats()
    print(f"压缩比 = {s['compression_ratio']}  code_bytes = {s['code_bytes']}  （原始 = {N_ROWS*DIM*4}）")
    print("nprobes  recall@10  recall@10(refine=8)")
    for p in (1, 2, 4, 8, 20):
        print(f"{p:7d}    {recall_of(ivf_sq, queries, truth, params={'nprobes': p}):.4f}      "
              f"{recall_of(ivf_sq, queries, truth, params={'nprobes': p, 'refine_factor': 8}):.4f}")

    banner("16.5 BQ：纯汉明 vs 精排")
    bq = BQ.build(data, row_ids, metadata=metadata_for("BQ"))
    bs = bq.stats()
    print(f"码字节 = {bs['code_bytes']}  原始 = {N_ROWS*DIM*4}  "
          f"压缩比 = {bs['compression_ratio']:.1f}  words/向量 = {bs['words_per_vector']}")
    print("refine_factor  recall@10")
    for rf in (1, 2, 5, 10, 20, 50):
        print(f"{rf:13d}    {recall_of(bq, queries, truth, params={'refine_factor': rf}):.4f}")
    rng = np.random.default_rng(3)
    d128 = rng.normal(size=(500, 128)).astype(np.float32)
    s128 = BQ.build(d128, np.arange(500), metadata=metadata_for("BQ")).stats()
    print(f"128 维: 压缩比 = {s128['compression_ratio']:.1f}  words/向量 = {s128['words_per_vector']}")
    print(f" 32 维: 压缩比 = {bs['compression_ratio']:.1f}  words/向量 = {bs['words_per_vector']}")

    banner("16.7 BQ 重建：符号保留方向，长度只剩 sqrt(2/pi)")
    rng = np.random.default_rng(5)
    g = rng.normal(size=(2000, 256)).astype(np.float32) * 4.0
    bqq = BinaryQuantizer(256)
    bqq.fit_scales(g)
    rec = bqq.reconstruct(bqq.encode(g))
    ratio = np.linalg.norm(rec, axis=1) / np.linalg.norm(g, axis=1)
    print(f"重建/原始 模长比: 均值 {ratio.mean():.4f}  (sqrt(2/pi) = {np.sqrt(2/np.pi):.4f})")
    print(f"绝对值的逐行均值精确等于 scale: "
          f"{np.allclose(np.abs(rec).mean(axis=1), np.abs(g).mean(axis=1), rtol=1e-5)}")

    banner("16.8 BQ 内存（算术）：十亿条向量的存放")
    for n, d in [(1_000_000_000, 768), (1_000_000_000, 1024), (1_000_000_000, 128)]:
        print(f"n={n:,}  d={d}: float32 = {n*d*4/1e9:7.2f} GB  "
              f"BQ = {n*((d+63)//64)*8/1e9:6.3f} GB  "
              f"SQ = {n*d/1e9:7.2f} GB  PQ(m=8) = {n*8/1e9:5.2f} GB")


CHAPTERS = {13: chapter_13, 14: chapter_14, 15: chapter_15, 16: chapter_16}

if __name__ == "__main__":
    wanted = [int(a) for a in sys.argv[1:]] or sorted(CHAPTERS)
    for c in wanted:
        CHAPTERS[c]()
    print()
    print("done")
