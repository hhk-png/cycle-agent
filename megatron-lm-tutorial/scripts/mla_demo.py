#!/usr/bin/env python
"""Run a tiny MLA prefill/decode and print the cache saving.

This is a demonstration, not a benchmark.  It exercises the same code path as
``tests/unit/test_mla.py`` and is useful when reading chapter 30:

    python scripts/mla_demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini_megatron.parallel.mla import MLAttention  # noqa: E402


def main() -> int:
    torch.manual_seed(0)
    b, s = 2, 16
    hidden, heads = 64, 8
    d_nope, d_rope, d_v = 16, 8, 16
    kv_lora = 32
    model = MLAttention(
        hidden,
        heads,
        q_lora_rank=32,
        kv_lora_rank=kv_lora,
        qk_nope_head_dim=d_nope,
        qk_rope_head_dim=d_rope,
        v_head_dim=d_v,
        max_seq_len=s,
    ).eval()

    x = torch.randn(b, s, hidden)
    pos = torch.arange(s).unsqueeze(0).expand(b, -1)
    with torch.no_grad():
        full = model(x, position_ids=pos)

        prefill = s // 2
        pref, cache = model(
            x[:, :prefill], position_ids=pos[:, :prefill], use_cache=True
        )
        pieces = [pref]
        for t in range(prefill, s):
            step, cache = model(
                x[:, t : t + 1],
                position_ids=torch.full((b, 1), t, dtype=torch.long),
                past_key_value=cache,
                use_cache=True,
            )
            pieces.append(step)
        incremental = torch.cat(pieces, dim=1)

    c_kv, k_pe = cache
    mla_bytes = (c_kv.numel() + k_pe.numel()) * c_kv.element_size()
    mha_bytes = 2 * heads * d_v * s * b * c_kv.element_size()
    result = {
        "max_abs_diff_full_vs_incremental": float((full - incremental).abs().max()),
        "cache_elements_mla": int(c_kv.numel() + k_pe.numel()),
        "cache_elements_mha_equivalent": int(2 * heads * d_v * s * b),
        "cache_ratio_mha_over_mla": round(mha_bytes / mla_bytes, 2),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
