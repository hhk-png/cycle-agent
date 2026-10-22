"""Financial models for an inference business.

The economics module answers "what does a token cost".  This module answers
the next questions an operator or investor asks: does the business pay back
its capital, what is a customer worth, how should a committed contract be
priced, what is the business worth under a DCF or a revenue multiple, and
which assumption destroys the plan.  All functions are pure and
use explicit cash-flow lists so that a notebook, an example or a test can
reproduce every number.

Nothing here is financial advice.  The value of the model is that it makes
assumptions visible and lets a team argue about them instead of arguing about
a single spreadsheet cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


def npv(rate: float, cashflows: Sequence[float]) -> float:
    """Net present value; ``cashflows[0]`` is normally the initial investment."""
    if rate <= -1.0:
        raise ValueError("rate must be greater than -1")
    if not cashflows:
        raise ValueError("cashflows must not be empty")
    return sum(value / (1.0 + rate) ** period for period, value in enumerate(cashflows))


def irr(cashflows: Sequence[float], *, low: float = -0.9, high: float = 10.0) -> float:
    """Internal rate of return by bisection.

    Returns the rate where NPV is zero.  A project with no sign change (for
    example all-positive cash flows) has no meaningful IRR and raises instead
    of returning a misleading number.
    """
    if len(cashflows) < 2:
        raise ValueError("at least two cashflows are required")
    if not (any(value < 0 for value in cashflows) and any(value > 0 for value in cashflows)):
        raise ValueError("cashflows must contain both negative and positive values")
    low_npv = npv(low, cashflows)
    high_npv = npv(high, cashflows)
    if low_npv * high_npv > 0:
        raise ValueError("no sign change in the search interval")
    for _ in range(200):
        mid = (low + high) / 2.0
        value = npv(mid, cashflows)
        if abs(value) < 1e-9 or (high - low) < 1e-12:
            return mid
        if value * low_npv > 0:
            low = mid
            low_npv = value
        else:
            high = mid
    return (low + high) / 2.0


def payback_period(cashflows: Sequence[float]) -> float:
    """Years until cumulative cash flow turns non-negative.

    Returns ``inf`` if the project never pays back.  Fractional years are
    interpolated within the crossing year.
    """
    if not cashflows:
        raise ValueError("cashflows must not be empty")
    cumulative = 0.0
    for period, value in enumerate(cashflows):
        previous = cumulative
        cumulative += value
        if cumulative >= 0 and previous < 0:
            if value == 0:
                return float(period)
            return period - 1 + (-previous) / value
        if period == 0 and cumulative >= 0:
            return 0.0
    return float("inf")


def discounted_payback(rate: float, cashflows: Sequence[float]) -> float:
    """Payback period using discounted cash flows."""
    if rate <= -1.0:
        raise ValueError("rate must be greater than -1")
    discounted = [value / (1.0 + rate) ** period for period, value in enumerate(cashflows)]
    return payback_period(discounted)


def ltv(
    *,
    monthly_revenue_per_customer: float,
    gross_margin: float,
    monthly_churn: float,
) -> float:
    """Customer lifetime value with a simple churn model.

    ``ltv = ARPU * gross_margin / churn`` assumes a geometric lifetime.  It
    ignores expansion revenue and discounting; both should be added explicitly
    when they matter.
    """
    if monthly_revenue_per_customer < 0:
        raise ValueError("revenue must be non-negative")
    if not 0.0 <= gross_margin <= 1.0:
        raise ValueError("gross_margin must be in [0, 1]")
    if not 0.0 < monthly_churn <= 1.0:
        raise ValueError("monthly_churn must be in (0, 1]")
    return monthly_revenue_per_customer * gross_margin / monthly_churn


def cac_payback_months(
    *,
    cac: float,
    monthly_revenue_per_customer: float,
    gross_margin: float,
) -> float:
    """Months to recover customer acquisition cost from gross profit."""
    if cac < 0:
        raise ValueError("cac must be non-negative")
    monthly_profit = monthly_revenue_per_customer * gross_margin
    if monthly_profit <= 0:
        return float("inf")
    return cac / monthly_profit


def price_for_margin(unit_cost: float, target_margin: float) -> float:
    """Price that yields ``target_margin`` as a fraction of revenue."""
    if unit_cost < 0:
        raise ValueError("unit_cost must be non-negative")
    if not 0.0 <= target_margin < 1.0:
        raise ValueError("target_margin must be in [0, 1)")
    return unit_cost / (1.0 - target_margin)


def committed_contract_value(
    *,
    monthly_commit: float,
    months: int,
    discount: float = 0.0,
    overage_rate: float = 0.0,
    expected_overage_units: float = 0.0,
) -> dict[str, float]:
    """Value of a committed-use contract with optional overage.

    A commit trades a discount for volume certainty.  The provider should
    compare the discounted contract value with the expected on-demand value,
    not just with the list price, because the commit also reduces acquisition
    and capacity risk.
    """
    if monthly_commit < 0 or months < 1:
        raise ValueError("invalid commit or term")
    if not 0.0 <= discount < 1.0:
        raise ValueError("discount must be in [0, 1)")
    if overage_rate < 0 or expected_overage_units < 0:
        raise ValueError("overage values must be non-negative")
    recurring = monthly_commit * (1.0 - discount) * months
    overage = overage_rate * expected_overage_units * months
    return {
        "committed_recurring": recurring,
        "expected_overage": overage,
        "total_contract_value": recurring + overage,
        "effective_monthly": (recurring + overage) / months,
        "discount_cost": monthly_commit * discount * months,
    }


def breakeven_utilization(
    *,
    annual_fixed_cost: float,
    gpu_count: int,
    contribution_per_gpu_hour: float,
    hours_per_year: float = 8760.0,
) -> float:
    """Utilisation at which contribution covers fixed cost.

    A result above 1.0 means the business cannot break even at this price and
    cost structure, no matter how full the cluster runs.  That is a useful
    "stop" signal, not a rounding error.
    """
    if annual_fixed_cost < 0 or gpu_count < 1:
        raise ValueError("invalid fixed cost or GPU count")
    if contribution_per_gpu_hour <= 0 or hours_per_year <= 0:
        raise ValueError("contribution and hours must be positive")
    capacity = gpu_count * hours_per_year * contribution_per_gpu_hour
    return annual_fixed_cost / capacity


@dataclass(frozen=True)
class Scenario:
    name: str
    cashflows: tuple[float, ...]

    def summary(self, rate: float) -> dict[str, float | str]:
        try:
            scenario_irr = irr(self.cashflows)
        except ValueError:
            # A scenario that never turns cash-positive has no meaningful IRR;
            # NaN keeps the row visible instead of aborting the whole table.
            scenario_irr = float("nan")
        return {
            "name": self.name,
            "npv": npv(rate, self.cashflows),
            "irr": scenario_irr,
            "payback_years": payback_period(self.cashflows),
        }


def scenario_table(
    base_cashflows: Sequence[float],
    *,
    rate: float,
    revenue_multiplier: float = 1.0,
    cost_multiplier: float = 1.0,
    annual_cost: float = 0.0,
) -> list[dict[str, float | str]]:
    """Best/base/worst NPV and payback for a revenue and cost shock.

    ``base_cashflows[0]`` is the initial investment; later entries are net
    operating cash flow.  When ``annual_cost`` is given, it reconstructs the
    revenue as ``net + annual_cost`` so a revenue shock and a cost shock are
    applied to different bases.  Without it, the older coarse behaviour is
    kept: positive flows scale with revenue, negative flows with cost.  The
    initial investment is never scaled, so the comparison isolates operating
    performance.
    """
    if not base_cashflows:
        raise ValueError("cashflows must not be empty")
    if revenue_multiplier <= 0 or cost_multiplier <= 0:
        raise ValueError("multipliers must be positive")
    if annual_cost < 0:
        raise ValueError("annual_cost must be non-negative")

    def transform(revenue_scale: float, cost_scale: float) -> tuple[float, ...]:
        rows = []
        for index, value in enumerate(base_cashflows):
            if index == 0:
                rows.append(value)
            elif annual_cost:
                revenue = value + annual_cost
                rows.append(revenue * revenue_scale - annual_cost * cost_scale)
            elif value >= 0:
                rows.append(value * revenue_scale)
            else:
                rows.append(value * cost_scale)
        return tuple(rows)

    scenarios = [
        Scenario("worst", transform(revenue_multiplier, cost_multiplier)),
        Scenario("base", tuple(base_cashflows)),
        Scenario("best", transform(1.0 / revenue_multiplier, 1.0 / cost_multiplier)),
    ]
    return [scenario.summary(rate) for scenario in scenarios]


def terminal_value(
    final_cashflow: float,
    *,
    discount_rate: float,
    growth_rate: float,
    periods: int,
) -> float:
    """Gordon growth terminal value, discounted back to today.

    ``periods`` is the number of explicit forecast periods before the
    terminal year.  The terminal value is computed one period after the last
    explicit cash flow, then discounted by ``periods``.  The model requires
    ``discount_rate > growth_rate``; otherwise the denominator is zero or
    negative and the value is meaningless.
    """
    if final_cashflow < 0:
        raise ValueError("final_cashflow must be non-negative")
    if discount_rate <= -1.0:
        raise ValueError("discount_rate must be greater than -1")
    if growth_rate <= -1.0:
        raise ValueError("growth_rate must be greater than -1")
    if discount_rate <= growth_rate:
        raise ValueError("discount_rate must exceed growth_rate")
    if periods < 0:
        raise ValueError("periods must be non-negative")
    value = final_cashflow * (1.0 + growth_rate) / (discount_rate - growth_rate)
    return value / (1.0 + discount_rate) ** periods


def dcf_value(
    cashflows: Sequence[float],
    *,
    discount_rate: float,
    terminal_growth: float = 0.0,
) -> dict[str, float]:
    """DCF value with an optional Gordon-growth terminal value.

    ``cashflows[0]`` is the present-period cash flow (usually negative capex)
    and is not discounted; later entries are end-of-period.  The terminal
    value uses the last explicit cash flow and is discounted by
    ``len(cashflows) - 1`` periods.  Set ``terminal_growth=0`` to value only
    the explicit horizon.
    """
    if not cashflows:
        raise ValueError("cashflows must not be empty")
    if discount_rate <= -1.0:
        raise ValueError("discount_rate must be greater than -1")
    explicit = sum(
        value / (1.0 + discount_rate) ** period
        for period, value in enumerate(cashflows)
    )
    if terminal_growth == 0.0:
        terminal = 0.0
    else:
        terminal = terminal_value(
            cashflows[-1],
            discount_rate=discount_rate,
            growth_rate=terminal_growth,
            periods=len(cashflows) - 1,
        )
    return {
        "explicit_value": explicit,
        "terminal_value": terminal,
        "enterprise_value": explicit + terminal,
    }


def revenue_multiple_value(revenue: float, multiple: float) -> float:
    """Value an enterprise from a revenue multiple.

    ``multiple`` should come from comparable companies or transactions in
    the same layer (infrastructure, API, application).  It is not a constant:
    it moves with growth, gross margin, retention and capital intensity.
    """
    if revenue < 0 or multiple < 0:
        raise ValueError("revenue and multiple must be non-negative")
    return revenue * multiple


def implied_revenue_multiple(enterprise_value: float, revenue: float) -> float:
    """Back out the revenue multiple implied by a valuation."""
    if enterprise_value < 0 or revenue < 0:
        raise ValueError("values must be non-negative")
    if revenue == 0:
        return float("inf")
    return enterprise_value / revenue


def venture_return(exit_value: float, investment: float) -> float:
    """Multiple on invested capital (MOIC) for a single position."""
    if exit_value < 0 or investment <= 0:
        raise ValueError("exit_value must be non-negative and investment positive")
    return exit_value / investment


def rule_of_forty(
    *,
    revenue_growth_rate: float,
    profit_margin: float,
) -> dict[str, float | bool]:
    """Growth plus margin heuristic for a subscription infrastructure business."""
    if not -1.0 <= revenue_growth_rate <= 10.0:
        raise ValueError("growth rate is out of range")
    if not -1.0 <= profit_margin <= 1.0:
        raise ValueError("profit margin is out of range")
    score = revenue_growth_rate + profit_margin
    return {"score": score, "passes": score >= 0.4}
