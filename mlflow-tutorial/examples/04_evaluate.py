"""Evaluate a tiny classifier and write metrics/artifacts to the run."""

from __future__ import annotations

from _common import client, print_json, unique_experiment

from mini_mlflow.evaluate import evaluate_model


class TokenClassifier:
    """Deliberately simple, deterministic classifier for the demo."""

    def predict(self, data):
        result = []
        for row in data:
            text = str(row["text"]).lower()
            result.append("billing" if "invoice" in text or "refund" in text else "product")
        return result


def main() -> None:
    mlflow_client = client()
    experiment = unique_experiment(mlflow_client, "evaluate-demo")
    rows = [
        {"text": "my invoice is wrong"},
        {"text": "refund please"},
        {"text": "the app crashes"},
        {"text": "feature request"},
        {"text": "invoice duplicate"},
        {"text": "how do I export data"},
    ]
    targets = ["billing", "billing", "product", "product", "billing", "product"]

    run = mlflow_client.start_run(
        experiment_id=experiment.experiment_id,
        run_name="classifier-eval",
    )
    rid = run.info.run_id
    try:
        result = evaluate_model(
            TokenClassifier(),
            rows,
            targets,
            task="classification",
            labels=["billing", "product"],
            client=mlflow_client,
            run_id=rid,
            prefix="test",
        )
    finally:
        mlflow_client.end_run(rid, "FINISHED")

    print_json(
        {
            "run_id": rid,
            "metrics": result.metrics,
            "confusion_matrix": result.report["confusion_matrix"],
            "artifacts": result.artifact_paths,
        }
    )
    mlflow_client.close()


if __name__ == "__main__":
    main()
