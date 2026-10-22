"""Unit economics for inference services.

The performance chapters answer "how fast"; this module answers "what does it
cost, and which assumption moves the answer most".  All functions are pure and
return dataclasses so that a notebook, an example or a test can assert on the
individual terms.

The numbers used by examples are illustrative.  Replace them with your own
purchase price, electricity contract, utilisation and measured throughput
before making a decision.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TCOBreakdown:
    years: float
    gpu_count: int
    capex: float
    power: float
    colocation: float
    operations: float
    network_storage: float
    total: float
    utilization: float
    cost_per_gpu_hour: float
    cost_per_effective_gpu_hour: float

    def as_dict(self) -> dict:
        return {
            "years": self.years,
            "gpu_count": self.gpu_count,
            "capex": self.capex,
            "power": self.power,
            "colocation": self.colocation,
            "operations": self.operations,
            "network_storage": self.network_storage,
            "total": self.total,
            "utilization": self.utilization,
            "cost_per_gpu_hour": self.cost_per_gpu_hour,
            "cost_per_effective_gpu_hour": self.cost_per_effective_gpu_hour,
        }


def tco_breakdown(
    *,
    gpu_price: float = 30_000.0,
    gpu_count: int = 8,
    server_price: float = 120_000.0,
    network_storage: float = 100_000.0,
    power_kw_per_gpu: float = 1.5,
    electricity_price_per_kwh: float = 0.10,
    colocation_per_gpu_year: float = 1_200.0,
    operations_per_year: float = 100_000.0,
    years: float = 3.0,
    utilization: float = 0.7,
) -> TCOBreakdown:
    """Three-year TCO with the main cost lines kept separate."""
    if gpu_count < 1 or years <= 0:
        raise ValueError("gpu_count and years must be positive")
    if not 0 < utilization <= 1:
        raise ValueError("utilization must be in (0, 1]")
    hours = years * 8_760.0
    capex = gpu_price * gpu_count + server_price
    power = power_kw_per_gpu * gpu_count * hours * electricity_price_per_kwh
    colocation = colocation_per_gpu_year * gpu_count * years
    operations = operations_per_year * years
    total = capex + network_storage + power + colocation + operations
    cost_per_gpu_hour = total / (gpu_count * hours)
    return TCOBreakdown(
        years=years,
        gpu_count=gpu_count,
        capex=capex,
        power=power,
        colocation=colocation,
        operations=operations,
        network_storage=network_storage,
        total=total,
        utilization=utilization,
        cost_per_gpu_hour=cost_per_gpu_hour,
        cost_per_effective_gpu_hour=cost_per_gpu_hour / utilization,
    )


def cost_per_million_tokens(
    gpu_hour_cost: float,
    output_tokens_per_s: float,
    utilization: float = 1.0,
) -> float:
    """Cost of one million output tokens for one GPU."""
    if gpu_hour_cost < 0 or output_tokens_per_s <= 0:
        raise ValueError("invalid cost or throughput")
    if not 0 < utilization <= 1:
        raise ValueError("utilization must be in (0, 1]")
    return gpu_hour_cost / utilization * 1e6 / (output_tokens_per_s * 3600.0)


def revenue_per_million_tokens(
    revenue_per_gpu_hour: float,
    output_tokens_per_s: float,
) -> float:
    if revenue_per_gpu_hour < 0 or output_tokens_per_s <= 0:
        raise ValueError("invalid revenue or throughput")
    return revenue_per_gpu_hour * 1e6 / (output_tokens_per_s * 3600.0)


def gross_margin(revenue_per_mtok: float, cost_per_mtok: float) -> float:
    """Contribution margin as a fraction of revenue."""
    if revenue_per_mtok <= 0:
        raise ValueError("revenue must be positive")
    return (revenue_per_mtok - cost_per_mtok) / revenue_per_mtok


def capacity_plan(
    *,
    qps: float,
    average_stay_s: float,
    average_context_tokens: float,
    kv_bytes_per_token: float,
    redundancy: float = 0.25,
) -> dict:
    """Little's law plus a KV-footprint estimate.

    ``average_context_tokens`` should include both prompt and generated tokens;
    using only the prompt underestimates the cache of long-output workloads.
    """
    if qps <= 0 or average_stay_s <= 0 or average_context_tokens <= 0:
        raise ValueError("qps, stay time and context must be positive")
    if kv_bytes_per_token <= 0:
        raise ValueError("kv_bytes_per_token must be positive")
    if redundancy < 0:
        raise ValueError("redundancy must be non-negative")
    concurrency = qps * average_stay_s
    kv_bytes = concurrency * average_context_tokens * kv_bytes_per_token
    return {
        "concurrency": concurrency,
        "kv_bytes": kv_bytes,
        "kv_gib": kv_bytes / (1024**3),
        "kv_bytes_with_redundancy": kv_bytes * (1.0 + redundancy),
        "input_tokens_per_s": qps * average_context_tokens,
    }


def p_d_gpu_split(
    *,
    prefill_tokens_per_s: float,
    decode_tokens_per_s: float,
    prefill_tokens_per_s_per_gpu: float,
    decode_tokens_per_s_per_gpu: float,
    headroom: float = 0.2,
) -> dict:
    """Estimate how many prefill and decode GPUs a steady load needs."""
    if min(prefill_tokens_per_s_per_gpu, decode_tokens_per_s_per_gpu) <= 0:
        raise ValueError("per-GPU throughput must be positive")
    if headroom < 0:
        raise ValueError("headroom must be non-negative")
    p = prefill_tokens_per_s / prefill_tokens_per_s_per_gpu
    d = decode_tokens_per_s / decode_tokens_per_s_per_gpu
    return {
        "prefill_gpus": p * (1.0 + headroom),
        "decode_gpus": d * (1.0 + headroom),
        "total_gpus": (p + d) * (1.0 + headroom),
        "p_to_d_ratio": (p / d) if d else float("inf"),
    }


def sensitivity(
    *,
    base_cost_per_mtok: float,
    throughput_change: float = 0.3,
    utilization_change: float = 0.2,
    electricity_change: float = 0.5,
    electricity_share: float = 0.15,
) -> list[tuple[str, float, float]]:
    """Return ``(variable, worst_cost, best_cost)`` for a tornado chart.

    Throughput and utilisation act directly on the denominator of the unit
    cost.  Electricity acts only on the share of cost it represents, which is
    why a large percentage change in the power price is often less important
    than a modest change in utilisation.
    """
    if base_cost_per_mtok <= 0:
        raise ValueError("base cost must be positive")
    if not 0 <= throughput_change < 1 or not 0 <= utilization_change < 1:
        raise ValueError("throughput/utilization changes must be in [0, 1)")
    if not 0 <= electricity_share <= 1:
        raise ValueError("electricity_share must be in [0, 1]")
    low_throughput = base_cost_per_mtok / (1.0 - throughput_change)
    high_throughput = base_cost_per_mtok / (1.0 + throughput_change)
    low_util = base_cost_per_mtok / (1.0 - utilization_change)
    high_util = base_cost_per_mtok / (1.0 + utilization_change)
    low_power = base_cost_per_mtok * (1.0 + electricity_share * electricity_change)
    high_power = base_cost_per_mtok * (1.0 - electricity_share * electricity_change)
    return [
        ("throughput", low_throughput, high_throughput),
        ("utilization", low_util, high_util),
        ("electricity", low_power, high_power),
    ]
