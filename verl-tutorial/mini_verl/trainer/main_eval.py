"""Standalone evaluation entry point, mirroring ``verl/trainer/main_eval.py``.

The evaluator loads the config stored next to a checkpoint, restores the actor
weights, draws ``n`` samples per validation prompt, and reports unbiased
pass@k.  It deliberately forces the *naive* reward manager: training-time
shaping (DAPO overlong penalty, format bonuses) is part of the optimization
objective, not part of the capability being measured.

Examples::

    python -m mini_verl.trainer.main_eval \
        --checkpoint outputs/checkpoints/demo/grpo/global_step_60 \
        --n 8 --k 1,4,8

    python -m mini_verl.trainer.main_eval \
        --checkpoint ... --split train --n 4 --k 1,4 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import load_config
from .ppo_trainer import PPOTrainer

__all__ = ["build_parser", "main", "evaluate_checkpoint"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniVerl standalone pass@k evaluator")
    parser.add_argument("--checkpoint", required=True, help="path to .../global_step_<N>")
    parser.add_argument("--split", default="val", choices=["val", "train"])
    parser.add_argument("--n", type=int, default=1, help="samples per prompt")
    parser.add_argument("--k", default="1", help="comma-separated k values, e.g. 1,4,8")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--json", action="store_true", help="print metrics as JSON")
    parser.add_argument("overrides", nargs="*", help="extra key=value config overrides")
    return parser


def evaluate_checkpoint(
    checkpoint: str | Path,
    n: int = 1,
    ks: tuple[int, ...] = (1,),
    split: str = "val",
    temperature: float | None = None,
    top_p: float = 1.0,
    overrides: list[str] | tuple[str, ...] = (),
) -> dict[str, float]:
    directory = Path(checkpoint)
    if not directory.exists():
        raise FileNotFoundError(f"checkpoint does not exist: {directory}")
    if not directory.is_dir() or not directory.name.startswith("global_step_"):
        raise ValueError("checkpoint must be a .../global_step_<N> directory")
    config_path = directory / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"missing {config_path}; cannot reconstruct the run config")

    config = load_config(config_path, overrides)
    config.trainer.resume_mode = "auto"
    config.trainer.resume_from_path = str(directory)
    # Evaluation measures capability, not the shaped training objective.
    config.reward.reward_manager = "naive"
    config.algorithm.overlong_penalty_factor = 0.0
    config.trainer.logger = []
    trainer = PPOTrainer(config).init()
    return trainer.evaluate(
        n=n,
        ks=ks,
        split=split,
        temperature=temperature,
        top_p=top_p,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ks = tuple(int(part) for part in args.k.split(",") if part.strip())
    metrics = evaluate_checkpoint(
        args.checkpoint,
        n=args.n,
        ks=ks,
        split=args.split,
        temperature=args.temperature,
        top_p=args.top_p,
        overrides=args.overrides,
    )
    if args.json:
        print(json.dumps(metrics, indent=2, ensure_ascii=False))
    else:
        print(f"checkpoint={args.checkpoint} split={args.split} n={args.n}")
        for key, value in sorted(metrics.items()):
            print(f"{key}={value:.4f}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
