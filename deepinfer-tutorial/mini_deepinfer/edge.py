"""Edge and heterogeneous inference planning.

A data-centre engine assumes many concurrent sequences, a wide interconnect
and a large HBM pool.  An edge deployment assumes the opposite: batch one, a
shared memory bus, a power/thermal budget and no reliable network.  The
mechanisms that matter change accordingly: quantisation, mmap, prompt reuse,
speculative decoding and a cloud fallback rather than Wide-EP and PD
disaggregation.

This module provides the arithmetic that a deployment decision needs:

* whether a quantised model fits a device;
* a roofline decode estimate driven by memory bandwidth;
* energy per token and a thermal sustained-throughput correction;
* a privacy/latency-aware edge-versus-cloud router.

The numbers are first-order.  They are useful for comparing devices and
quantisation plans before writing a kernel, not for promising a benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeDevice:
    """The parts of an edge device that determine decode throughput."""

    name: str
    memory_gb: float
    bandwidth_gbs: float
    compute_tflops: float
    power_w: float
    npu_tops: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("device name must not be empty")
        if self.memory_gb <= 0 or self.bandwidth_gbs <= 0 or self.compute_tflops <= 0:
            raise ValueError("memory, bandwidth and compute must be positive")
        if self.power_w < 0 or self.npu_tops < 0:
            raise ValueError("power and NPU throughput must be non-negative")

    @property
    def ridge_point(self) -> float:
        """FLOP per byte at which the device switches from memory to compute."""
        return self.compute_tflops * 1e12 / (self.bandwidth_gbs * 1e9)


def quantized_weight_bytes(parameters: int, bits: int, *, scale_overhead: float = 0.0) -> int:
    """Bytes for a quantised weight tensor, including scale overhead.

    ``scale_overhead`` is a fraction of the data bytes, for example 0.0625 for
    one FP8 scale per 16 values, or 0.03125 for one scale per 32 values.
    """
    if parameters < 1:
        raise ValueError("parameters must be positive")
    if bits not in (2, 3, 4, 5, 6, 8, 16, 32):
        raise ValueError("unsupported bit width")
    if not 0.0 <= scale_overhead < 1.0:
        raise ValueError("scale_overhead must be in [0, 1)")
    data = parameters * bits / 8.0
    return int(data * (1.0 + scale_overhead))


@dataclass(frozen=True)
class MemoryPlan:
    weight_bytes: int
    kv_bytes: int
    activation_bytes: int
    total_bytes: int
    device_bytes: int
    fits: bool
    utilization: float


def plan_memory(
    device: EdgeDevice,
    *,
    parameters: int,
    bits: int,
    context_tokens: int,
    kv_bytes_per_token: int,
    batch_size: int = 1,
    activation_bytes: int = 64 * 1024 * 1024,
    headroom: float = 0.15,
    scale_overhead: float = 0.0,
) -> MemoryPlan:
    """Check whether a quantised model plus KV fits on an edge device.

    Headroom covers the runtime, graph buffers, tokenizer and fragmentation.
    Edge runtimes cannot rely on a paging allocator as large as a server's, so
    a plan that uses 99% of memory is not a plan.
    """
    if context_tokens < 1 or kv_bytes_per_token < 1 or batch_size < 1:
        raise ValueError("context, KV size and batch must be positive")
    if not 0.0 <= headroom < 1.0:
        raise ValueError("headroom must be in [0, 1)")
    weights = quantized_weight_bytes(parameters, bits, scale_overhead=scale_overhead)
    kv = context_tokens * kv_bytes_per_token * batch_size
    total = weights + kv + activation_bytes
    device_bytes = int(device.memory_gb * (1024**3) * (1.0 - headroom))
    return MemoryPlan(
        weight_bytes=weights,
        kv_bytes=kv,
        activation_bytes=activation_bytes,
        total_bytes=total,
        device_bytes=device_bytes,
        fits=total <= device_bytes,
        utilization=total / device_bytes if device_bytes else float("inf"),
    )


@dataclass(frozen=True)
class DecodeEstimate:
    tokens_per_s: float
    step_s: float
    memory_s: float
    compute_s: float
    bottleneck: str
    bytes_per_step: int


def estimate_decode(
    device: EdgeDevice,
    *,
    active_parameters: int,
    weight_bits: int,
    context_tokens: int,
    kv_bytes_per_token: int,
    batch_size: int = 1,
    efficiency: float = 0.45,
) -> DecodeEstimate:
    """Roofline decode estimate for a memory-bound edge workload.

    ``efficiency`` folds in the fact that a real runtime does not reach the
    datasheet bandwidth: dequantisation, attention, sampling and framework
    overhead all consume part of the step.  A value of 0.4 to 0.6 is a useful
    planning range for CPU/NPU runtimes; measure the target device before
    making a promise.
    """
    if active_parameters < 1 or context_tokens < 1 or kv_bytes_per_token < 1:
        raise ValueError("model and context sizes must be positive")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not 0.0 < efficiency <= 1.0:
        raise ValueError("efficiency must be in (0, 1]")
    weight_bytes = quantized_weight_bytes(active_parameters, weight_bits)
    kv_bytes = batch_size * context_tokens * kv_bytes_per_token
    bytes_per_step = weight_bytes + kv_bytes
    memory_s = bytes_per_step / (device.bandwidth_gbs * 1e9 * efficiency)
    flops = 2.0 * active_parameters * batch_size
    compute_s = flops / (device.compute_tflops * 1e12 * efficiency)
    step_s = max(memory_s, compute_s)
    return DecodeEstimate(
        tokens_per_s=batch_size / step_s,
        step_s=step_s,
        memory_s=memory_s,
        compute_s=compute_s,
        bottleneck="memory" if memory_s >= compute_s else "compute",
        bytes_per_step=bytes_per_step,
    )


def estimate_prefill_tokens_per_s(
    device: EdgeDevice,
    *,
    active_parameters: int,
    efficiency: float = 0.35,
) -> float:
    """Compute-bound prefill estimate for one token at a time."""
    if active_parameters < 1:
        raise ValueError("active_parameters must be positive")
    if not 0.0 < efficiency <= 1.0:
        raise ValueError("efficiency must be in (0, 1]")
    flops_per_token = 2.0 * active_parameters
    return device.compute_tflops * 1e12 * efficiency / flops_per_token


def energy_per_token(device: EdgeDevice, tokens_per_s: float) -> float:
    """Joules per generated token at the device's average power."""
    if tokens_per_s <= 0:
        raise ValueError("tokens_per_s must be positive")
    return device.power_w / tokens_per_s


def thermal_sustained_tokens_per_s(
    peak_tokens_per_s: float,
    *,
    throttle_fraction: float = 0.6,
    throttle_after_s: float = 30.0,
    workload_s: float = 300.0,
) -> float:
    """Average throughput after a thermal throttle kicks in.

    Many edge devices burst above their sustained clock for tens of seconds.
    A benchmark shorter than the throttle window reports the burst number; a
    product that runs for minutes sees the sustained number.
    """
    if peak_tokens_per_s <= 0 or workload_s <= 0:
        raise ValueError("throughput and workload must be positive")
    if not 0.0 < throttle_fraction <= 1.0:
        raise ValueError("throttle_fraction must be in (0, 1]")
    if throttle_after_s < 0:
        raise ValueError("throttle_after_s must be non-negative")
    burst_s = min(throttle_after_s, workload_s)
    sustained_s = max(0.0, workload_s - burst_s)
    tokens = peak_tokens_per_s * burst_s
    tokens += peak_tokens_per_s * throttle_fraction * sustained_s
    return tokens / workload_s


@dataclass(frozen=True)
class EdgeRequest:
    prompt_tokens: int
    output_tokens: int
    privacy: str = "public"
    latency_budget_ms: float | None = None
    network_available: bool = True

    def __post_init__(self) -> None:
        if self.prompt_tokens < 1 or self.output_tokens < 1:
            raise ValueError("token counts must be positive")
        if self.privacy not in ("public", "private", "confidential"):
            raise ValueError("privacy must be public, private or confidential")
        if self.latency_budget_ms is not None and self.latency_budget_ms <= 0:
            raise ValueError("latency_budget_ms must be positive")


@dataclass(frozen=True)
class EdgeRoute:
    target: str
    reason: str
    feasible: bool
    estimated_latency_ms: float
    estimated_cost_usd: float


def route_edge_or_cloud(
    request: EdgeRequest,
    *,
    edge: EdgeDevice,
    edge_tokens_per_s: float,
    edge_fixed_latency_ms: float = 20.0,
    cloud_tokens_per_s: float = 200.0,
    cloud_fixed_latency_ms: float = 120.0,
    cloud_cost_per_mtok: float = 0.5,
    edge_energy_cost_per_kwh: float = 0.10,
) -> EdgeRoute:
    """Choose edge or cloud under privacy, latency and availability constraints.

    Confidential requests never leave the device.  If they do not fit the
    latency budget, the decision is an explicit infeasible edge result rather
    than a silent cloud fallback; the caller can then reject or ask the user
    to relax the budget.
    """
    if edge_tokens_per_s <= 0 or cloud_tokens_per_s <= 0:
        raise ValueError("throughput must be positive")
    edge_latency = edge_fixed_latency_ms + request.output_tokens / edge_tokens_per_s * 1000.0
    cloud_latency = cloud_fixed_latency_ms + request.output_tokens / cloud_tokens_per_s * 1000.0
    cloud_cost = request.output_tokens * cloud_cost_per_mtok / 1_000_000.0
    # Electricity is tiny per request but makes the comparison explicit.
    edge_energy_kwh = (
        edge.power_w * (request.output_tokens / edge_tokens_per_s) / 3600.0 / 1000.0
    )
    edge_cost = edge_energy_kwh * edge_energy_cost_per_kwh

    if request.privacy in ("private", "confidential"):
        feasible = (
            request.latency_budget_ms is None
            or edge_latency <= request.latency_budget_ms
        )
        return EdgeRoute(
            target="edge",
            reason="privacy_requires_local" if feasible else "privacy_but_latency_infeasible",
            feasible=feasible,
            estimated_latency_ms=edge_latency,
            estimated_cost_usd=edge_cost,
        )

    if request.latency_budget_ms is not None and edge_latency <= request.latency_budget_ms:
        return EdgeRoute(
            target="edge",
            reason="edge_meets_latency",
            feasible=True,
            estimated_latency_ms=edge_latency,
            estimated_cost_usd=edge_cost,
        )
    if not request.network_available:
        feasible = (
            request.latency_budget_ms is None
            or edge_latency <= request.latency_budget_ms
        )
        return EdgeRoute(
            target="edge",
            reason="offline_fallback" if feasible else "offline_but_latency_infeasible",
            feasible=feasible,
            estimated_latency_ms=edge_latency,
            estimated_cost_usd=edge_cost,
        )
    return EdgeRoute(
        target="cloud",
        reason="edge_latency_miss",
        feasible=True,
        estimated_latency_ms=cloud_latency,
        estimated_cost_usd=cloud_cost,
    )
