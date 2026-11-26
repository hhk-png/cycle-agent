"""Enable autolog, fit once, and inspect what was captured."""

from __future__ import annotations

from _common import client, configure_fluent, print_json, unique_experiment

import mini_mlflow as mlflow
from mini_mlflow.models import LinearRegression


def main() -> None:
    mlflow_client = client()
    experiment = unique_experiment(mlflow_client, "autolog-demo")
    configure_fluent(mlflow_client)
    mlflow.autolog(log_input_examples=True)
    try:
        with mlflow.start_run(
            experiment_id=experiment.experiment_id,
            run_name="autolog-fit",
        ) as active:
            LinearRegression(alpha=0.001).fit(
                [{"x": 1.0}, {"x": 2.0}, {"x": 3.0}, {"x": 4.0}],
                [2.0, 4.0, 6.0, 8.0],
            )
        run = mlflow_client.get_run(active.info.run_id)
        print_json(
            {
                "run_id": active.info.run_id,
                "params": run.data.params,
                "metrics": run.data.metrics,
                "tags": run.data.tags,
                "artifacts": [
                    item.path for item in mlflow_client.list_artifacts(active.info.run_id)
                ],
            }
        )
    finally:
        mlflow.disable_autolog()
        mlflow_client.close()


if __name__ == "__main__":
    main()
