from __future__ import annotations

import pytest

from mini_deepinfer.router import (
    ModelRouter,
    ModelSpec,
    break_even_acceptance,
    cascade_estimate,
)


def models() -> list[ModelSpec]:
    return [
        ModelSpec("small", 0.70, 0.10, 0.30, 100.0, 20.0, max_context=32_000),
        ModelSpec("medium", 0.85, 0.50, 1.50, 200.0, 35.0, max_context=128_000),
        ModelSpec("large", 0.95, 2.00, 6.00, 400.0, 60.0, max_context=1_000_000),
    ]


def test_estimate_cost_and_latency() -> None:
    small = models()[0]
    cost, latency = small.estimate(1_000_000, 1_000_000)
    assert cost == pytest.approx(0.40)
    assert latency == pytest.approx(100.0 + 1_000_000 * 20.0)


def test_router_picks_cheapest_feasible_model() -> None:
    router = ModelRouter(models(), quality_floor=0.80)
    decision = router.route(1_000, 500)
    assert decision.model == "medium"
    assert decision.feasible
    assert decision.reason == "lowest_cost_within_constraints"


def test_router_honors_quality_floor_and_latency_budget() -> None:
    router = ModelRouter(models())
    assert router.route(1_000, 100, quality_floor=0.90).model == "large"
    # A tight latency budget excludes medium and large, even though small has
    # the lowest quality.
    decision = router.route(1_000, 100, quality_floor=0.90, latency_budget_ms=150)
    assert not decision.feasible
    assert decision.reason == "no_feasible_model_fallback"
    assert decision.model == "large"


def test_router_filters_by_context_and_raises_when_nothing_fits() -> None:
    router = ModelRouter(models())
    decision = router.route(64_000, 1_000)
    assert decision.model in {"medium", "large"}
    with pytest.raises(ValueError):
        router.route(2_000_000, 1_000)


def test_router_ties_break_toward_quality() -> None:
    a = ModelSpec("a", 0.80, 1.0, 1.0, 100.0, 10.0)
    b = ModelSpec("b", 0.90, 1.0, 1.0, 100.0, 10.0)
    decision = ModelRouter([a, b]).route(1_000, 1_000)
    assert decision.model == "b"


def test_cascade_cost_decreases_with_acceptance() -> None:
    chain = models()[:2]
    high = cascade_estimate(chain, 10_000, 1_000, [0.95, 0.95])
    low = cascade_estimate(chain, 10_000, 1_000, [0.20, 0.20])
    assert high.expected_cost_usd < low.expected_cost_usd
    assert high.expected_calls < low.expected_calls
    # High acceptance means most answers come from the cheap model, so the
    # average quality is lower even though the cascade is cheaper.  This is
    # the central trade-off a router has to expose, not hide.
    assert high.expected_quality < low.expected_quality
    assert low.expected_quality <= models()[1].quality


def test_break_even_acceptance() -> None:
    cheap, strong = models()[0], models()[2]
    rate = break_even_acceptance(cheap, strong, 10_000, 1_000)
    assert 0.0 < rate < 1.0
    # At the break-even rate the cascade costs the same as the strong model.
    estimate = cascade_estimate([cheap, strong], 10_000, 1_000, [rate, 1.0])
    strong_cost, _ = strong.estimate(10_000, 1_000)
    assert estimate.expected_cost_usd == pytest.approx(strong_cost)


def test_cascade_validation() -> None:
    with pytest.raises(ValueError):
        cascade_estimate(models()[:2], 10, 10, [0.5])
    with pytest.raises(ValueError):
        cascade_estimate(models()[:1], 10, 10, [1.2])
    with pytest.raises(ValueError):
        ModelRouter([])
    with pytest.raises(ValueError):
        ModelSpec("x", 1.2, 1.0, 1.0, 1.0, 1.0)
