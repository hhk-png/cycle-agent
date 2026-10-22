from __future__ import annotations

import numpy as np
import pytest

from mini_deepinfer.embedding import (
    CrossEncoder,
    EmbeddingConfig,
    HashEmbedder,
    RerankPipeline,
    VectorIndex,
    plan_embedding_batches,
)


def test_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashEmbedder(EmbeddingConfig(dim=16, seed=3))
    a = embedder.encode_one([1, 2, 3])
    b = embedder.encode_one([1, 2, 3])
    assert np.allclose(a, b)
    assert np.linalg.norm(a) == pytest.approx(1.0)
    assert HashEmbedder.similarity(a, a) == pytest.approx(1.0)

    raw = HashEmbedder(EmbeddingConfig(dim=16, seed=3, normalize=False))
    assert np.linalg.norm(raw.encode_one([1, 2, 3])) != pytest.approx(1.0)


def test_embedder_batch_and_validation() -> None:
    embedder = HashEmbedder(EmbeddingConfig(dim=8, seed=0))
    batch = embedder.encode([[1, 2], [3], []])
    assert batch.shape == (3, 8)
    assert np.allclose(batch[2], 0.0)
    with pytest.raises(ValueError):
        embedder.encode_one([999])


def test_flat_index_returns_sorted_cosine_matches() -> None:
    embedder = HashEmbedder(EmbeddingConfig(dim=12, seed=1))
    docs = {"a": [1, 2, 3], "b": [4, 5], "c": [1, 2], "d": [9]}
    index = VectorIndex(12)
    index.add(list(docs), embedder.encode(list(docs.values())))
    query = embedder.encode_one([1, 2, 3])
    results = index.search(query, 3)
    assert len(results) == 3
    assert results[0][0] == "a"
    assert [score for _, score in results] == sorted(
        [score for _, score in results], reverse=True
    )


def test_bucket_index_probing_all_buckets_matches_flat() -> None:
    rng = np.random.default_rng(0)
    vectors = rng.normal(size=(40, 8)).astype(np.float32)
    ids = [f"d{i}" for i in range(40)]
    flat = VectorIndex(8)
    bucketed = VectorIndex(8, n_buckets=4, seed=7)
    flat.add(ids, vectors)
    bucketed.add(ids, vectors)
    query = rng.normal(size=8).astype(np.float32)
    assert [item[0] for item in bucketed.search(query, 5, probe_buckets=4)] == [
        item[0] for item in flat.search(query, 5)
    ]
    partial = bucketed.search(query, 5, probe_buckets=1)
    assert len(partial) <= 5


def test_index_rejects_duplicate_ids_and_bad_shapes() -> None:
    index = VectorIndex(4)
    index.add(["a"], np.zeros((1, 4), dtype=np.float32))
    with pytest.raises(ValueError):
        index.add(["a"], np.zeros((1, 4), dtype=np.float32))
    with pytest.raises(ValueError):
        index.add(["b"], np.zeros((2, 4), dtype=np.float32))
    with pytest.raises(KeyError):
        index.vector("missing")


def test_batch_planner_partitions_every_item() -> None:
    lengths = [10, 200, 10, 200, 50, 50]
    batches = plan_embedding_batches(
        lengths, max_batch_size=2, max_batch_tokens=420
    )
    seen = sorted(index for batch in batches for index in batch.indices)
    assert seen == list(range(len(lengths)))
    for batch in batches:
        assert len(batch.indices) <= 2
        assert batch.padded_tokens <= 420
        assert batch.padded_tokens >= batch.useful_tokens


def test_batch_planner_sorts_and_reduces_padding() -> None:
    lengths = [100, 10, 10, 10]
    batches = plan_embedding_batches(lengths, max_batch_size=4, max_batch_tokens=120)
    # Sorted order keeps the three short sequences together; a naive
    # arrival-order batch would pad all four to 100.
    assert batches[0].indices == (1, 2, 3)
    assert batches[0].padding_waste == pytest.approx(0.0)
    assert batches[0].padded_tokens == 30


def test_batch_planner_rejects_oversized_and_buckets() -> None:
    with pytest.raises(ValueError):
        plan_embedding_batches([1000], max_batch_tokens=512)
    batches = plan_embedding_batches([11, 12], bucket_width=16, max_batch_tokens=64)
    assert batches[0].max_len == 16
    with pytest.raises(ValueError):
        plan_embedding_batches([-1])


def test_rerank_pipeline_returns_shortlist() -> None:
    embedder = HashEmbedder(EmbeddingConfig(dim=16, seed=2))
    reranker = CrossEncoder(16, seed=4)
    pipeline = RerankPipeline(
        embedder, reranker, n_buckets=2, retrieve_k=4, rerank_k=2
    )
    pipeline.index_documents(
        {
            "a": [1, 2, 3, 4],
            "b": [5, 6, 7],
            "c": [1, 2, 3, 4, 5],
            "d": [8, 9],
        }
    )
    results = pipeline.search([1, 2, 3, 4], k=2)
    assert len(results) == 2
    assert [score for _, score in results] == sorted(
        [score for _, score in results], reverse=True
    )
    assert all(identifier in {"a", "b", "c", "d"} for identifier, _ in results)
