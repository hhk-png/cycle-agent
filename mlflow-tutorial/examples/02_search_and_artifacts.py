"""Search, metric history, and artifact round-trip."""

from __future__ import annotations

import tempfile
from pathlib import Path

from _common import client, print_json, unique_experiment


def main() -> None:
    mlflow_client = client()
    experiment = unique_experiment(mlflow_client, "search-demo")
    for alpha, score in [(0.1, 0.72), (0.5, 0.88), (1.0, 0.81)]:
        run = mlflow_client.start_run(
            experiment_id=experiment.experiment_id,
            run_name=f"alpha-{alpha}",
            tags={"team": "tutorial", "stage": "explore"},
        )
        rid = run.info.run_id
        mlflow_client.log_param(rid, "alpha", alpha)
        mlflow_client.log_metric(rid, "score", score, step=1)
        mlflow_client.log_metric(rid, "score", score + 0.01, step=2)
        mlflow_client.log_input(
            rid,
            dataset_name="tutorial-transactions",
            dataset_digest=f"sha256:alpha-{alpha}",
            dataset_source="s3://tutorial/transactions/2026-11-20",
            dataset_source_type="s3",
            context="training",
        )
        mlflow_client.log_text(
            rid,
            f"alpha={alpha}\nscore={score}\n",
            "reports/summary.txt",
        )
        mlflow_client.end_run(rid, "FINISHED")

    best, token = mlflow_client.search_runs(
        'metrics.score > 0.8 and tags.team = "tutorial"',
        order_by=["metrics.score DESC"],
        max_results=10,
    )
    dataset_runs, _ = mlflow_client.search_runs(
        'datasets.name = "tutorial-transactions" AND datasets.context = "training"',
        max_results=10,
    )
    best_run = best[0]
    history = mlflow_client.get_metric_history(best_run.info.run_id, "score")
    with tempfile.TemporaryDirectory(prefix="mini-mlflow-download-") as tmp:
        downloaded = mlflow_client.download_artifacts(
            best_run.info.run_id, "reports", tmp
        )
        content = (Path(downloaded) / "summary.txt").read_text(encoding="utf-8")

    print_json(
        {
            "experiment": experiment.name,
            "matched_runs": [run.info.run_name for run in best],
            "dataset_matched_runs": sorted(run.info.run_name for run in dataset_runs),
            "best": best_run.info.run_name,
            "next_page_token": token,
            "metric_history": [point.value for point in history],
            "artifact_content": content.strip().splitlines(),
        }
    )
    mlflow_client.close()


if __name__ == "__main__":
    main()
