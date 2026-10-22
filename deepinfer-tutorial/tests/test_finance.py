from __future__ import annotations

import math

import pytest

from mini_deepinfer.finance import (
    breakeven_utilization,
    cac_payback_months,
    committed_contract_value,
    dcf_value,
    discounted_payback,
    implied_revenue_multiple,
    irr,
    ltv,
    npv,
    payback_period,
    price_for_margin,
    revenue_multiple_value,
    rule_of_forty,
    scenario_table,
    terminal_value,
    venture_return,
)


def test_npv_of_a_simple_project() -> None:
    value = npv(0.10, [-100.0, 60.0, 60.0])
    assert value == pytest.approx(4.132231404958678, rel=1e-9)


def test_irr_recovers_the_rate() -> None:
    rate = irr([-100.0, 110.0])
    assert rate == pytest.approx(0.10, abs=1e-6)


def test_irr_requires_a_sign_change() -> None:
    with pytest.raises(ValueError):
        irr([100.0, 110.0])


def test_payback_and_discounted_payback() -> None:
    cashflows = [-100.0, 40.0, 40.0, 40.0]
    assert payback_period(cashflows) == pytest.approx(2.5)
    assert discounted_payback(0.10, cashflows) > payback_period(cashflows)
    assert payback_period([-100.0, 10.0]) == math.inf


def test_ltv_and_cac_payback() -> None:
    value = ltv(monthly_revenue_per_customer=1000, gross_margin=0.6, monthly_churn=0.02)
    assert value == pytest.approx(30_000)
    months = cac_payback_months(cac=12_000, monthly_revenue_per_customer=1000, gross_margin=0.6)
    assert months == pytest.approx(20)


def test_price_for_margin() -> None:
    assert price_for_margin(70.0, 0.30) == pytest.approx(100.0)
    with pytest.raises(ValueError):
        price_for_margin(70.0, 1.0)


def test_committed_contract_value() -> None:
    value = committed_contract_value(
        monthly_commit=10_000,
        months=12,
        discount=0.2,
        overage_rate=0.5,
        expected_overage_units=1_000,
    )
    assert value["committed_recurring"] == pytest.approx(96_000)
    assert value["expected_overage"] == pytest.approx(6_000)
    assert value["total_contract_value"] == pytest.approx(102_000)
    assert value["discount_cost"] == pytest.approx(24_000)


def test_breakeven_utilization_can_exceed_one() -> None:
    assert breakeven_utilization(
        annual_fixed_cost=1_000_000,
        gpu_count=8,
        contribution_per_gpu_hour=20,
    ) == pytest.approx(0.713, rel=1e-3)
    assert breakeven_utilization(
        annual_fixed_cost=10_000_000,
        gpu_count=1,
        contribution_per_gpu_hour=100,
    ) > 1.0


def test_scenario_table_orders_worst_base_best() -> None:
    rows = scenario_table([-100.0, 40.0, 50.0, 60.0], rate=0.1, revenue_multiplier=0.8, cost_multiplier=1.2)
    assert [row["name"] for row in rows] == ["worst", "base", "best"]
    assert rows[0]["npv"] < rows[1]["npv"] < rows[2]["npv"]


def test_scenario_table_handles_a_never_profitable_case() -> None:
    rows = scenario_table([-100.0, -10.0, -5.0, -1.0], rate=0.1)
    assert all(math.isnan(row["irr"]) for row in rows)
    assert all(row["payback_years"] == math.inf for row in rows)


def test_rule_of_forty() -> None:
    assert rule_of_forty(revenue_growth_rate=0.30, profit_margin=0.15)["passes"] is True
    assert rule_of_forty(revenue_growth_rate=0.10, profit_margin=0.10)["passes"] is False


def test_terminal_value_and_dcf() -> None:
    # 100 growing at 3%, discounted at 10%, one explicit period.
    terminal = terminal_value(100, discount_rate=0.10, growth_rate=0.03, periods=1)
    assert terminal == pytest.approx(100 * 1.03 / 0.07 / 1.10)
    result = dcf_value([-500, 100, 120], discount_rate=0.10, terminal_growth=0.03)
    assert result["explicit_value"] == pytest.approx(-500 + 100 / 1.1 + 120 / 1.21)
    assert result["terminal_value"] > 0
    assert result["enterprise_value"] == pytest.approx(
        result["explicit_value"] + result["terminal_value"]
    )


def test_dcf_rejects_growth_above_discount_rate() -> None:
    with pytest.raises(ValueError):
        terminal_value(100, discount_rate=0.05, growth_rate=0.05, periods=1)


def test_revenue_multiple_and_venture_return() -> None:
    assert revenue_multiple_value(10_000_000, 4.0) == pytest.approx(40_000_000)
    assert implied_revenue_multiple(40_000_000, 10_000_000) == pytest.approx(4.0)
    assert venture_return(50_000_000, 10_000_000) == pytest.approx(5.0)
