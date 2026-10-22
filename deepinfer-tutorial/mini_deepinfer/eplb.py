"""Expert-parallel load balancing (EPLB).

The implementation follows the two-stage idea of DeepInfer's public policy:
first give hot logical experts extra replicas, then place all physical experts
onto ranks so that the maximum per-rank load is minimized.  At toy scale the
placement is a greedy algorithm; production uses a batched greedy over a
history window and shuffles weights over NVLink shared memory.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class EPLBPlan:
    physical_to_logical: np.ndarray  # [num_physical]
    logical_to_physical: np.ndarray  # [num_logical, max_replicas], -1 padded
    logical_count: np.ndarray  # [num_logical]
    rank_experts: list[list[int]]  # one list of physical ids per rank


def _add_replicas(loads: np.ndarray, num_redundant: int) -> tuple[np.ndarray, np.ndarray]:
    """Greedily replicate the expert with the highest per-replica load."""
    num_logical = loads.size
    physical_to_logical = list(range(num_logical))
    counts = np.ones(num_logical, dtype=np.int64)
    for _ in range(num_redundant):
        per_replica = loads / counts
        target = int(np.argmax(per_replica))
        physical_to_logical.append(target)
        counts[target] += 1
    return np.asarray(physical_to_logical, dtype=np.int64), counts


def _place_greedy(
    loads: np.ndarray,
    physical_to_logical: np.ndarray,
    counts: np.ndarray,
    num_ranks: int,
    capacity: int,
) -> list[list[int]]:
    """Place physical experts on ranks, minimising the maximum rank load."""
    per_replica = loads / counts
    order = sorted(
        range(len(physical_to_logical)),
        key=lambda physical: (-per_replica[physical_to_logical[physical]], physical),
    )
    rank_loads = np.zeros(num_ranks, dtype=np.float64)
    rank_counts = np.zeros(num_ranks, dtype=np.int64)
    rank_experts: list[list[int]] = [[] for _ in range(num_ranks)]
    for physical in order:
        logical = int(physical_to_logical[physical])
        candidates = [
            rank for rank in range(num_ranks) if rank_counts[rank] < capacity
        ]
        if not candidates:
            raise ValueError("not enough rank capacity for physical experts")
        # Try each candidate and keep the one with the smallest resulting
        # maximum load (ties go to the lowest rank id).
        best_rank = min(
            candidates,
            key=lambda rank: (
                max(rank_loads[rank] + per_replica[logical], np.max(rank_loads)),
                rank,
            ),
        )
        rank_experts[best_rank].append(physical)
        rank_loads[best_rank] += per_replica[logical]
        rank_counts[best_rank] += 1
    return rank_experts


def rebalance_experts(
    tokens_per_expert: np.ndarray,
    num_physical_experts: int,
    num_local_physical_experts: int,
) -> EPLBPlan:
    """Compute an EPLB plan.

    ``tokens_per_expert`` is either ``[num_logical]`` or
    ``[num_steps, num_logical]`` (the history window).
    """
    loads = np.asarray(tokens_per_expert, dtype=np.float64)
    if loads.ndim == 2:
        loads = loads.sum(axis=0)
    if loads.ndim != 1:
        raise ValueError("tokens_per_expert must be 1-D or 2-D")
    num_logical = loads.size
    if num_physical_experts < num_logical:
        raise ValueError("physical experts cannot be fewer than logical experts")
    if num_physical_experts % num_local_physical_experts:
        raise ValueError("physical experts must be divisible by local capacity")
    num_ranks = num_physical_experts // num_local_physical_experts
    redundant = num_physical_experts - num_logical
    physical_to_logical, counts = _add_replicas(loads, redundant)
    rank_experts = _place_greedy(
        loads, physical_to_logical, counts, num_ranks, num_local_physical_experts
    )
    max_replicas = int(counts.max()) if counts.size else 0
    logical_to_physical = np.full((num_logical, max_replicas), -1, dtype=np.int64)
    for physical, logical in enumerate(physical_to_logical):
        slot = int(np.sum(logical_to_physical[logical] >= 0))
        logical_to_physical[logical, slot] = physical
    return EPLBPlan(
        physical_to_logical=physical_to_logical,
        logical_to_physical=logical_to_physical,
        logical_count=counts,
        rank_experts=rank_experts,
    )


def rank_loads(loads: np.ndarray, plan: EPLBPlan) -> np.ndarray:
    loads = np.asarray(loads, dtype=np.float64)
    if loads.ndim == 2:
        loads = loads.sum(axis=0)
    per_replica = loads / plan.logical_count
    return np.asarray(
        [
            sum(per_replica[int(plan.physical_to_logical[p])] for p in experts)
            for experts in plan.rank_experts
        ],
        dtype=np.float64,
    )


def imbalance(loads: np.ndarray, plan: EPLBPlan) -> float:
    """``max(rank load) / mean(rank load)``; 1.0 is perfectly balanced."""
    distribution = rank_loads(loads, plan)
    mean = float(np.mean(distribution))
    return float(np.max(distribution) / mean) if mean > 0 else 1.0


def no_replication_plan(num_logical: int, num_physical: int, capacity: int) -> EPLBPlan:
    """A baseline that places logical experts round-robin without replicas."""
    physical_to_logical = np.arange(num_physical, dtype=np.int64) % num_logical
    counts = np.ones(num_logical, dtype=np.int64)
    rank_experts = [
        list(range(rank * capacity, (rank + 1) * capacity))
        for rank in range(num_physical // capacity)
    ]
    logical_to_physical = np.full((num_logical, 1), -1, dtype=np.int64)
    for physical, logical in enumerate(physical_to_logical):
        if logical_to_physical[logical, 0] < 0:
            logical_to_physical[logical, 0] = physical
    return EPLBPlan(physical_to_logical, logical_to_physical, counts, rank_experts)
