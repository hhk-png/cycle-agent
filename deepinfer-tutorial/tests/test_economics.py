from __future__ import annotations

import pytest

from mini_deepinfer.economics import (
    capacity_plan,
    cost_per_million_tokens,
    gross_margin,
    p_d_gpu_split,
    revenue_per_million_tokens,
    sensitivity,
    tco_breakdown,
)


def test_tco_breakdown_sums_and_utilization() -> None:
    tco = tco_breakdown(gpu_count=8, utilization=0.7)
    assert tco.total == pytest.approx(
        tco.capex + tco.network_storage + tco.power + tco.colocation + tco.operations
    )
    assert tco.cost_per_effective_gpu_hour == pytest.approx(
        tco.cost_per_gpu_hour / 0.7
    )
    assert tco.network_storage > 0
    assert tco.total > tco.capex


def test_cost_and_revenue_per_million_tokens() -> None:
    cost = cost_per_million_tokens(3.0, 3000.0)
    assert cost == pytest.approx(3e6 / (3000 * 3600))
    assert cost_per_million_tokens(3.0, 3000.0, utilization=0.5) == pytest.approx(2 * cost)
    revenue = revenue_per_million_tokens(38.0, 3000.0)
    assert revenue > cost
    assert gross_margin(revenue, cost) > 0
    assert gross_margin(revenue, revenue) == 0


def test_capacity_plan_uses_littles_law() -> None:
    plan = capacity_plan(
        qps=20,
        average_stay_s=30,
        average_context_tokens=32_768,
        kv_bytes_per_token=890,
        redundancy=0.25,
    )
    assert plan["concurrency"] == pytest.approx(600)
    assert plan["kv_bytes"] == pytest.approx(600 * 32_768 * 890)
    assert plan["kv_bytes_with_redundancy"] == pytest.approx(plan["kv_bytes"] * 1.25)
    assert plan["input_tokens_per_s"] == pytest.approx(20 * 32_768)


def test_p_d_split_scales_with_load_and_headroom() -> None:
    split = p_d_gpu_split(
        prefill_tokens_per_s=100_000,
        decode_tokens_per_s=20_000,
        prefill_tokens_per_s_per_gpu=60_000,
        decode_tokens_per_s_per_gpu=1_000,
        headroom=0.2,
    )
    assert split["prefill_gpus"] == pytest.approx((100_000 / 60_000) * 1.2)
    assert split["decode_gpus"] == pytest.approx(20 * 1.2)
    assert split["p_to_d_ratio"] == pytest.approx((100_000 / 60_000) / 20)


def test_sensitivity_orders_by_impact() -> None:
    rows = sensitivity(base_cost_per_mtok=0.28)
    assert [row[0] for row in rows] == ["throughput", "utilization", "electricity"]
    throughput_span = abs(rows[0][1] - rows[0][2])
    electricity_span = abs(rows[2][1] - rows[2][2])
    assert throughput_span > electricity_span


def test_invalid_economics_inputs() -> None:
    with pytest.raises(ValueError):
        cost_per_million_tokens(3.0, 0.0)
    with pytest.raises(ValueError):
        cost_per_million_tokens(3.0, 100.0, utilization=0.0)
    with pytest.raises(ValueError):
        capacity_plan(qps=0, average_stay_s=1, average_context_tokens=1, kv_bytes_per_token=1)
    with pytest.raises(ValueError):
        p_d_gpu_split(
            prefill_tokens_per_s=1,
            decode_tokens_per_s=1,
            prefill_tokens_per_s_per_gpu=0,
            decode_tokens_per_s_per_gpu=1,
        )
    with pytest.raises(ValueError):
        sensitivity(base_cost_per_mtok=1.0, throughput_change=1.0)
    with pytest.raises(ValueError):
        sensitivity(base_cost_per_mtok=1.0, electricity_share=1.5)
