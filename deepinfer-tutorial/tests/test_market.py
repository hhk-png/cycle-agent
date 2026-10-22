from __future__ import annotations

import pytest

from mini_deepinfer.market import (
    Segment,
    adoption_curve,
    funnel_conversion,
    magic_number,
    market_share,
    obtainable_market,
    sales_capacity,
    serviceable_market,
    tam_bottom_up,
)


def test_tam_is_sum_of_segment_revenue_pools() -> None:
    segments = [
        Segment("enterprise", accounts=1_000, annual_tokens=12_000_000, price_per_mtok=0.5),
        Segment("self-serve", accounts=20_000, annual_tokens=2_000_000, price_per_mtok=0.3),
    ]
    # 1,000 * 12M / 1M * 0.5 = 6,000; 20,000 * 2M / 1M * 0.3 = 12,000.
    assert tam_bottom_up(segments) == pytest.approx(18_000)


def test_serviceable_and_obtainable_market() -> None:
    sam = serviceable_market(18_000, 0.4)
    assert sam == pytest.approx(7_200)
    assert obtainable_market(sam, 0.1, year=3, ramp_years=3) == pytest.approx(720)
    assert obtainable_market(sam, 0.1, year=1, ramp_years=3) == pytest.approx(240)


def test_adoption_curve_is_symmetric_and_bounded() -> None:
    assert adoption_curve(0, ceiling=1000, rate=1.0, midpoint=0) == pytest.approx(500)
    assert adoption_curve(100, ceiling=1000, rate=1.0, midpoint=0) > 999
    assert adoption_curve(-100, ceiling=1000, rate=1.0, midpoint=0) < 1


def test_funnel_conversion_compounds_rates() -> None:
    result = funnel_conversion(10_000, [0.2, 0.5, 0.1])
    assert result["final_count"] == pytest.approx(100)
    assert result["compound_rate"] == pytest.approx(0.01)


def test_magic_number_and_market_share() -> None:
    assert magic_number(1_000_000, 800_000) == pytest.approx(1.25)
    assert market_share(50, 1_000) == pytest.approx(0.05)
    assert market_share(50, 0) == 0.0


def test_sales_capacity_ramps() -> None:
    assert sales_capacity(reps=10, quota_per_rep=100, ramp_months=6, month=0) == 0
    assert sales_capacity(reps=10, quota_per_rep=100, ramp_months=6, month=3) == pytest.approx(500)
    assert sales_capacity(reps=10, quota_per_rep=100, ramp_months=6, month=12) == pytest.approx(1_000)


def test_invalid_market_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        serviceable_market(100, 1.5)
    with pytest.raises(ValueError):
        funnel_conversion(100, [20.0])
    with pytest.raises(ValueError):
        Segment("bad", -1, 1, 1).annual_revenue()
