"""Evaluate a saved checkpoint without a training loop.

This mirrors ``verl/trainer/main_eval.py``: the run config is read from the
checkpoint directory, weights are restored, ``n`` samples per prompt are drawn,
and unbiased pass@k is reported.  The reward manager is forced to ``naive`` so
training-time shaping does not leak into the capability metric.

The same path is available from the CLI::

    python -m mini_verl.trainer.main_eval \
        --checkpoint <run>/global_step_<N> --n 8 --k 1,4,8
"""

import tempfile
from pathlib import Path

from _common import bandit_config, header

from mini_verl.trainer.main_eval import evaluate_checkpoint
from mini_verl.trainer.ppo_trainer import PPOTrainer


def main() -> None:
    header("Standalone evaluation: checkpoint -> n samples -> pass@k")
    with tempfile.TemporaryDirectory() as directory:
        config = bandit_config(steps=30, n=8)
        config.trainer.log_dir = directory
        trainer = PPOTrainer(config).init()
        trainer.fit()
        checkpoint = trainer.save_checkpoint()
        print("checkpoint:", Path(checkpoint).name)

        metrics = evaluate_checkpoint(checkpoint, n=8, ks=(1, 4, 8), split="val")
        for key in ("val/accuracy", "val/pass@1", "val/pass@4", "val/pass@8"):
            print(f"{key}={metrics[key]:.3f}")
        print("n=8 is the group size; pass@8 measures whether any of 8 tries works.")


if __name__ == "__main__":
    main()
