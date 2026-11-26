"""Register a model version and resolve it through an alias."""

from __future__ import annotations

from _common import client, print_json, unique_experiment

from mini_mlflow.models import LinearRegression


def main() -> None:
    mlflow_client = client()
    experiment = unique_experiment(mlflow_client, "registry-demo")
    model = LinearRegression().fit(
        [{"x": 1.0}, {"x": 2.0}, {"x": 3.0}],
        [2.0, 4.0, 6.0],
    )
    run = mlflow_client.start_run(
        experiment_id=experiment.experiment_id,
        run_name="train-registry",
    )
    rid = run.info.run_id
    info = mlflow_client.log_model(
        rid,
        model,
        registered_model_name="demo-linear",
        input_example=[{"x": 4.0}],
    )
    mlflow_client.end_run(rid, "FINISHED")

    mlflow_client.set_registered_model_alias("demo-linear", "champion", info.version)
    version = mlflow_client.get_model_version_by_alias("demo-linear", "champion")
    loaded = mlflow_client.load_model(f"models:/demo-linear@champion")
    print_json(
        {
            "run_id": rid,
            "model_version": version.version,
            "source": version.source,
            "prediction": loaded.predict([{"x": 10.0}]),
        }
    )
    mlflow_client.close()


if __name__ == "__main__":
    main()
