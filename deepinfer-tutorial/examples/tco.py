"""A reproducible three-year TCO and unit-economics scenario."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mini_deepinfer.economics import (  # noqa: E402
    capacity_plan,
    cost_per_million_tokens,
    gross_margin,
    p_d_gpu_split,
    revenue_per_million_tokens,
    sensitivity,
    tco_breakdown,
)


def main() -> None:
    tco = tco_breakdown(
        gpu_price=30_000,
        gpu_count=8,
        server_price=120_000,
        network_storage=100_000,
        power_kw_per_gpu=1.5,
        electricity_price_per_kwh=0.10,
        colocation_per_gpu_year=1_200,
        operations_per_year=100_000,
        years=3,
        utilization=0.70,
    )
    print("TCO breakdown (8 GPUs, 3 years):")
    for key, value in tco.as_dict().items():
        if isinstance(value, float) and value > 1_000:
            print(f"  {key:28s} {value:,.0f}")
        else:
            print(f"  {key:28s} {value}")
    print(f"  capex share                  {tco.capex / tco.total:.1%}")
    print(f"  power share                  {tco.power / tco.total:.1%}")

    # Throughput and price are illustrative; replace them with measurements.
    cost = cost_per_million_tokens(tco.cost_per_effective_gpu_hour, 3_000)
    revenue = revenue_per_million_tokens(38.0, 3_000)
    print(f"\ncost per Mtok   : ${cost:.3f}")
    print(f"revenue per Mtok: ${revenue:.3f}")
    print(f"gross margin    : {gross_margin(revenue, cost):.1%}")

    print("\nsensitivity (worst, best):")
    # Use the actual power share of this TCO, not the generic 15% default.
    electricity_share = tco.power / tco.total
    for name, low, high in sensitivity(
        base_cost_per_mtok=cost, electricity_share=electricity_share
    ):
        print(f"  {name:12s} ${low:.3f} .. ${high:.3f}")

    plan = capacity_plan(
        qps=20,
        average_stay_s=30,
        average_context_tokens=34_000,
        kv_bytes_per_token=890,
        redundancy=0.25,
    )
    print("\ncapacity plan:")
    print(f"  concurrency      {plan['concurrency']:.0f}")
    print(f"  KV footprint     {plan['kv_gib']:.1f} GiB")
    print(f"  with redundancy  {plan['kv_bytes_with_redundancy'] / (1024**3):.1f} GiB")

    split = p_d_gpu_split(
        prefill_tokens_per_s=120_000,
        decode_tokens_per_s=40_000,
        prefill_tokens_per_s_per_gpu=60_000,
        decode_tokens_per_s_per_gpu=14_000,
        headroom=0.2,
    )
    print("\nP/D split:")
    for key, value in split.items():
        print(f"  {key:16s} {value:.2f}")


if __name__ == "__main__":
    main()
