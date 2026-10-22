"""Edge inference: memory fit, decode roofline, energy and hybrid routing."""

from __future__ import annotations

from _common import add_project_root

add_project_root()

from mini_deepinfer.edge import (
    EdgeDevice,
    EdgeRequest,
    energy_per_token,
    estimate_decode,
    plan_memory,
    quantized_weight_bytes,
    route_edge_or_cloud,
    thermal_sustained_tokens_per_s,
)


def main() -> None:
    devices = [
        EdgeDevice("laptop-cpu", memory_gb=16, bandwidth_gbs=50, compute_tflops=0.5, power_w=45),
        EdgeDevice("phone-npu", memory_gb=8, bandwidth_gbs=60, compute_tflops=20, power_w=6, npu_tops=40),
        EdgeDevice("mini-pc-gpu", memory_gb=32, bandwidth_gbs=300, compute_tflops=30, power_w=150),
    ]
    parameters = 1_500_000_000
    context = 4096
    kv_per_token = 890

    print("quantised model size:")
    for bits in (16, 8, 4):
        size = quantized_weight_bytes(parameters, bits)
        print(f"  {bits:2d}-bit: {size / 1e9:.3f} GB")

    print("memory plans (4-bit, 4K context):")
    for device in devices:
        plan = plan_memory(
            device,
            parameters=parameters,
            bits=4,
            context_tokens=context,
            kv_bytes_per_token=kv_per_token,
        )
        print(
            f"  {device.name}: fits={plan.fits} "
            f"weights={plan.weight_bytes / 1e9:.3f}GB "
            f"kv={plan.kv_bytes / 1e6:.2f}MB "
            f"util={plan.utilization:.3f}"
        )

    print("decode estimates (4-bit, 4K context):")
    for device in devices:
        estimate = estimate_decode(
            device,
            active_parameters=parameters,
            weight_bits=4,
            context_tokens=context,
            kv_bytes_per_token=kv_per_token,
        )
        joules = energy_per_token(device, estimate.tokens_per_s)
        print(
            f"  {device.name}: {estimate.tokens_per_s:.2f} tok/s "
            f"step={estimate.step_s * 1000:.2f}ms bottleneck={estimate.bottleneck} "
            f"energy={joules:.3f} J/token"
        )

    laptop = devices[0]
    peak = estimate_decode(
        laptop,
        active_parameters=parameters,
        weight_bits=4,
        context_tokens=context,
        kv_bytes_per_token=kv_per_token,
    ).tokens_per_s
    sustained = thermal_sustained_tokens_per_s(peak, workload_s=600)
    print(f"thermal: peak={peak:.2f} tok/s sustained_10min={sustained:.2f} tok/s")

    requests = [
        EdgeRequest(200, 300, privacy="public", latency_budget_ms=2000),
        EdgeRequest(200, 300, privacy="private", latency_budget_ms=60_000),
        EdgeRequest(200, 300, privacy="public", latency_budget_ms=50),
        EdgeRequest(200, 300, privacy="public", latency_budget_ms=50, network_available=False),
    ]
    print("routing:")
    for request in requests:
        decision = route_edge_or_cloud(
            request,
            edge=laptop,
            edge_tokens_per_s=max(1.0, peak),
            cloud_tokens_per_s=200,
        )
        print(
            f"  privacy={request.privacy} budget={request.latency_budget_ms}ms "
            f"network={request.network_available} -> {decision.target} "
            f"({decision.reason}, feasible={decision.feasible}, "
            f"latency={decision.estimated_latency_ms:.1f}ms)"
        )


if __name__ == "__main__":
    main()
