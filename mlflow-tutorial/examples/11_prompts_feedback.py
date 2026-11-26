"""Prompt Registry + trace feedback + assessment-based search.

Run from the tutorial root:

    python examples/11_prompts_feedback.py

The example is intentionally offline: it uses the local SQLite backend unless
MINI_MLFLOW_TRACKING_URI points at a server.
"""

from __future__ import annotations

from _common import client, configure_fluent, print_json, unique_experiment

import mini_mlflow as mlflow


def main() -> None:
    active = client()
    configure_fluent(active)
    experiment = unique_experiment(active, "prompt-feedback-demo")

    # 1. A prompt is a versioned asset. v1 is kept for audit; v2 becomes
    #    production through a mutable alias.
    v1 = active.register_prompt(
        "support-reply",
        "Customer {{name}} says: {{message}}. Reply in {{tone}}.",
        commit_message="initial template",
        tags={"owner": "support", "language": "en"},
    )
    v2 = active.register_prompt(
        "support-reply",
        "You are a support agent. Customer {{name}} says: {{message}}. "
        "Reply in {{tone}} and include one next step.",
        commit_message="add next-step instruction",
        tags={"owner": "support", "language": "en"},
    )
    active.set_prompt_alias("support-reply", "production", v2.version)
    prompt = active.load_prompt("prompts:/support-reply@production")
    rendered = prompt.format(name="Ada", message="my order is late", tone="English")
    print(f"prompt v{prompt.version} aliases={prompt.aliases}")
    print(rendered)

    # 2. Trace one request and attach a human judgment. Feedback is a typed
    #    first-class entity, not a free-form tag.
    with mlflow.start_run(experiment_id=experiment.experiment_id, run_name="answer"):
        with mlflow.start_trace(
            "support-request",
            inputs={"message": "my order is late"},
            tags={"prompt": "support-reply", "prompt_version": str(prompt.version)},
        ) as trace_ctx:
            with mlflow.start_span("llm-call", span_type="LLM") as span:
                span.set_outputs({"answer": "I checked: it ships tomorrow."})
        mlflow.end_run()

    trace_id = trace_ctx.trace.trace_id
    feedback = active.log_feedback(
        trace_id,
        "quality",
        0.9,
        source_type="HUMAN",
        source_id="reviewer-7",
        rationale="accurate and actionable",
        metadata={"channel": "email"},
    )
    expectation = active.log_feedback(
        trace_id,
        "must_include_next_step",
        True,
        source_type="HUMAN",
        source_id="reviewer-7",
        is_expectation=True,
    )
    print_json(
        {
            "trace_id": trace_id,
            "feedback": feedback.to_dict(),
            "expectation": expectation.to_dict(),
        }
    )

    # 3. Assessments participate in trace search. This is how you build a
    #    review queue: "show me all traces a human rated below 0.5".
    traces = active.search_traces(
        filter_string=f'assessments.quality >= 0.8 and tags.prompt = "support-reply"',
        max_results=20,
    )
    print(f"matching traces: {[item.trace_id[:8] for item in traces]}")


if __name__ == "__main__":
    main()
