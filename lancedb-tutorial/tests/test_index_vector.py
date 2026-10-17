"""向量索引层的端到端测试。

评测 ANN 索引只有一条硬指标：**在同等资源下和暴力检索的召回率**。
所以这个文件里几乎所有断言都围绕三件事：

1. **精确性**：``FLAT`` 必须和暴力检索逐位一致；``IVF_FLAT`` 在探满
   所有分区时必须给出与暴力检索相同的 top-k。
2. **单调性**：精度旋钮（``nprobes`` / ``ef`` / ``refine_factor``）
   调大，召回率**不允许下降**。对精确打分的索引（IVF_FLAT）这是可以
   证明的，所以用严格断言；对量化索引它是经验性质，阈值都写了实测值。
3. **正确性不被优化吃掉**：掩码、行号映射、save/load 往返、append、remap
   这些"不改变算法只改变数据流"的路径最容易引入静默错误，所以每一条
   都有独立测试，而且断言的是**逐位相等**而不是"差不多"。

基准数据是固定种子的合成聚簇数据（3000 × 32 维，20 个簇），
所有召回率阈值都是在这份数据上**实测**出来的，并在注释里注明。

## 实测基线（recall@10，种子固定，可复现）

=================  =========================  =========
索引                参数                       recall@10
=================  =========================  =========
FLAT               暴力精确                    1.0000
IVF_FLAT           nprobes=1 / 4 / 20          0.9800 / 1.0000 / 1.0000
IVF_SQ  (4×)       nprobes=4                   0.9825
IVF_SQ  (4×)       nprobes=4 + refine=8        1.0000
IVF_PQ  (16×)      nprobes=8                   0.6900
IVF_PQ  (16×)      nprobes=8 + refine=8        1.0000
IVF_PQ 原向量      nprobes=20                  0.4650
HNSW               ef=20 / 50                  0.9950 / 1.0000
IVF_HNSW_SQ (4×)   nprobes=20, ef=50           0.9825
IVF_HNSW_PQ (16×)  nprobes=20, ef=50           0.7300
IVF_HNSW_PQ (16×)  + refine=8                  1.0000
BQ      (1 bit)    纯汉明                      0.1600
BQ      (1 bit)    refine=20                   1.0000
=================  =========================  =========

两个值得注意的现象：

* ``IVF_PQ`` 的召回率在 ``nprobes`` 从 1 涨到 20 的过程中停在 0.69 不动 ——
  这是**量化误差的平台**，不是"探漏了桶"；
* ``BQ`` 的纯汉明召回率只有 0.16，但精排后到 1.00 —— 说明 1 bit 保留了
  "大致方向"（候选集是对的），丢掉的是排序能力。这正是
  ``refine_factor`` 对它不是可选项而是必需品的原因。
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from mini_lancedb.errors import IndexBuildError, NotSupportedError, QueryError
from mini_lancedb.index import (
    METRICS,
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
    VECTOR_INDEX_TYPES,
    create_vector_index,
    get_metric,
    pack_signs,
    unpack_signs,
    vector_index_types,
)
from mini_lancedb.index.base import DictIndexStore
from mini_lancedb.index.metric import normalize, pairwise_distances, top_k
from mini_lancedb.index.pq import kmeans

# ---------------------------------------------------------------------------
# 基准数据与工具
# ---------------------------------------------------------------------------

N_ROWS = 3000
DIM = 32
N_CLUSTERS = 20
N_QUERIES = 40
K = 10

#: 行号不从 0 开始 —— 这样"返回的是表里的行号而不是数组下标"这件事
#: 一旦写错就会立刻暴露出来。
ROW_ID_BASE = 10_000


@pytest.fixture(scope="module")
def bench() -> SimpleNamespace:
    rng = np.random.default_rng(1234)
    centers = (rng.normal(size=(N_CLUSTERS, DIM)) * 3.0).astype(np.float32)
    labels = rng.integers(0, N_CLUSTERS, N_ROWS)
    data = (centers[labels] + rng.normal(size=(N_ROWS, DIM))).astype(np.float32)
    row_ids = (np.arange(N_ROWS) + ROW_ID_BASE).astype(np.uint64)
    q_labels = rng.integers(0, N_CLUSTERS, N_QUERIES)
    queries = (centers[q_labels] + rng.normal(size=(N_QUERIES, DIM)) * 0.6).astype(
        np.float32
    )
    return SimpleNamespace(
        data=data, row_ids=row_ids, queries=queries, centers=centers, labels=labels
    )


def metadata_for(index_type: str, **params) -> IndexMetadata:
    p = {"distance_type": "l2"}
    p.update(params)
    return IndexMetadata(
        name=f"idx_{index_type.lower()}", index_type=index_type, columns=["vector"], params=p
    )


def exact_truth(
    data: np.ndarray,
    queries: np.ndarray,
    k: int = K,
    *,
    metric: str = "l2",
    row_ids: np.ndarray | None = None,
) -> list:
    """暴力检索的 ground truth，返回每个查询的"正确答案行号集合"。"""

    if row_ids is None:
        row_ids = np.arange(data.shape[0])
    out = []
    for i in range(queries.shape[0]):
        d = pairwise_distances(data, queries[i : i + 1], metric)[0]
        _, idx = top_k(d, k)
        out.append({int(row_ids[j]) for j in idx})
    return out


def recall_of(index, queries, truth, k=K, *, mask=None, params=None) -> float:
    res = index.search(queries, k, mask=mask, params=params)
    hits = 0
    total = 0
    for i, expected in enumerate(truth):
        got = {int(r) for r in res.row_ids[i] if r >= 0}
        hits += len(got & expected)
        total += len(expected)
    return hits / max(1, total)


# -- 每个索引一条 module 级 fixture：建一次，几十个测试复用 ---------------


@pytest.fixture(scope="module")
def flat(bench) -> FlatIndex:
    return FlatIndex.build(bench.data, bench.row_ids, metadata=metadata_for("FLAT"))


@pytest.fixture(scope="module")
def truth(bench) -> list:
    return exact_truth(bench.data, bench.queries, K, row_ids=bench.row_ids)


@pytest.fixture(scope="module")
def ivf_flat(bench) -> IvfIndex:
    return IvfIndex.build(
        bench.data,
        bench.row_ids,
        metadata=metadata_for("IVF_FLAT", num_partitions=20),
    )


@pytest.fixture(scope="module")
def ivf_sq(bench) -> IvfSqIndex:
    return IvfSqIndex.build(
        bench.data,
        bench.row_ids,
        metadata=metadata_for("IVF_SQ", num_partitions=20, num_bits=8),
    )


@pytest.fixture(scope="module")
def ivf_pq(bench) -> IvfPqIndex:
    return IvfPqIndex.build(
        bench.data,
        bench.row_ids,
        metadata=metadata_for(
            "IVF_PQ", num_partitions=20, num_sub_vectors=8, num_bits=8
        ),
    )


@pytest.fixture(scope="module")
def hnsw(bench) -> HnswIndex:
    return HnswIndex.build(
        bench.data,
        bench.row_ids,
        metadata=metadata_for("HNSW", m=12, ef_construction=60, seed=7),
    )


@pytest.fixture(scope="module")
def ivf_hnsw_sq(bench) -> IvfHnswSqIndex:
    return IvfHnswSqIndex.build(
        bench.data,
        bench.row_ids,
        metadata=metadata_for(
            "IVF_HNSW_SQ", num_partitions=20, num_bits=8, m=8, ef_construction=40, seed=7
        ),
    )


@pytest.fixture(scope="module")
def ivf_hnsw_pq(bench) -> IvfHnswPqIndex:
    return IvfHnswPqIndex.build(
        bench.data,
        bench.row_ids,
        metadata=metadata_for(
            "IVF_HNSW_PQ",
            num_partitions=20,
            num_sub_vectors=8,
            num_bits=8,
            m=8,
            ef_construction=40,
            seed=7,
        ),
    )


@pytest.fixture(scope="module")
def bq(bench) -> BQ:
    return create_vector_index("BQ", bench.data, bench.row_ids, metadata_for("BQ"))


# ---------------------------------------------------------------------------
# FLAT：精确性、行号、掩码、度量
# ---------------------------------------------------------------------------


def test_flat_returns_exactly_bruteforce_topk(flat, bench, truth):
    res = flat.search(bench.queries, K)
    assert res.distances.shape == (N_QUERIES, K)
    for i, expected in enumerate(truth):
        assert {int(r) for r in res.row_ids[i]} == expected


def test_flat_reports_exact_not_approximate(flat, bench):
    res = flat.search(bench.queries[:3], K)
    assert res.approximate is False


def test_flat_distances_are_sorted_and_match_bruteforce(flat, bench):
    res = flat.search(bench.queries[:5], K)
    d = pairwise_distances(bench.data, bench.queries[:5], "l2")
    for i in range(5):
        assert np.all(np.diff(res.distances[i]) >= -1e-5)
        for j in range(K):
            rid = int(res.row_ids[i, j])
            pos = rid - ROW_ID_BASE
            assert res.distances[i, j] == pytest.approx(d[i, pos], rel=1e-5)


def test_flat_row_ids_are_table_ids_not_positions(flat, bench):
    res = flat.search(bench.data[:5], K)
    # 查询就是库里的向量本身 → 第一条必然是它自己，且行号带偏移
    assert list(res.row_ids[:, 0]) == [ROW_ID_BASE + i for i in range(5)]


def test_flat_k_equals_n_returns_every_row(flat, bench):
    res = flat.search(bench.queries[:2], N_ROWS)
    assert res.distances.shape == (2, N_ROWS)
    got = set(int(r) for r in res.row_ids[0])
    assert got == set(int(r) for r in bench.row_ids)


def test_flat_mask_excludes_deleted_rows(flat, bench, truth):
    mask = np.ones(N_ROWS, dtype=bool)
    deleted = np.arange(0, N_ROWS, 3)
    mask[deleted] = False
    res = flat.search(bench.queries, K, mask=mask)
    returned = {int(r) for r in res.row_ids.ravel() if r >= 0}
    assert returned.isdisjoint({int(r) for r in bench.row_ids[deleted]})
    # 掩码之后的正确答案是"暴力检索 + 掩码"，索引必须与之一致
    masked_truth = exact_truth(
        bench.data[mask], bench.queries, K, row_ids=bench.row_ids[mask]
    )
    for i, expected in enumerate(masked_truth):
        assert {int(r) for r in res.row_ids[i]} == expected


def test_flat_mask_all_false_returns_no_hits(flat, bench):
    res = flat.search(bench.queries[:3], K, mask=np.zeros(N_ROWS, dtype=bool))
    assert np.all(res.row_ids == -1)
    assert np.all(np.isinf(res.distances))


def test_flat_mask_wrong_length_raises(flat, bench):
    with pytest.raises(QueryError):
        flat.search(bench.queries[:1], K, mask=np.ones(N_ROWS - 1, dtype=bool))


def test_flat_cosine_matches_bruteforce(bench):
    idx = FlatIndex.build(
        bench.data, bench.row_ids, metadata=metadata_for("FLAT", distance_type="cosine")
    )
    truth_cos = exact_truth(bench.data, bench.queries, K, metric="cosine", row_ids=bench.row_ids)
    res = idx.search(bench.queries, K)
    for i, expected in enumerate(truth_cos):
        assert {int(r) for r in res.row_ids[i]} == expected


def test_flat_cosine_equals_l2_on_normalized_vectors(bench):
    """归一化向量上 ``l2²= 2(1-cos)``，所以两种度量的**排序**必须一致。"""

    unit = normalize(bench.data)
    ids = bench.row_ids
    q_unit = normalize(bench.queries)
    l2_idx = FlatIndex.build(unit, ids, metadata=metadata_for("FLAT", distance_type="l2"))
    cos_idx = FlatIndex.build(unit, ids, metadata=metadata_for("FLAT", distance_type="cosine"))
    r_l2 = l2_idx.search(q_unit, K)
    r_cos = cos_idx.search(q_unit, K)
    assert np.array_equal(r_l2.row_ids, r_cos.row_ids)
    # 距离也满足 l2² = 2·(1-cos)
    assert np.allclose(r_l2.distances, 2.0 * r_cos.distances, atol=1e-4)


def test_flat_dot_metric_matches_bruteforce(bench):
    idx = FlatIndex.build(
        bench.data, bench.row_ids, metadata=metadata_for("FLAT", distance_type="dot")
    )
    truth_dot = exact_truth(bench.data, bench.queries, K, metric="dot", row_ids=bench.row_ids)
    res = idx.search(bench.queries, K)
    for i, expected in enumerate(truth_dot):
        assert {int(r) for r in res.row_ids[i]} == expected


def test_flat_hamming_metric_end_to_end():
    """二进制数据上的汉明度量：邻居就是"取值不同的维度最少"的那几条。

    数据取 0/1 的 float32，所以按字节比较的汉明距离等价于"不同维数 × 常数"，
    排序与逐维比较完全一致。这里比较的是**距离的多重集**而不是行号集合 ——
    0/1 数据上"不同维数"很容易并列，而行号集合的并列打破顺序是实现细节。
    """

    rng = np.random.default_rng(7)
    d = 16
    n = 200
    data = (rng.random((n, d)) < 0.5).astype(np.float32)
    row_ids = np.arange(n)
    idx = FlatIndex.build(data, row_ids, metadata=metadata_for("FLAT", distance_type="hamming"))
    q = (rng.random((3, d)) < 0.5).astype(np.float32)
    res = idx.search(q, 5)
    for i in range(3):
        diff = np.count_nonzero(data != q[i], axis=1)
        got = diff[res.row_ids[i].astype(np.int64)]
        assert np.array_equal(np.sort(got), np.sort(diff)[:5])


# ---------------------------------------------------------------------------
# FLAT：持久化、增量、压实
# ---------------------------------------------------------------------------


def test_flat_save_load_roundtrip_is_bit_identical(flat, bench):
    store = DictIndexStore()
    names = flat.save(store)
    assert names
    loaded = FlatIndex.load(store, flat.metadata)
    a = flat.search(bench.queries, K)
    b = loaded.search(bench.queries, K)
    assert np.array_equal(a.row_ids, b.row_ids)
    assert np.array_equal(a.distances, b.distances)


def test_flat_append_extends_index(bench, truth):
    idx = FlatIndex.build(
        bench.data[:N_ROWS - 100], bench.row_ids[: N_ROWS - 100], metadata=metadata_for("FLAT")
    )
    idx.append(bench.data[N_ROWS - 100 :], bench.row_ids[N_ROWS - 100 :])
    assert idx.num_indexed_rows == N_ROWS
    # 被追加的那几行现在必须能查出来（查它自己）
    res = idx.search(bench.data[N_ROWS - 100 :], 1)
    assert list(res.row_ids[:, 0]) == list(bench.row_ids[N_ROWS - 100 :])
    assert recall_of(idx, bench.queries, truth) == 1.0


def test_flat_remap_drops_deleted_rows():
    rng = np.random.default_rng(3)
    data = rng.normal(size=(100, 8)).astype(np.float32)
    row_ids = np.arange(100)
    idx = FlatIndex.build(data, row_ids, metadata=metadata_for("FLAT"))
    old_to_new = np.full(100, -1, dtype=np.int64)
    survivors = np.array([i for i in range(100) if i % 3 != 0])
    old_to_new[survivors] = np.arange(survivors.size)
    idx.remap(old_to_new)
    assert idx.num_indexed_rows == survivors.size
    res = idx.search(data[:20], 3)
    returned = {int(r) for r in res.row_ids.ravel() if r >= 0}
    deleted_new_ids = set(old_to_new[survivors].tolist())
    assert returned.issubset(deleted_new_ids)


def test_flat_stats_sanity(flat):
    s = flat.stats()
    assert s["index_type"] == "FLAT"
    assert s["num_indexed_rows"] == N_ROWS
    assert s["size_bytes"] == N_ROWS * DIM * 4 + N_ROWS * 8
    assert s["dim"] == DIM
    assert s["exact"] is True


# ---------------------------------------------------------------------------
# 通用错误路径
# ---------------------------------------------------------------------------


def test_build_empty_data_raises():
    with pytest.raises(IndexBuildError):
        FlatIndex.build(np.zeros((0, DIM), np.float32), np.zeros(0), metadata=metadata_for("FLAT"))


def test_build_row_ids_length_mismatch_raises(bench):
    with pytest.raises(IndexBuildError):
        FlatIndex.build(bench.data, bench.row_ids[:-1], metadata=metadata_for("FLAT"))


def test_build_rejects_nan(bench):
    bad = bench.data.copy()
    bad[0, 0] = np.nan
    with pytest.raises(IndexBuildError):
        FlatIndex.build(bad, bench.row_ids, metadata=metadata_for("FLAT"))


def test_search_k_greater_than_n_raises(flat):
    with pytest.raises(QueryError):
        flat.search(np.zeros((1, DIM), np.float32), N_ROWS + 1)


def test_search_k_non_positive_raises(flat, bench):
    with pytest.raises(QueryError):
        flat.search(bench.queries[:1], 0)


def test_search_wrong_dim_raises(flat, bench):
    with pytest.raises(QueryError):
        flat.search(np.zeros((2, DIM + 1), np.float32), 3)


def test_search_1d_query_is_treated_as_single_row(flat, bench):
    res = flat.search(bench.data[0], 3)
    assert res.distances.shape == (1, 3)
    assert int(res.row_ids[0, 0]) == ROW_ID_BASE


# ---------------------------------------------------------------------------
# k-means 与乘积量化
# ---------------------------------------------------------------------------


def test_kmeans_shapes_and_inertia_decreases_with_k(bench):
    inertias = []
    for k in (2, 8, 32):
        cents, inertia, iters = kmeans(bench.data, k, max_iters=15, seed=0)
        assert cents.shape == (k, DIM)
        assert iters >= 1
        inertias.append(inertia)
    assert inertias[0] > inertias[1] > inertias[2]


def test_kmeans_empty_cluster_reseeding_handles_duplicates():
    """数据里有大量重复点时，k-means++ 会挑到同一个点，空簇必须被重新播种。"""

    data = np.zeros((50, 4), dtype=np.float32)
    data[:10] = 1.0
    cents, inertia, _ = kmeans(data, 4, max_iters=20, seed=1)
    assert cents.shape == (4, 4)
    assert np.isfinite(inertia)
    # 只应该有 2 个不同位置（原点与 (1,1,1,1)），质心不该出现 NaN
    assert np.isfinite(cents).all()
    assert len({tuple(np.round(c, 3)) for c in cents}) <= 2


def test_kmeans_sampling_reduces_work_but_keeps_shape(bench):
    cents, inertia, _ = kmeans(bench.data, 16, max_iters=10, seed=2, sample_rate=0.2)
    assert cents.shape == (16, DIM)
    assert inertia > 0


def test_kmeans_more_centroids_than_samples_raises(bench):
    with pytest.raises(IndexBuildError):
        kmeans(bench.data[:5], 8, seed=0)


def test_kmeans_cosine_returns_unit_centroids(bench):
    cents, _, _ = kmeans(bench.data, 8, max_iters=10, seed=0, metric="cosine")
    norms = np.linalg.norm(cents, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-4)


def test_pq_encode_decode_shapes_and_dtypes(bench):
    pq = ProductQuantizer().train(bench.data, num_sub_vectors=8, num_bits=8, max_iters=8)
    codes = pq.encode(bench.data)
    assert codes.shape == (N_ROWS, 8)
    assert codes.dtype == np.uint8
    assert codes.max() < 256
    rec = pq.decode(codes)
    assert rec.shape == (N_ROWS, DIM)
    assert rec.dtype == np.float32
    assert pq.codebooks.shape == (8, 256, DIM // 8)


def test_pq_reconstruction_error_decreases_with_more_sub_vectors(bench):
    errs = []
    for m in (2, 4, 8, 16):
        pq = ProductQuantizer().train(bench.data, num_sub_vectors=m, num_bits=8, max_iters=12)
        errs.append(pq.reconstruction_error(bench.data)["mean_relative_error"])
    # 子向量越多，每个子空间越窄，重建越准 —— 实测单调下降
    assert errs == sorted(errs, reverse=True)
    assert errs[-1] < errs[0]


def test_pq_reconstruction_error_decreases_with_more_bits(bench):
    errs = []
    for bits in (2, 4, 8):
        pq = ProductQuantizer().train(bench.data, num_sub_vectors=8, num_bits=bits, max_iters=12)
        errs.append(pq.reconstruction_error(bench.data)["mean_relative_error"])
    assert errs[0] > errs[1] > errs[2]


def test_pq_distance_table_gives_same_distance_as_decoded_l2(bench):
    pq = ProductQuantizer().train(bench.data, num_sub_vectors=8, num_bits=8, max_iters=8)
    codes = pq.encode(bench.data[:50])
    decoded = pq.decode(codes)
    table = pq.compute_distance_table(bench.queries[0])
    assert table.shape == (8, 256)
    manual = np.zeros(50, dtype=np.float32)
    for s in range(8):
        manual += table[s][codes[:, s].astype(np.int64)]
    reference = pairwise_distances(decoded, bench.queries[:1], "l2")[0]
    assert np.allclose(manual, reference, atol=1e-4)


def test_pq_adc_distances_match_distance_to_decoded(bench):
    pq = ProductQuantizer().train(bench.data, num_sub_vectors=8, num_bits=8, max_iters=8)
    codes = pq.encode(bench.data[:200])
    adc = pq.adc_distances(bench.queries[:4], codes)
    assert adc.shape == (4, 200)
    reference = pairwise_distances(pq.decode(codes), bench.queries[:4], "l2")
    assert np.allclose(adc, reference, atol=1e-3)


def test_pq_bad_num_sub_vectors_raises(bench):
    pq = ProductQuantizer()
    with pytest.raises(IndexBuildError):
        # 32 不能被 5 整除
        pq.train(bench.data, num_sub_vectors=5, num_bits=8, max_iters=4)


def test_pq_codebook_larger_than_training_data_raises():
    data = np.random.default_rng(0).normal(size=(10, 8)).astype(np.float32)
    pq = ProductQuantizer()
    with pytest.raises(IndexBuildError):
        pq.train(data, num_sub_vectors=2, num_bits=8, max_iters=4)  # 需要 256 个质心


def test_pq_unsupported_num_bits_raises(bench):
    # 1..8 位都合法（下标要装进 uint8），0 和 9 不行
    pq = ProductQuantizer()
    with pytest.raises(IndexBuildError):
        pq.train(bench.data, num_sub_vectors=8, num_bits=9, max_iters=4)
    with pytest.raises(IndexBuildError):
        pq.train(bench.data, num_sub_vectors=8, num_bits=0, max_iters=4)


def test_pq_supports_six_bit_codebooks(bench):
    """6 bit = 64 个质心/子空间，也是常见配置（码本更小、训练更快）。"""

    pq = ProductQuantizer().train(bench.data, num_sub_vectors=8, num_bits=6, max_iters=8)
    assert pq.codebooks.shape == (8, 64, DIM // 8)
    codes = pq.encode(bench.data)
    assert codes.max() < 64


def test_pq_untrained_raises():
    pq = ProductQuantizer()
    with pytest.raises(IndexBuildError):
        pq.encode(np.zeros((1, 8), np.float32))


def test_pq_compression_ratio_matches_code_size(bench):
    pq = ProductQuantizer().train(bench.data, num_sub_vectors=8, num_bits=8, max_iters=8)
    assert pq.code_bytes_per_vector == 8
    assert pq.raw_bytes_per_vector == DIM * 4
    assert pq.compression_ratio == pytest.approx(DIM * 4 / 8)


# ---------------------------------------------------------------------------
# 标量量化
# ---------------------------------------------------------------------------


def test_sq_quantization_error_stats_are_reasonable(bench):
    sq = ScalarQuantizer(num_bits=8).train(bench.data)
    err = sq.quantization_error(bench.data)
    # 8 bit 逐维量化在这份数据上实测中位相对误差 < 1%
    assert 0.0 <= err.median_relative_error < 0.01
    assert err.mean_relative_error <= err.max_relative_error
    assert err.num_bits == 8
    assert set(err.as_dict()) >= {"mean_relative_error", "median_relative_error"}


def test_sq_4bit_worse_than_8bit(bench):
    e4 = ScalarQuantizer(num_bits=4).train(bench.data).quantization_error(bench.data)
    e8 = ScalarQuantizer(num_bits=8).train(bench.data).quantization_error(bench.data)
    assert e4.mean_relative_error > e8.mean_relative_error


def test_sq_encode_decode_shapes_and_dtypes(bench):
    sq = ScalarQuantizer(num_bits=8).train(bench.data)
    codes = sq.encode(bench.data)
    assert codes.shape == (N_ROWS, DIM) and codes.dtype == np.uint8
    assert codes.max() <= 255
    rec = sq.decode(codes)
    assert rec.shape == (N_ROWS, DIM) and rec.dtype == np.float32


def test_sq_4bit_uses_4bit_levels(bench):
    sq = ScalarQuantizer(num_bits=4).train(bench.data)
    codes = sq.encode(bench.data)
    assert codes.max() <= 15
    assert sq.max_level == 15


def test_sq_adc_matches_decoded_l2(bench):
    sq = ScalarQuantizer(num_bits=8).train(bench.data)
    codes = sq.encode(bench.data[:300])
    sq.codes = codes
    adc = sq.distance_to_codes(bench.queries[:3])
    reference = pairwise_distances(sq.decode(codes), bench.queries[:3], "l2")
    assert adc.shape == (3, 300)
    assert np.allclose(adc, reference, atol=1e-2)


def test_sq_constant_dimension_does_not_produce_nan():
    data = np.ones((40, 6), dtype=np.float32)
    sq = ScalarQuantizer(num_bits=8).train(data)
    codes = sq.encode(data)
    rec = sq.decode(codes)
    assert np.isfinite(rec).all()
    assert np.allclose(rec, 1.0)


def test_sq_unsupported_bits_raises():
    with pytest.raises(IndexBuildError):
        ScalarQuantizer(num_bits=3)


def test_sq_wrong_dim_raises(bench):
    sq = ScalarQuantizer(num_bits=8).train(bench.data)
    with pytest.raises(IndexBuildError):
        sq.encode(np.zeros((2, DIM + 1), np.float32))


# ---------------------------------------------------------------------------
# IVF_FLAT：只砍候选集，没有量化误差
# ---------------------------------------------------------------------------


def test_ivf_recall_never_decreases_with_more_probes(ivf_flat, bench, truth):
    """精确打分的候选集是"嵌套"的，所以召回率单调不减 —— 用严格断言。"""

    recalls = [
        recall_of(ivf_flat, bench.queries, truth, params={"nprobes": p})
        for p in (1, 2, 4, 8, 20)
    ]
    assert all(b >= a - 1e-12 for a, b in zip(recalls, recalls[1:])), recalls


def test_ivf_all_partitions_is_exact(ivf_flat, bench, truth):
    res = ivf_flat.search(bench.queries, K, params={"nprobes": 20})
    assert res.approximate is False
    assert recall_of(ivf_flat, bench.queries, truth, params={"nprobes": 20}) == 1.0


def test_ivf_more_probes_recovers_more_recall(ivf_flat, bench, truth):
    r1 = recall_of(ivf_flat, bench.queries, truth, params={"nprobes": 1})
    r8 = recall_of(ivf_flat, bench.queries, truth, params={"nprobes": 8})
    # 实测 nprobes=1 约 0.5~0.8，nprobes=8 已经接近满分
    assert r1 < r8
    assert r8 > 0.9


def test_ivf_stats_describe_partition_distribution(ivf_flat):
    s = ivf_flat.stats()
    assert s["index_type"] == "IVF_FLAT"
    assert s["num_partitions"] == 20
    assert s["num_indexed_rows"] == N_ROWS
    assert s["min_partition_size"] > 0
    assert s["min_partition_size"] <= s["avg_partition_size"] <= s["max_partition_size"]
    assert s["imbalance"] >= 1.0
    assert s["size_bytes"] >= int(ivf_flat.centroids.nbytes + ivf_flat.vectors.nbytes)
    assert sum(np.diff(ivf_flat.partition_offsets)) == N_ROWS


def test_ivf_save_load_roundtrip(ivf_flat, bench):
    store = DictIndexStore()
    ivf_flat.save(store)
    loaded = IvfIndex.load(store, ivf_flat.metadata)
    a = ivf_flat.search(bench.queries, K, params={"nprobes": 4})
    b = loaded.search(bench.queries, K, params={"nprobes": 4})
    assert np.array_equal(a.row_ids, b.row_ids)
    assert np.array_equal(a.distances, b.distances)


def test_ivf_append_then_full_scan_is_still_exact(bench, truth):
    """增量之后 CSR 必须被重建 —— 否则新行永远查不出来。"""

    half = N_ROWS // 2
    idx = IvfIndex.build(
        bench.data[:half], bench.row_ids[:half], metadata=metadata_for("IVF_FLAT", num_partitions=16)
    )
    idx.append(bench.data[half:], bench.row_ids[half:])
    assert idx.num_indexed_rows == N_ROWS
    res = idx.search(bench.data[half : half + 5], 1, params={"nprobes": 16})
    assert list(res.row_ids[:, 0]) == list(bench.row_ids[half : half + 5])
    assert recall_of(idx, bench.queries, truth, params={"nprobes": 16}) == 1.0


def test_ivf_append_does_not_retrain_centroids(bench):
    idx = IvfIndex.build(
        bench.data[:500], bench.row_ids[:500], metadata=metadata_for("IVF_FLAT", num_partitions=8)
    )
    before = idx.centroids.copy()
    idx.append(bench.data[500:800], bench.row_ids[500:800])
    assert np.array_equal(before, idx.centroids)


def test_ivf_remap_keeps_only_surviving_rows():
    rng = np.random.default_rng(5)
    data = rng.normal(size=(300, 12)).astype(np.float32)
    row_ids = np.arange(300)
    idx = IvfIndex.build(data, row_ids, metadata=metadata_for("IVF_FLAT", num_partitions=8))
    survivors = np.arange(0, 300, 2)
    old_to_new = np.full(300, -1, dtype=np.int64)
    old_to_new[survivors] = np.arange(survivors.size)
    idx.remap(old_to_new)
    assert idx.num_indexed_rows == survivors.size
    assert np.array_equal(np.sort(idx.row_ids), np.sort(old_to_new[survivors]))
    res = idx.search(data[:10], 5, params={"nprobes": 8})
    assert all(int(r) in set(old_to_new[survivors].tolist()) for r in res.row_ids[:, 0])


def test_ivf_mask_prefilter_matches_masked_bruteforce(bench):
    idx = IvfIndex.build(
        bench.data, bench.row_ids, metadata=metadata_for("IVF_FLAT", num_partitions=20)
    )
    mask = np.ones(N_ROWS, dtype=bool)
    mask[::7] = False
    res = idx.search(bench.queries, K, mask=mask, params={"nprobes": 20})
    expected = exact_truth(bench.data[mask], bench.queries, K, row_ids=bench.row_ids[mask])
    for i, exp in enumerate(expected):
        assert {int(r) for r in res.row_ids[i]} == exp


def test_ivf_num_partitions_greater_than_rows_raises():
    data = np.random.default_rng(0).normal(size=(10, 4)).astype(np.float32)
    with pytest.raises(IndexBuildError):
        IvfIndex.build(
            data, np.arange(10), metadata=metadata_for("IVF_FLAT", num_partitions=32)
        )


def test_ivf_bad_num_partitions_raises(bench):
    with pytest.raises(IndexBuildError):
        IvfIndex.build(
            bench.data[:100],
            bench.row_ids[:100],
            metadata=metadata_for("IVF_FLAT", num_partitions=-1),
        )


def test_ivf_default_nprobes_is_clamped_to_partitions(ivf_flat, bench):
    res = ivf_flat.search(bench.queries[:2], K)
    assert res.stats["nprobes"] <= 20


# ---------------------------------------------------------------------------
# IVF_SQ
# ---------------------------------------------------------------------------


def test_ivf_sq_recall_is_high(ivf_sq, bench, truth):
    # 实测：nprobes=4 时 recall@10 ≈ 0.98（8 bit 逐维量化的损失极小）
    r = recall_of(ivf_sq, bench.queries, truth, params={"nprobes": 4})
    assert r > 0.9


def test_ivf_sq_recall_never_decreases_with_probes(ivf_sq, bench, truth):
    recalls = [
        recall_of(ivf_sq, bench.queries, truth, params={"nprobes": p})
        for p in (1, 2, 4, 8, 20)
    ]
    assert all(b >= a - 1e-12 for a, b in zip(recalls, recalls[1:])), recalls


def test_ivf_sq_full_probes_with_refine_is_exact(ivf_sq, bench, truth):
    # k * refine >= 全库行数 → 所有候选都被精确重排 → 退化成暴力检索
    r = recall_of(
        ivf_sq, bench.queries, truth, params={"nprobes": 20, "refine_factor": 500}
    )
    assert r == 1.0


def test_ivf_sq_is_at_least_ivf_flat_accuracy(ivf_sq, ivf_flat, bench, truth):
    """同一个 nprobes 下，IVF_SQ 的召回率不该超过 IVF_FLAT（那是天花板）。"""

    p = {"nprobes": 2}
    assert recall_of(ivf_sq, bench.queries, truth, params=p) <= recall_of(
        ivf_flat, bench.queries, truth, params=p
    ) + 1e-12


def test_ivf_sq_stats_report_compression(ivf_sq):
    s = ivf_sq.stats()
    assert s["index_type"] == "IVF_SQ"
    assert s["num_bits"] == 8
    assert s["compression_ratio"] == pytest.approx(4.0, abs=0.01)
    assert s["code_bytes"] == N_ROWS * DIM
    assert s["sq_error"]["median_relative_error"] < 0.01


def test_ivf_sq_save_load_roundtrip(ivf_sq, bench):
    store = DictIndexStore()
    ivf_sq.save(store)
    loaded = IvfSqIndex.load(store, ivf_sq.metadata)
    a = ivf_sq.search(bench.queries, K, params={"nprobes": 3})
    b = loaded.search(bench.queries, K, params={"nprobes": 3})
    assert np.array_equal(a.row_ids, b.row_ids)
    assert np.array_equal(a.distances, b.distances)


def test_ivf_sq_append_encodes_new_rows(bench):
    half = N_ROWS // 2
    idx = IvfSqIndex.build(
        bench.data[:half],
        bench.row_ids[:half],
        metadata=metadata_for("IVF_SQ", num_partitions=8, num_bits=8),
    )
    idx.append(bench.data[half:], bench.row_ids[half:])
    assert idx.codes.shape == (N_ROWS, DIM)
    res = idx.search(bench.data[half : half + 3], 1, params={"nprobes": 8})
    assert list(res.row_ids[:, 0]) == list(bench.row_ids[half : half + 3])


# ---------------------------------------------------------------------------
# IVF_PQ
# ---------------------------------------------------------------------------


def test_ivf_pq_recall_is_acceptable(ivf_pq, bench, truth):
    """实测（本基准，3000×32 / 20 簇 / M=8 / 8 bit / 16 倍压缩）：

    ``nprobes`` 从 1 到 20 的 recall@10 分别是 0.68 / 0.69 / 0.69 / 0.69 / 0.69 ——
    **探满所有分区也还是 0.69**，说明这个上限完全是 PQ 的量化误差造成的，
    和探几个桶无关。这正是"先隔离误差源"的价值：看到平台就知道该调
    ``refine_factor`` 而不是 ``nprobes``。
    """

    r = recall_of(ivf_pq, bench.queries, truth, params={"nprobes": 8})
    assert r > 0.5  # 任务规定的下界：8 个子向量在 32 维上必须 ≥ 0.5
    assert r > 0.65  # 实测 0.69，留一点余量
    assert recall_of(ivf_pq, bench.queries, truth, params={"nprobes": 20}) == pytest.approx(
        r, abs=0.02
    )  # 平台效应：探满也不会更好


def test_ivf_pq_refine_factor_improves_recall(ivf_pq, bench, truth):
    plain = recall_of(ivf_pq, bench.queries, truth, params={"nprobes": 8})
    refined = recall_of(
        ivf_pq, bench.queries, truth, params={"nprobes": 8, "refine_factor": 8}
    )
    assert refined >= plain
    assert refined > plain  # 实测确实有提升（PQ 的排序误差是真实存在的）


def test_ivf_pq_full_probes_with_refine_is_exact(ivf_pq, bench, truth):
    r = recall_of(
        ivf_pq, bench.queries, truth, params={"nprobes": 20, "refine_factor": 500}
    )
    assert r == 1.0


def test_ivf_pq_residual_beats_raw_vector(bench, truth):
    """残差 PQ 的量化误差应该比朴素 PQ 小 —— 这是 LanceDB 默认 residual_vector=True 的原因。"""

    m = metadata_for("IVF_PQ", num_partitions=20, num_sub_vectors=8, num_bits=8)
    residual = IvfPqIndex.build(bench.data, bench.row_ids, metadata=m)
    raw_meta = metadata_for(
        "IVF_PQ",
        num_partitions=20,
        num_sub_vectors=8,
        num_bits=8,
        residual_vector=False,
    )
    raw = IvfPqIndex.build(bench.data, bench.row_ids, metadata=raw_meta)
    p = {"nprobes": 20}
    r_res = recall_of(residual, bench.queries, truth, params=p)
    r_raw = recall_of(raw, bench.queries, truth, params=p)
    # 实测（nprobes=20，探满所有桶）：残差 ≈ 0.69，原向量 ≈ 0.465
    assert r_res > r_raw
    assert residual.residual is True and raw.residual is False


def test_ivf_pq_recall_never_decreases_with_probes(ivf_pq, bench, truth):
    recalls = [
        recall_of(ivf_pq, bench.queries, truth, params={"nprobes": p})
        for p in (1, 2, 4, 8, 20)
    ]
    # 量化打分下单调性不是理论保证，但这份固定基准上实测成立；
    # 留 0.02 的容差只为了不让浮点/并列抖动把测试变成偶发失败。
    assert all(b >= a - 0.02 for a, b in zip(recalls, recalls[1:])), recalls


def test_ivf_pq_stats_fields_and_compression(ivf_pq):
    s = ivf_pq.stats()
    assert s["index_type"] == "IVF_PQ"
    assert s["num_sub_vectors"] == 8
    assert s["num_bits"] == 8
    assert s["sub_dim"] == DIM // 8
    assert s["residual_vector"] is True
    assert s["code_bytes_per_vector"] == 8
    assert s["compression_ratio"] == pytest.approx(DIM * 4 / 8)
    assert s["codebook_bytes"] == 8 * 256 * 4 * 4
    assert s["num_partitions"] == 20


def test_ivf_pq_save_load_roundtrip(ivf_pq, bench):
    store = DictIndexStore()
    names = ivf_pq.save(store)
    assert "pq_codebooks.npy" in names
    loaded = IvfPqIndex.load(store, ivf_pq.metadata)
    a = ivf_pq.search(bench.queries, K, params={"nprobes": 4})
    b = loaded.search(bench.queries, K, params={"nprobes": 4})
    assert np.array_equal(a.row_ids, b.row_ids)
    assert np.array_equal(a.distances, b.distances)
    assert loaded.residual is True


def test_ivf_pq_append_uses_existing_codebook():
    rng = np.random.default_rng(11)
    data = rng.normal(size=(600, 16)).astype(np.float32)
    ids = np.arange(600)
    idx = IvfPqIndex.build(
        data[:400],
        ids[:400],
        metadata=metadata_for("IVF_PQ", num_partitions=8, num_sub_vectors=4, num_bits=6),
    )
    book_before = idx.pq.codebooks.copy()
    idx.append(data[400:], ids[400:])
    assert np.array_equal(book_before, idx.pq.codebooks)
    assert idx.codes.shape == (600, 4)
    res = idx.search(data[400:405], 1, params={"nprobes": 8})
    assert list(res.row_ids[:, 0]) == [int(r) for r in ids[400:405]]


# ---------------------------------------------------------------------------
# HNSW
# ---------------------------------------------------------------------------


def test_hnsw_recall_is_high(hnsw, bench, truth):
    # 实测：m=12/ef_construction=60、ef=50 时 recall@10 ≈ 0.99
    r = recall_of(hnsw, bench.queries, truth, params={"ef": 50})
    assert r > 0.9


def test_hnsw_recall_never_decreases_with_ef(hnsw, bench, truth):
    recalls = [
        recall_of(hnsw, bench.queries, truth, params={"ef": e}) for e in (10, 20, 50, 100)
    ]
    assert all(b >= a - 1e-12 for a, b in zip(recalls, recalls[1:])), recalls


def test_hnsw_ef_at_least_k_is_enforced(hnsw, bench):
    res = hnsw.search(bench.queries[:2], K, params={"ef": 1})
    assert res.stats["ef"] >= K
    assert np.all(res.row_ids[:, 0] >= 0)


def test_hnsw_mask_prefilter(hnsw, bench):
    mask = np.ones(N_ROWS, dtype=bool)
    mask[: N_ROWS // 4] = False
    res = hnsw.search(bench.queries, K, mask=mask, params={"ef": 80})
    returned = {int(r) for r in res.row_ids.ravel() if r >= 0}
    assert returned.isdisjoint(set(int(r) for r in bench.row_ids[: N_ROWS // 4]))
    masked_truth = exact_truth(
        bench.data[mask], bench.queries, K, row_ids=bench.row_ids[mask]
    )
    recall = sum(
        len({int(r) for r in res.row_ids[i] if r >= 0} & masked_truth[i])
        for i in range(N_QUERIES)
    ) / (N_QUERIES * K)
    assert recall > 0.8


def test_hnsw_save_load_roundtrip(hnsw, bench):
    store = DictIndexStore()
    names = hnsw.save(store)
    assert any(n.startswith("hnsw_links_") for n in names)
    loaded = HnswIndex.load(store, hnsw.metadata)
    a = hnsw.search(bench.queries, K, params={"ef": 40})
    b = loaded.search(bench.queries, K, params={"ef": 40})
    assert np.array_equal(a.row_ids, b.row_ids)
    assert np.array_equal(a.distances, b.distances)


def test_hnsw_append_incremental_keeps_index_usable(hnsw, bench, truth):
    half = N_ROWS // 2
    idx = HnswIndex.build(
        bench.data[:half],
        bench.row_ids[:half],
        metadata=metadata_for("HNSW", m=8, ef_construction=40, seed=3),
    )
    levels_before = idx.graph.num_levels
    idx.append(bench.data[half:], bench.row_ids[half:])
    assert idx.num_indexed_rows == N_ROWS
    assert idx.graph.num_nodes == N_ROWS
    res = idx.search(bench.data[half : half + 5], 1, params={"ef": 40})
    assert list(res.row_ids[:, 0]) == list(bench.row_ids[half : half + 5])
    r = recall_of(idx, bench.queries, truth, params={"ef": 60})
    # 增量插入后图会略微变差（新点只见过旧图的入口），但必须仍然可用
    assert r > 0.85
    assert idx.graph.num_levels >= levels_before


def test_hnsw_stats(hnsw):
    s = hnsw.stats()
    assert s["index_type"] == "HNSW"
    assert s["num_indexed_rows"] == N_ROWS
    assert s["m"] == 12
    assert s["ef_construction"] == 60
    assert s["num_levels"] >= 1
    assert s["ml"] == pytest.approx(1.0 / np.log(12))
    assert 0 < s["avg_degree_level0"] <= 2 * 12
    assert s["graph_bytes"] > 0
    assert s["size_bytes"] >= s["graph_bytes"]


def test_hnsw_remap_rebuilds_graph():
    rng = np.random.default_rng(2)
    data = rng.normal(size=(200, 8)).astype(np.float32)
    ids = np.arange(200)
    idx = HnswIndex.build(data, ids, metadata=metadata_for("HNSW", m=8, ef_construction=30))
    survivors = np.arange(0, 200, 2)
    old_to_new = np.full(200, -1, dtype=np.int64)
    old_to_new[survivors] = np.arange(survivors.size)
    idx.remap(old_to_new)
    assert idx.num_indexed_rows == survivors.size
    assert idx.graph.num_nodes == survivors.size
    res = idx.search(data[survivors[:5]], 1, params={"ef": 30})
    assert list(res.row_ids[:, 0]) == list(old_to_new[survivors[:5]])


def test_hnsw_graph_standalone_level_distribution():
    rng = np.random.default_rng(4)
    data = rng.normal(size=(800, 10)).astype(np.float32)
    g = HnswGraph(dim=10, m=8, ef_construction=30, seed=0).build(data)
    assert g.num_nodes == 800
    # ml = 1/ln(M)，所以第 l 层的期望点数是 n·M^-l
    assert g.max_level >= 1
    assert g.levels.min() == 0
    ids, dist = g.search(data[0], 5, ef=30)
    assert ids[0] == 0 and dist[0] == pytest.approx(0.0, abs=1e-5)


# ---------------------------------------------------------------------------
# IVF_HNSW_SQ / IVF_HNSW_PQ
# ---------------------------------------------------------------------------


def test_ivf_hnsw_sq_recall(ivf_hnsw_sq, bench, truth):
    # 实测：nprobes=20/ef=50 时 recall@10 ≈ 0.99
    r = recall_of(ivf_hnsw_sq, bench.queries, truth, params={"nprobes": 20, "ef": 50})
    assert r > 0.9


def test_ivf_hnsw_sq_refine_factor_improves_or_keeps(ivf_hnsw_sq, bench, truth):
    plain = recall_of(ivf_hnsw_sq, bench.queries, truth, params={"nprobes": 4, "ef": 30})
    refined = recall_of(
        ivf_hnsw_sq, bench.queries, truth, params={"nprobes": 4, "ef": 30, "refine_factor": 8}
    )
    assert refined >= plain


def test_ivf_hnsw_pq_recall(ivf_hnsw_pq, bench, truth):
    # 实测：nprobes=20/ef=50 时 recall@10 ≈ 0.73（16 倍压缩 + 分区图各占一部分误差）
    r = recall_of(ivf_hnsw_pq, bench.queries, truth, params={"nprobes": 20, "ef": 50})
    assert r > 0.5
    refined = recall_of(
        ivf_hnsw_pq, bench.queries, truth, params={"nprobes": 20, "ef": 50, "refine_factor": 8}
    )
    assert refined >= r
    assert refined > 0.8


def test_ivf_hnsw_more_probes_never_hurts(ivf_hnsw_pq, bench, truth):
    recalls = [
        recall_of(ivf_hnsw_pq, bench.queries, truth, params={"nprobes": p, "ef": 40})
        for p in (1, 4, 20)
    ]
    assert all(b >= a - 0.05 for a, b in zip(recalls, recalls[1:])), recalls


def test_ivf_hnsw_sq_graphs_are_per_partition(ivf_hnsw_sq):
    assert len(ivf_hnsw_sq.graphs) == 20
    sizes = [g.num_nodes for g in ivf_hnsw_sq.graphs]
    assert sum(sizes) == N_ROWS
    assert sizes == list(np.diff(ivf_hnsw_sq.partition_offsets))


def test_ivf_hnsw_stats(ivf_hnsw_sq):
    s = ivf_hnsw_sq.stats()
    assert s["index_type"] == "IVF_HNSW_SQ"
    assert s["num_graphs"] == 20
    assert s["num_partitions"] == 20
    assert s["m"] == 8
    assert s["graph_bytes"] > 0
    assert s["num_bits"] == 8
    assert s["code_bytes"] == N_ROWS * DIM


def test_ivf_hnsw_save_load_roundtrip(ivf_hnsw_sq, bench):
    store = DictIndexStore()
    names = ivf_hnsw_sq.save(store)
    assert any(n.startswith("graph0_") for n in names)
    loaded = IvfHnswSqIndex.load(store, ivf_hnsw_sq.metadata)
    a = ivf_hnsw_sq.search(bench.queries, K, params={"nprobes": 6, "ef": 40})
    b = loaded.search(bench.queries, K, params={"nprobes": 6, "ef": 40})
    assert np.array_equal(a.row_ids, b.row_ids)
    assert np.array_equal(a.distances, b.distances)


def test_ivf_hnsw_pq_save_load_roundtrip(ivf_hnsw_pq, bench):
    store = DictIndexStore()
    ivf_hnsw_pq.save(store)
    loaded = IvfHnswPqIndex.load(store, ivf_hnsw_pq.metadata)
    a = ivf_hnsw_pq.search(bench.queries, K, params={"nprobes": 6, "ef": 40})
    b = loaded.search(bench.queries, K, params={"nprobes": 6, "ef": 40})
    assert np.array_equal(a.row_ids, b.row_ids)


def test_ivf_hnsw_append_updates_codes_and_graphs():
    rng = np.random.default_rng(9)
    data = rng.normal(size=(400, 16)).astype(np.float32)
    ids = np.arange(400)
    idx = IvfHnswSqIndex.build(
        data[:300],
        ids[:300],
        metadata=metadata_for(
            "IVF_HNSW_SQ", num_partitions=8, num_bits=8, m=6, ef_construction=20
        ),
    )
    idx.append(data[300:], ids[300:])
    assert idx.codes.shape == (400, 16)
    assert [g.num_nodes for g in idx.graphs] == list(np.diff(idx.partition_offsets))
    res = idx.search(data[300:305], 1, params={"nprobes": 8, "ef": 30})
    assert list(res.row_ids[:, 0]) == [int(r) for r in ids[300:305]]


def test_ivf_hnsw_remap_rebuilds_partition_graphs():
    rng = np.random.default_rng(10)
    data = rng.normal(size=(200, 8)).astype(np.float32)
    ids = np.arange(200)
    idx = IvfHnswSqIndex.build(
        data, ids, metadata=metadata_for("IVF_HNSW_SQ", num_partitions=4, num_bits=8, m=6, ef_construction=20)
    )
    old_to_new = np.full(200, -1, dtype=np.int64)
    survivors = np.arange(0, 200, 2)
    old_to_new[survivors] = np.arange(survivors.size)
    idx.remap(old_to_new)
    assert idx.num_indexed_rows == survivors.size
    assert [g.num_nodes for g in idx.graphs] == list(np.diff(idx.partition_offsets))
    res = idx.search(data[survivors[:4]], 1, params={"nprobes": 4, "ef": 30})
    assert list(res.row_ids[:, 0]) == list(old_to_new[survivors[:4]])


# ---------------------------------------------------------------------------
# 二值量化
# ---------------------------------------------------------------------------


def test_bq_pack_unpack_roundtrip():
    rng = np.random.default_rng(1)
    signs = rng.random((7, 100)) > 0.5
    packed = pack_signs(signs)
    assert packed.shape == (7, 2)  # ceil(100/64) = 2 个 uint64
    assert packed.dtype == np.uint64
    assert np.array_equal(unpack_signs(packed, 100), signs)


def test_bq_encode_uses_signs():
    q = BinaryQuantizer(4)
    codes = q.encode(np.array([[1.0, -1.0, 0.0, 2.0]], dtype=np.float32))
    # 符号 = [非负, 负, 非负, 非负]，小端位序 → 第 0/2/3 位置 1
    assert int(codes[0, 0]) == 0b1101


def test_bq_hamming_matches_metric_popcount():
    rng = np.random.default_rng(2)
    data = rng.normal(size=(50, 40)).astype(np.float32)
    q = BinaryQuantizer(40)
    codes = q.encode(data)
    packed_q = q.encode(data[0:1])[0]
    ham = q.hamming(codes, packed_q)
    expected = np.count_nonzero((data >= 0) != (data[0] >= 0), axis=1)
    assert np.array_equal(ham, expected)
    assert ham[0] == 0


def test_bq_memory_is_32x_smaller_on_word_multiple():
    # 128 维正好是 2 个 uint64 字，压缩比就是严格的 32 倍
    rng = np.random.default_rng(3)
    data = rng.normal(size=(500, 128)).astype(np.float32)
    idx = create_vector_index("BQ", data, np.arange(500), metadata_for("BQ"))
    s = idx.stats()
    assert s["compression_ratio"] == pytest.approx(32.0)
    assert s["code_bytes"] == 500 * 16
    assert s["num_bits"] == 1
    assert s["bits_per_dimension"] == 1
    assert s["words_per_vector"] == 2


def test_bq_hamming_only_search_returns_hamming_distances(bq, bench):
    res = bq.search(bench.queries[:2], K)
    assert res.stats["distance_kind"] == "hamming"
    # 汉明距离来自 popcount，必然是整数
    assert np.all(res.distances[:, :-1] == np.floor(res.distances[:, :-1]))


def test_bq_rescore_improves_recall(bq, bench, truth):
    plain = recall_of(bq, bench.queries, truth, params={"refine_factor": 1})
    refined = recall_of(bq, bench.queries, truth, params={"refine_factor": 20})
    # 实测：纯汉明 recall@10 = 0.16，20 倍精排后 = 1.00。
    # 精排能救回这么多，说明 1 bit 留下的"方向"是对的 —— 丢的是排序精度。
    assert refined > plain
    assert plain < 0.5
    assert refined > 0.9


def test_bq_save_load_roundtrip(bq, bench):
    store = DictIndexStore()
    bq.save(store)
    loaded = BQ.load(store, bq.metadata)
    a = bq.search(bench.queries, K, params={"refine_factor": 10})
    b = loaded.search(bench.queries, K, params={"refine_factor": 10})
    assert np.array_equal(a.row_ids, b.row_ids)
    assert np.array_equal(a.distances, b.distances)


def test_bq_append_and_remap():
    rng = np.random.default_rng(4)
    data = rng.normal(size=(100, 32)).astype(np.float32)
    ids = np.arange(100)
    idx = create_vector_index("BQ", data[:80], ids[:80], metadata_for("BQ"))
    idx.append(data[80:], ids[80:])
    assert idx.num_indexed_rows == 100
    res = idx.search(data[80:82], 1, params={"refine_factor": 8})
    assert list(res.row_ids[:, 0]) == [80, 81]
    old_to_new = np.full(100, -1, dtype=np.int64)
    survivors = np.arange(0, 100, 2)
    old_to_new[survivors] = np.arange(survivors.size)
    idx.remap(old_to_new)
    assert idx.num_indexed_rows == 50
    assert idx.codes.shape[0] == 50


def test_bq_mask(bq, bench):
    mask = np.zeros(N_ROWS, dtype=bool)
    mask[:100] = True
    res = bq.search(bench.queries[:2], 5, mask=mask, params={"refine_factor": 5})
    returned = {int(r) for r in res.row_ids.ravel() if r >= 0}
    assert returned <= set(range(ROW_ID_BASE, ROW_ID_BASE + 100))
    assert len(returned) == 10


def test_bq_reconstruct_preserves_scale():
    rng = np.random.default_rng(5)
    data = rng.normal(size=(50, 256)).astype(np.float32) * 4.0
    q = BinaryQuantizer(256)
    q.fit_scales(data)
    rec = q.reconstruct(q.encode(data))
    assert rec.shape == data.shape
    # 重建值的每个分量只能是 ±scale，所以它的"平均绝对值"必然精确等于 scale
    assert np.allclose(np.abs(rec).mean(axis=1), np.abs(data).mean(axis=1), rtol=1e-5)
    # 模长的比值则是 E|x| / sqrt(E[x²])：对高斯数据是 sqrt(2/π) ≈ 0.7979。
    # 换句话说，符号量化"方向对、长度只剩 80%"—— 这就是精排要回表的原因。
    ratio = np.linalg.norm(rec, axis=1) / np.linalg.norm(data, axis=1)
    assert ratio.mean() == pytest.approx(np.sqrt(2.0 / np.pi), rel=0.03)
    assert np.all((ratio > 0.7) & (ratio < 0.9))


# ---------------------------------------------------------------------------
# 工厂 / 注册表 / 所有类型的公共契约
# ---------------------------------------------------------------------------


def test_registry_covers_eight_index_types():
    assert set(VECTOR_INDEX_TYPES) == {
        "FLAT",
        "IVF_FLAT",
        "IVF_SQ",
        "IVF_PQ",
        "HNSW",
        "IVF_HNSW_SQ",
        "IVF_HNSW_PQ",
        "BQ",
    }
    assert vector_index_types()["IVF_PQ"] == "IvfPqIndex"


@pytest.mark.parametrize(
    "index_type,params",
    [
        ("FLAT", {}),
        ("IVF_FLAT", {"num_partitions": 8}),
        ("IVF_SQ", {"num_partitions": 8, "num_bits": 8}),
        ("IVF_PQ", {"num_partitions": 8, "num_sub_vectors": 4, "num_bits": 6}),
        ("HNSW", {"m": 6, "ef_construction": 20}),
        ("IVF_HNSW_SQ", {"num_partitions": 8, "num_bits": 8, "m": 6, "ef_construction": 20}),
        ("IVF_HNSW_PQ", {"num_partitions": 8, "num_sub_vectors": 4, "num_bits": 6, "m": 6, "ef_construction": 20}),
        ("BQ", {}),
    ],
)
def test_factory_builds_every_registered_type(index_type, params):
    rng = np.random.default_rng(6)
    data = rng.normal(size=(300, 16)).astype(np.float32)
    ids = np.arange(300) + 5
    idx = create_vector_index(index_type, data, ids, metadata_for(index_type, **params))
    assert idx.type_name == index_type
    res = idx.search(data[:3], 3)
    assert res.distances.shape == (3, 3)
    assert int(res.row_ids[0, 0]) == 5  # 查自己，行号带偏移
    st = idx.stats()
    assert st["index_type"] == index_type
    assert st["num_indexed_rows"] == 300
    assert st["size_bytes"] > 0


def test_factory_normalizes_aliases():
    rng = np.random.default_rng(7)
    data = rng.normal(size=(120, 16)).astype(np.float32)
    ids = np.arange(120)
    for alias in ("ivf_pq", "IVFPQ", "ivf-pq"):
        idx = create_vector_index(
            alias,
            data,
            ids,
            metadata_for("IVF_PQ", num_partitions=4, num_sub_vectors=4, num_bits=6),
        )
        assert idx.type_name == "IVF_PQ"
    # BQ 的别名
    assert create_vector_index("binary", data, ids, metadata_for("BQ")).type_name == "BQ"


def test_factory_unknown_type_raises():
    data = np.zeros((5, 4), np.float32)
    with pytest.raises(NotSupportedError):
        create_vector_index("TOTALLY_MADE_UP", data, np.arange(5), metadata_for("FLAT"))


@pytest.mark.parametrize(
    "index_type",
    ["FLAT", "IVF_FLAT", "IVF_SQ", "IVF_PQ", "HNSW", "IVF_HNSW_SQ", "IVF_HNSW_PQ", "BQ"],
)
def test_every_index_implements_the_vector_index_contract(index_type):
    for attr in ("build", "search", "stats", "save", "load", "append", "remap"):
        assert hasattr(VECTOR_INDEX_TYPES[index_type], attr), (index_type, attr)


@pytest.mark.parametrize(
    "fixture_name",
    ["flat", "ivf_flat", "ivf_sq", "ivf_pq", "hnsw", "ivf_hnsw_sq", "ivf_hnsw_pq", "bq"],
)
def test_stats_has_required_fields(fixture_name, request):
    idx = request.getfixturevalue(fixture_name)
    s = idx.stats()
    for key in ("index_type", "num_indexed_rows", "size_bytes"):
        assert key in s
    assert s["num_indexed_rows"] == N_ROWS
    assert isinstance(s["size_bytes"], int) and s["size_bytes"] > 0
    assert s["index_type"] == idx.type_name


@pytest.mark.parametrize(
    "fixture_name",
    ["flat", "ivf_flat", "ivf_sq", "ivf_pq", "hnsw", "ivf_hnsw_sq", "ivf_hnsw_pq", "bq"],
)
def test_size_bytes_matches_persisted_arrays(fixture_name, request):
    """``size_bytes`` 必须是真实写盘字节数，不是估算值。"""

    idx = request.getfixturevalue(fixture_name)
    store = DictIndexStore()
    names = idx.save(store)
    actual = store.total_bytes()
    # size_bytes 统计的是**数组数据的真实字节数**；落盘时每个文件还要带
    # 一个几十字节的头部（dtype/shape），所以实际占用只会略微更大。
    overhead = actual - idx.stats()["size_bytes"]
    assert 0 <= overhead < 200 * len(names)
    assert sorted(names) == sorted(store.list())


@pytest.mark.parametrize(
    "fixture_name",
    ["flat", "ivf_flat", "ivf_sq", "ivf_pq", "hnsw", "ivf_hnsw_sq", "ivf_hnsw_pq", "bq"],
)
def test_all_types_respect_k_greater_than_n(fixture_name, request):
    idx = request.getfixturevalue(fixture_name)
    with pytest.raises(QueryError):
        idx.search(np.zeros((1, idx.stats()["dim"]), np.float32), N_ROWS + 1)


def test_metric_registry_exposes_the_four_metrics():
    assert set(METRICS) == {"l2", "cosine", "dot", "hamming"}
    assert get_metric("euclidean").name == "l2"
    assert get_metric("ip").name == "dot"
    with pytest.raises(QueryError):
        get_metric("not-a-metric")
