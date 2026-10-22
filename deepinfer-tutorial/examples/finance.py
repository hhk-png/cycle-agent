"""Financial analysis: cash flow, LTV/CAC, DCF, multiples and market sizing."""

from __future__ import annotations

from _common import add_project_root

add_project_root()

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
    venture_return,
)
from mini_deepinfer.market import (
    Segment,
    funnel_conversion,
    magic_number,
    market_share,
    obtainable_market,
    serviceable_market,
    tam_bottom_up,
)


def main() -> None:
    # An 8-GPU inference pod.  The 820k three-year TCO is split into 460k
    # upfront capex (GPU + server + network/storage) and 120k annual opex
    # (power + colo + operations), not treated as a single t=0 outflow.
    capex = -460_000.0
    annual_opex = 120_000.0
    revenue = [260_000.0, 360_000.0, 420_000.0]
    cashflows = [capex] + [r - annual_opex for r in revenue]
    print(f"capex: ${-capex:,.0f}; annual opex: ${annual_opex:,.0f}")
    print(f"revenue: {revenue}")
    print(f"net cashflows (revenue - opex): {cashflows}")
    print(f"NPV at 10%: ${npv(0.10, cashflows):,.0f}")
    print(f"IRR: {irr(cashflows) * 100:.2f}%")
    print(f"payback: {payback_period(cashflows):.2f} years")
    print(f"discounted payback at 10%: {discounted_payback(0.10, cashflows):.2f} years")

    print("scenarios at 10% (revenue -20%, cost +20%):")
    for row in scenario_table(
        cashflows,
        rate=0.10,
        revenue_multiplier=0.8,
        cost_multiplier=1.2,
        annual_cost=annual_opex,
    ):
        print(
            f"  {row['name']}: npv=${row['npv']:,.0f} "
            f"irr={row['irr'] * 100:.2f}% payback={row['payback_years']:.2f}y"
        )

    customer_ltv = ltv(
        monthly_revenue_per_customer=1_100,
        gross_margin=0.55,
        monthly_churn=0.02,
    )
    payback = cac_payback_months(
        cac=20_000,
        monthly_revenue_per_customer=1_100,
        gross_margin=0.55,
    )
    print(f"LTV: ${customer_ltv:,.0f}")
    print(f"CAC payback: {payback:.1f} months")

    price = price_for_margin(unit_cost=0.52, target_margin=0.35)
    print(f"price for 35% margin on $0.52/Mtok: ${price:.4f}/Mtok")

    contract = committed_contract_value(
        monthly_commit=10_000,
        months=12,
        discount=0.20,
        overage_rate=0.50,
        expected_overage_units=1_000,
    )
    print("committed contract:")
    for key, value in contract.items():
        print(f"  {key}: ${value:,.0f}")

    utilization = breakeven_utilization(
        annual_fixed_cost=273_000,
        gpu_count=8,
        contribution_per_gpu_hour=20,
    )
    print(f"breakeven utilisation: {utilization:.3f}")
    print(f"rule of forty (30% growth, 15% margin): {rule_of_forty(revenue_growth_rate=0.30, profit_margin=0.15)}")

    valuation = dcf_value(cashflows, discount_rate=0.10, terminal_growth=0.02)
    print("DCF with 2% terminal growth:")
    for key, value in valuation.items():
        print(f"  {key}: ${value:,.0f}")
    print(f"EV at 4x revenue on $10M: ${revenue_multiple_value(10_000_000, 4.0):,.0f}")
    print(f"implied multiple: {implied_revenue_multiple(40_000_000, 10_000_000):.1f}x")
    print(f"venture MOIC on $10M -> $50M: {venture_return(50_000_000, 10_000_000):.1f}x")

    segments = [
        Segment("enterprise", accounts=20_000, annual_tokens=10_000_000_000, price_per_mtok=0.5),
        Segment("self-serve", accounts=200_000, annual_tokens=200_000_000, price_per_mtok=0.3),
    ]
    tam = tam_bottom_up(segments)
    sam = serviceable_market(tam, 0.30)
    som = obtainable_market(sam, 0.10, year=3, ramp_years=3)
    print(f"market TAM/SAM/SOM: ${tam:,.0f} / ${sam:,.0f} / ${som:,.0f}")
    print(f"funnel 100k visitors: {funnel_conversion(100_000, [0.20, 0.30, 0.25])}")
    print(f"magic number (2M new ARR / 1.5M spend): {magic_number(2_000_000, 1_500_000):.2f}")
    print(f"share at $3M / $100M: {market_share(3_000_000, 100_000_000):.1%}")


if __name__ == "__main__":
    main()
