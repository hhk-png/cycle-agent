"""Create a nested trace and read it back from the tracking store."""

from __future__ import annotations

from _common import client, print_json, unique_experiment

from mini_mlflow import tracing


def main() -> None:
    mlflow_client = client()
    experiment = unique_experiment(mlflow_client, "tracing-demo")
    with tracing.start_trace(
        "answer-question",
        experiment_id=experiment.experiment_id,
        inputs={"question": "What is MLflow?"},
        tags={"demo": "true", "env": "local"},
        client=mlflow_client,
    ) as session:
        with tracing.start_span(
            "retrieve",
            span_type="RETRIEVER",
            inputs={"query": "MLflow tracking"},
        ) as retrieval:
            retrieval.set_outputs([{"doc_id": "doc-1", "score": 0.92}])
        with tracing.start_span("generate", span_type="LLM") as llm:
            llm.set_outputs({"answer": "MLflow tracks experiments and models."})
            tracing.add_event("token_usage", {"input": 12, "output": 7})
        session.set_outputs({"answer": "MLflow tracks experiments and models."})
        session.add_tag("model", "demo-llm-v1")

    stored = mlflow_client.get_trace(session.trace.trace_id)
    otel = tracing.trace_to_otel(stored)
    print_json(
        {
            "trace_id": stored.trace_id,
            "status": stored.status,
            "latency_ms": stored.latency_ms,
            "span_names": [span.name for span in stored.spans],
            "otel_span_count": len(otel["resourceSpans"][0]["scopeSpans"][0]["spans"]),
            "request": stored.request,
            "response": stored.response,
        }
    )
    mlflow_client.close()


if __name__ == "__main__":
    main()
