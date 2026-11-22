#!/usr/bin/env python
"""Show aux-free bias balancing and node-limited top-k on synthetic routes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torch

from mini_megatron.parallel.moe_balance import AuxFreeBalancer, node_limited_topk


def main() -> int:
    torch.manual_seed(0)
    experts, tokens, top_k = 8, 4096, 2
    # A small update speed avoids the sign-update overshoot: a bias change
    # larger than the score gap flips a whole expert on/off every step.
    balancer = AuxFreeBalancer(experts, update_speed=0.005, normalize=True)
    base = torch.randn(tokens, experts) * 0.05
    base[:, 0] += 0.10
    base[:, 1] += 0.08
    scores = torch.sigmoid(base)

    loads = []
    for _ in range(200):
        _, idx = balancer.select(scores, top_k)
        counts = torch.bincount(idx.flatten(), minlength=experts).float()
        loads.append(counts)
        balancer.update(counts)

    # Node-limited routing: 8 experts, 2 per node.
    _, idx_unlimited = scores[:8].topk(top_k, dim=-1)
    _, idx_limited = node_limited_topk(
        scores[:8], top_k, experts_per_node=2, max_nodes=1
    )

    def nodes(idx):
        return [sorted(set((row // 2).tolist())) for row in idx]

    print(
        json.dumps(
            {
                "initial_counts": [int(x) for x in loads[0]],
                "final_counts": [int(x) for x in loads[-1]],
                "max_load_first": int(loads[0].max()),
                "max_load_last": int(loads[-1].max()),
                "ideal_load": tokens * top_k // experts,
                "final_bias": [round(float(x), 4) for x in balancer.bias],
                "node_unlimited": nodes(idx_unlimited),
                "node_limited_max1": nodes(idx_limited),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
