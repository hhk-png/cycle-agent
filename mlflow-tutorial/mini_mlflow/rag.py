"""Retrieval and RAG evaluation primitives used by chapter 47.

MLflow does not ship a retriever: it ships the experiment, trace, dataset and
evaluation control plane *around* one.  This module is the application-side
glue that makes a knowledge assistant measurable without a network, an
embedding service or a GPU:

* a deterministic BM25 index (lexical baseline, not a vector database);
* ``recall@k`` / ``precision@k`` / ``MRR@k`` / ``nDCG@k``;
* citation extraction and precision/recall/F1;
* a lexical groundedness lower bound.

The last two are deliberately simple.  A lexical score is cheap, reproducible
and useful as a CI tripwire; it is **not** a semantic judge.  Chapter 47 and
appendix M explain where to replace it with model-graded and human review.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import InvalidParameter

_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]+")
_CITATION_RE = re.compile(r"\[\[?([A-Za-z0-9_.:/\-]+)\]\]?")

# Function words add noise to a lexical support score.  Negations are kept on
# purpose: dropping "not" would turn a contradiction into "grounded".
_STOPWORDS = frozenset(
    "a an the is are was were be been being to of and or in on at by for with "
    "from as it this that these those we you your i our their they he she "
    "can could should would will may might do does did has have had".split()
)

# A whole stemmer would be a dependency and a source of surprising matches.
# Folding common plurals is enough to make "refund" match "refunds" without
# turning "business" into "busines".
_NO_STEM = frozenset(
    "this his has was is its us as does yes news series business analysis".split()
)


def _fold_plural(token: str) -> str:
    if token in _NO_STEM or len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, light plural folding, and CJK bigrams.

    Chinese has no whitespace boundaries.  Bigrams are a crude but stable
    compromise: they make ``退款政策`` match ``退款`` without a segmenter, and
    they keep the index dependency-free.  Production should use the tokenizer
    of the embedding/reranking model so offline and online retrieval agree.
    """
    if not isinstance(text, str):
        raise InvalidParameter("text must be a string")
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        value = match.group(0)
        if "一" <= value[0] <= "鿿":
            if len(value) == 1:
                tokens.append(value)
            else:
                tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
        else:
            tokens.append(_fold_plural(value))
    return tokens


@dataclass(frozen=True)
class Document:
    """One retrievable unit.

    ``doc_id`` is the citation key and must be stable across re-indexing.
    ``metadata`` carries ACL, source, version and freshness fields; treating
    them as filters *before* ranking is what prevents cross-tenant leakage.
    """

    doc_id: str
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.doc_id, str) or not self.doc_id.strip():
            raise InvalidParameter("doc_id must be a non-empty string")
        if len(self.doc_id) > 256:
            raise InvalidParameter("doc_id must be at most 256 characters")
        if not isinstance(self.text, str) or not self.text.strip():
            raise InvalidParameter(f"document {self.doc_id!r} text must be non-empty")
        if len(self.text.encode("utf-8")) > 1_000_000:
            raise InvalidParameter(f"document {self.doc_id!r} exceeds 1 MB")
        if not isinstance(self.metadata, Mapping):
            raise InvalidParameter("document metadata must be a mapping")


@dataclass(frozen=True)
class RetrievalHit:
    doc_id: str
    score: float
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "score": self.score, "rank": self.rank}


class BM25Index:
    """A small, deterministic BM25 index.

    The scoring formula is the classic Okapi BM25::

        score(q, d) = sum_t idf(t) * tf(t,d) * (k1 + 1)
                      / (tf(t,d) + k1 * (1 - b + b * |d| / avgdl))

    Ties are broken by ``doc_id`` so evaluation reports do not change when the
    input order changes.  Terms with zero document frequency are skipped.
    """

    def __init__(
        self,
        documents: Iterable[Document],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if isinstance(k1, bool) or not isinstance(k1, (int, float)) or not math.isfinite(float(k1)) or k1 < 0:
            raise InvalidParameter("k1 must be a finite number >= 0")
        if isinstance(b, bool) or not isinstance(b, (int, float)) or not math.isfinite(float(b)) or not 0 <= b <= 1:
            raise InvalidParameter("b must be a finite number between 0 and 1")
        docs = list(documents)
        if not docs:
            raise InvalidParameter("BM25Index requires at least one document")
        for document in docs:
            if not isinstance(document, Document):
                raise InvalidParameter("BM25Index accepts Document objects only")
        ids = [document.doc_id for document in docs]
        if len(ids) != len(set(ids)):
            duplicates = sorted({doc_id for doc_id in ids if ids.count(doc_id) > 1})
            raise InvalidParameter(f"duplicate document ids: {duplicates}")

        self.k1 = float(k1)
        self.b = float(b)
        self._documents = {document.doc_id: document for document in docs}
        self._doc_ids = sorted(self._documents)
        self._counts: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        document_frequency: Counter[str] = Counter()
        for doc_id in self._doc_ids:
            tokens = tokenize(self._documents[doc_id].text)
            counts = Counter(tokens)
            self._counts[doc_id] = counts
            self._lengths[doc_id] = len(tokens)
            document_frequency.update(counts.keys())
        total_length = sum(self._lengths.values())
        self._average_length = total_length / len(self._doc_ids)
        size = len(self._doc_ids)
        self._idf = {
            term: math.log(1 + (size - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def __len__(self) -> int:
        return len(self._doc_ids)

    @property
    def documents(self) -> list[Document]:
        return [self._documents[doc_id] for doc_id in self._doc_ids]

    def document(self, doc_id: str) -> Document:
        try:
            return self._documents[str(doc_id)]
        except KeyError as exc:
            raise InvalidParameter(f"unknown document id {doc_id!r}") from exc

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        metadata_filter: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> list[RetrievalHit]:
        if not isinstance(query, str) or not query.strip():
            raise InvalidParameter("query must be a non-empty string")
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
            raise InvalidParameter("k must be an integer between 1 and 100")
        if metadata_filter is not None and not callable(metadata_filter):
            raise InvalidParameter("metadata_filter must be callable")
        terms = sorted(set(tokenize(query)))
        if not terms:
            return []

        average = self._average_length or 1.0
        scores: dict[str, float] = {}
        for doc_id in self._doc_ids:
            document = self._documents[doc_id]
            if metadata_filter is not None and not metadata_filter(document.metadata):
                continue
            counts = self._counts[doc_id]
            length = self._lengths[doc_id]
            score = 0.0
            for term in terms:
                frequency = counts.get(term, 0)
                if frequency == 0:
                    continue
                denominator = frequency + self.k1 * (1 - self.b + self.b * length / average)
                score += self._idf[term] * frequency * (self.k1 + 1) / denominator
            if score > 0.0:
                scores[doc_id] = score

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]
        return [
            RetrievalHit(doc_id=doc_id, score=round(score, 6), rank=rank)
            for rank, (doc_id, score) in enumerate(ranked, start=1)
        ]


def _validate_ranking(
    retrieved_ids: Sequence[str],
    relevant_ids: Iterable[str],
    k: int,
) -> tuple[list[str], set[str]]:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise InvalidParameter("k must be a positive integer")
    retrieved = list(retrieved_ids)
    relevant = set(relevant_ids)
    if not relevant:
        raise InvalidParameter("relevant_ids must not be empty")
    if any(not isinstance(item, str) or not item for item in retrieved):
        raise InvalidParameter("retrieved ids must be non-empty strings")
    if any(not isinstance(item, str) or not item for item in relevant):
        raise InvalidParameter("relevant ids must be non-empty strings")
    # A retriever that returns the same document twice must not get credit for
    # two relevant hits (or nDCG > 1).  Keep the first occurrence, which is the
    # one a reader would actually see.
    deduplicated: list[str] = []
    seen: set[str] = set()
    for doc_id in retrieved:
        if doc_id not in seen:
            seen.add(doc_id)
            deduplicated.append(doc_id)
    return deduplicated, relevant


def recall_at_k(retrieved_ids: Sequence[str], relevant_ids: Iterable[str], *, k: int) -> float:
    """Fraction of relevant documents present in the top-k ranking."""
    retrieved, relevant = _validate_ranking(retrieved_ids, relevant_ids, k)
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def precision_at_k(retrieved_ids: Sequence[str], relevant_ids: Iterable[str], *, k: int) -> float:
    """Fraction of the top-k slots that are relevant.

    The denominator is ``k``, not ``len(retrieved)``: a retriever that returns
    two good documents when five were requested has not filled the context
    budget, and pretending otherwise inflates the score.
    """
    retrieved, relevant = _validate_ranking(retrieved_ids, relevant_ids, k)
    return len(set(retrieved[:k]) & relevant) / k


def mrr_at_k(retrieved_ids: Sequence[str], relevant_ids: Iterable[str], *, k: int) -> float:
    """Reciprocal rank of the first relevant document (0 if none in top-k)."""
    retrieved, relevant = _validate_ranking(retrieved_ids, relevant_ids, k)
    for rank, doc_id in enumerate(retrieved[:k], start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: Sequence[str], relevant_ids: Iterable[str], *, k: int) -> float:
    """Binary-relevance nDCG@k.

    Graded relevance (for example "partially answers") is intentionally out of
    scope: it requires a labeling rubric, and a half-implemented gain function
    would make comparisons across teams meaningless.
    """
    retrieved, relevant = _validate_ranking(retrieved_ids, relevant_ids, k)
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, doc_id in enumerate(retrieved[:k], start=1)
        if doc_id in relevant
    )
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def retrieval_report(
    retrieved_per_query: Sequence[Sequence[str]],
    relevant_per_query: Sequence[Iterable[str]],
    *,
    k: int,
) -> dict[str, Any]:
    """Average the four retrieval metrics and keep per-query rows for slices."""
    if len(retrieved_per_query) != len(relevant_per_query):
        raise InvalidParameter("retrieved and relevant lists must have the same length")
    if not retrieved_per_query:
        raise InvalidParameter("at least one query is required")
    rows: list[dict[str, Any]] = []
    for index, (retrieved, relevant) in enumerate(zip(retrieved_per_query, relevant_per_query)):
        relevant_set = set(relevant)
        rows.append(
            {
                "query_index": index,
                "retrieved": list(retrieved)[:k],
                "relevant": sorted(relevant_set),
                "recall_at_k": recall_at_k(retrieved, relevant_set, k=k),
                "precision_at_k": precision_at_k(retrieved, relevant_set, k=k),
                "mrr_at_k": mrr_at_k(retrieved, relevant_set, k=k),
                "ndcg_at_k": ndcg_at_k(retrieved, relevant_set, k=k),
            }
        )
    count = len(rows)
    return {
        "queries": count,
        "k": k,
        "recall_at_k": sum(row["recall_at_k"] for row in rows) / count,
        "precision_at_k": sum(row["precision_at_k"] for row in rows) / count,
        "mrr_at_k": sum(row["mrr_at_k"] for row in rows) / count,
        "ndcg_at_k": sum(row["ndcg_at_k"] for row in rows) / count,
        "rows": rows,
    }


def extract_citations(answer: str) -> list[str]:
    """Extract ``[doc-id]`` citations in first-seen order, de-duplicated."""
    if not isinstance(answer, str):
        raise InvalidParameter("answer must be a string")
    seen: set[str] = set()
    result: list[str] = []
    for match in _CITATION_RE.finditer(answer):
        doc_id = match.group(1)
        if doc_id not in seen:
            seen.add(doc_id)
            result.append(doc_id)
    return result


def citation_metrics(
    answer: str,
    expected_doc_ids: Iterable[str],
    *,
    cited_doc_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Precision/recall/F1 of citations against the expected evidence set.

    A citation to a document outside ``expected_doc_ids`` counts as a false
    positive.  That is stricter than "must include at least one good source"
    and is the right default for grounded assistants: an answer supported by
    one correct document plus one unrelated document is not fully grounded.
    """
    expected = {str(item) for item in expected_doc_ids if str(item)}
    if not expected:
        raise InvalidParameter("expected_doc_ids must not be empty")
    cited = list(cited_doc_ids) if cited_doc_ids is not None else extract_citations(answer)
    cited_set = {str(item) for item in cited if str(item)}
    true_positives = len(cited_set & expected)
    precision = true_positives / len(cited_set) if cited_set else 0.0
    recall = true_positives / len(expected)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "has_citation": bool(cited_set),
        "cited": sorted(cited_set),
        "expected": sorted(expected),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def grounding_report(
    answer: str,
    contexts: Sequence[str | Document],
    *,
    ignore_citations: bool = True,
) -> dict[str, Any]:
    """Lexical lower bound on whether an answer is supported by its contexts.

    This is a *tripwire*, not a judge.  It catches answers that invent numbers
    or entities absent from every retrieved passage, but it cannot detect a
    subtle contradiction built from words that all appear somewhere in the
    context.  Chapter 47 pairs it with a model-graded scorer and human review.
    """
    if not isinstance(answer, str):
        raise InvalidParameter("answer must be a string")
    texts: list[str] = []
    for context in contexts:
        if isinstance(context, Document):
            texts.append(context.text)
        elif isinstance(context, str):
            texts.append(context)
        else:
            raise InvalidParameter("contexts must contain strings or Document objects")
    if not texts:
        raise InvalidParameter("at least one context is required")

    cleaned = _CITATION_RE.sub(" ", answer) if ignore_citations else answer
    answer_tokens = [
        token for token in tokenize(cleaned) if token not in _STOPWORDS
    ]
    if not answer_tokens:
        return {"score": 0.0, "supported": 0, "total": 0, "unsupported": []}
    context_tokens: set[str] = set()
    for text in texts:
        context_tokens.update(tokenize(text))
    unsupported = sorted({token for token in answer_tokens if token not in context_tokens})
    supported = len(answer_tokens) - sum(
        1 for token in answer_tokens if token not in context_tokens
    )
    return {
        "score": supported / len(answer_tokens),
        "supported": supported,
        "total": len(answer_tokens),
        "unsupported": unsupported[:50],
    }


def grounding_score(
    answer: str,
    contexts: Sequence[str | Document],
    *,
    ignore_citations: bool = True,
) -> float:
    """Convenience wrapper around :func:`grounding_report`."""
    return float(grounding_report(answer, contexts, ignore_citations=ignore_citations)["score"])
