"""Embedding batching, vector search and retrieve-then-rerank."""

from __future__ import annotations

import numpy as np

from _common import add_project_root

add_project_root()

from mini_deepinfer.embedding import (
    CrossEncoder,
    EmbeddingConfig,
    HashEmbedder,
    RerankPipeline,
    VectorIndex,
    plan_embedding_batches,
)


def main() -> None:
    embedder = HashEmbedder(EmbeddingConfig(dim=16, seed=3))
    documents = {
        "kv-cache": [10, 11, 12, 13, 14, 15],
        "scheduler": [20, 21, 22],
        "lora": [10, 11, 30, 31],
        "compiler": [40, 41, 42, 43, 44],
    }
    vectors = embedder.encode(list(documents.values()))
    print("embedding matrix      :", vectors.shape)
    print("norm of first vector  :", round(float(np.linalg.norm(vectors[0])), 4))

    lengths = [6, 3, 4, 5, 64, 63, 62, 120]
    batches = plan_embedding_batches(
        lengths, max_batch_size=4, max_batch_tokens=256, bucket_width=16
    )
    print("embedding batches     :", [batch.indices for batch in batches])
    total_padded = sum(batch.padded_tokens for batch in batches)
    total_useful = sum(batch.useful_tokens for batch in batches)
    print("padding waste         :", round((total_padded - total_useful) / total_useful, 4))

    index = VectorIndex(16, n_buckets=2, seed=7)
    index.add(list(documents), vectors)
    query = embedder.encode_one([10, 11, 12, 13])
    print("flat search           :", [(key, round(score, 4)) for key, score in index.search(query, 2)])
    print("two-bucket search     :", [(key, round(score, 4)) for key, score in index.search(query, 2)])
    print("one-bucket search     :", [(key, round(score, 4)) for key, score in index.search(query, 2, probe_buckets=1)])

    reranker = CrossEncoder(16, seed=5)
    pipeline = RerankPipeline(embedder, reranker, n_buckets=2, retrieve_k=3, rerank_k=2)
    pipeline.index_documents(documents)
    reranked = pipeline.search([10, 11, 12, 13], k=2)
    print("retrieve + rerank     :", [(key, round(score, 4)) for key, score in reranked])


if __name__ == "__main__":
    main()
