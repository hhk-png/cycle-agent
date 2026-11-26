"""GenAI capstone: a grounded knowledge assistant from prompt to operations.

This is the runnable companion to chapter 47.  It uses only MiniMLflow
modules and a deterministic stand-in for an LLM, so it needs no network, GPU
or API key:

    python examples/20_genai_capstone.py

The point is not the fake model.  The point is the control plane around it:
versioned prompts, a routed gateway, traced retrieval/generation, a golden
dataset, paired evaluation gates, online sampling, human feedback, and drift
monitoring bound to a managed baseline.
"""

from __future__ import annotations

import math
import os
import re
import time

from _common import client, configure_fluent, print_json, unique_experiment

import mini_mlflow as mlflow
from mini_mlflow.agentops import (
    SESSION_TAG,
    OnlineScorer,
    ScorerResult,
    ScorerSamplingConfig,
    assert_improved,
    compare_paired,
    score_online,
)
from mini_mlflow.evaluate import llm_metrics
from mini_mlflow.gateway import CallableProvider, Gateway, GatewayConfig
from mini_mlflow.rag import (
    BM25Index,
    Document,
    citation_metrics,
    extract_citations,
    grounding_report,
    grounding_score,
    recall_at_k,
    retrieval_report,
)
from mini_mlflow.utils import new_id

PROMPT_NAME = "support-answer"

KNOWLEDGE_BASE = [
    Document(
        "refund-policy",
        "Refunds are available within 30 days with a receipt. "
        "Digital goods are non-refundable after download.",
    ),
    Document(
        "password-reset",
        "Open Settings > Security > Reset password. "
        "A reset link expires after 15 minutes.",
    ),
    Document(
        "shipping",
        "Express shipping takes 1 business day. "
        "Standard shipping takes 3 to 5 business days.",
    ),
    Document(
        "data-export",
        "Workspace admins can export data as CSV from Settings > Data. "
        "Exports are retained for 7 days.",
    ),
]

CASES = [
    {
        "case_id": "refund-window",
        "question": "How long do I have to request a refund?",
        "expected_doc": "refund-policy",
        "must_include": "30 days",
        "reference": "Refunds are available within 30 days with a receipt.",
        "slice": "policy",
    },
    {
        "case_id": "digital-goods",
        "question": "Can I get a refund for a digital download?",
        "expected_doc": "refund-policy",
        "must_include": "non-refundable",
        "reference": "Digital goods are non-refundable after download.",
        "slice": "policy",
    },
    {
        "case_id": "reset-steps",
        "question": "How do I reset my password?",
        "expected_doc": "password-reset",
        "must_include": "reset link",
        "reference": "A reset link expires after 15 minutes.",
        "slice": "account",
    },
    {
        "case_id": "express-sla",
        "question": "How long does express shipping take?",
        "expected_doc": "shipping",
        "must_include": "1 business day",
        "reference": "Express shipping takes 1 business day.",
        "slice": "shipping",
    },
    {
        "case_id": "export-where",
        "question": "Where can I export workspace data?",
        "expected_doc": "data-export",
        "must_include": "CSV",
        "reference": "Workspace admins can export data as CSV from Settings > Data.",
        "slice": "data",
    },
    {
        "case_id": "export-retention",
        "question": "How long are exports retained?",
        "expected_doc": "data-export",
        "must_include": "7 days",
        "reference": "Exports are retained for 7 days.",
        "slice": "data",
    },
    {
        "case_id": "reset-expiry",
        "question": "What happens if my reset link expires?",
        "expected_doc": "password-reset",
        "must_include": "15 minutes",
        "reference": "A reset link expires after 15 minutes.",
        "slice": "account",
    },
    {
        "case_id": "shipping-options",
        "question": "What shipping options exist?",
        "expected_doc": "shipping",
        "must_include": "3 to 5 business days",
        "reference": (
            "Standard shipping takes 3 to 5 business days. "
            "Express shipping takes 1 business day."
        ),
        "slice": "shipping",
    },
]

PROMPT_V1 = (
    "Context:\n{{context}}\n\n"
    "Question: {{question}}\n"
    "Answer in one sentence."
)
PROMPT_V2 = (
    "Context:\n{{context}}\n\n"
    "Question: {{question}}\n"
    "Answer completely. Cite the source as [doc_id]. "
    "Then add one actionable next step."
)

NEXT_STEP = {
    "refund-policy": "Next step: keep the receipt.",
    "password-reset": "Next step: open Settings > Security.",
    "shipping": "Next step: choose express shipping if you need it sooner.",
    "data-export": "Next step: ask a workspace admin to export the data.",
}


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if item.strip()]


def fake_llm(messages, model: str, **params):
    """A deterministic, prompt-sensitive stand-in for a chat model.

    v1 answers with the first sentence of the top passage.  v2 follows the
    instruction to answer completely, cite the source and add a next step.
    The contrast is mechanical on purpose: it makes the *evaluation pipeline*
    observable instead of hiding it behind model nondeterminism.
    """
    system = "\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "system"
    )
    documents = params.get("documents") or []
    if not documents:
        return "I do not have enough context to answer that."
    top = documents[0]
    sentences = _sentences(str(top["text"]))
    if "Cite the source" in system:
        answer = (
            " ".join(sentences)
            + f" [{top['doc_id']}] "
            + NEXT_STEP.get(str(top["doc_id"]), "Next step: contact support.")
        )
    else:
        answer = sentences[0]
    return {
        "text": answer,
        "input_tokens": max(1, len(system) // 4),
        "output_tokens": max(1, len(answer) // 4),
    }


def build_gateway() -> Gateway:
    config = GatewayConfig.from_dict(
        {
            "providers": {
                "primary": {
                    "kind": "callable",
                    "model": "capstone-llm-v1",
                    "cost_per_1k_input": 0.50,
                    "cost_per_1k_output": 1.50,
                    "timeout_ms": 5000,
                },
                "backup": {
                    "kind": "callable",
                    "model": "capstone-llm-backup",
                    "cost_per_1k_input": 1.00,
                    "cost_per_1k_output": 3.00,
                    "timeout_ms": 8000,
                },
            },
            "routes": {
                "assistant": {
                    "primary": "primary",
                    "fallbacks": ["backup"],
                    "rate_limit_rpm": 600,
                    "cache_ttl_s": 30,
                }
            },
        }
    )
    return Gateway(
        config,
        providers={
            "primary": CallableProvider(config.providers["primary"], fake_llm),
            "backup": CallableProvider(config.providers["backup"], fake_llm),
        },
    )


def answer_case(
    active,
    index: BM25Index,
    gateway: Gateway,
    prompt,
    case: dict,
    *,
    experiment_id: str,
    phase: str,
    batch: str = "",
) -> dict:
    question = case["question"]
    session_id = f"{phase}-{prompt.version}-{case['case_id']}"
    with mlflow.start_trace(
        "support-answer",
        experiment_id=experiment_id,
        inputs={"question": question},
        tags={
            SESSION_TAG: session_id,
            "phase": phase,
            "batch_id": batch,
            "prompt_version": str(prompt.version),
            "gateway_route": "assistant",
        },
        client=active,
    ) as trace_ctx:
        with mlflow.start_span(
            "retrieve", span_type="RETRIEVER", inputs={"question": question}
        ) as span:
            hits = index.search(question, k=2)
            span.set_outputs([hit.to_dict() for hit in hits])
            span.set_attributes(
                {
                    "retrieval.k": 2,
                    "retrieval.top_score": hits[0].score if hits else 0.0,
                    "retrieval.empty": not hits,
                }
            )
        with mlflow.start_span(
            "rerank",
            span_type="RERANKER",
            inputs={"candidates": [hit.doc_id for hit in hits]},
        ) as span:
            hits = hits[:2]
            span.set_outputs([hit.to_dict() for hit in hits])
            span.set_attributes(
                {"rerank.strategy": "identity", "rerank.count": len(hits)}
            )
        context = "\n".join(
            f"[{hit.doc_id}] {index.document(hit.doc_id).text}" for hit in hits
        )
        rendered = prompt.format(context=context, question=question)
        with mlflow.start_span(
            "generate",
            span_type="LLM",
            inputs={"prompt_version": prompt.version, "prompt": rendered},
        ) as span:
            response = gateway.complete(
                "assistant",
                [
                    {"role": "system", "content": rendered},
                    {"role": "user", "content": question},
                ],
                params={
                    "documents": [
                        {"doc_id": hit.doc_id, "text": index.document(hit.doc_id).text}
                        for hit in hits
                    ]
                },
            )
            span.set_outputs({"answer": response.text})
            span.set_attributes(
                {
                    "provider": response.provider,
                    "model": response.model,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "cost_usd": response.cost_usd,
                    "cache_hit": response.cache_hit,
                }
            )
        contexts = [index.document(hit.doc_id).text for hit in hits]
        with mlflow.start_span(
            "validate",
            span_type="PARSER",
            inputs={"answer": response.text},
        ) as span:
            citations = extract_citations(response.text)
            # An empty retrieval is a legitimate outcome (the answer should
            # abstain), not a reason to crash the validation span.
            grounding = grounding_score(response.text, contexts) if contexts else 0.0
            span.set_outputs({"citations": citations, "grounding": grounding})
            span.set_attributes(
                {
                    "citation.count": len(citations),
                    "grounding": grounding,
                    "has_next_step": "Next step:" in response.text,
                }
            )
        trace_ctx.set_outputs(
            {
                "answer": response.text,
                "retrieved": [hit.doc_id for hit in hits],
                "citations": citations,
            }
        )
    return {
        "case_id": case["case_id"],
        "slice": case["slice"],
        "question": question,
        "answer": response.text,
        "retrieved": [hit.doc_id for hit in hits],
        "retrieval_score": hits[0].score if hits else 0.0,
        "answer_chars": len(response.text),
        "expected_doc": case["expected_doc"],
        "must_include": case["must_include"],
        "reference": case["reference"],
        "trace_id": trace_ctx.trace.trace_id,
        "provider": response.provider,
        "cost_usd": response.cost_usd,
        "latency_ms": response.latency_ms,
    }


def score_rows(rows: list[dict], index: BM25Index) -> dict:
    retrievals: list[list[str]] = []
    relevants: list[set[str]] = []
    scored: list[dict] = []
    for row in rows:
        answer = row["answer"]
        must = row["must_include"].lower() in answer.lower()
        citations = citation_metrics(answer, [row["expected_doc"]])
        # Grounding measures support by the passages the model actually saw.
        # If retrieval returned the wrong document, a fluent answer can still
        # look grounded; the retrieval metrics are what expose the miss.
        seen_contexts = [index.document(doc_id).text for doc_id in row["retrieved"]]
        grounding = grounding_report(answer, seen_contexts) if seen_contexts else {"score": 0.0}
        llm = llm_metrics([answer], [row["reference"]])
        retrieval = recall_at_k(row["retrieved"], {row["expected_doc"]}, k=2)
        retrievals.append(row["retrieved"])
        relevants.append({row["expected_doc"]})
        scored.append(
            {
                **row,
                "must_include_ok": 1.0 if must else 0.0,
                "citation_f1": citations["f1"],
                "citation_precision": citations["precision"],
                "grounding": grounding["score"],
                "token_f1": llm["token_f1"],
                "rouge_l": llm["rouge_l"],
                "retrieval_recall_at_2": retrieval,
                "citations": extract_citations(answer),
            }
        )
    retrieval = retrieval_report(retrievals, relevants, k=2)
    metrics = {
        "must_include_rate": sum(item["must_include_ok"] for item in scored) / len(scored),
        "citation_f1": sum(item["citation_f1"] for item in scored) / len(scored),
        "citation_precision": sum(item["citation_precision"] for item in scored) / len(scored),
        "grounding": sum(item["grounding"] for item in scored) / len(scored),
        "token_f1": sum(item["token_f1"] for item in scored) / len(scored),
        "rouge_l": sum(item["rouge_l"] for item in scored) / len(scored),
        "retrieval_recall_at_2": retrieval["recall_at_k"],
        "retrieval_mrr_at_2": retrieval["mrr_at_k"],
        "retrieval_ndcg_at_2": retrieval["ndcg_at_k"],
        "cost_usd": sum(item["cost_usd"] for item in scored),
        "latency_p95_ms": sorted(item["latency_ms"] for item in scored)[
            max(0, math.ceil(len(scored) * 0.95) - 1)
        ],
    }
    return {"metrics": metrics, "rows": scored, "retrieval": retrieval}


class CitationPresent:
    """Online scorer: a grounded answer must name at least one source."""

    name = "citation_present"

    def __call__(self, session) -> ScorerResult:
        citations = extract_citations(session.final_response)
        return ScorerResult(
            self.name,
            1.0 if citations else 0.0,
            f"{len(citations)} citation(s) in the final response",
            {"citations": citations, "session_id": session.session_id},
        )


def telemetry_rows(count: int, *, drifted: bool) -> list[dict]:
    """Synthetic request telemetry for the monitoring demo.

    Production would derive these fields from traces/gateway metrics (intent
    from a classifier or route, retrieval score from the RETRIEVER span,
    answer length from the LLM span) and bind the resulting dataset as the
    monitor baseline.
    """
    rows = []
    for index in range(count):
        if drifted:
            intent = "billing" if index % 5 else "policy"
            retrieval_score = 0.28 + (index % 6) * 0.04
            answer_chars = 48 + (index % 12) * 3
        else:
            intent = ["policy", "account", "shipping", "data"][index % 4]
            retrieval_score = 0.68 + (index % 7) * 0.035
            answer_chars = 120 + (index % 20) * 4
        rows.append(
            {
                "intent": intent,
                "retrieval_score": round(retrieval_score, 3),
                "answer_chars": answer_chars,
            }
        )
    return rows


def main() -> None:
    active = client()
    configure_fluent(active)
    suffix = os.environ.get("MINI_MLFLOW_DEMO_SUFFIX") or str(int(time.time() * 1000))[-6:]
    experiment = unique_experiment(active, "genai-capstone")
    index = BM25Index(KNOWLEDGE_BASE)
    gateway = build_gateway()

    v1 = active.register_prompt(
        PROMPT_NAME,
        PROMPT_V1,
        commit_message="baseline: one-sentence answer",
        tags={"owner": "support", "stage": "baseline"},
    )
    v2 = active.register_prompt(
        PROMPT_NAME,
        PROMPT_V2,
        commit_message="complete answer + citation + next step",
        tags={"owner": "support", "stage": "candidate"},
    )

    dataset_name = f"support-golden-{suffix}"
    for old in active.search_datasets(filter_string=f"name = '{dataset_name}'"):
        active.delete_dataset(old.dataset_id)
    golden = active.create_dataset(
        dataset_name,
        [
            {
                "inputs": {"question": case["question"]},
                "expectations": {
                    "must_include": case["must_include"],
                    "doc_id": case["expected_doc"],
                },
                "tags": {"slice": case["slice"]},
            }
            for case in CASES
        ],
        source="kb://support/2026-11-01",
        source_type="knowledge-base",
        tags={"owner": "support", "stage": "golden"},
    )

    offline: dict[int, dict] = {}
    for prompt in (v1, v2):
        rows = [
            answer_case(
                active,
                index,
                gateway,
                prompt,
                case,
                experiment_id=experiment.experiment_id,
                phase="offline",
            )
            for case in CASES
        ]
        report = score_rows(rows, index)
        run = active.start_run(
            experiment_id=experiment.experiment_id,
            run_name=f"offline-eval-prompt-v{prompt.version}",
            tags={
                "stage": "offline-eval",
                "prompt_version": str(prompt.version),
                "dataset_digest": golden.digest,
            },
        )
        run_id = run.info.run_id
        try:
            active.log_params(
                run_id,
                {
                    "prompt_name": PROMPT_NAME,
                    "prompt_version": prompt.version,
                    "dataset_id": golden.dataset_id,
                    "dataset_digest": golden.digest,
                    "retriever": "bm25",
                    "retrieval_k": 2,
                    "gateway_route": "assistant",
                },
            )
            active.log_metrics(run_id, report["metrics"])
            active.log_dict(
                run_id,
                {"metrics": report["metrics"], "cases": report["rows"]},
                "evaluation/report.json",
            )
        finally:
            active.end_run(run_id, "FINISHED")
        offline[prompt.version] = {"run_id": run_id, **report}

    baseline = {row["case_id"]: row["must_include_ok"] for row in offline[v1.version]["rows"]}
    candidate = {row["case_id"]: row["must_include_ok"] for row in offline[v2.version]["rows"]}
    comparison = compare_paired(
        baseline, candidate, metric="must_include", n_boot=2000, seed=17
    )
    baseline_citation = {
        row["case_id"]: row["citation_f1"] for row in offline[v1.version]["rows"]
    }
    candidate_citation = {
        row["case_id"]: row["citation_f1"] for row in offline[v2.version]["rows"]
    }
    citation_comparison = compare_paired(
        baseline_citation,
        candidate_citation,
        metric="citation_f1",
        n_boot=2000,
        seed=17,
    )
    gate = "pass"
    try:
        assert_improved(comparison, min_delta=0.1, min_pairs=6)
        assert_improved(citation_comparison, min_delta=0.1, min_pairs=6)
    except AssertionError as exc:  # noqa: PERF203 - the failure is the demo
        gate = f"fail: {exc}"

    active.set_prompt_alias(PROMPT_NAME, "previous", v1.version)
    if gate == "pass":
        active.set_prompt_alias(PROMPT_NAME, "production", v2.version)
    else:
        # Fail closed: a failed candidate must not become the production
        # pointer just because the previous run left one behind.
        active.set_prompt_alias(PROMPT_NAME, "production", v1.version)
    production = active.load_prompt(f"prompts:/{PROMPT_NAME}@production")

    online_batch = new_id()
    online_rows = [
        answer_case(
            active,
            index,
            gateway,
            production,
            case,
            experiment_id=experiment.experiment_id,
            phase="online",
            batch=online_batch,
        )
        for case in CASES
    ]
    online_assessments = score_online(
        active,
        [
            OnlineScorer(
                CitationPresent(),
                ScorerSamplingConfig(
                    sample_rate=1.0,
                    filter_string=(
                        f'tags.phase = "online" and tags.batch_id = "{online_batch}"'
                    ),
                ),
            )
        ],
        experiment_ids=[experiment.experiment_id],
        max_results=1000,
        # Keep the sampling key stable across reruns; batch_id is a query
        # filter, not part of the deterministic sampling bucket.
        source_id="capstone-online",
    )
    human_feedback = active.log_feedback(
        online_rows[0]["trace_id"],
        "quality",
        0.95,
        source_type="HUMAN",
        source_id="reviewer-7",
        rationale="complete answer with a correct citation",
        metadata={"channel": "capstone"},
    )
    review_queue = active.search_traces(
        [experiment.experiment_id],
        filter_string=(
            'assessments.citation_present >= 1.0 and tags.phase = "online" '
            f'and tags.batch_id = "{online_batch}"'
        ),
        max_results=50,
    )

    telemetry_name = f"support-telemetry-{suffix}"
    for old in active.search_datasets(filter_string=f"name = '{telemetry_name}'"):
        active.delete_dataset(old.dataset_id)
    telemetry_dataset = active.create_dataset(
        telemetry_name,
        [{"inputs": row} for row in telemetry_rows(40, drifted=False)],
        source="stream://support/requests",
        source_type="stream",
        tags={"owner": "support", "stage": "monitoring-baseline"},
    )
    monitor = active.create_monitor(
        f"support-monitor-{suffix}",
        [],
        {
            "intent": "categorical",
            "retrieval_score": "numeric",
            "answer_chars": "numeric",
        },
        model_name="support-assistant",
        model_version=str(production.version),
        dataset_id=telemetry_dataset.dataset_id,
        thresholds={"psi_alert": 0.2},
        tags={"owner": "support", "alert_channel": "#support-oncall"},
    )
    stable_window = active.record_monitor_window(
        monitor.monitor_id,
        telemetry_rows(40, drifted=False),
        window_start=1_000,
        window_end=2_000,
    )
    drift_window = active.record_monitor_window(
        monitor.monitor_id,
        telemetry_rows(40, drifted=True),
        window_start=2_000,
        window_end=3_000,
    )

    release_run = active.start_run(
        experiment_id=experiment.experiment_id,
        run_name="release-gate",
        tags={
            "stage": "release",
            "prompt_version": str(production.version),
            "gate": gate,
        },
    )
    release_id = release_run.info.run_id
    try:
        active.log_params(
            release_id,
            {
                "prompt_name": PROMPT_NAME,
                "prompt_version": production.version,
                "dataset_digest": golden.digest,
                "monitor_id": monitor.monitor_id,
            },
        )
        active.log_metrics(
            release_id,
            {
                "gate_passed": 1.0 if gate == "pass" else 0.0,
                "online_citation_rate": (
                    sum(item.value for item in online_assessments) / len(online_assessments)
                    if online_assessments
                    else 0.0
                ),
                "drift_alert": 1.0 if drift_window.status == "ALERT" else 0.0,
                "baseline_psi_max": max(
                    (
                        float(metrics.get("psi") or 0.0)
                        for metrics in stable_window.metrics.values()
                    ),
                    default=0.0,
                ),
            },
        )
        active.log_dict(
            release_id,
            {
                "comparison": comparison.to_dict(),
                "citation_comparison": citation_comparison.to_dict(),
                "monitor": monitor.to_dict(),
                "stable_window": stable_window.to_dict(),
                "drift_window": drift_window.to_dict(),
            },
            "release/evidence.json",
        )
    finally:
        active.end_run(release_id, "FINISHED")

    summary = {
        "experiment_id": experiment.experiment_id,
        "golden_dataset": {
            "dataset_id": golden.dataset_id,
            "name": golden.name,
            "records": golden.record_count,
            "digest": golden.digest,
        },
        "prompts": {
            "versions": [v1.version, v2.version],
            "production": {
                "version": production.version,
                "aliases": production.aliases,
                "template": production.template,
            },
        },
        "offline": {
            "v1": offline[v1.version]["metrics"],
            "v2": offline[v2.version]["metrics"],
            "comparison": comparison.to_dict(),
            "citation_comparison": citation_comparison.to_dict(),
            "gate": gate,
        },
        "online": {
            "requests": len(online_rows),
            "assessments": [item.to_dict() for item in online_assessments],
            "human_feedback": human_feedback.to_dict(),
            "review_queue": len(review_queue),
        },
        "monitoring": {
            "monitor_id": monitor.monitor_id,
            "baseline_dataset": telemetry_dataset.dataset_id,
            "stable": stable_window.status,
            "drift": drift_window.status,
            "drift_reasons": drift_window.report.get("reasons", []),
        },
        "gateway": gateway.stats(),
    }
    print_json(summary)

    # Telemetry and monitor are demo artifacts; the golden dataset and runs
    # remain as the auditable record a real deployment would keep.
    active.delete_monitor(monitor.monitor_id)
    active.delete_dataset(telemetry_dataset.dataset_id)
    active.close()


if __name__ == "__main__":
    main()
