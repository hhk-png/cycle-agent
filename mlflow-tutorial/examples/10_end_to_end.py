"""End-to-end: train → evaluate → package → register → alias → reload."""

from __future__ import annotations

from _common import client, print_json, unique_experiment

from mini_mlflow.evaluate import evaluate_model
from mini_mlflow.models import LinearRegression, infer_signature


def main() -> None:
    mlflow_client = client()
    experiment = unique_experiment(mlflow_client, "lifecycle-demo")
    train = [
        {"tenure": 1.0, "calls": 8.0},
        {"tenure": 6.0, "calls": 4.0},
        {"tenure": 12.0, "calls": 1.0},
        {"tenure": 24.0, "calls": 0.0},
    ]
    target = [0.8, 0.6, 0.3, 0.1]
    test = [{"tenure": 3.0, "calls": 6.0}, {"tenure": 18.0, "calls": 1.0}]
    test_target = [0.6, 0.2]

    run = mlflow_client.start_run(
        experiment_id=experiment.experiment_id,
        run_name="candidate-v1",
        tags={"stage": "candidate"},
    )
    rid = run.info.run_id
    try:
        model = LinearRegression(alpha=0.1).fit(train, target)
        signature = infer_signature(test, model.predict(test))
        info = mlflow_client.log_model(
            rid,
            model,
            registered_model_name="lifecycle-demo-model",
            input_example=test,
            signature=signature,
        )
        evaluation = evaluate_model(
            model,
            test,
            test_target,
            task="regression",
            client=mlflow_client,
            run_id=rid,
            prefix="test",
        )
        mlflow_client.set_tag(rid, "eval_status", "passed")
    finally:
        mlflow_client.end_run(rid, "FINISHED")

    mlflow_client.set_registered_model_alias(
        "lifecycle-demo-model", "champion", info.version
    )
    resolved = mlflow_client.get_model_version_by_alias(
        "lifecycle-demo-model", "champion"
    )
    loaded = mlflow_client.load_model("models:/lifecycle-demo-model@champion")
    print_json(
        {
            "run_id": rid,
            "model_version": resolved.version,
            "source": resolved.source,
            "eval": evaluation.metrics,
            "predictions": loaded.predict(test),
        }
    )
    mlflow_client.close()


if __name__ == "__main__":
    main()
