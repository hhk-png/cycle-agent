"""CLI entry point for process-reward-model training.

Example:
    python -X utf8 -m mini_verl.trainer.main_prm configs/prm_bandit.yaml

The config tree is the same as PPO/SFT/DPO/RM.  PRM-specific fields live under
``reward.prm``; after training, point ``reward.prm.model_path`` at the saved
checkpoint to use the PRM as the PPO reward.

This trainer is a MiniVerl teaching extension.  Real verl serves reward models
through the reward loop and does not ship a PRM trainer with this CLI.
"""

from __future__ import annotations

import argparse
import json

from ..config import load_config
from ..prm import PRMTrainer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniVerl process-reward-model trainer")
    parser.add_argument("config", nargs="?", default=None)
    parser.add_argument("overrides", nargs="*")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config, args.overrides)
    options = dict(config.reward.prm or {})
    trainer = PRMTrainer(config)
    result = trainer.fit()
    if not args.no_save and options.get("save", True):
        path = options.get(
            "output_dir",
            f"{config.trainer.log_dir}/process_reward_model/{config.trainer.experiment_name}",
        )
        saved = trainer.save(path)
        result["checkpoint"] = str(saved)
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
