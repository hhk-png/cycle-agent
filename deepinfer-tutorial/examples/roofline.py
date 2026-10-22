"""Run the tutorial's roofline calculations on an illustrative device.

The point of this script is not to benchmark your machine.  It shows how to
turn a hardware spec, a model shape and a workload into a first-order answer
to three questions:

1. Is this operation compute-bound or memory-bound?
2. How much does materialising attention scores cost?
3. What token cost and speculative speedup should we expect?

Run with ``python examples/roofline.py``.
"""

from __future__ import annotations

from _common import add_project_root

add_project_root()

from mini_deepinfer.roofline import (
    Hardware,
    attention_cost,
    cost_per_million_tokens,
    decode_step_cost,
    expected_accept_length,
    gemm_cost,
    kv_bytes_per_token,
    speculative_speedup,
    ttl_storage_bytes,
)

# Deliberately round numbers, close to a current high-end accelerator.  The
# conclusions depend on the ratio, not on the exact SKU.
GPU = Hardware(
    name="illustrative 1 PFLOP/s, 3 TB/s",
    peak_flops=1000e12,
    memory_bw=3e12,
    sram_bytes=256 * 1024 * 1024,
)


def show(title: str, cost) -> None:
    print(
        f"{title}: flops={cost.flops / 1e12:.3f} TFLOP, "
        f"bytes={cost.bytes_moved / 1e9:.3f} GB, "
        f"compute={cost.compute_s * 1000:.3f} ms, "
        f"memory={cost.memory_s * 1000:.3f} ms, "
        f"intensity={cost.intensity:.1f}, bound={cost.bound}"
    )


def main() -> None:
    print(f"device: {GPU.name}")
    print(f"ridge point: {GPU.ridge_point:.1f} FLOP/byte")

    # 1. A large prefill GEMM.
    big = gemm_cost(8192, 8192, 8192, GPU, input_bytes=1, output_bytes=2)
    show("prefill GEMM 8192^3 FP8", big)
    assert big.bound == "compute"

    # 2. A decode step that reads 16B active parameters in FP8.
    decode = decode_step_cost(16_000_000_000, GPU)
    show("decode 16B active, batch=1 FP8", decode)
    assert decode.bound == "memory"
    print(f"  weight read alone: {16e9 / GPU.memory_bw * 1000:.2f} ms")

    # 3. Prefill attention with and without score materialisation.
    q_len = kv_len = 32768
    flash = attention_cost(q_len, kv_len, 64, 128, GPU, flash=True)
    naive = attention_cost(q_len, kv_len, 64, 128, GPU, flash=False)
    show("prefill attention 32K, FlashMLA-style", flash)
    show("prefill attention 32K, materialised scores", naive)
    assert naive.time_s > flash.time_s
    print(f"  score materialisation adds {naive.memory_s * 1000:.1f} ms of traffic")

    # 4. The V4.1 KV footprint at the published 890 B/token.
    per_token = kv_bytes_per_token(40, 1, 128, 4, compression_ratio=2, scale_bytes=1, scale_group=16)
    print(f"C2 + FP4 + per-16 scale KV estimate: {per_token:.0f} B/token")
    published = 890.0
    context = 131072
    print(
        f"published {published:.0f} B/token x 128K = "
        f"{published * context / 1e6:.1f} MB per sequence"
    )

    # 5. Speculative decoding: acceptance decay is what limits k.
    confidences = [0.9, 0.8, 0.7, 0.6, 0.5]
    expected = expected_accept_length(confidences)
    speedup = speculative_speedup(confidences, draft_relative_cost=0.1)
    print(f"expected accepted length: {expected:.3f} tokens per step")
    print(f"speculative speedup at draft cost 0.1: {speedup:.3f}x")
    assert speedup > 1.0

    # 6. Unit economics and cache sizing.
    unit = cost_per_million_tokens(3.0, 3000.0, utilization=0.7)
    print(f"cost per million output tokens: ${unit:.3f}")
    storage = ttl_storage_bytes(50_000, 3 * 86400, published)
    print(f"KV stored at 50K tok/s for a 3-day TTL: {storage / 1e12:.2f} TB")
    assert 0 < unit < 1.0


if __name__ == "__main__":
    main()
