#!/usr/bin/env python
"""Compare the Ulysses reference with dense attention at several CP sizes.

This is the Ulysses counterpart of ``scripts/ring_attention_demo.py``.  It
simulates the two all-to-all layout transformations in one process and prints
the numerical error, the exactness of the layout round trip, and the
per-rank/aggregate communication volume.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini_megatron.parallel.ulysses import (
    dense_causal_attention,
    ulysses_attention,
    ulysses_layout_roundtrip,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cp", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--batch", type=int, default=2)
    parser.add_argument("--seq", type=int, default=64)
    parser.add_argument("--heads", type=int, default=8)
    parser.add_argument("--dim", type=int, default=16)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    g = torch.Generator().manual_seed(args.seed)
    q = torch.randn(args.batch, args.heads, args.seq, args.dim, generator=g)
    k = torch.randn(args.batch, args.heads, args.seq, args.dim, generator=g)
    v = torch.randn(args.batch, args.heads, args.seq, args.dim, generator=g)
    ref = dense_causal_attention(q, k, v)

    for cp in args.cp:
        out = ulysses_attention(q, k, v, cp_size=cp)
        max_abs = (out - ref).abs().max().item()
        _, exact = ulysses_layout_roundtrip(q, cp_size=cp)
        _, stats = ulysses_attention(q, k, v, cp_size=cp, return_stats=True)
        print(
            json.dumps(
                {
                    "cp": cp,
                    "max_abs_error": max_abs,
                    "layout_roundtrip_exact": exact,
                    "send_elements_per_rank": stats["send_elements_per_rank"],
                    "total_elements": stats["total_elements"],
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
