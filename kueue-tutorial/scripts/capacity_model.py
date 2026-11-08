#!/usr/bin/env python3
"""A small, dependency-free capacity simulator for GPU batch clusters.

It answers questions that a static quota spreadsheet cannot:

  * If jobs arrive at rate lambda and need k GPUs for S hours, what are the
    P95/P99 queueing times?
  * How much does requiring a whole job to fit on one node reduce effective
    capacity (fragmentation)?
  * How much does StrictFIFO head-of-line blocking cost compared with
    BestEffortFIFO?

The model is deliberately simple and deterministic given --seed: Poisson
arrivals, per-job size/duration distributions, whole-node gang placement (or
multi-node placement with --allow-multi-node), and a FIFO queue with either
strict or best-effort head-of-line behavior.

Examples:
  python scripts/capacity_model.py --nodes 12 --gpus-per-node 8 \
      --arrival-rate 0.5 --mean-duration 6 --job-size 8 --hours 2160

  python scripts/capacity_model.py --nodes 12 --gpus-per-node 8 \
      --arrival-rate 2 --mean-duration 4 \
      --job-size-mix "1:0.5,2:0.2,8:0.2,32:0.1" \
      --policy best-effort --allow-multi-node --hours 2160

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Job:
    job_id: int
    size: int
    arrival: float
    duration: float
    start: Optional[float] = None
    # Exact per-node allocation, used to release capacity symmetrically.
    allocation: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Result:
    arrivals: int
    completed: int
    still_queued: int
    running: int
    wait_p50: Optional[float]
    wait_p95: Optional[float]
    wait_p99: Optional[float]
    wait_mean: Optional[float]
    max_queue: int
    avg_queue: float
    gpu_utilization: float
    fully_free_nodes_at_end: int
    free_gpus_per_node_at_end: float


def parse_mix(spec: str) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        size_s, weight_s = part.split(":", 1)
        size = int(size_s)
        weight = float(weight_s)
        if size <= 0 or weight < 0:
            raise argparse.ArgumentTypeError(f"invalid job-size mix entry: {part!r}")
        if weight > 0:
            out.append((size, weight))
    if not out:
        raise argparse.ArgumentTypeError("job-size mix must contain at least one positive weight")
    return out


def sample_size(rng: random.Random, fixed: int, mix: Optional[list[tuple[int, float]]]) -> int:
    if not mix:
        return fixed
    total = sum(w for _, w in mix)
    pick = rng.random() * total
    upto = 0.0
    for size, weight in mix:
        upto += weight
        if pick <= upto:
            return size
    return mix[-1][0]


def sample_duration(rng: random.Random, mean: float, cv: float, distribution: str) -> float:
    if mean <= 0:
        return 0.0
    if distribution == "exponential":
        return rng.expovariate(1.0 / mean)
    # Log-normal with the requested coefficient of variation. This is a common
    # shape for job runtimes: many short jobs and a long tail.
    if cv <= 0:
        return mean
    sigma2 = math.log(1.0 + cv * cv)
    mu = math.log(mean) - sigma2 / 2.0
    return rng.lognormvariate(mu, math.sqrt(sigma2))


def percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def simulate(args: argparse.Namespace) -> Result:
    rng = random.Random(args.seed)
    free = [args.gpus_per_node] * args.nodes
    queued: list[Job] = []
    completions: list[tuple[float, int, Job]] = []
    waits: list[float] = []
    gpu_hours = 0.0
    completed = 0
    arrivals = 0
    max_queue = 0
    queue_area = 0.0
    last_event = 0.0
    next_id = 0
    now = 0.0
    next_arrival = rng.expovariate(args.arrival_rate) if args.arrival_rate > 0 else math.inf

    def place_job(job: Job, at: float) -> bool:
        if not args.allow_multi_node:
            for node, available in enumerate(free):
                if available >= job.size:
                    free[node] -= job.size
                    job.start = at
                    job.allocation = [(node, job.size)]
                    waits.append(at - job.arrival)
                    heapq.heappush(completions, (at + job.duration, job.job_id, job))
                    return True
            return False
        need = job.size
        alloc: list[tuple[int, int]] = []
        for node, available in enumerate(free):
            if need == 0:
                break
            take = min(available, need)
            if take <= 0:
                continue
            free[node] -= take
            alloc.append((node, take))
            need -= take
        if need > 0:
            # Roll back a partial multi-node allocation.
            for node, amount in alloc:
                free[node] += amount
            return False
        job.start = at
        job.allocation = alloc
        waits.append(at - job.arrival)
        heapq.heappush(completions, (at + job.duration, job.job_id, job))
        return True

    def drain_queue(at: float) -> None:
        nonlocal max_queue
        if args.policy == "strict":
            while queued and place_job(queued[0], at):
                queued.pop(0)
                max_queue = max(max_queue, len(queued))
        else:
            i = 0
            while i < len(queued):
                if place_job(queued[i], at):
                    queued.pop(i)
                else:
                    i += 1
                max_queue = max(max_queue, len(queued))

    while True:
        next_completion = completions[0][0] if completions else math.inf
        if next_arrival <= next_completion:
            now = next_arrival
            if now > args.hours:
                break
            queue_area += len(queued) * (now - last_event)
            last_event = now
            arrivals += 1
            size = sample_size(rng, args.job_size, args.job_size_mix)
            job = Job(
                job_id=next_id,
                size=size,
                arrival=now,
                duration=sample_duration(rng, args.mean_duration, args.cv, args.distribution),
            )
            next_id += 1
            if not place_job(job, now):
                queued.append(job)
                max_queue = max(max_queue, len(queued))
            next_arrival = now + rng.expovariate(args.arrival_rate) if args.arrival_rate > 0 else math.inf
        else:
            now = next_completion
            if now > args.hours:
                break
            queue_area += len(queued) * (now - last_event)
            last_event = now
            _, _, job = heapq.heappop(completions)
            gpu_hours += job.size * (now - job.start) if job.start is not None else 0.0
            for node, amount in job.allocation:
                free[node] += amount
            completed += 1
            drain_queue(now)

    # Jobs still running at the horizon contribute partial GPU time but are not
    # counted as completed. The end-of-run free counts reflect only released
    # capacity, so they are a conservative measure of fragmentation.
    total_capacity = args.nodes * args.gpus_per_node * args.hours
    util = gpu_hours / total_capacity if total_capacity > 0 else 0.0
    full_free = sum(1 for n in free if n == args.gpus_per_node)
    avg_free = sum(free) / len(free) if free else 0.0
    avg_queue = queue_area / last_event if last_event > 0 else 0.0
    return Result(
        arrivals=arrivals,
        completed=completed,
        still_queued=len(queued),
        running=len(completions),
        wait_p50=percentile(waits, 0.50),
        wait_p95=percentile(waits, 0.95),
        wait_p99=percentile(waits, 0.99),
        wait_mean=statistics.fmean(waits) if waits else None,
        max_queue=max_queue,
        avg_queue=avg_queue,
        gpu_utilization=util,
        fully_free_nodes_at_end=full_free,
        free_gpus_per_node_at_end=avg_free,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nodes", type=int, default=12, help="number of GPU nodes (default: 12)")
    p.add_argument("--gpus-per-node", type=int, default=8, help="GPUs per node (default: 8)")
    p.add_argument("--arrival-rate", type=float, default=0.5, help="job arrivals per hour (default: 0.5)")
    p.add_argument("--mean-duration", type=float, default=6.0, help="mean job duration in hours (default: 6)")
    p.add_argument("--cv", type=float, default=1.0, help="duration coefficient of variation (default: 1.0)")
    p.add_argument("--distribution", choices=["exponential", "lognormal"], default="exponential")
    p.add_argument("--job-size", type=int, default=8, help="fixed GPUs per job (default: 8)")
    p.add_argument("--job-size-mix", type=parse_mix, default=None,
                   help='weighted sizes, e.g. "1:0.5,2:0.2,8:0.2,32:0.1"')
    p.add_argument("--policy", choices=["strict", "best-effort"], default="strict",
                   help="queue head-of-line policy (default: strict)")
    p.add_argument("--allow-multi-node", action="store_true",
                   help="allow one job to span nodes (default: whole job on one node)")
    p.add_argument("--hours", type=float, default=2160.0,
                   help="simulation horizon in hours (default: 2160 = 90 days)")
    p.add_argument("--seed", type=int, default=42, help="random seed (default: 42)")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.nodes <= 0 or args.gpus_per_node <= 0:
        raise SystemExit("nodes and gpus-per-node must be positive")
    if args.arrival_rate <= 0:
        raise SystemExit("arrival-rate must be positive")
    if args.mean_duration < 0:
        raise SystemExit("mean-duration must not be negative")
    if args.job_size <= 0:
        raise SystemExit("job-size must be positive")
    if args.job_size > args.nodes * args.gpus_per_node:
        raise SystemExit("job-size exceeds total cluster GPUs")
    if not args.allow_multi_node and args.job_size > args.gpus_per_node:
        print(
            f"warning: job-size {args.job_size} exceeds gpus-per-node {args.gpus_per_node} "
            "and --allow-multi-node is off; these jobs can never start",
            flush=True,
        )

    result = simulate(args)
    if args.json:
        print(json.dumps(result.__dict__, indent=2, sort_keys=True))
        return 0

    def fmt(value: Optional[float], unit: str = "") -> str:
        return "n/a" if value is None else f"{value:.2f}{unit}"

    print("capacity model")
    print(f"  cluster:        {args.nodes} nodes x {args.gpus_per_node} GPUs = {args.nodes * args.gpus_per_node} GPUs")
    print(f"  arrivals:       {args.arrival_rate}/h, mean duration {args.mean_duration}h, cv={args.cv}")
    if args.job_size_mix:
        print(f"  job sizes:      {args.job_size_mix}")
    else:
        print(f"  job size:       {args.job_size} GPUs")
    print(f"  placement:      {'multi-node' if args.allow_multi_node else 'whole job on one node'}")
    print(f"  policy:         {args.policy}")
    print()
    print(f"  arrivals:       {result.arrivals}")
    print(f"  completed:      {result.completed}")
    print(f"  still queued:   {result.still_queued}")
    print(f"  running at end: {result.running}")
    print(f"  GPU utilization:{result.gpu_utilization * 100:6.2f}%")
    print(f"  wait p50:       {fmt(result.wait_p50, ' h')}")
    print(f"  wait p95:       {fmt(result.wait_p95, ' h')}")
    print(f"  wait p99:       {fmt(result.wait_p99, ' h')}")
    print(f"  wait mean:      {fmt(result.wait_mean, ' h')}")
    print(f"  max queue:      {result.max_queue}")
    print(f"  avg queue:      {result.avg_queue:.2f}")
    print(f"  fully free nodes at end: {result.fully_free_nodes_at_end}")
    print(f"  free GPUs/node at end:   {result.free_gpus_per_node_at_end:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
