"""Traffic engineering: queueing, admission, routing and failure budgets."""

from __future__ import annotations

from _common import add_project_root

add_project_root()

from mini_deepinfer.traffic import (
    TrafficRequest,
    Worker,
    admit,
    capacity_buffer,
    circuit_breaker,
    hedge_delay_ms,
    load_shedding,
    mm1,
    mmc,
    region_failover,
    retry_budget,
    route,
)


def main() -> None:
    print("queueing (service rate 10 req/s/server):")
    for arrival in (2, 5, 8, 9):
        one = mm1(arrival, 10)
        four = mmc(arrival, 10, servers=4)
        print(
            f"  arrival={arrival:2d}: mm1 rho={one['utilization']:.2f} "
            f"wait={one['wait_s'] * 1000:.1f}ms | "
            f"mmc4 rho={four['utilization']:.2f} wait={four['wait_s'] * 1000:.1f}ms"
        )

    print("admission:")
    cases = [
        dict(inflight=2, concurrency_limit=8, queue_length=0, max_queue=16,
             estimated_wait_s=0.0, slo_wait_s=0.8),
        dict(inflight=8, concurrency_limit=8, queue_length=4, max_queue=16,
             estimated_wait_s=0.4, slo_wait_s=0.8),
        dict(inflight=8, concurrency_limit=8, queue_length=16, max_queue=16,
             estimated_wait_s=2.0, slo_wait_s=0.8),
    ]
    for case in cases:
        decision = admit(**case)
        print(
            f"  inflight={case['inflight']} queue={case['queue_length']} -> "
            f"{decision.action} ({decision.reason})"
        )

    workers = [
        Worker("w0", inflight=6, capacity=8, kv_utilization=0.7, prefixes=frozenset({"p0"})),
        Worker("w1", inflight=2, capacity=8, kv_utilization=0.3),
        Worker("w2", inflight=4, capacity=8, kv_utilization=0.5),
    ]
    request = TrafficRequest("r0", prefix_hash="p0", region="default")
    for policy in ("least_loaded", "power_of_two", "prefix_affinity"):
        print(f"  route {policy}: {route(request, workers, policy=policy, seed=1)}")

    budget = retry_budget(error_rate=0.2, requests_per_s=1000)
    print(f"retry budget at 20% errors: {budget}")
    breaker = circuit_breaker(failures=80, window=100, threshold=0.5, cooldown_s=10)
    print(f"breaker: state={breaker.state} allow={breaker.allow_request}")
    print(f"hedge delay for P99=1200ms: {hedge_delay_ms(1200):.1f}ms")

    capacity = capacity_buffer(mean_qps=100, peak_factor=3, failure_domains=8)
    print(f"capacity buffer: {capacity}")
    for utilization, wait in ((0.5, 0.1), (0.95, 0.1), (0.95, 2.0)):
        print(
            f"  shed util={utilization} wait={wait}s -> "
            f"{load_shedding(utilization=utilization, queue_wait_s=wait, slo_wait_s=1.0)}"
        )

    plan = region_failover(
        [
            {"name": "eu-1", "capacity_qps": 100, "residency": "eu"},
            {"name": "eu-2", "capacity_qps": 100, "residency": "eu"},
            {"name": "us-1", "capacity_qps": 100, "residency": "us"},
        ],
        failed_region="eu-1",
        reserve_fraction=0.2,
    )[0]
    print(
        f"failover: healthy={plan['healthy_capacity_qps']} "
        f"legal_for_failed_region={plan['failover_capacity_qps']}"
    )
    for row in plan["regions"]:
        print(
            f"  {row['name']}: accepts_failover={row['accepts_failover']} "
            f"available_qps={row['available_qps']}"
        )


if __name__ == "__main__":
    main()
