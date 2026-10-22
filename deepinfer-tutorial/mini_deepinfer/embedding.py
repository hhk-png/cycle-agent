"""Embedding and rerank serving primitives.

Generative inference produces tokens; representation inference produces
vectors.  The serving economics are different: embedding requests are short,
high-QPS and latency-sensitive, rerank requests are pairwise and benefit from
large batches, and both care about a vector index rather than a KV cache.
DeepInfer does not ship this path, so the module is a teaching extension that
keeps the parts a platform team actually has to build: normalized embeddings,
length-bucketed batching, an approximate index, and a two-stage retrieve-then-
rerank pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class EmbeddingConfig:
    vocab_size: int = 264
    dim: int = 32
    normalize: bool = True
    seed: int = 0

    def __post_init__(self) -> None:
        if self.vocab_size < 1 or self.dim < 1:
            raise ValueError("vocab_size and dim must be positive")


class HashEmbedder:
    """A deterministic bag-of-token embedder.

    It is not a trained bi-encoder.  Its job is to make the serving mechanics
    observable: the same token ids always produce the same vector, mean
    pooling is explicit, and normalization can be switched off to show why
    cosine search depends on it.
    """

    def __init__(self, config: EmbeddingConfig | None = None) -> None:
        self.config = config or EmbeddingConfig()
        rng = np.random.default_rng(self.config.seed)
        # Small initial values keep mean-pooled vectors in a stable range.
        self.table = rng.normal(
            0.0, 1.0 / np.sqrt(self.config.dim), size=(self.config.vocab_size, self.config.dim)
        ).astype(np.float32)

    def encode_one(self, token_ids: Sequence[int]) -> np.ndarray:
        if not token_ids:
            return np.zeros(self.config.dim, dtype=np.float32)
        ids = np.asarray(token_ids, dtype=np.int64)
        if np.any(ids < 0) or np.any(ids >= self.config.vocab_size):
            raise ValueError("token id out of range")
        vector = self.table[ids].mean(axis=0)
        if self.config.normalize:
            norm = float(np.linalg.norm(vector))
            if norm > 0:
                vector = vector / norm
        return vector.astype(np.float32)

    def encode(self, batch: Sequence[Sequence[int]]) -> np.ndarray:
        if not batch:
            return np.zeros((0, self.config.dim), dtype=np.float32)
        return np.stack([self.encode_one(tokens) for tokens in batch]).astype(np.float32)

    @staticmethod
    def similarity(left: np.ndarray, right: np.ndarray) -> float:
        left = np.asarray(left, dtype=np.float32)
        right = np.asarray(right, dtype=np.float32)
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        if denominator == 0.0:
            return 0.0
        return float(np.dot(left, right) / denominator)


class CrossEncoder:
    """A tiny pairwise scorer used as the rerank stage.

    A real cross-encoder runs a Transformer over the concatenated query and
    document.  This version scores four pooled features with a linear layer.
    The interface is the same: it consumes a query and a document and returns
    a relevance score, and it is much more expensive per pair than a dot
    product, which is exactly why reranking is limited to a shortlist.
    """

    def __init__(self, dim: int, seed: int = 0) -> None:
        if dim < 1:
            raise ValueError("dim must be positive")
        self.dim = dim
        rng = np.random.default_rng(seed)
        self.weights = rng.normal(0.0, 1.0 / np.sqrt(4 * dim), size=(4 * dim,)).astype(
            np.float32
        )

    def features(self, query: np.ndarray, document: np.ndarray) -> np.ndarray:
        query = np.asarray(query, dtype=np.float32)
        document = np.asarray(document, dtype=np.float32)
        if query.shape != (self.dim,) or document.shape != (self.dim,):
            raise ValueError("query and document must match the encoder dimension")
        return np.concatenate(
            [query, document, query * document, np.abs(query - document)]
        )

    def score(self, query: np.ndarray, document: np.ndarray) -> float:
        logit = float(np.dot(self.weights, self.features(query, document)))
        return float(1.0 / (1.0 + np.exp(-logit)))


class VectorIndex:
    """A flat index with optional random-projection buckets.

    ``n_buckets=1`` is exact brute-force search.  With more buckets, the query
    probes only the most promising buckets, trading recall for speed.  This is
    the same trade-off as IVF-style indexes, at a scale that fits in a test.
    """

    def __init__(self, dim: int, n_buckets: int = 1, seed: int = 0) -> None:
        if dim < 1:
            raise ValueError("dim must be positive")
        if n_buckets < 1:
            raise ValueError("n_buckets must be >= 1")
        self.dim = dim
        self.n_buckets = n_buckets
        self._ids: list[str] = []
        self._rows: dict[str, int] = {}
        self._vectors = np.zeros((0, dim), dtype=np.float32)
        self._buckets: list[list[int]] = [[] for _ in range(n_buckets)]
        rng = np.random.default_rng(seed)
        self._projections = (
            rng.normal(size=(n_buckets, dim)).astype(np.float32) if n_buckets > 1 else None
        )

    def __len__(self) -> int:
        return len(self._ids)

    def _bucket_of(self, vector: np.ndarray) -> int:
        if self._projections is None:
            return 0
        return int(np.argmax(self._projections @ vector))

    def add(self, ids: Sequence[str], vectors: np.ndarray) -> None:
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.shape != (len(ids), self.dim):
            raise ValueError("vectors shape does not match ids and dim")
        for identifier in ids:
            if identifier in self._rows:
                raise ValueError(f"duplicate id {identifier!r}")
        start = len(self._ids)
        for offset, identifier in enumerate(ids):
            row = start + offset
            self._ids.append(identifier)
            self._rows[identifier] = row
            self._buckets[self._bucket_of(vectors[offset])].append(row)
        self._vectors = np.vstack([self._vectors, vectors]).astype(np.float32)

    def vector(self, identifier: str) -> np.ndarray:
        try:
            return self._vectors[self._rows[identifier]]
        except KeyError as exc:
            raise KeyError(identifier) from exc

    def _candidate_rows(self, query: np.ndarray, probe_buckets: int | None) -> np.ndarray:
        if self._projections is None:
            return np.arange(len(self._ids), dtype=np.int64)
        probes = self.n_buckets if probe_buckets is None else max(1, min(probe_buckets, self.n_buckets))
        bucket_scores = self._projections @ query
        chosen = np.argsort(-bucket_scores)[:probes]
        rows: list[int] = []
        for bucket in chosen:
            rows.extend(self._buckets[int(bucket)])
        return np.asarray(sorted(set(rows)), dtype=np.int64)

    def search(
        self,
        query: np.ndarray,
        k: int,
        *,
        probe_buckets: int | None = None,
    ) -> list[tuple[str, float]]:
        if k < 1:
            raise ValueError("k must be >= 1")
        if not self._ids:
            return []
        query = np.asarray(query, dtype=np.float32)
        if query.shape != (self.dim,):
            raise ValueError("query has the wrong dimension")
        rows = self._candidate_rows(query, probe_buckets)
        if rows.size == 0:
            return []
        candidates = self._vectors[rows]
        query_norm = float(np.linalg.norm(query))
        candidate_norms = np.linalg.norm(candidates, axis=1)
        denominator = candidate_norms * query_norm
        denominator[denominator == 0.0] = 1.0
        scores = (candidates @ query) / denominator
        take = min(k, len(rows))
        top = np.argpartition(-scores, take - 1)[:take]
        top = top[np.argsort(-scores[top])]
        return [(self._ids[int(rows[index])], float(scores[index])) for index in top]


@dataclass(frozen=True)
class EmbeddingBatch:
    indices: tuple[int, ...]
    max_len: int
    useful_tokens: int
    padded_tokens: int

    @property
    def padding_waste(self) -> float:
        if self.useful_tokens == 0:
            return 0.0
        return (self.padded_tokens - self.useful_tokens) / self.useful_tokens


def plan_embedding_batches(
    lengths: Sequence[int],
    *,
    max_batch_size: int = 8,
    max_batch_tokens: int = 512,
    bucket_width: int | None = None,
) -> list[EmbeddingBatch]:
    """Pack variable-length sequences into padding-efficient batches.

    Sorting by length before first-fit is the cheap trick that makes
    embedding serving efficient: it keeps a short query from sharing a batch
    with a long document.  ``bucket_width`` rounds each length up to a bucket,
    which trades a little padding for more predictable shapes.
    """
    if max_batch_size < 1 or max_batch_tokens < 1:
        raise ValueError("batch limits must be positive")
    if bucket_width is not None and bucket_width < 1:
        raise ValueError("bucket_width must be positive or None")
    for length in lengths:
        if length < 0:
            raise ValueError("lengths must be non-negative")

    def effective(length: int) -> int:
        if bucket_width is None:
            return length
        return max(1, ((length + bucket_width - 1) // bucket_width) * bucket_width)

    order = sorted(range(len(lengths)), key=lambda index: (effective(lengths[index]), index))
    batches: list[dict[str, object]] = []
    for index in order:
        width = effective(lengths[index])
        if width > max_batch_tokens:
            raise ValueError(f"sequence {index} needs {width} tokens, exceeding the batch budget")
        placed = False
        for batch in batches:
            indices = batch["indices"]  # type: ignore[assignment]
            max_len = int(batch["max_len"])
            new_max = max(max_len, width)
            if len(indices) < max_batch_size and new_max * (len(indices) + 1) <= max_batch_tokens:
                indices.append(index)  # type: ignore[union-attr]
                batch["max_len"] = new_max
                batch["useful"] = int(batch["useful"]) + lengths[index]
                placed = True
                break
        if not placed:
            batches.append(
                {
                    "indices": [index],
                    "max_len": width,
                    "useful": lengths[index],
                }
            )

    result = []
    for batch in batches:
        indices = tuple(batch["indices"])  # type: ignore[arg-type]
        max_len = int(batch["max_len"])
        useful = int(batch["useful"])
        result.append(
            EmbeddingBatch(
                indices=indices,
                max_len=max_len,
                useful_tokens=useful,
                padded_tokens=max_len * len(indices),
            )
        )
    return result


class RerankPipeline:
    """Retrieve a shortlist with vectors, then rerank it pairwise."""

    def __init__(
        self,
        embedder: HashEmbedder,
        reranker: CrossEncoder,
        *,
        n_buckets: int = 1,
        retrieve_k: int = 8,
        rerank_k: int = 3,
    ) -> None:
        if retrieve_k < 1 or rerank_k < 1:
            raise ValueError("retrieve_k and rerank_k must be positive")
        self.embedder = embedder
        self.reranker = reranker
        self.retrieve_k = retrieve_k
        self.rerank_k = rerank_k
        self.index = VectorIndex(embedder.config.dim, n_buckets=n_buckets)

    def index_documents(self, documents: Mapping[str, Sequence[int]]) -> None:
        if not documents:
            return
        ids = list(documents)
        vectors = self.embedder.encode([documents[identifier] for identifier in ids])
        self.index.add(ids, vectors)

    def search(self, query_tokens: Sequence[int], k: int | None = None) -> list[tuple[str, float]]:
        query = self.embedder.encode_one(query_tokens)
        shortlist = self.index.search(query, self.retrieve_k)
        reranked = [
            (identifier, self.reranker.score(query, self.index.vector(identifier)))
            for identifier, _ in shortlist
        ]
        reranked.sort(key=lambda item: (-item[1], item[0]))
        limit = self.rerank_k if k is None else k
        return reranked[:limit]
