"""Shared helper for the example scripts."""

from __future__ import annotations

import sys
from pathlib import Path


def add_project_root() -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def fmt_stats(stats: dict) -> str:
    metrics = stats["metrics"]
    return (
        f"steps={metrics['steps']} out_tok/s={metrics['output_tokens_per_s']:.1f} "
        f"ttft={metrics['ttft_mean_ms']:.1f}ms tpot={metrics['tpot_mean_ms']:.1f}ms "
        f"cache_hits={stats['block_manager']['cache_hits']}"
    )
