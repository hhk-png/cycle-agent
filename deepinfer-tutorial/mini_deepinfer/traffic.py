"""Traffic engineering for inference services.

An inference cluster fails in ways that a single engine's metrics do not show:
a retry storm, a queue that crosses its knee, a hot prefix that defeats
least-loaded routing, a region that loses its KV store.  This module provides
small, testable models for those failure modes:

* M/M/1 and M/M/c queueing, including the tail-latency knee;
* admission control with an explicit queue budget;
* worker routing with least-loaded, power-of-two and prefix-affinity policies;
* retry budgets and circuit breakers that stop metastable failures;
* capacity buffers that account for peak-to-mean and failure domains.

The formulas assume Poisson arrivals and exponential service.  Real LLM
traffic is bursty and heavy-tailed, so use them to locate the knee and the
right order of magnitude, then validate with a replay of production traces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence


def mm1(arrival_rate: float, service_rate: float) -> dict[str, float]:
    """M/M/1 queue metrics; ``service_rate`` is requests per second per server."""
    if arrival_rate < 0 or service_rate <= 0:
        raise ValueError("invalid arrival or service rate")
    rho = arrival_rate / service_rate
    if rho >= 1.0:
        return {
            "utilization": rho,
            "wait_s": math.inf,
            "sojourn_s": math.inf,
            "queue_length": math.inf,
            "stable": 0.0,
        }
    wait = rho / (service_rate * (1.0 - rho))
    return {
        "utilization": rho,
        "wait_s": wait,
        "sojourn_s": wait + 1.0 / service_rate,
        "queue_length": rho * rho / (1.0 - rho),
        "stable": 1.0,
    }


def erlang_c(servers: int, offered_load: float) -> float:
    """Probability that an arriving request waits in an M/M/c queue."""
    if servers < 1:
        raise ValueError("servers must be positive")
    if offered_load < 0:
        raise ValueError("offered_load must be non-negative")
    if offered_load >= servers:
        return 1.0
    term = 1.0
    total = 1.0
    for index in range(1, servers + 1):
        term *= offered_load / index
        total += term
    tail = term * servers / (servers - offered_load)
    # ``total`` already contains ``term`` (the k=servers term), so the
    # Erlang C denominator is total - term + tail, not total + tail.
    return tail / (total - term + tail)


def mmc(arrival_rate: float, service_rate: float, servers: int) -> dict[str, float]:
    """M/M/c queue metrics (Erlang C)."""
    if arrival_rate < 0 or service_rate <= 0 or servers < 1:
        raise ValueError("invalid queue parameters")
    offered = arrival_rate / service_rate
    rho = offered / servers
    if rho >= 1.0:
        return {
            "utilization": rho,
            "wait_probability": 1.0,
            "wait_s": math.inf,
            "sojourn_s": math.inf,
            "stable": 0.0,
        }
    p_wait = erlang_c(servers, offered)
    wait = p_wait / (servers * service_rate - arrival_rate)
    return {
        "utilization": rho,
        "wait_probability": p_wait,
        "wait_s": wait,
        "sojourn_s": wait + 1.0 / service_rate,
        "stable": 1.0,
    }


@dataclass(frozen=True)
class AdmissionDecision:
    action: str
    reason: str
    estimated_wait_s: float
    retry_after_s: float | None = None


def admit(
    *,
    inflight: int,
    concurrency_limit: int,
    queue_length: int,
    max_queue: int,
    estimated_wait_s: float,
    slo_wait_s: float,
    priority: int = 0,
) -> AdmissionDecision:
    """Admit, queue or reject a request.

    High-priority traffic may jump the queue but cannot exceed the hard
    concurrency limit; doing so would turn a latency problem into an OOM.
    ``retry_after_s`` is a hint for the gateway, not a promise.
    """
    if min(inflight, queue_length, concurrency_limit, max_queue) < 0:
        raise ValueError("counts must be non-negative")
    if concurrency_limit < 1 or max_queue < 0 or slo_wait_s <= 0:
        raise ValueError("invalid limits")
    if priority < 0:
        raise ValueError("priority must be non-negative")

    if inflight < concurrency_limit and queue_length == 0:
        return AdmissionDecision("admit", "capacity_available", 0.0)
    if estimated_wait_s <= slo_wait_s and queue_length < max_queue:
        return AdmissionDecision("queue", "within_wait_budget", estimated_wait_s)
    if priority >= 8 and inflight < concurrency_limit:
        return AdmissionDecision("admit", "priority_bypass", estimated_wait_s)
    retry = max(0.05, estimated_wait_s - slo_wait_s)
    return AdmissionDecision("reject", "queue_or_wait_budget_exceeded", estimated_wait_s, retry)


@dataclass(frozen=True)
class Worker:
    name: str
    inflight: int = 0
    capacity: int = 1
    kv_utilization: float = 0.0
    region: str = "default"
    prefixes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("worker name must not be empty")
        if self.inflight < 0 or self.capacity < 1:
            raise ValueError("invalid worker load")
        if not 0.0 <= self.kv_utilization <= 1.0:
            raise ValueError("kv_utilization must be in [0, 1]")


@dataclass(frozen=True)
class TrafficRequest:
    request_id: str
    prefix_hash: str = ""
    context_tokens: int = 1024
    region: str = "default"
    tenant: str = "default"


def _load_score(worker: Worker) -> float:
    # Inflight and KV pressure are the two resources a decode worker runs out
    # of first; weight them so neither is ignored.
    return worker.inflight / worker.capacity + 0.5 * worker.kv_utilization


def route(
    request: TrafficRequest,
    workers: Sequence[Worker],
    *,
    policy: str = "least_loaded",
    max_kv_utilization: float = 0.95,
    seed: int = 0,
) -> str:
    """Pick a worker; raise when no worker can serve the request."""
    if not workers:
        raise ValueError("at least one worker is required")
    eligible = [worker for worker in workers if worker.kv_utilization <= max_kv_utilization]
    if not eligible:
        raise ValueError("all workers are above the KV utilization limit")
    same_region = [worker for worker in eligible if worker.region == request.region]
    if same_region:
        eligible = same_region

    if policy == "round_robin":
        return eligible[seed % len(eligible)].name
    if policy == "least_loaded":
        return min(eligible, key=lambda worker: (_load_score(worker), worker.name)).name
    if policy == "power_of_two":
        if len(eligible) == 1:
            return eligible[0].name
        first = eligible[seed % len(eligible)]
        second = eligible[(seed * 7 + 3) % len(eligible)]
        if first.name == second.name:
            second = eligible[(eligible.index(first) + 1) % len(eligible)]
        return min((first, second), key=lambda worker: (_load_score(worker), worker.name)).name
    if policy == "prefix_affinity":
        matches = [worker for worker in eligible if request.prefix_hash in worker.prefixes]
        if matches:
            # Prefer an affinity match unless it is close to saturation.
            healthy = [worker for worker in matches if _load_score(worker) < 0.9]
            if healthy:
                return min(healthy, key=lambda worker: (_load_score(worker), worker.name)).name
        return min(eligible, key=lambda worker: (_load_score(worker), worker.name)).name
    raise ValueError(f"unknown routing policy: {policy}")


def retry_budget(
    *,
    error_rate: float,
    requests_per_s: float,
    retry_ratio: float = 0.1,
    window_s: float = 1.0,
) -> dict[str, float]:
    """Cap retries as a fraction of successful traffic.

    A fixed retry count lets a failing dependency multiply load without bound.
    A budget proportional to successes keeps retries from dominating the
    system: if the error rate rises, the retry allowance falls.
    """
    if not 0.0 <= error_rate <= 1.0:
        raise ValueError("error_rate must be in [0, 1]")
    if requests_per_s < 0 or window_s <= 0:
        raise ValueError("invalid traffic or window")
    if not 0.0 <= retry_ratio <= 1.0:
        raise ValueError("retry_ratio must be in [0, 1]")
    successes = requests_per_s * window_s * (1.0 - error_rate)
    allowed = successes * retry_ratio
    return {
        "successes": successes,
        "retries_allowed": allowed,
        "effective_retry_ratio": allowed / max(1.0, requests_per_s * window_s),
    }


@dataclass(frozen=True)
class BreakerState:
    state: str
    allow_request: bool
    reason: str


def circuit_breaker(
    *,
    failures: int,
    window: int,
    threshold: float,
    cooldown_s: float,
    seconds_since_open: float = 0.0,
) -> BreakerState:
    """Three-state breaker: closed, open, half-open."""
    if failures < 0 or window < 1:
        raise ValueError("invalid failure counters")
    if not 0.0 < threshold <= 1.0:
        raise ValueError("threshold must be in (0, 1]")
    if cooldown_s < 0 or seconds_since_open < 0:
        raise ValueError("cooldown must be non-negative")
    rate = failures / window
    if rate < threshold:
        return BreakerState("closed", True, "failure_rate_below_threshold")
    if seconds_since_open < cooldown_s:
        return BreakerState("open", False, "cooldown_active")
    return BreakerState("half_open", True, "probe_allowed")


def capacity_buffer(
    *,
    mean_qps: float,
    peak_factor: float = 3.0,
    failure_domains: int = 8,
    target_utilization: float = 0.7,
) -> dict[str, float]:
    """Capacity needed for peak traffic, one domain loss, and a utilisation cap.

    Running every GPU at 100% destroys the tail latency; running N+1 means one
    failure domain can disappear without dropping below the target.  The two
    multipliers are applied together because they address different risks.
    """
    if mean_qps < 0 or peak_factor <= 0:
        raise ValueError("invalid traffic parameters")
    if failure_domains < 1 or not 0.0 < target_utilization <= 1.0:
        raise ValueError("invalid redundancy parameters")
    peak = mean_qps * peak_factor
    usable = peak / target_utilization
    with_failure = usable * failure_domains / max(1, failure_domains - 1)
    return {
        "peak_qps": peak,
        "usable_qps": usable,
        "capacity_with_failure_domain": with_failure,
        "headroom_over_peak": usable / peak - 1.0,
    }


def load_shedding(
    *,
    utilization: float,
    queue_wait_s: float,
    slo_wait_s: float,
    degradation_threshold: float = 0.9,
) -> str:
    """Return ``accept``, ``degrade`` or ``reject`` for an overloaded service."""
    if not 0.0 <= utilization <= 2.0:
        raise ValueError("utilization is out of range")
    if queue_wait_s < 0 or slo_wait_s <= 0:
        raise ValueError("invalid wait values")
    if not 0.0 < degradation_threshold <= 1.0:
        raise ValueError("degradation_threshold must be in (0, 1]")
    if queue_wait_s > slo_wait_s:
        return "reject"
    if utilization >= degradation_threshold:
        return "degrade"
    return "accept"


def hedge_delay_ms(p99_ms: float, *, fraction: float = 0.95) -> float:
    """Delay before sending a hedged copy of a slow request.

    Hedging helps tail latency only if the hedge fires before the original
    would have failed.  Sending it too early doubles load; too late misses the
    SLO.  A fraction of P99 is a common starting point.
    """
    if p99_ms <= 0:
        raise ValueError("p99_ms must be positive")
    if not 0.0 < fraction < 1.0:
        raise ValueError("fraction must be in (0, 1)")
    return p99_ms * fraction


def region_failover(
    regions: Iterable[dict],
    *,
    failed_region: str,
    reserve_fraction: float = 0.2,
) -> list[dict]:
    """Plan traffic redistribution after one region fails.

    Each region dict needs ``name`` and ``capacity_qps``.  The plan preserves
    data-residency boundaries when a ``residency`` field is present: traffic
    may only move to a region with the same residency label.
    """
    rows = [dict(region) for region in regions]
    if not rows:
        raise ValueError("at least one region is required")
    if not 0.0 <= reserve_fraction < 1.0:
        raise ValueError("reserve_fraction must be in [0, 1)")
    failed = [row for row in rows if row.get("name") == failed_region]
    if not failed:
        raise ValueError("failed_region is not in the region list")
    failed_residency = failed[0].get("residency")
    survivors = [row for row in rows if row.get("name") != failed_region]
    healthy_capacity = sum(
        row.get("capacity_qps", 0.0) * (1.0 - reserve_fraction) for row in survivors
    )
    failover_capacity = 0.0
    for row in survivors:
        same = failed_residency is None or row.get("residency") == failed_residency
        row["accepts_failover"] = same
        row["available_qps"] = (
            row.get("capacity_qps", 0.0) * (1.0 - reserve_fraction) if same else 0.0
        )
        failover_capacity += row["available_qps"]
    return [
        {
            "failed_region": failed_region,
            "healthy_capacity_qps": healthy_capacity,
            # Capacity that may legally absorb the failed region's traffic.
            "failover_capacity_qps": failover_capacity,
            "regions": survivors,
        }
    ]
