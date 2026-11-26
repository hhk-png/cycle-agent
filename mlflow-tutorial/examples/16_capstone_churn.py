"""Capstone: data contract → train → evaluate → register → serve → feedback.

Run from the tutorial root:

    python examples/16_capstone_churn.py

The dataset is synthetic and deliberately small.  The point is the shape of
the lifecycle, not the model quality: every step produces an auditable object
that the next step consumes.
"""

from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.request

from _common import client, configure_fluent, print_json, unique_experiment

import mini_mlflow as mlflow
from mini_mlflow import tracing
from mini_mlflow.evaluate import evaluate_model
from mini_mlflow.models import LinearRegression, infer_signature
from mini_mlflow.scoring import create_scoring_server

FEATURES = ["tenure_months", "monthly_calls", "support_tickets", "plan_price"]
TOKEN = "capstone-token"


def _make_rows(count: int = 72) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for index in range(count):
        tenure = 1 + (index * 7) % 36
        calls = (index * 3) % 12
        tickets = (index * 5) % 6
        price = 29 + (index * 11) % 70
        churn = 0.95 - 0.018 * tenure + 0.035 * calls + 0.045 * tickets - 0.002 * price
        churn = max(0.02, min(0.98, churn))
        rows.append(
            {
                "tenure_months": float(tenure),
                "monthly_calls": float(calls),
                "support_tickets": float(tickets),
                "plan_price": float(price),
                "churn_30d": round(churn, 6),
            }
        )
    return rows


def _x(rows: list[dict[str, float]]) -> list[dict[str, float]]:
    return [{name: row[name] for name in FEATURES} for row in rows]


def _y(rows: list[dict[str, float]]) -> list[float]:
    return [row["churn_30d"] for row in rows]


def _rmse(predictions: list[float], targets: list[float]) -> float:
    return math.sqrt(sum((p - t) ** 2 for p, t in zip(predictions, targets)) / len(targets))


def _post(base: str, payload: dict, token: str | None = TOKEN) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base + "/invocations",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> None:
    active = client()
    configure_fluent(active)
    experiment = unique_experiment(active, "churn-capstone")
    rows = _make_rows()
    train_rows, test_rows = rows[:56], rows[56:]
    x_train, y_train = _x(train_rows), _y(train_rows)
    x_test, y_test = _x(test_rows), _y(test_rows)

    run = active.start_run(
        experiment_id=experiment.experiment_id,
        run_name="candidate-2026-11",
        tags={"stage": "candidate", "owner": "risk-ml", "demo": "true"},
    )
    run_id = run.info.run_id
    info = None
    try:
        # 1. Data contract: the run records a pointer, not the bytes.
        active.log_params(
            run_id,
            {
                "model": "linear",
                "alpha": 0.05,
                "features": ",".join(FEATURES),
                "split": "index-based-demo",
                "seed": 42,
            },
        )
        active.log_input(
            run_id,
            dataset_name="churn_snapshot",
            dataset_digest="sha256:synthetic-demo-2026-11",
            dataset_source="file://data/churn_snapshot_2026_11.csv",
            context="training",
            tags={"owner": "risk-data"},
        )

        # 2. Train a deliberately interpretable baseline.
        model = LinearRegression(alpha=0.05).fit(x_train, y_train)
        signature = infer_signature(x_test, model.predict(x_test))

        # 3. Evaluate globally and by slice.
        evaluation = evaluate_model(
            model,
            x_test,
            y_test,
            task="regression",
            client=active,
            run_id=run_id,
            prefix="test",
        )
        for name, predicate in {
            "new": lambda row: row["tenure_months"] <= 12,
            "tenured": lambda row: row["tenure_months"] > 12,
        }.items():
            subset = [row for row in test_rows if predicate(row)]
            active.log_metric(
                run_id,
                f"slice_{name}_rmse",
                _rmse(model.predict(_x(subset)), _y(subset)),
            )

        baseline = sum(y_train) / len(y_train)
        baseline_rmse = _rmse([baseline] * len(y_test), y_test)
        active.log_metric(run_id, "baseline_rmse", baseline_rmse)
        gate_passed = evaluation.metrics["rmse"] < baseline_rmse * 0.9
        active.set_tag(run_id, "gate", "pass" if gate_passed else "fail")
        assert gate_passed, "candidate failed the demo quality gate"

        # 4. Package and register.  The version is immutable; the alias moves.
        info = active.log_model(
            run_id,
            model,
            registered_model_name="churn-propensity",
            signature=signature,
            input_example=x_test[:2],
            metadata={"owner": "risk-ml", "model_card": "model_card.json"},
        )
        model_card = {
            "intended_use": "rank active subscription customers for retention outreach",
            "not_intended_for": "credit, pricing or contract termination decisions",
            "training_data": "synthetic churn_snapshot 2026-11",
            "features": FEATURES,
            "metrics": evaluation.metrics,
            "baseline_rmse": baseline_rmse,
            "limitations": ["synthetic demo data", "no causal claim about intervention"],
            "owner": "risk-ml",
        }
        active.log_dict(run_id, model_card, "model_card.json")
    finally:
        current = active.get_run(run_id)
        if not current.info.is_terminal:
            active.end_run(run_id, "FINISHED")

    assert info is not None and info.version is not None
    version = info.version
    active.set_model_version_tag("churn-propensity", version, "approval", "approved")
    active.set_model_version_tag(
        "churn-propensity", version, "model_card", f"runs:/{run_id}/model_card.json"
    )
    active.set_registered_model_alias("churn-propensity", "challenger", version)
    active.set_registered_model_alias("churn-propensity", "champion", version)
    resolved = active.get_model_version_by_alias("churn-propensity", "champion")

    # 5. Serve the champion and exercise the scoring contract.
    server = create_scoring_server(
        "models:/churn-propensity@champion",
        host="127.0.0.1",
        port=0,
        auth_token=TOKEN,
        client=active,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = {
            "dataframe_split": {
                "columns": FEATURES,
                "data": [[row[name] for name in FEATURES] for row in test_rows[:4]],
            }
        }
        status, response = _post(base, payload)
        assert status == 200 and len(response["predictions"]) == 4
        status_denied, _ = _post(base, payload, token=None)
        assert status_denied == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # 6. Trace the decision and attach a human judgment.
    with tracing.start_trace(
        "churn-score-request",
        experiment_id=experiment.experiment_id,
        inputs={"rows": len(payload["dataframe_split"]["data"])},
        tags={"model": "churn-propensity", "model_version": str(version)},
        client=active,
    ) as session:
        with tracing.start_span("feature_lookup", span_type="RETRIEVER") as span:
            span.set_outputs({"dataset": "churn_snapshot", "features": FEATURES})
        with tracing.start_span("model_predict", span_type="LLM") as span:
            span.set_outputs({"predictions": response["predictions"]})
        session.set_outputs({"predictions": response["predictions"]})
    trace_id = session.trace.trace_id
    feedback = active.log_feedback(
        trace_id,
        "business_acceptance",
        1.0,
        source_type="HUMAN",
        source_id="risk-owner",
        rationale="scores are actionable and aligned with policy",
        metadata={"model_version": str(version)},
    )
    stored = active.get_trace(trace_id)
    traces = active.search_traces([experiment.experiment_id], max_results=20)

    print_json(
        {
            "experiment_id": experiment.experiment_id,
            "run_id": run_id,
            "model_version": version,
            "champion_source": resolved.source,
            "metrics": evaluation.metrics,
            "baseline_rmse": baseline_rmse,
            "gate": "pass",
            "predictions": response["predictions"],
            "auth_denied": status_denied,
            "trace": {
                "trace_id": trace_id,
                "spans": [span.name for span in stored.spans],
                "status": stored.status,
            },
            "feedback": feedback.to_dict(),
            "traces_found": len(traces),
            "loaded_prediction_count": len(active.load_model("models:/churn-propensity@champion").predict(x_test[:3])),
        }
    )
    active.close()


if __name__ == "__main__":
    main()
