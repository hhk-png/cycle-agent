"""Run the bundled MLproject entry point, with source snapshot and logs."""

from __future__ import annotations

from pathlib import Path

from _common import client, print_json

from mini_mlflow.projects import run_project

PROJECT = Path(__file__).resolve().parent / "projects" / "churn"


def main() -> None:
    mlflow_client = client()
    result = run_project(
        PROJECT,
        parameters={"alpha": 0.05, "seed": 7},
        experiment_name="churn-project",
        run_name="churn-demo",
        client=mlflow_client,
    )
    run = mlflow_client.get_run(result.run_id)
    print_json(
        {
            "run_id": result.run_id,
            "status": result.status,
            "exit_code": result.exit_code,
            "command": result.command,
            "metrics": run.data.metrics,
            "artifacts": [item.path for item in mlflow_client.list_artifacts(result.run_id)],
        }
    )
    mlflow_client.close()


if __name__ == "__main__":
    main()
