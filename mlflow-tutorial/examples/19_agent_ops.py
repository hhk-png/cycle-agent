"""Agent operations: sessions, scripted simulation, scoring, triage and comparison."""

from __future__ import annotations

import os
import time

from _common import client, print_json, unique_experiment

from mini_mlflow.agentops import (
    SESSION_TAG,
    ConversationCompleteness,
    ConversationalSafety,
    LatencyBudget,
    ScriptedConversationSimulator,
    ToolSuccessRate,
    UserFrustration,
    assert_improved,
    compare_paired,
    detect_issues,
    generate_test_cases,
    score_sessions,
    search_sessions,
    sessions_to_dataset_records,
)
from mini_mlflow.entities import Span, Trace
from mini_mlflow.utils import new_id, now_ms


def support_agent(message: str, history: list[dict[str, str]]) -> str:
    """A deterministic stand-in for an agent under test."""
    text = message.casefold()
    if "crash" in text:
        raise RuntimeError("vector store unavailable: connection reset")
    if "refund" in text:
        return "Refunds are available within 30 days when you include the receipt."
    if "password" in text:
        return "Open Settings, choose Security, and request a reset link by email."
    if "angry" in text or "still" in text:
        return "I understand the frustration; let me escalate this to a human agent."
    return "I can help with refunds, passwords, and account access."


def log_failed_tool_turn(mlflow_client, experiment_id: str, session_id: str, turn: int) -> str:
    trace_id = new_id()
    started = now_ms() * 1_000_000 + turn * 1_000_000
    ended = started + 30_000_000
    agent_span = Span(
        span_id=new_id()[:16],
        trace_id=trace_id,
        name="agent.turn",
        span_type="AGENT",
        start_time_ns=started,
        end_time_ns=ended,
        inputs={"message": "look up order"},
        outputs={"answer": ""},
    )
    tool_span = Span(
        span_id=new_id()[:16],
        trace_id=trace_id,
        name="orders.lookup",
        parent_span_id=agent_span.span_id,
        span_type="TOOL",
        start_time_ns=started + 1,
        end_time_ns=ended,
        status="ERROR",
        events=[
            {
                "name": "exception",
                "timestamp_ns": ended,
                "attributes": {
                    "exception.type": "TimeoutError",
                    "exception.message": f"timeout after {8000 + turn * 137} ms",
                },
            }
        ],
    )
    spans = [agent_span, tool_span]
    trace = Trace(
        trace_id=trace_id,
        name="agent.turn",
        experiment_id=experiment_id,
        timestamp=started // 1_000_000,
        status="ERROR",
        tags={SESSION_TAG: session_id},
        request={"message": "look up order"},
        response={"answer": ""},
        spans=spans,
    )
    mlflow_client.log_trace(trace)
    return trace_id


def main() -> None:
    mlflow_client = client()
    experiment = unique_experiment(mlflow_client, "agent-ops")
    simulator = ScriptedConversationSimulator(
        [
            {
                "case_id": "refund",
                "goal": "learn the refund window",
                "turns": ["hello", "can I get a refund?"],
                "expectations": {"must_include": ["30 days"]},
            },
            {
                "case_id": "password",
                "goal": "reset a password",
                "turns": ["I cannot log in", "how do I reset my password?"],
                "expectations": {"must_include": ["reset link"]},
            },
            {
                "case_id": "crash",
                "goal": "observe a failing tool path",
                "turns": ["please crash the agent"],
            },
        ],
        max_turns=4,
        stop_on_error=False,
    )
    results = simulator.run(
        support_agent, client=mlflow_client, experiment_id=experiment.experiment_id
    )

    sessions = search_sessions(mlflow_client, [experiment.experiment_id])
    logged = score_sessions(
        mlflow_client,
        sessions,
        [
            ConversationCompleteness(),
            UserFrustration(),
            ConversationalSafety(),
            ToolSuccessRate(),
            LatencyBudget(max_ms=5000),
        ],
    )

    # Two turns with the same normalised timeout signature become one issue.
    log_failed_tool_turn(mlflow_client, experiment.experiment_id, "orders-session", turn=0)
    log_failed_tool_turn(mlflow_client, experiment.experiment_id, "orders-session", turn=1)
    traces = mlflow_client.search_traces([experiment.experiment_id], max_results=200)
    issues = detect_issues(traces, slow_ms=5000, min_count=2)

    baseline = {f"case-{index}": 0.50 for index in range(8)}
    candidate = {f"case-{index}": 0.62 + index * 0.01 for index in range(8)}
    comparison = compare_paired(baseline, candidate, metric="quality", n_boot=500, seed=11)
    gate = "pass"
    try:
        assert_improved(comparison)
    except AssertionError as exc:  # noqa: PERF203 - the failure is the demo
        gate = f"fail: {exc}"

    records = sessions_to_dataset_records(sessions[:2])
    suffix = os.environ.get("MINI_MLFLOW_DEMO_SUFFIX") or str(int(time.time() * 1000))[-6:]
    dataset_name = f"agent-golden-{suffix}"
    existing = mlflow_client.search_datasets(filter_string=f"name = '{dataset_name}'")
    dataset = (
        existing[0]
        if existing
        else mlflow_client.create_dataset(dataset_name, records, source="example-19")
    )

    print_json(
        {
            "experiment_id": experiment.experiment_id,
            "simulated_runs": [result.to_dict() for result in results],
            "sessions": [session.to_dict() for session in sessions],
            "assessments": [
                {
                    "name": item.name,
                    "value": item.value,
                    "source_type": item.source_type,
                    "trace_id": item.trace_id,
                }
                for item in logged
            ],
            "issues": [issue.to_dict() for issue in issues],
            "comparison": comparison.to_dict(),
            "ci_gate": gate,
            "generated_cases": [case.goal for case in generate_test_cases(sessions)],
            "dataset": {
                "name": dataset.name,
                "record_count": dataset.record_count,
                "digest": dataset.digest,
            },
        }
    )
    mlflow_client.close()


if __name__ == "__main__":
    main()
