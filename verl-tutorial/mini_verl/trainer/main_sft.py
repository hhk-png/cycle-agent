"""CLI entry point for supervised fine-tuning."""

from __future__ import annotations

import argparse
import json

from ..config import load_config
from .sft_trainer import SFTTrainer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniVerl SFT trainer")
    parser.add_argument("config", nargs="?", default=None)
    parser.add_argument("overrides", nargs="*")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config, args.overrides)
    result = SFTTrainer(config).fit()
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
