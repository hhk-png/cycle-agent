"""CLI entry point for reward-model training.

Example:
    python -X utf8 -m mini_verl.trainer.main_rm configs/rm_bandit.yaml

The same YAML tree as PPO is used (``data``/``trainer``) plus
``reward.reward_model`` for RM-specific fields.  This mirrors the way the
tutorial keeps one configuration vocabulary across trainers.
"""

from __future__ import annotations

import argparse
import json

from ..config import load_config
from ..reward_model import RMTrainer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniVerl reward-model trainer")
    parser.add_argument("config", nargs="?", default=None)
    parser.add_argument("overrides", nargs="*")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config, args.overrides)
    options = dict(config.reward.reward_model or {})
    trainer = RMTrainer(config)
    result = trainer.fit()
    if not args.no_save and options.get("save", True):
        path = options.get(
            "output_dir",
            f"{config.trainer.log_dir}/reward_model/{config.trainer.experiment_name}",
        )
        saved = trainer.save(path)
        result["checkpoint"] = str(saved)
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
