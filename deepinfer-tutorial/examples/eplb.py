"""Expert-parallel load balancing on a skewed expert-load trace."""

from __future__ import annotations

import numpy as np

from _common import add_project_root

add_project_root()

from mini_deepinfer.eplb import imbalance, no_replication_plan, rank_loads, rebalance_experts


def main() -> None:
    loads = np.array([1200, 900, 700, 300, 120, 60, 30, 10], dtype=float)
    print("logical expert load:", loads.tolist())

    baseline = no_replication_plan(8, 8, 4)
    print("baseline ranks:", baseline.rank_experts)
    print("baseline imbalance:", round(imbalance(loads, baseline), 4))

    for ep_size, redundant in [(2, 4), (4, 8)]:
        physical = len(loads) + redundant
        capacity = physical // ep_size
        plan = rebalance_experts(loads, physical, capacity)
        print()
        print(f"EP={ep_size}, physical={physical}, experts/rank={capacity}")
        print("physical -> logical:", plan.physical_to_logical.tolist())
        print("logical replica counts:", plan.logical_count.tolist())
        print("rank experts:", plan.rank_experts)
        print("rank load:", np.round(rank_loads(loads, plan), 1).tolist())
        print("imbalance:", round(imbalance(loads, plan), 4))


if __name__ == "__main__":
    main()
