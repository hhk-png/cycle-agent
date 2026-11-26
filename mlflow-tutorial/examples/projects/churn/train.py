"""A dependency-free churn demo used by the MLproject runner.

The child process resumes the run created by the parent via MINI_MLFLOW_RUN_ID.
That explicit resume is the contract: active runs never cross process
boundaries by themselves.
"""

from __future__ import annotations

import argparse
import os
import random

import mini_mlflow as mlflow
from mini_mlflow.models import LinearRegression


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alpha", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    rows = []
    targets = []
    for _ in range(120):
        tenure = rng.uniform(1, 48)
        support_calls = rng.randint(0, 10)
        monthly = rng.uniform(10, 120)
        churn_score = 0.6 - 0.012 * tenure + 0.04 * support_calls + 0.0005 * monthly
        rows.append(
            {
                "tenure_months": tenure,
                "support_calls": support_calls,
                "monthly_charge": monthly,
            }
        )
        targets.append(churn_score)

    run_id = os.environ["MINI_MLFLOW_RUN_ID"]
    with mlflow.start_run(run_id=run_id):
        model = LinearRegression(alpha=args.alpha).fit(rows, targets)
        predictions = model.predict(rows)
        mae = sum(abs(p - t) for p, t in zip(predictions, targets)) / len(targets)
        mlflow.log_metric("train_mae", mae)
        mlflow.log_metric("n_rows", len(rows))
        mlflow.log_model(
            model,
            artifact_path="model",
            input_example=rows[:3],
        )
        print(f"trained rows={len(rows)} train_mae={mae:.6f}")


if __name__ == "__main__":
    main()
