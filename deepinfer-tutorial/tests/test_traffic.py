from __future__ import annotations

import math

import pytest

from mini_deepinfer.traffic import (
    TrafficRequest,
    Worker,
    admit,
    capacity_buffer,
    circuit_breaker,
    erlang_c,
    hedge_delay_ms,
    load_shedding,
    mm1,
    mmc,
    region_failover,
    retry_budget,
    route,
)


def test_mm1_hits_the_knee_near_utilization_one() -> None:
    light = mm1(arrival_rate=5, service_rate=10)
    heavy = mm1(arrival_rate=9, service_rate=10)
    assert light["utilization"] == 0.5
    assert heavy["wait_s"] > light["wait_s"] * 4
    assert mm1(10, 10)["stable"] == 0.0


def test_mmc_reduces_wait_relative_to_one_server() -> None:
    one = mm1(arrival_rate=5, service_rate=10)
    four = mmc(arrival_rate=5, service_rate=10, servers=4)
    assert four["utilization"] == 0.125
    assert four["wait_s"] < one["wait_s"]


def test_erlang_c_is_bounded() -> None:
    assert erlang_c(4, 0.0) == 0.0
    assert erlang_c(4, 4.0) == 1.0
    value = erlang_c(4, 2.0)
    assert 0.0 < value < 1.0


def test_erlang_c_high_load_matches_reference() -> None:
    # Reference: c=4, a=3.8 -> 0.8914 (Erlang C).
    assert abs(erlang_c(4, 3.8) - 0.8914) < 1e-3


def test_admission_queues_within_budget_and_rejects_overload() -> None:
    assert admit(inflight=1, concurrency_limit=4, queue_length=0, max_queue=8,
                 estimated_wait_s=0.0, slo_wait_s=1.0).action == "admit"
    queued = admit(inflight=4, concurrency_limit=4, queue_length=3, max_queue=8,
                   estimated_wait_s=0.5, slo_wait_s=1.0)
    assert queued.action == "queue"
    rejected = admit(inflight=4, concurrency_limit=4, queue_length=8, max_queue=8,
                     estimated_wait_s=3.0, slo_wait_s=1.0)
    assert rejected.action == "reject"
    assert rejected.retry_after_s == pytest.approx(2.0)


def test_priority_bypass_respects_concurrency_limit() -> None:
    decision = admit(inflight=2, concurrency_limit=4, queue_length=8, max_queue=8,
                     estimated_wait_s=5.0, slo_wait_s=1.0, priority=9)
    assert decision.action == "admit"


def test_least_loaded_and_prefix_affinity() -> None:
    workers = [
        Worker("a", inflight=4, capacity=8, prefixes=frozenset({"hot"})),
        Worker("b", inflight=1, capacity=8),
    ]
    request = TrafficRequest("r", prefix_hash="hot", region="default")
    assert route(request, workers, policy="least_loaded") == "b"
    assert route(request, workers, policy="prefix_affinity") == "a"


def test_prefix_affinity_yields_when_worker_is_saturated() -> None:
    workers = [
        Worker("a", inflight=8, capacity=8, prefixes=frozenset({"hot"})),
        Worker("b", inflight=2, capacity=8),
    ]
    request = TrafficRequest("r", prefix_hash="hot")
    assert route(request, workers, policy="prefix_affinity") == "b"


def test_region_filter_and_kv_limit() -> None:
    workers = [
        Worker("a", inflight=0, capacity=4, region="eu", kv_utilization=0.99),
        Worker("b", inflight=3, capacity=4, region="us"),
    ]
    request = TrafficRequest("r", region="eu")
    assert route(request, workers, policy="least_loaded") == "b"
    with pytest.raises(ValueError):
        route(request, [workers[0]], policy="least_loaded")


def test_retry_budget_falls_with_error_rate() -> None:
    healthy = retry_budget(error_rate=0.01, requests_per_s=1000)
    unhealthy = retry_budget(error_rate=0.5, requests_per_s=1000)
    assert healthy["retries_allowed"] > unhealthy["retries_allowed"]


def test_circuit_breaker_three_states() -> None:
    assert circuit_breaker(failures=1, window=100, threshold=0.5, cooldown_s=10).state == "closed"
    opened = circuit_breaker(failures=80, window=100, threshold=0.5, cooldown_s=10)
    assert opened.state == "open" and not opened.allow_request
    half = circuit_breaker(
        failures=80, window=100, threshold=0.5, cooldown_s=10, seconds_since_open=11
    )
    assert half.state == "half_open" and half.allow_request


def test_capacity_buffer_accounts_for_failure_domain() -> None:
    plan = capacity_buffer(mean_qps=100, peak_factor=3, failure_domains=8, target_utilization=0.7)
    assert plan["peak_qps"] == 300
    assert plan["usable_qps"] == pytest.approx(300 / 0.7)
    assert plan["capacity_with_failure_domain"] > plan["usable_qps"]


def test_load_shedding_order() -> None:
    assert load_shedding(utilization=0.5, queue_wait_s=0.1, slo_wait_s=1.0) == "accept"
    assert load_shedding(utilization=0.95, queue_wait_s=0.1, slo_wait_s=1.0) == "degrade"
    assert load_shedding(utilization=0.95, queue_wait_s=2.0, slo_wait_s=1.0) == "reject"


def test_hedge_delay_is_below_p99() -> None:
    assert hedge_delay_ms(1000) == pytest.approx(950)
    assert hedge_delay_ms(1000, fraction=0.5) == 500


def test_region_failover_respects_residency() -> None:
    plan = region_failover(
        [
            {"name": "eu-1", "capacity_qps": 100, "residency": "eu"},
            {"name": "eu-2", "capacity_qps": 100, "residency": "eu"},
            {"name": "us-1", "capacity_qps": 100, "residency": "us"},
        ],
        failed_region="eu-1",
        reserve_fraction=0.2,
    )
    rows = {row["name"]: row for row in plan[0]["regions"]}
    assert rows["eu-2"]["accepts_failover"] is True
    assert rows["us-1"]["accepts_failover"] is False
    assert rows["us-1"]["available_qps"] == 0.0
    assert plan[0]["healthy_capacity_qps"] == pytest.approx(160)
    assert plan[0]["failover_capacity_qps"] == pytest.approx(80)
