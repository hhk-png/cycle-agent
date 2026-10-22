from __future__ import annotations

import pytest

from mini_deepinfer.edge import (
    EdgeDevice,
    EdgeRequest,
    energy_per_token,
    estimate_decode,
    estimate_prefill_tokens_per_s,
    plan_memory,
    quantized_weight_bytes,
    route_edge_or_cloud,
    thermal_sustained_tokens_per_s,
)


CPU = EdgeDevice("cpu", memory_gb=16, bandwidth_gbs=50, compute_tflops=0.5, power_w=45)


def test_quantized_weight_bytes_scale_with_bits() -> None:
    fp16 = quantized_weight_bytes(1_000_000, 16)
    fp8 = quantized_weight_bytes(1_000_000, 8)
    fp4 = quantized_weight_bytes(1_000_000, 4)
    assert fp16 == 2_000_000
    assert fp8 == 1_000_000
    assert fp4 == 500_000
    assert quantized_weight_bytes(1_000_000, 4, scale_overhead=0.0625) == 531_250


def test_memory_plan_fits_or_reports_utilization() -> None:
    plan = plan_memory(
        CPU,
        parameters=100_000_000,
        bits=4,
        context_tokens=4096,
        kv_bytes_per_token=890,
        activation_bytes=32 * 1024 * 1024,
    )
    assert plan.weight_bytes == 50_000_000
    assert plan.kv_bytes == 4096 * 890
    assert plan.total_bytes == plan.weight_bytes + plan.kv_bytes + plan.activation_bytes
    assert plan.fits
    assert 0 < plan.utilization < 1

    too_big = plan_memory(
        CPU,
        parameters=8_000_000_000,
        bits=16,
        context_tokens=4096,
        kv_bytes_per_token=890,
    )
    assert not too_big.fits


def test_decode_estimate_is_bandwidth_bound_for_large_weights() -> None:
    estimate = estimate_decode(
        CPU,
        active_parameters=1_000_000_000,
        weight_bits=4,
        context_tokens=2048,
        kv_bytes_per_token=890,
    )
    assert estimate.bottleneck == "memory"
    assert estimate.tokens_per_s > 0
    assert estimate.bytes_per_step == 500_000_000 + 2048 * 890


def test_decode_estimate_improves_with_more_bandwidth() -> None:
    fast = EdgeDevice("fast", memory_gb=32, bandwidth_gbs=200, compute_tflops=10, power_w=150)
    slow = estimate_decode(
        CPU,
        active_parameters=100_000_000,
        weight_bits=4,
        context_tokens=1024,
        kv_bytes_per_token=890,
    )
    quick = estimate_decode(
        fast,
        active_parameters=100_000_000,
        weight_bits=4,
        context_tokens=1024,
        kv_bytes_per_token=890,
    )
    assert quick.tokens_per_s > slow.tokens_per_s


def test_prefill_and_energy_are_positive() -> None:
    prefill = estimate_prefill_tokens_per_s(CPU, active_parameters=100_000_000)
    assert prefill > 0
    assert energy_per_token(CPU, tokens_per_s=50) == pytest.approx(45 / 50)


def test_thermal_throttle_reduces_average_throughput() -> None:
    sustained = thermal_sustained_tokens_per_s(
        100.0, throttle_fraction=0.5, throttle_after_s=30, workload_s=300
    )
    assert 50 < sustained < 100
    burst_only = thermal_sustained_tokens_per_s(
        100.0, throttle_fraction=0.5, throttle_after_s=1000, workload_s=300
    )
    assert burst_only == 100.0


def test_private_request_stays_on_edge_even_if_slow() -> None:
    decision = route_edge_or_cloud(
        EdgeRequest(100, 100, privacy="private", latency_budget_ms=60_000),
        edge=CPU,
        edge_tokens_per_s=5,
    )
    assert decision.target == "edge"
    assert decision.reason == "privacy_requires_local"
    assert decision.feasible


def test_public_request_falls_back_to_cloud_when_edge_is_slow() -> None:
    decision = route_edge_or_cloud(
        EdgeRequest(100, 200, privacy="public", latency_budget_ms=100),
        edge=CPU,
        edge_tokens_per_s=5,
        cloud_tokens_per_s=200,
    )
    assert decision.target == "cloud"
    assert decision.reason == "edge_latency_miss"
    assert decision.estimated_latency_ms < 5000


def test_offline_request_cannot_fall_back_to_cloud() -> None:
    decision = route_edge_or_cloud(
        EdgeRequest(
            100, 1000, privacy="public", latency_budget_ms=100, network_available=False
        ),
        edge=CPU,
        edge_tokens_per_s=5,
    )
    assert decision.target == "edge"
    assert decision.reason == "offline_but_latency_infeasible"
    assert not decision.feasible
