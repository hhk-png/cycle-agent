"""MLflow 3 logged models: create, search, tag, load by ``models:/<id>``.

This example uses the local backend: MiniMLflow can serve the metadata over
REST, but it does not implement remote logged-model artifact download.
"""

from __future__ import annotations

from _common import client, print_json, unique_experiment

from mini_mlflow.models import LinearRegression


def main() -> None:
    mlflow_client = client()
    experiment = unique_experiment(mlflow_client, "logged-models")
    run = mlflow_client.start_run(
        experiment_id=experiment.experiment_id,
        run_name="train-two-models",
    )
    run_id = run.info.run_id
    try:
        x = [
            [1.0, 0.0],
            [0.0, 1.0],
            [1.0, 1.0],
            [2.0, 1.0],
            [1.0, 2.0],
        ]
        y = [3.0, 5.0, 7.0, 9.0, 11.0]
        model = LinearRegression(alpha=0.1).fit(x, y)

        # In MLflow 3, ``log_model(name=...)`` creates a LoggedModel and puts
        # the package under the model's own artifact location.  The returned
        # URI is ``models:/<model_id>``, not ``runs:/...``.
        info = mlflow_client.log_model(
            run_id,
            model,
            name="underwriting-linear",
            model_type="Regressor",
            params={"alpha": 0.1, "features": "two"},
            tags={"team": "risk", "stage": "candidate"},
            input_example=x[:1],
        )
        mlflow_client.log_logged_model_metrics(
            info.model_id, {"validation_rmse": 0.42, "latency_ms": 3.1}, step=1
        )
        mlflow_client.set_logged_model_tags(
            info.model_id, {"stage": "production", "reviewed": "true"}
        )

        # A second, deliberately unfinished model shows the lifecycle:
        # pending models are visible but must not be served.
        draft = mlflow_client.create_logged_model(
            "underwriting-draft",
            experiment_id=experiment.experiment_id,
            source_run_id=run_id,
            model_type="Regressor",
            params={"alpha": 1.0},
        )
        mlflow_client.finalize_logged_model(
            draft.model_id,
            "LOGGED_MODEL_UPLOAD_FAILED",
            status_message="artifact upload was interrupted",
        )

        ready, token = mlflow_client.search_logged_models(
            [experiment.experiment_id],
            filter_string="status = 'READY' AND metrics.validation_rmse < 1",
            order_by=["metrics.validation_rmse ASC"],
        )
        loaded = mlflow_client.load_model(info.model_uri)
        fetched = mlflow_client.get_logged_model(info.model_id)
        print_json(
            {
                "experiment_id": experiment.experiment_id,
                "source_run_id": run_id,
                "model_id": info.model_id,
                "model_uri": info.model_uri,
                "status": fetched.status,
                "tags": fetched.tags,
                "params": fetched.params,
                "metrics": {key: metric.value for key, metric in fetched.metrics.items()},
                "ready_models": [item.name for item in ready],
                "next_page_token": token,
                "draft_status": mlflow_client.get_logged_model(draft.model_id).status,
                "prediction": loaded.predict([[3.0, 3.0]]),
            }
        )
    finally:
        current = mlflow_client.get_run(run_id)
        if not current.info.is_terminal:
            mlflow_client.end_run(run_id, "FINISHED")
        mlflow_client.close()


if __name__ == "__main__":
    main()
