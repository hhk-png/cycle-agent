"""Tests for the retrieval / RAG evaluation primitives (chapter 47)."""

from __future__ import annotations

import math

import pytest

from mini_mlflow.errors import InvalidParameter
from mini_mlflow.rag import (
    BM25Index,
    Document,
    citation_metrics,
    extract_citations,
    grounding_report,
    grounding_score,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    retrieval_report,
    tokenize,
)


def _docs() -> list[Document]:
    return [
        Document("refund-policy", "Refunds are available within 30 days with a receipt."),
        Document("shipping", "Express shipping takes 1 business day. Standard shipping takes 3 to 5 days."),
        Document("password-reset", "A reset link expires after 15 minutes."),
    ]


def test_tokenize_english_and_cjk_bigrams() -> None:
    assert tokenize("Refunds, receipts!") == ["refund", "receipt"]
    assert tokenize("business analysis") == ["business", "analysis"]
    assert tokenize("退款政策") == ["退款", "款政", "政策"]
    assert tokenize("v2 is ready") == ["v2", "is", "ready"]


def test_tokenize_rejects_non_string() -> None:
    with pytest.raises(InvalidParameter):
        tokenize(123)  # type: ignore[arg-type]


def test_bm25_ranks_the_relevant_document_first() -> None:
    index = BM25Index(_docs())
    hits = index.search("how many days for a refund?", k=2)
    assert hits[0].doc_id == "refund-policy"
    assert hits[0].rank == 1
    assert hits[0].score > 0
    # Lexical retrieval still returns low-idf matches such as the article "a";
    # the ranking, not the presence of every candidate, is what matters.
    assert all(hit.score > 0 for hit in hits)


def test_bm25_is_deterministic_on_ties() -> None:
    index = BM25Index(
        [
            Document("b-doc", "same text"),
            Document("a-doc", "same text"),
        ]
    )
    assert [hit.doc_id for hit in index.search("same", k=2)] == ["a-doc", "b-doc"]


def test_bm25_returns_empty_for_no_overlap() -> None:
    assert BM25Index(_docs()).search("kubernetes operator") == []


def test_bm25_metadata_filter_runs_before_ranking() -> None:
    index = BM25Index(
        [
            Document("a", "shared policy", {"tenant": "a"}),
            Document("b", "shared policy", {"tenant": "b"}),
        ]
    )
    hits = index.search("policy", metadata_filter=lambda metadata: metadata["tenant"] == "a")
    assert [hit.doc_id for hit in hits] == ["a"]


def test_bm25_validation() -> None:
    with pytest.raises(InvalidParameter):
        BM25Index([])
    with pytest.raises(InvalidParameter):
        BM25Index([Document("a", "x"), Document("a", "y")])
    with pytest.raises(InvalidParameter):
        BM25Index([Document("a", "x")], k1=float("nan"))
    with pytest.raises(InvalidParameter):
        BM25Index([Document("a", "x")], b=1.5)
    with pytest.raises(InvalidParameter):
        BM25Index([Document("a", "x")]).search("x", k=0)
    with pytest.raises(InvalidParameter):
        BM25Index([Document("a", "x")]).search("")


def test_recall_at_k() -> None:
    assert recall_at_k(["a", "b", "c"], {"b", "d"}, k=3) == 0.5
    assert recall_at_k(["a", "b", "c"], {"b", "d"}, k=1) == 0.0
    assert recall_at_k(["b"], {"b"}, k=5) == 1.0


def test_precision_at_k_uses_the_requested_budget() -> None:
    assert precision_at_k(["a", "x"], {"a"}, k=2) == 0.5
    assert precision_at_k(["a", "x"], {"a"}, k=4) == 0.25
    assert precision_at_k([], {"a"}, k=2) == 0.0


def test_mrr_at_k() -> None:
    assert mrr_at_k(["x", "b"], {"b"}, k=2) == 0.5
    assert mrr_at_k(["x", "b"], {"b"}, k=1) == 0.0
    assert mrr_at_k(["a", "b"], {"a", "b"}, k=2) == 1.0


def test_ndcg_at_k_binary_relevance() -> None:
    assert ndcg_at_k(["a"], {"a"}, k=1) == 1.0
    assert ndcg_at_k(["x", "a"], {"a"}, k=2) == pytest.approx(1 / math.log2(3))
    assert ndcg_at_k(["a"], {"a", "b"}, k=1) == 1.0
    assert ndcg_at_k(["x"], {"a"}, k=1) == 0.0


def test_duplicate_retrieved_ids_are_not_double_counted() -> None:
    assert ndcg_at_k(["a", "a"], {"a"}, k=2) == 1.0
    assert precision_at_k(["a", "a"], {"a"}, k=2) == 0.5
    assert recall_at_k(["a", "a"], {"a", "b"}, k=2) == 0.5


def test_retrieval_report_averages_and_validates() -> None:
    report = retrieval_report([["a"], ["x", "b"]], [{"a"}, {"b"}], k=2)
    assert report["queries"] == 2
    assert report["recall_at_k"] == 1.0
    # query 1 fills 1 of 2 slots; query 2 fills 1 of 2 slots.
    assert report["precision_at_k"] == pytest.approx(0.5)
    assert len(report["rows"]) == 2
    with pytest.raises(InvalidParameter):
        retrieval_report([["a"]], [], k=2)
    with pytest.raises(InvalidParameter):
        retrieval_report([], [], k=2)
    with pytest.raises(InvalidParameter):
        retrieval_report([["a"]], [set()], k=2)


def test_extract_citations_is_ordered_and_deduplicated() -> None:
    text = "See [refund-policy] and [shipping]. Again [refund-policy], plus [[data-export]]."
    assert extract_citations(text) == ["refund-policy", "shipping", "data-export"]
    assert extract_citations("no citation here") == []
    with pytest.raises(InvalidParameter):
        extract_citations(None)  # type: ignore[arg-type]


def test_citation_precision_recall_f1() -> None:
    report = citation_metrics("[a] [x]", {"a", "b"})
    assert report["has_citation"] is True
    assert report["precision"] == 0.5
    assert report["recall"] == 0.5
    assert report["f1"] == 0.5

    empty = citation_metrics("no citation", {"a"})
    assert empty["has_citation"] is False
    assert empty["precision"] == 0.0
    assert empty["recall"] == 0.0
    assert empty["f1"] == 0.0

    with pytest.raises(InvalidParameter):
        citation_metrics("x", set())


def test_grounding_report_finds_unsupported_terms() -> None:
    context = "Refunds are available within 30 days with a receipt."
    supported = grounding_report("Refunds are available within 30 days.", [context])
    assert supported["score"] == 1.0
    assert supported["unsupported"] == []

    invented = grounding_report("Refunds require a passport.", [context])
    assert invented["score"] < 1.0
    assert "passport" in invented["unsupported"]


def test_grounding_can_ignore_citations_and_accepts_documents() -> None:
    document = Document("refund-policy", "Refunds are available within 30 days.")
    answer = "[secret-doc] Refunds are available within 30 days."
    assert grounding_score(answer, [document]) == 1.0
    assert grounding_score(answer, [document], ignore_citations=False) < 1.0
    with pytest.raises(InvalidParameter):
        grounding_score("text", [])
    with pytest.raises(InvalidParameter):
        grounding_score("text", [123])  # type: ignore[list-item]
