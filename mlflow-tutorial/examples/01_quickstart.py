"""Quickstart: train, log, evaluate, package, register, reload."""

from __future__ import annotations

from _common import client, print_json, unique_experiment

from mini_mlflow.evaluate import evaluate_model
from mini_mlflow.models import LinearRegression, infer_signature


def main() -> None:
    mlflow_client = client()
    experiment = unique_experiment(mlflow_client, "quickstart")
    X = [{"floor": 2.0}, {"floor": 5.0}, {"floor": 8.0}, {"floor": 11.0}]
    y = [2.1, 3.9, 6.2, 8.1]

    run = mlflow_client.start_run(
        experiment_id=experiment.experiment_id,
        run_name="linear-baseline",
    )
    run_id = run.info.run_id
    try:
        model = LinearRegression(alpha=0.01).fit(X, y)
        predictions = model.predict(X)
        mlflow_client.log_params(run_id, {"model": "linear", "alpha": 0.01, "features": "floor"})
        rmse = (sum((p - t) ** 2 for p, t in zip(predictions, y)) / len(y)) ** 0.5
        mlflow_client.log_metric(run_id, "train_rmse", rmse)
        mlflow_client.log_text(run_id, "first run", "notes/readme.md")
        signature = infer_signature(X, predictions)
        info = mlflow_client.log_model(
            run_id,
            model,
            artifact_path="model",
            registered_model_name="house-price-linear",
            input_example=[{"floor": 6.0}],
            signature=signature,
        )
        evaluation = evaluate_model(
            model,
            X,
            y,
            task="regression",
            client=mlflow_client,
            run_id=run_id,
            prefix="eval",
        )
    finally:
        current = mlflow_client.get_run(run_id)
        if not current.info.is_terminal:
            mlflow_client.end_run(run_id, "FINISHED")

    loaded = mlflow_client.load_model(info.model_uri)
    print_json(
        {
            "tracking_uri": mlflow_client.tracking_uri,
            "experiment_id": experiment.experiment_id,
            "run_id": run_id,
            "model_uri": info.model_uri,
            "registered_version": info.version,
            "eval_metrics": evaluation.metrics,
            "prediction_for_floor_6": loaded.predict([{"floor": 6.0}]),
        }
    )
    mlflow_client.close()


if __name__ == "__main__":
    main()
