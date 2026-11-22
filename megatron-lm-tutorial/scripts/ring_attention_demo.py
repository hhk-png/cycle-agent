#!/usr/bin/env python
"""Compare ring attention with a dense causal reference and print the plan."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torch

from mini_megatron.parallel.ring_attention import (
    dense_causal_attention,
    ring_attention,
    ring_attention_plan,
)


def main() -> int:
    torch.manual_seed(0)
    seq, heads, dim = 32, 4, 16
    q = torch.randn(2, heads, seq, dim)
    k = torch.randn(2, heads, seq, dim)
    v = torch.randn(2, heads, seq, dim)
    ref = dense_causal_attention(q, k, v)
    rows = []
    for cp in (1, 2, 4, 8):
        out, stats = ring_attention(q, k, v, cp_size=cp, return_stats=True)
        rows.append(
            {
                "cp": cp,
                "max_abs_error": float((out - ref).abs().max()),
                "blocks_per_rank": stats["blocks_per_rank"],
                "imbalance": stats["imbalance"],
            }
        )
    print(json.dumps({"cases": rows, "plan_cp8": ring_attention_plan(8)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
