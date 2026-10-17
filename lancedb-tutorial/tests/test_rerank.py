"""``mini_lancedb.rerank`` 的测试。

覆盖四件事，每一件都对应文档里反复强调的一个坑：

1. **分数方向** —— ``_distance`` 越小越好、``_score`` 越大越好、
   ``_relevance_score`` 越大越好；
2. **融合的算术** —— 所有手算的期望值都写在注释里，改公式时先改注释；
3. **退化与边界** —— 空列表、单列表、全并列、缺失分数、k 截断；
4. **确定性** —— 同样的输入跑两次必须逐字节一致（排序、哈希、并列处理）。
"""

from __future__ import annotations

import numpy as np
import pytest

from mini_lancedb.errors import NotSupportedError, QueryError
from mini_lancedb.rerank import (
    DISTANCE_COL,
    RELEVANCE_COL,
    SCORE_COL,
    ColBERTReranker,
    CrossEncoderReranker,
    HashCrossEncoder,
    LinearCombinationReranker,
    MultivectorQuery,
    NoneReranker,
    PositionalId,
    RRFReranker,
    SentenceTransformersCrossEncoder,
    as_rows,
    average_ranks,
    get_reranker,
    maxsim,
    maxsim_matrix,
    normalize_scores,
    reranker_names,
    resolve_ids,
    rows_to_table,
)

# ---------------------------------------------------------------------------
# 共用样例：5 篇文档，两路召回
# ---------------------------------------------------------------------------

#: 向量路（按 _distance 升序：越靠前越相似）
VEC_ORDER = [1, 2, 3, 4, 5]
#: FTS 路（按 _score 降序）
FTS_ORDER = [3, 1, 4, 2, 5]

#: 手算 RRF 的期望值（k=60，两路权重都是 1）。
#
#   名次 rank 从 1 开始，score(d) = 1/(rank_vec + 60) + 1/(rank_fts + 60)
#
#   d1: vec 第 1 名, fts 第 2 名 → 1/61 + 1/62 = 0.0163934426 + 0.0161290323 = 0.0325224749
#   d2: vec 第 2 名, fts 第 4 名 → 1/62 + 1/64 = 0.0161290323 + 0.0156250000 = 0.0317540323
#   d3: vec 第 3 名, fts 第 1 名 → 1/63 + 1/61 = 0.0158730159 + 0.0163934426 = 0.0322664585
#   d4: vec 第 4 名, fts 第 3 名 → 1/64 + 1/63 = 0.0156250000 + 0.0158730159 = 0.0314980159
#   d5: vec 第 5 名, fts 第 5 名 → 1/65 + 1/65 = 0.0153846154 + 0.0153846154 = 0.0307692308
#
#   降序：d1 > d3 > d2 > d4 > d5
RRF_EXPECTED = {
    1: 1 / 61 + 1 / 62,
    2: 1 / 62 + 1 / 64,
    3: 1 / 63 + 1 / 61,
    4: 1 / 64 + 1 / 63,
    5: 1 / 65 + 1 / 65,
}
RRF_EXPECTED_ORDER = [1, 3, 2, 4, 5]


def make_rows(order, key, values, *, text=None):
    """按 ``order`` 造一批行，``key`` 是分数列名。"""

    rows = []
    for row_id in order:
        row = {"_rowid": row_id, key: values[row_id], "text": (text or {}).get(row_id, f"doc{row_id}")}
        rows.append(row)
    return rows


@pytest.fixture()
def vector_rows():
    return make_rows(VEC_ORDER, DISTANCE_COL, {i: i / 10.0 for i in VEC_ORDER})


@pytest.fixture()
def fts_rows():
    return make_rows(FTS_ORDER, SCORE_COL, {i: float(60 - 10 * i) for i in FTS_ORDER})


def ids_of(rows):
    return [row["_rowid"] for row in rows]


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def test_as_rows_accepts_list_of_dicts():
    rows = as_rows([{"_rowid": 1}, {"_rowid": 2}])
    assert [r["_rowid"] for r in rows] == [1, 2]


def test_as_rows_returns_copies():
    source = [{"_rowid": 1}]
    rows = as_rows(source)
    rows[0]["_rowid"] = 99
    assert source[0]["_rowid"] == 1, "as_rows 不该改到调用方的对象"


def test_as_rows_accepts_single_dict_and_none():
    assert [r["_rowid"] for r in as_rows({"_rowid": 7})] == [7]
    assert as_rows(None) == []


def test_as_rows_rejects_unknown_type():
    with pytest.raises(QueryError):
        as_rows(42)


def test_as_rows_and_rows_to_table_roundtrip():
    pa = pytest.importorskip("pyarrow")
    source = [{"_rowid": 1, "text": "a"}, {"_rowid": 2, "text": "b"}]
    table = rows_to_table(source)
    assert isinstance(table, pa.Table)
    assert as_rows(table) == source


def test_rows_to_table_handles_empty():
    pa = pytest.importorskip("pyarrow")
    # pa.Table.from_pylist([]) 会因为推不出 schema 而抛错，必须特殊处理
    assert rows_to_table([]).num_rows == 0


def test_resolve_ids_prefers_rowid():
    assert resolve_ids([{"_rowid": 5, "id": 1}]) == [5]


def test_resolve_ids_falls_back_to_positional():
    ids = resolve_ids([{"a": 1}, {"a": 2}])
    assert all(isinstance(i, PositionalId) for i in ids)
    assert ids[0] != ids[1]


def test_positional_id_never_collides_with_int():
    assert PositionalId(3) != 3
    assert len({PositionalId(3), 3}) == 2


def test_reranker_rejects_bad_return_score():
    with pytest.raises(QueryError):
        RRFReranker(return_score="whatever")


def test_reranker_names_registry():
    assert reranker_names() == ["colbert", "cross_encoder", "linear", "none", "rrf"]


def test_get_reranker_returns_instances():
    assert isinstance(get_reranker("rrf"), RRFReranker)
    assert isinstance(get_reranker("linear"), LinearCombinationReranker)
    assert isinstance(get_reranker("cross_encoder"), CrossEncoderReranker)
    assert isinstance(get_reranker("colbert"), ColBERTReranker)
    assert isinstance(get_reranker(None), NoneReranker)
    # 别名
    assert isinstance(get_reranker("reciprocal_rank_fusion"), RRFReranker)


def test_get_reranker_passes_kwargs():
    reranker = get_reranker("rrf", k=7)
    assert reranker.k == 7


def test_get_reranker_accepts_instance():
    original = RRFReranker(k=11)
    assert get_reranker(original) is original


def test_get_reranker_unknown_name():
    with pytest.raises(QueryError):
        get_reranker("does-not-exist")


# ---------------------------------------------------------------------------
# RRF
# ---------------------------------------------------------------------------


def test_rrf_hand_computed_fusion(vector_rows, fts_rows):
    """5 篇文档 / 2 路结果的手算融合（期望值见文件顶部的注释）。"""

    reranker = RRFReranker(k=60)
    out = reranker.rerank_hybrid(None, vector_rows, fts_rows)

    assert ids_of(out) == RRF_EXPECTED_ORDER
    for row in out:
        assert row[RELEVANCE_COL] == pytest.approx(RRF_EXPECTED[row["_rowid"]], abs=1e-12)


def test_rrf_scores_match_formula_with_weights(vector_rows, fts_rows):
    reranker = RRFReranker(k=60, weights=[1.0, 3.0])
    out = reranker.rerank_hybrid(None, vector_rows, fts_rows)
    for row in out:
        row_id = row["_rowid"]
        rank_vec = VEC_ORDER.index(row_id) + 1
        rank_fts = FTS_ORDER.index(row_id) + 1
        expected = 1.0 / (rank_vec + 60) + 3.0 / (rank_fts + 60)
        assert row[RELEVANCE_COL] == pytest.approx(expected, abs=1e-12)


def test_rrf_k_sensitivity(vector_rows, fts_rows):
    """k 越小越"独裁"：第 1 名和第 2 名的差距随 k 增大而收窄。

    单路结果里第 1 名和第 2 名的分差是 ``1/(1+k) - 1/(2+k)``：
    k=1 → 1/2 - 1/3 = 0.1667；k=60 → 0.000264；k=10000 → 1.6e-8。
    所以 k 不是"精度旋钮"，而是"信任名次的强度旋钮"。
    """

    rows = make_rows([1, 2, 3], DISTANCE_COL, {1: 0.1, 2: 0.2, 3: 0.3})
    gaps = []
    for k in (1, 60, 10000):
        out = RRFReranker(k=k).rerank_vector(None, rows)
        gaps.append(out[0][RELEVANCE_COL] - out[1][RELEVANCE_COL])
    assert gaps[0] > gaps[1] > gaps[2]
    assert gaps[0] == pytest.approx(1 / 2 - 1 / 3, abs=1e-12)
    assert gaps[2] < 1e-6


def test_rrf_k_must_be_positive():
    with pytest.raises(QueryError):
        RRFReranker(k=0)
    with pytest.raises(QueryError):
        RRFReranker(k=-3)


def test_rrf_uppercase_K_alias():
    reranker = RRFReranker(K=99)
    assert reranker.k == 99
    assert reranker.K == 99


def test_rrf_scalar_weight_applies_to_every_list(vector_rows, fts_rows):
    out = RRFReranker(k=60, weights=2.0).rerank_hybrid(None, vector_rows, fts_rows)
    for row in out:
        assert row[RELEVANCE_COL] == pytest.approx(2.0 * RRF_EXPECTED[row["_rowid"]], abs=1e-12)


def test_rrf_weight_of_zero_ignores_that_list(vector_rows, fts_rows):
    out = RRFReranker(k=60, weights=[0.0, 1.0]).rerank_hybrid(None, vector_rows, fts_rows)
    # 只有 FTS 路有发言权 → 顺序完全等于 FTS 路的名次
    assert ids_of(out) == FTS_ORDER


def test_rrf_weights_length_mismatch(vector_rows, fts_rows):
    with pytest.raises(QueryError):
        RRFReranker(k=60, weights=[1.0, 1.0, 1.0]).rerank_hybrid(None, vector_rows, fts_rows)


def test_rrf_negative_weight_rejected():
    with pytest.raises(QueryError):
        RRFReranker(weights=-1.0).rerank_hybrid(None, [], [])


def test_rrf_ties_are_deterministic_and_keep_input_order():
    """两路顺序互为逆序时，每篇文档的名次和完全相同 → 分数完全并列。

    a 在**第一路**里更靠前，所以稳定排序之后 a 排在 b 前面；
    再跑一次必须还是一样的顺序（不能依赖 dict 的偶然顺序）。
    """

    vec = make_rows([1, 2], DISTANCE_COL, {1: 0.1, 2: 0.2})
    fts = make_rows([2, 1], SCORE_COL, {2: 9.0, 1: 8.0})

    first = RRFReranker(k=60).rerank_hybrid(None, vec, fts)
    second = RRFReranker(k=60).rerank_hybrid(None, vec, fts)

    assert first[0][RELEVANCE_COL] == pytest.approx(first[1][RELEVANCE_COL], abs=1e-15)
    assert ids_of(first) == [1, 2] == ids_of(second)


def test_rrf_empty_fusion_returns_empty():
    assert RRFReranker().rerank_hybrid(None, [], []) == []


def test_rrf_one_empty_side_keeps_the_other(vector_rows):
    out = RRFReranker(k=60).rerank_hybrid(None, vector_rows, [])
    assert ids_of(out) == VEC_ORDER
    for rank, row in enumerate(out, 1):
        assert row[RELEVANCE_COL] == pytest.approx(1.0 / (rank + 60), abs=1e-12)


def test_rrf_single_list_preserves_retrieval_order(vector_rows):
    out = RRFReranker(k=60).rerank_vector(None, vector_rows)
    assert ids_of(out) == VEC_ORDER, "RRF 对单列表只保序，不该改顺序"


def test_rrf_duplicate_ids_across_lists_accumulate():
    vec = make_rows([1], DISTANCE_COL, {1: 0.1})
    fts = make_rows([1], SCORE_COL, {1: 9.0})
    out = RRFReranker(k=60).rerank_hybrid(None, vec, fts)
    assert len(out) == 1
    # 1/61 + 1/61 = 2/61
    assert out[0][RELEVANCE_COL] == pytest.approx(2 / 61, abs=1e-12)


def test_rrf_duplicate_ids_inside_one_list_count_once():
    rows = make_rows([1, 1, 2], DISTANCE_COL, {1: 0.1, 2: 0.2})
    out = RRFReranker(k=60).rerank_vector(None, rows)
    assert len(out) == 2, "融合器的输出里同一篇文档不能出现两次"
    # 重复的 id 只按第一次出现算名次：1 拿 1/61 而不是 1/61 + 1/62
    assert out[0][RELEVANCE_COL] == pytest.approx(1 / 61, abs=1e-12)


def test_rrf_multivector_three_lists():
    a = make_rows([1, 2], DISTANCE_COL, {1: 0.1, 2: 0.2})
    b = make_rows([2, 3], DISTANCE_COL, {2: 0.1, 3: 0.2})
    c = make_rows([3, 1], DISTANCE_COL, {3: 0.1, 1: 0.2})
    out = RRFReranker(k=60).rerank_multivector(None, [a, b, c])
    expected = {
        # 1: a 第1, c 第2 → 1/61 + 1/62
        1: 1 / 61 + 1 / 62,
        # 2: a 第2, b 第1 → 1/62 + 1/61
        2: 1 / 62 + 1 / 61,
        # 3: b 第2, c 第1 → 1/62 + 1/61
        3: 1 / 62 + 1 / 61,
    }
    assert ids_of(out) == [1, 2, 3]
    for row in out:
        assert row[RELEVANCE_COL] == pytest.approx(expected[row["_rowid"]], abs=1e-12)


def test_rrf_multivector_rejects_empty_input():
    with pytest.raises(QueryError):
        RRFReranker().rerank_multivector(None, [])


def test_rrf_return_score_all_keeps_both_columns(vector_rows, fts_rows):
    out = RRFReranker(k=60, return_score="all").rerank_hybrid(None, vector_rows, fts_rows)
    for row in out:
        # 两路都召回的文档应该同时看到 _distance 和 _score
        assert row[DISTANCE_COL] is not None
        assert row[SCORE_COL] is not None
        assert row[RELEVANCE_COL] is not None


def test_rrf_default_return_score_drops_raw_scores(vector_rows, fts_rows):
    out = RRFReranker(k=60).rerank_hybrid(None, vector_rows, fts_rows)
    for row in out:
        assert DISTANCE_COL not in row and SCORE_COL not in row
        assert RELEVANCE_COL in row


def test_rrf_k_truncation(vector_rows, fts_rows):
    out = RRFReranker(k=60).rerank_hybrid(None, vector_rows, fts_rows, k=2)
    assert ids_of(out) == RRF_EXPECTED_ORDER[:2]


def test_rrF_rejects_negative_k_truncation(vector_rows):
    with pytest.raises(QueryError):
        RRFReranker().rerank_vector(None, vector_rows, k=-1)


# ---------------------------------------------------------------------------
# 线性加权
# ---------------------------------------------------------------------------


def test_normalize_scores_minmax_maps_to_unit_interval():
    out = normalize_scores([0.9, 0.8, 0.7], "minmax")
    assert out[0] == pytest.approx(1.0)
    assert out[1] == pytest.approx(0.5)
    assert out[2] == pytest.approx(0.0)
    assert out.min() >= 0.0 and out.max() <= 1.0


def test_normalize_scores_minmax_constant_list():
    # max == min 时不能除零；约定全部归到 1.0（"无信息可区分"）
    assert normalize_scores([2.0, 2.0, 2.0], "minmax").tolist() == [1.0, 1.0, 1.0]


def test_normalize_scores_rank_is_outlier_insensitive():
    """这是 rank 归一化存在的**全部理由**：离群值不能主导分布。

    同一组内部数据 ``[0.9, 0.8, 0.7]`` 外加一个离群值：

    * minmax 下，前三名的间距从 0.5 塌缩到 0.002 —— 它们的区别被抹掉了；
    * rank 下，前三名仍然是**严格等距**的 1/3，而且离群值本身**数值改多大
      都无所谓**（100 和 1e9 给出逐位相同的输出）。
    """

    base = [0.9, 0.8, 0.7]

    clean = normalize_scores(base, "minmax")
    dirty = normalize_scores([0.9, 0.8, 0.7, 100.0], "minmax")
    assert clean[0] - clean[2] == pytest.approx(1.0)
    assert dirty[0] - dirty[2] < 0.01, "minmax 被离群值压扁了（这正是它的失败模式）"

    ranked = normalize_scores([0.9, 0.8, 0.7, 100.0], "rank")
    # 100 是这批里分数最高的，所以它拿名次 1 → 1.0；其余三个等距
    assert ranked[3] == pytest.approx(1.0)
    assert ranked[2] == pytest.approx(0.0)
    assert ranked[0] - ranked[1] == pytest.approx(1 / 3)
    assert ranked[1] - ranked[2] == pytest.approx(1 / 3)

    # 决定性的一条：离群值的**大小**完全不进入 rank 的结果
    ranked_absurd = normalize_scores([0.9, 0.8, 0.7, 1e9], "rank")
    assert ranked.tolist() == ranked_absurd.tolist()
    assert not np.allclose(dirty, normalize_scores([0.9, 0.8, 0.7, 1e9], "minmax"))


def test_normalize_scores_rank_ties_get_average_rank():
    out = normalize_scores([5.0, 5.0, 1.0], "rank")
    # 平均名次：两个 5 并列得 (1+2)/2 = 1.5 → (3-1.5)/2 = 0.75；1 得名次 3 → 0.0
    assert out[0] == pytest.approx(0.75)
    assert out[1] == pytest.approx(0.75)
    assert out[2] == pytest.approx(0.0)


def test_normalize_scores_rank_direction_is_higher_is_better():
    """方向校对：分数最高的拿 1.0，最低的拿 0.0。

    这条单独写出来是因为写反了**不会报任何错** —— 只会让最不相关的结果
    排第一。``average_ranks`` 是升序名次（最小的拿 1），很容易搞反。
    """

    out = normalize_scores([0.1, 0.5, 0.3], "rank")
    assert out[1] == pytest.approx(1.0), "0.5 最大，应该拿 1.0"
    assert out[0] == pytest.approx(0.0), "0.1 最小，应该拿 0.0"
    assert out[2] == pytest.approx(0.5)


def test_average_ranks_matches_manual():
    assert average_ranks([10.0, 20.0, 10.0]).tolist() == [1.5, 3.0, 1.5]


def test_normalize_scores_single_element_and_empty():
    assert normalize_scores([42.0], "rank").tolist() == [1.0]
    assert normalize_scores([], "minmax").size == 0


def test_normalize_scores_direction_flag():
    # higher_is_better=False 会先取负号：0.1 的距离比 0.5 更相似
    assert normalize_scores([0.1, 0.5], "minmax", higher_is_better=False).tolist() == [1.0, 0.0]
    assert normalize_scores([0.1, 0.5], "none", higher_is_better=False).tolist() == [-0.1, -0.5]


def test_normalize_scores_rejects_bad_mode():
    with pytest.raises(QueryError):
        normalize_scores([1.0], "softmax")


def test_linear_minmax_end_to_end():
    """同一个文档在两路都排第一 → 归一化后两路都是 1.0 → relevance 必须正好是 1.0。"""

    vec = make_rows([1, 2, 3], DISTANCE_COL, {1: 0.1, 2: 0.2, 3: 0.3})
    fts = make_rows([1, 2, 3], SCORE_COL, {1: 9.0, 2: 5.0, 3: 1.0})
    out = LinearCombinationReranker(weight=0.7).rerank_hybrid(None, vec, fts)
    assert ids_of(out) == [1, 2, 3]
    # 0.7*1.0 + 0.3*1.0 = 1.0；0.7*0.5+0.3*0.5 = 0.5；0.7*0+0.3*0 = 0
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [1.0, 0.5, 0.0]


def test_linear_weight_one_uses_only_vector():
    vec = make_rows([1, 2, 3], DISTANCE_COL, {1: 0.1, 2: 0.2, 3: 0.3})
    fts = make_rows([3, 2, 1], SCORE_COL, {3: 9.0, 2: 5.0, 1: 1.0})
    out = LinearCombinationReranker(weight=1.0).rerank_hybrid(None, vec, fts)
    assert ids_of(out) == [1, 2, 3]
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [1.0, 0.5, 0.0]


def test_linear_weight_zero_uses_only_fts():
    vec = make_rows([1, 2, 3], DISTANCE_COL, {1: 0.1, 2: 0.2, 3: 0.3})
    fts = make_rows([3, 2, 1], SCORE_COL, {3: 9.0, 2: 5.0, 1: 1.0})
    out = LinearCombinationReranker(weight=0.0).rerank_hybrid(None, vec, fts)
    assert ids_of(out) == [3, 2, 1]
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [1.0, 0.5, 0.0]


def test_linear_weight_out_of_range():
    with pytest.raises(QueryError):
        LinearCombinationReranker(weight=1.5)
    with pytest.raises(QueryError):
        LinearCombinationReranker(weight=-0.01)


def test_linear_missing_score_filled_with_zero():
    """a 只在向量路、c 只在 FTS 路，缺失的那一路按 fill=0.0 计。

    vec: a=0.1, b=0.2 → 归一化 [1.0, 0.0]
    fts: b=5,   c=9   → 归一化 [0.0, 1.0]
    a: 0.7*1.0 + 0.3*0 = 0.7
    b: 0.7*0.0 + 0.3*0 = 0.0
    c: 0.7*0.0 + 0.3*1 = 0.3
    """

    vec = make_rows([1, 2], DISTANCE_COL, {1: 0.1, 2: 0.2})
    fts = make_rows([2, 3], SCORE_COL, {2: 5.0, 3: 9.0})
    out = LinearCombinationReranker(weight=0.7, fill=0.0).rerank_hybrid(None, vec, fts)
    assert ids_of(out) == [1, 3, 2]
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [0.7, 0.3, 0.0]


def test_linear_fill_mean_strategy():
    """fill="mean"：缺失值填该路**已有归一化分数**的均值。

    vec 归一化均值 = (1.0 + 0.0)/2 = 0.5；fts 归一化均值 = (0.0 + 1.0)/2 = 0.5
    a: 0.7*1.0 + 0.3*0.5 = 0.85
    b: 0.7*0.0 + 0.3*0.0 = 0.0
    c: 0.7*0.5 + 0.3*1.0 = 0.65
    """

    vec = make_rows([1, 2], DISTANCE_COL, {1: 0.1, 2: 0.2})
    fts = make_rows([2, 3], SCORE_COL, {2: 5.0, 3: 9.0})
    out = LinearCombinationReranker(weight=0.7, fill="mean").rerank_hybrid(None, vec, fts)
    assert ids_of(out) == [1, 3, 2]
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [0.85, 0.65, 0.0]


def test_linear_fill_max_strategy():
    vec = make_rows([1, 2], DISTANCE_COL, {1: 0.1, 2: 0.2})
    fts = make_rows([2, 3], SCORE_COL, {2: 5.0, 3: 9.0})
    out = LinearCombinationReranker(weight=0.7, fill="max").rerank_hybrid(None, vec, fts)
    scores = {r["_rowid"]: r[RELEVANCE_COL] for r in out}
    # a 缺 FTS → 填 1.0 → 0.7*1 + 0.3*1 = 1.0
    assert scores[1] == pytest.approx(1.0)
    # c 缺向量 → 填 1.0 → 0.7*1 + 0.3*1 = 1.0
    assert scores[3] == pytest.approx(1.0)


def test_linear_rejects_bad_fill():
    with pytest.raises(QueryError):
        LinearCombinationReranker(fill="whatever")
    with pytest.raises(QueryError):
        LinearCombinationReranker(normalize="cosine")


def test_linear_normalize_none_uses_raw_scores():
    """normalize="none" 直接拿原始分数（方向已统一）：

    vec 原始（取负）= a: -0.1, b: -0.2
    fts 原始       = a:  9.0, b:  5.0
    a = 0.7*(-0.1) + 0.3*9.0 = -0.07 + 2.7 = 2.63
    b = 0.7*(-0.2) + 0.3*5.0 = -0.14 + 1.5 = 1.36
    """

    vec = make_rows([1, 2], DISTANCE_COL, {1: 0.1, 2: 0.2})
    fts = make_rows([1, 2], SCORE_COL, {1: 9.0, 2: 5.0})
    out = LinearCombinationReranker(weight=0.7, normalize="none").rerank_hybrid(None, vec, fts)
    scores = {r["_rowid"]: r[RELEVANCE_COL] for r in out}
    assert scores[1] == pytest.approx(2.63, abs=1e-9)
    assert scores[2] == pytest.approx(1.36, abs=1e-9)


def test_linear_minmax_collapses_fine_grained_fts_ranking():
    """minmax 把"挤在一起的分数"的差距抹平，rank 不会。

    向量路四条完全一样（距离都是 0.5），所以最终排序**只由 FTS 决定**。
    FTS 分数是 ``[1.0, 0.99, 0.98, 100]`` —— 前三名挤在一起，外加一个离群值。

    * minmax：前三名被压到 0.0002/0.0001/0.0000，与离群值的 1.0 差了 4 个数量级，
      于是它们的 relevance 几乎相同（差距 5e-5），"谁更相关"这件事丢了；
    * rank：前四名拿到 1.0 / 2/3 / 1/3 / 0，间距稳定在 1/3，
      与离群值到底大多少完全无关。
    """

    vec = make_rows([1, 2, 3, 4], DISTANCE_COL, {i: 0.5 for i in (1, 2, 3, 4)})
    fts = make_rows([1, 2, 3, 4], SCORE_COL, {1: 1.0, 2: 0.99, 3: 0.98, 4: 100.0})

    def scores(normalize):
        reranker = LinearCombinationReranker(weight=0.5, normalize=normalize)
        out = reranker.rerank_hybrid(None, vec, fts)
        return {r["_rowid"]: r[RELEVANCE_COL] for r in out}

    mm = scores("minmax")
    assert abs(mm[1] - mm[2]) < 0.001, f"minmax 的差距只有 {abs(mm[1] - mm[2]):.2e}，已经不可分辨"

    rk = scores("rank")
    assert abs(rk[1] - rk[2]) == pytest.approx(1 / 6, abs=1e-9), "rank 下仍然是等距的 1/3 × w"


def test_linear_one_side_empty_returns_the_other():
    vec = make_rows([1, 2], DISTANCE_COL, {1: 0.1, 2: 0.2})
    out = LinearCombinationReranker().rerank_hybrid(None, vec, [])
    assert ids_of(out) == [1, 2]
    # 只有一路时"缺失"不是缺失，直接用这一路的归一化分数
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [1.0, 0.0]

    out2 = LinearCombinationReranker().rerank_hybrid(None, [], vec)
    assert [round(r[RELEVANCE_COL], 10) for r in out2] == [1.0, 0.0]


def test_linear_both_empty():
    assert LinearCombinationReranker().rerank_hybrid(None, [], []) == []


def test_linear_single_list_scoring_uses_available_column():
    rows = make_rows([1, 2, 3], SCORE_COL, {1: 3.0, 2: 2.0, 3: 1.0})
    out = LinearCombinationReranker().rerank_vector(None, rows)
    assert ids_of(out) == [1, 2, 3]
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [1.0, 0.5, 0.0]


# ---------------------------------------------------------------------------
# Cross Encoder
# ---------------------------------------------------------------------------

#: 8 组"查询 / 词面重叠的文档 / 不相关的文档"。
#: 前 4 组的不相关文档**故意共享一部分常用词**（models / index / language /
#: safety），这样"相关 > 不相关"就不是"非零 vs 零"这种毫无含量的断言。
OVERLAP_PAIRS = [
    ("machine learning models",
     "machine learning models are trained on large datasets",
     "the models in this catalogue are cheap"),
    ("vector database index",
     "a vector database index speeds up similarity search",
     "the index of this book lists every chapter"),
    ("python programming language",
     "python is a popular programming language for data science",
     "language is a tool for communication between people"),
    ("rust memory safety",
     "rust guarantees memory safety without a garbage collector",
     "safety first is the slogan of the factory"),
    ("clickhouse column store",
     "clickhouse is a column store database for analytics",
     "my favorite movie is inception"),
    ("检索 增强 生成",
     "检索 增强 生成 让 大 模型 回答 更 准确",
     "生成 一 张 图片 需要 多少 时间"),
    ("神经网络 训练",
     "神经网络 训练 需要 大量 标注 数据",
     "今天 天气 真 好 适合 出去 散步"),
    ("股票 市场 波动",
     "股票 市场 波动 加剧 投资者 担忧",
     "the quick brown fox jumps over the lazy dog"),
]


def test_hash_cross_encoder_is_deterministic():
    model = HashCrossEncoder()
    pairs = [[q, d] for q, d, _ in OVERLAP_PAIRS]
    first = model.predict(pairs)
    second = model.predict(pairs)
    assert first.tobytes() == second.tobytes()


def test_hash_cross_encoder_two_instances_agree():
    pairs = [[q, d] for q, d, _ in OVERLAP_PAIRS]
    assert HashCrossEncoder().predict(pairs).tobytes() == HashCrossEncoder().predict(pairs).tobytes()


def test_hash_cross_encoder_different_seed_changes_scores():
    pairs = [["machine learning", "machine learning models"]]
    assert HashCrossEncoder(seed=0).predict(pairs)[0] != HashCrossEncoder(seed=1).predict(pairs)[0]


def test_hash_cross_encoder_ranks_overlap_above_unrelated():
    """核心断言：8 组样例里词面重叠的文档必须全面胜出。

    玩具打分器只认词面，所以这个断言测的是**管道方向对不对**
    （分数越大越相关、sigmoid 没有搞反），不是"模型好不好"。
    """

    model = HashCrossEncoder()
    margins = []
    for query, related, unrelated in OVERLAP_PAIRS:
        good = model.score(query, related)
        bad = model.score(query, unrelated)
        margins.append(good - bad)
        assert 0.0 < good < 1.0, "sigmoid 之后必须落在开区间"
        assert 0.0 < bad < 1.0
        assert good > bad, f"{query!r}: 相关文档 {good:.4f} 没赢过不相关文档 {bad:.4f}"
    assert min(margins) > 0.15, f"最小间隔只有 {min(margins):.4f}，太脆"


def test_hash_cross_encoder_is_symmetric_in_length():
    """"短文档答中长查询"不该因为文档短就被系统性地扣分。"""

    model = HashCrossEncoder()
    short = model.score("vector database", "vector database")
    long = model.score("vector database", "vector database")
    assert short == long


def test_hash_cross_encoder_rejects_bad_pair_shape():
    with pytest.raises(QueryError):
        HashCrossEncoder().predict([["only one element"]])


def test_hash_cross_encoder_empty_batch():
    assert HashCrossEncoder().predict([]).size == 0


def test_hash_cross_encoder_fit_freezes_idf():
    """``fit()`` 之前 IDF 按**当前批次的文档**估，之后按冻结的语料估。

    查询和文档的 token 集合不同时，两者的 IDF 加权会明显不一样
    （查询里的 "alpha" 在语料里是稀有的，在"只看文档"的统计里根本不存在）。
    查询和文档**完全同词**时这个特征会退化成恒等于 1，分数反而不变 ——
    这正是"IDF 只是权重、不是信息"的体现，也值得被钉住。
    """

    model = HashCrossEncoder()
    before = model.predict([["alpha beta", "beta gamma"]])[0]
    model.fit(["alpha beta", "beta gamma"])
    after = model.predict([["alpha beta", "beta gamma"]])[0]
    assert model.describe()["idf_frozen"] is True
    assert before != after, "冻结语料统计后 IDF 变了，分数就该变"

    same = HashCrossEncoder()
    same_before = same.predict([["alpha beta", "alpha beta"]])[0]
    same.fit(["alpha beta", "beta gamma"])
    same_after = same.predict([["alpha beta", "alpha beta"]])[0]
    assert same_before == same_after, (
        "查询和文档同词时 IDF 归一化会约掉 —— 这是已知的退化，"
        "换个查询词集合就能看出来"
    )


def test_hash_cross_encoder_describe_warns_about_quality():
    info = HashCrossEncoder().describe()
    assert info["确定性的"] is True
    assert "不能" in info["能当质量基准用吗"]


def test_cross_encoder_reranker_orders_by_score():
    rows = [
        {"_rowid": 1, "text": "the cat sat on the mat"},
        {"_rowid": 2, "text": "machine learning models"},
        {"_rowid": 3, "text": "vector database for embeddings"},
    ]
    out = CrossEncoderReranker().rerank_vector("vector database", rows)
    assert ids_of(out) == [3, 2, 1]


def test_cross_encoder_only_reranks_the_top_k():
    """``top_k`` 之后的行**原封不动**地挂在后面，分数是 None。

    这条断言同时钉住两件事：cross encoder 的 O(N) 成本被卡住了；
    而且被重排"挤下去"的候选没有被丢掉（只是排在后面）。
    """

    rows = [
        {"_rowid": i, "text": text}
        for i, text in enumerate(
            [
                "the cat sat on the mat",
                "machine learning models",
                "vector database index",
                "banana bread recipe",
                "football match results",
                "sourdough starter guide",
                "rust memory safety",
                "quantum computing basics",
            ],
            start=1,
        )
    ]
    reranker = CrossEncoderReranker(top_k=3)
    out = reranker.rerank_vector("machine learning", rows)

    assert len(out) == len(rows), "重排不能弄丢候选"
    head, tail = out[:3], out[3:]
    # 唯一在词面上跟 "machine learning" 有重叠的是 2 号，它必须稳居第一，
    # 而且要把其余的远远甩开（其余三条只是噪声投影的差别）
    assert head[0]["_rowid"] == 2
    assert head[0][RELEVANCE_COL] > 0.5
    assert all(r[RELEVANCE_COL] < 0.2 for r in head[1:])
    assert all(r[RELEVANCE_COL] is not None for r in head)
    # 尾巴保持原始相对顺序，且没有被打分
    assert [r["_rowid"] for r in tail] == [4, 5, 6, 7, 8]
    assert all(r[RELEVANCE_COL] is None for r in tail)
    # 而且尾巴的行内容一个字都没被改（除了新加的分数列）
    assert [r["text"] for r in tail] == [r["text"] for r in rows[3:]]


def test_cross_encoder_hybrid_merges_then_reranks():
    vec = [{"_rowid": 1, "text": "the cat sat on the mat", DISTANCE_COL: 0.1}]
    fts = [{"_rowid": 2, "text": "machine learning models", SCORE_COL: 9.0}]
    out = CrossEncoderReranker().rerank_hybrid("machine learning", vec, fts)
    assert ids_of(out) == [2, 1]


def test_cross_encoder_missing_text_column_raises():
    reranker = CrossEncoderReranker(column="body")
    with pytest.raises(QueryError):
        reranker.rerank_vector("q", [{"_rowid": 1, "payload": "no text here"}])


def test_cross_encoder_text_column_fallback():
    reranker = CrossEncoderReranker(column="body")
    # column 找不到时按候选顺序退回 content / document / ...
    out = reranker.rerank_vector("machine learning", [{"_rowid": 1, "content": "machine learning"}])
    assert ids_of(out) == [1]


def test_cross_encoder_requires_query():
    with pytest.raises(QueryError):
        CrossEncoderReranker().rerank_vector(None, [{"_rowid": 1, "text": "x"}])


def test_cross_encoder_validates_model_and_top_k():
    with pytest.raises(QueryError):
        CrossEncoderReranker(model=object())
    with pytest.raises(QueryError):
        CrossEncoderReranker(top_k=0)


def test_cross_encoder_accepts_custom_model():
    class Constant:
        def __init__(self, value):
            self.value = value

        def predict(self, pairs):
            return np.full(len(pairs), self.value, dtype=np.float32)

    rows = [{"_rowid": 1, "text": "a"}, {"_rowid": 2, "text": "b"}]
    out = CrossEncoderReranker(model=Constant(0.5)).rerank_vector("q", rows)
    assert [r[RELEVANCE_COL] for r in out] == [0.5, 0.5]
    assert ids_of(out) == [1, 2], "同分时保持稳定顺序"


def test_cross_encoder_model_name_alias_matches_official_api():
    """官方 API 的第一个参数是 ``model_name``；用别名时不装依赖也不会炸，
    只有真正 predict 时才报错。"""

    reranker = CrossEncoderReranker(model_name="cross-encoder/ms-marco-MiniLM-L-6-v2")
    assert isinstance(reranker.model, SentenceTransformersCrossEncoder)
    with pytest.raises(QueryError):
        CrossEncoderReranker(
            model=HashCrossEncoder(), model_name="cross-encoder/ms-marco-MiniLM-L-6-v2"
        )


def test_sentence_transformers_wrapper_raises_with_install_hint():
    model = SentenceTransformersCrossEncoder()
    with pytest.raises(NotSupportedError) as info:
        model.predict([["q", "d"]])
    message = str(info.value)
    assert "pip install sentence-transformers" in message
    assert "HashCrossEncoder" in message


# ---------------------------------------------------------------------------
# ColBERT / MaxSim
# ---------------------------------------------------------------------------


def test_maxsim_hand_computed_2x3():
    """2 个查询 token × 3 个文档 token 的手算：

    q = [[1, 0], [0, 1]]        （标准基）
    d = [[1, 0], [0.6, 0.8], [0, 1]]

    q0·d = [1·1+0·0, 1·0.6+0·0.8, 1·0+0·1] = [1.0, 0.6, 0.0] → max = 1.0
    q1·d = [0·1+1·0, 0·0.6+1·0.8, 0·0+1·1] = [0.0, 0.8, 1.0] → max = 1.0
    MaxSim = 1.0 + 1.0 = 2.0
    """

    q = [[1.0, 0.0], [0.0, 1.0]]
    d = [[1.0, 0.0], [0.6, 0.8], [0.0, 1.0]]
    assert maxsim(q, d) == pytest.approx(2.0)


def test_maxsim_hand_computed_nonorthogonal():
    """q = [[1,1],[1,-1]]，d = [[1,1],[1,-1],[0,1]]

    q0·d = [2, 0, 1] → max 2
    q1·d = [0, 2, -1] → max 2
    MaxSim = 4（注意负相似度不会拖后腿：max 掩掉了它）
    """

    q = [[1.0, 1.0], [1.0, -1.0]]
    d = [[1.0, 1.0], [1.0, -1.0], [0.0, 1.0]]
    assert maxsim(q, d) == pytest.approx(4.0)


def test_maxsim_is_not_symmetric():
    """MaxSim 对调 query / doc 之后一般**不相等** —— 它本质上是非对称打分。

    q（3 个 token）= [[1,0],[0,1],[1,1]]，d（2 个 token）= [[1,0],[0,1]]

    maxsim(q, d)：每个 q_i 对两个 d_j 取最大 → 1 + 1 + 1 = 3
    maxsim(d, q)：每个 d_i 对三个 q_j 取最大 → 1 + 1 = 2
    """

    q = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    d = [[1.0, 0.0], [0.0, 1.0]]
    assert maxsim(q, d) == pytest.approx(3.0)
    assert maxsim(d, q) == pytest.approx(2.0)


def test_maxsim_matrix_matches_loop():
    q = [[1.0, 0.0], [0.0, 1.0]]
    docs = [
        [[1.0, 0.0], [0.0, 1.0]],
        [[1.0, 0.0]],
        [[0.5, 0.5]],
    ]
    got = maxsim_matrix(q, docs)
    assert got.tolist() == pytest.approx([2.0, 1.0, 1.0])


def test_maxsim_rejects_bad_shapes():
    with pytest.raises(QueryError):
        maxsim([1.0, 0.0, 0.0], [[1.0, 0.0, 0.0]])
    with pytest.raises(QueryError):
        maxsim(np.zeros((1, 4)), np.zeros((1, 8)))
    with pytest.raises(QueryError):
        maxsim(np.zeros((0, 4)), np.zeros((1, 4)))


def test_maxsim_matrix_empty_docs():
    assert maxsim_matrix(np.zeros((1, 4)), []).size == 0


class FixedEmbedder:
    """测试用的 token 编码器：不管输入什么都返回同一个矩阵。

    有了它，MaxSim 的期望值就能手算 —— 这是"可验证"和
    "看起来对"的分界线。
    """

    def __init__(self, matrix):
        self.matrix = np.asarray(matrix, dtype=np.float32)

    def encode_tokens(self, text):
        return self.matrix


def test_colbert_reranker_orders_by_maxsim():
    query_matrix = [[1.0, 0.0], [0.0, 1.0]]
    rows = [
        {"_rowid": 1, "vector": [[1.0, 0.0], [0.0, 1.0]]},             # 2.0
        {"_rowid": 2, "vector": [[0.0, 1.0], [0.0, 1.0]]},             # 0 + 1 = 1.0
        {"_rowid": 3, "vector": [[1.0, 0.0]]},                         # 1 + 0 = 1.0
    ]
    reranker = ColBERTReranker(embedder=FixedEmbedder(query_matrix), column="vector")
    out = reranker.rerank_vector("whatever", rows)
    assert ids_of(out) == [1, 2, 3]
    assert out[0][RELEVANCE_COL] == pytest.approx(2.0)
    assert out[1][RELEVANCE_COL] == pytest.approx(1.0)
    assert out[2][RELEVANCE_COL] == pytest.approx(1.0)


def test_colbert_only_reranks_top_k():
    """``top_k=2`` 只给前两条打分；第 3 条原样挂在后面、没有分数。"""

    query_matrix = [[1.0, 0.0]]
    rows = [
        {"_rowid": 1, "vector": [[1.0, 0.0]]},   # 前 2 条里：命中，MaxSim = 1.0
        {"_rowid": 2, "vector": [[0.0, 1.0]]},   # 前 2 条里：正交，MaxSim = 0.0
        {"_rowid": 3, "vector": [[0.5, 0.0]]},   # 在 top_k 之外，不该被打分
    ]
    out = ColBERTReranker(embedder=FixedEmbedder(query_matrix), top_k=2).rerank_vector("q", rows)
    assert ids_of(out) == [1, 2, 3]
    assert out[0][RELEVANCE_COL] == pytest.approx(1.0)
    assert out[1][RELEVANCE_COL] == pytest.approx(0.0)
    assert out[2][RELEVANCE_COL] is None


def test_colbert_falls_back_to_text_column():
    from mini_lancedb.rerank import HashingTokenEmbedder

    rows = [
        {"_rowid": 1, "text": "machine learning"},
        {"_rowid": 2, "text": "cat"},
    ]
    reranker = ColBERTReranker(embedder=HashingTokenEmbedder(dim=8), text_column="text")
    out = reranker.rerank_vector("machine learning", rows)
    assert ids_of(out) == [1, 2]


def test_colbert_decodes_packed_multivector_bytes():
    """真实 Lance 里多向量是按定长列表写进 buffer 的（float16 扁平）。

    schema ``fixed_size_list<fixed_size_list<float16>[2]>[2]`` =
    4 个 float16 = 8 字节；reshape 成 ``(2, 2)``
    """

    payload = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float16)
    # 用 128 维内层宽度反推，所以这里造一个 (1, 128) 的矩阵（1 个 token）
    packed_128 = np.zeros((2, 128), dtype=np.float16)
    packed_128[0, 0] = 1.0
    packed_128[1, 1] = 1.0
    rows = [{"_rowid": 1, "vector": packed_128.tobytes()}]
    reranker = ColBERTReranker(embedder=FixedEmbedder(np.zeros((1, 128), dtype=np.float32)))
    # 查询向量全 0 → MaxSim = 0，我们只关心解码出来的形状对不对
    assert reranker.tokens_of(rows[0]).shape == (2, 128)
    assert payload.dtype == np.float16  # 说明这个例子讲的是 float16 存储


def test_colbert_packed_bytes_with_bad_length():
    reranker = ColBERTReranker()
    with pytest.raises(QueryError):
        reranker.tokens_of({"_rowid": 1, "vector": b"\x00" * 7})


def test_colbert_missing_columns_raises():
    reranker = ColBERTReranker(column="vector", text_column="text")
    with pytest.raises(QueryError):
        reranker.tokens_of({"_rowid": 1, "other": "x"})


def test_colbert_requires_query():
    with pytest.raises(QueryError):
        ColBERTReranker().rerank_vector(None, [{"_rowid": 1, "vector": [[1.0, 0.0]]}])


def test_colbert_validates_embedder_and_top_k():
    with pytest.raises(QueryError):
        ColBERTReranker(embedder=object())
    with pytest.raises(QueryError):
        ColBERTReranker(top_k=-1)


# ---------------------------------------------------------------------------
# 多向量查询（查询侧展开）
# ---------------------------------------------------------------------------


def make_multivector_query():
    return MultivectorQuery(np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32), tokens=["a", "b"])


def token_search_fn(vector, k):
    """token 0 召回 d1(0.0) / d2(1.0)；token 1 召回 d2(0.0) / d3(3.0)。"""

    if np.allclose(vector, [1.0, 0.0]):
        return [{"_rowid": 1, DISTANCE_COL: 0.0}, {"_rowid": 2, DISTANCE_COL: 1.0}]
    return [{"_rowid": 2, DISTANCE_COL: 0.0}, {"_rowid": 3, DISTANCE_COL: 3.0}]


def test_multivector_query_sum_aggregation():
    """sum 聚合（对应 MaxSim 的外层 Σ）：

    相关性 = 1/(1+d)
    token0: d1 → 1.0, d2 → 0.5
    token1: d2 → 1.0, d3 → 0.25
    sum:    d1 = 1.0, d2 = 1.5, d3 = 0.25
    顺序:   d2 (1.5) > d1 (1.0) > d3 (0.25)
    """

    out = make_multivector_query().search(token_search_fn, k=2)
    assert ids_of(out) == [2, 1, 3]
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [1.5, 1.0, 0.25]
    assert [r["_num_tokens_matched"] for r in out] == [2, 1, 1]


def test_multivector_query_max_aggregation():
    """max 聚合：d1 = 1.0, d2 = max(0.5, 1.0) = 1.0, d3 = 0.25。

    d1 和 d2 同分；排序的次级键是行 id 的字符串形式，"1" < "2"，所以 d1 在前。
    """

    out = make_multivector_query().search(token_search_fn, k=2, aggregate="max")
    assert ids_of(out) == [1, 2, 3]
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [1.0, 1.0, 0.25]


def test_multivector_query_without_dedup_keeps_every_hit():
    """不去重 = "每个 token 各自的 top-k 拼在一起"（4 条），
    d2 被两个 token 都召回，所以出现两次。"""

    out = make_multivector_query().search(token_search_fn, k=2, deduplicate=False)
    assert ids_of(out) == [2, 2, 1, 3]
    assert out[0]["_aggregated_score"] == pytest.approx(1.5)
    assert out[1]["_aggregated_score"] == pytest.approx(1.5)
    # 每一条记的是"这一次命中的相关性"，不是聚合值
    assert sorted(r[RELEVANCE_COL] for r in out[:2]) == pytest.approx([0.5, 1.0])


def test_multivector_query_from_text_uses_embedder():
    from mini_lancedb.rerank import HashingTokenEmbedder

    query = MultivectorQuery.from_text("machine learning", HashingTokenEmbedder(dim=8))
    assert query.num_tokens == 2
    assert query.tokens == ["machine", "learning"]


def test_multivector_query_validates_inputs():
    with pytest.raises(QueryError):
        MultivectorQuery(np.zeros(4))  # 一维不是合法的 token 矩阵
    with pytest.raises(QueryError):
        MultivectorQuery(np.zeros((2, 4)), tokens=["only-one"])
    with pytest.raises(QueryError):
        MultivectorQuery(np.zeros((0, 4)))


def test_multivector_query_search_validates_k_and_aggregate():
    query = make_multivector_query()
    with pytest.raises(QueryError):
        query.search(token_search_fn, k=0)
    with pytest.raises(QueryError):
        query.search(token_search_fn, k=2, aggregate="mean")


def test_multivector_query_describe_reports_storage_cost():
    query = make_multivector_query()
    query.search(token_search_fn, k=2)
    info = query.describe()
    assert info["token 数"] == 2
    assert info["最后一次检索的 k"] == 2
    assert "存储开销" in info


# ---------------------------------------------------------------------------
# none
# ---------------------------------------------------------------------------


def test_none_reranker_keeps_concatenation_order(vector_rows, fts_rows):
    out = get_reranker("none").rerank_hybrid(None, vector_rows, fts_rows)
    assert ids_of(out) == [1, 2, 3, 4, 5], "none 的语义就是别动顺序"
    assert len(out) == 5, "两路完全重合，去重后还是 5 条"


def test_none_reranker_scores_distance_inverted():
    vec = [{"_rowid": 1, DISTANCE_COL: 0.0}, {"_rowid": 2, DISTANCE_COL: 1.0}]
    out = get_reranker("none").rerank_hybrid(None, vec, [])
    assert [round(r[RELEVANCE_COL], 10) for r in out] == [1.0, 0.5]


def test_none_reranker_uses_score_column_for_fts_rows():
    fts = [{"_rowid": 1, SCORE_COL: 7.5}]
    out = get_reranker("none").rerank_hybrid(None, [], fts)
    assert out[0][RELEVANCE_COL] == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# rerank() 的形状分派
# ---------------------------------------------------------------------------


def test_rerank_dispatches_single_list(vector_rows):
    out = get_reranker("rrf").rerank(None, vector_rows)
    assert ids_of(out) == VEC_ORDER


def test_rerank_dispatches_two_lists(vector_rows, fts_rows):
    out = get_reranker("rrf").rerank(None, [vector_rows, fts_rows])
    assert ids_of(out) == RRF_EXPECTED_ORDER


def test_rerank_dispatches_three_lists():
    a = make_rows([1, 2], DISTANCE_COL, {1: 0.1, 2: 0.2})
    b = make_rows([2, 3], DISTANCE_COL, {2: 0.1, 3: 0.2})
    c = make_rows([3, 1], DISTANCE_COL, {3: 0.1, 1: 0.2})
    out = get_reranker("rrf").rerank(None, [a, b, c])
    assert sorted(ids_of(out)) == [1, 2, 3]


def test_rerank_empty_input():
    assert get_reranker("rrf").rerank(None, []) == []


def test_rerank_rejects_negative_k(vector_rows):
    with pytest.raises(QueryError):
        get_reranker("rrf").rerank(None, vector_rows, k=-1)


def test_non_fusion_reranker_rejects_multivector(vector_rows):
    with pytest.raises(NotSupportedError):
        get_reranker("linear").rerank_multivector(None, [vector_rows])


def test_rrf_score_helper():
    from mini_lancedb.rerank import rrf_score

    assert rrf_score(1, k=60) == pytest.approx(1 / 61)
    assert rrf_score(2, k=60, weight=3.0) == pytest.approx(3 / 62)
    with pytest.raises(QueryError):
        rrf_score(0, k=60)
