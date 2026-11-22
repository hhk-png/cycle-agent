"""CLI entry point: ``python -m mini_verl.trainer.main_ppo``.

Mirrors ``verl.trainer.main_ppo``'s Hydra override style::

    python -m mini_verl.trainer.main_ppo \
        algorithm.adv_estimator=grpo \
        actor_rollout_ref.rollout.n=4 \
        trainer.total_training_steps=20
"""

from __future__ import annotations

import argparse
import json
from pprint import pprint

from ..config import load_config
from .ppo_trainer import PPOTrainer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniVerl PPO/GRPO trainer")
    parser.add_argument("config", nargs="?", default=None, help="YAML config path")
    parser.add_argument("overrides", nargs="*", help="Hydra-style key=value overrides")
    parser.add_argument("--dry-run", action="store_true", help="print the resolved config and exit")
    parser.add_argument("--json", action="store_true", help="print the final metrics as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config, args.overrides)
    if args.dry_run:
        pprint(config.to_dict())
        return 0
    trainer = PPOTrainer(config).init()
    result = trainer.fit()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"steps={result['steps']}")
        pprint(result.get("history", {}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
