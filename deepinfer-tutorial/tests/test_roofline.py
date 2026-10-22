from __future__ import annotations

import pytest

from mini_deepinfer.roofline import (
    Hardware,
    attention_cost,
    cost_per_million_tokens,
    decode_step_cost,
    expected_accept_length,
    gemm_cost,
    kv_bytes_per_token,
    linear_cost,
    speculative_speedup,
    ttl_storage_bytes,
)

GPU = Hardware("test", peak_flops=1000e12, memory_bw=3e12)


def test_ridge_point_is_compute_over_bandwidth() -> None:
    assert GPU.ridge_point == pytest.approx(1000e12 / 3e12)
    assert GPU.ridge_point > 300


def test_large_gemm_is_compute_bound_and_small_decode_is_memory_bound() -> None:
    gemm = gemm_cost(8192, 8192, 8192, GPU, input_bytes=1, output_bytes=2)
    decode = decode_step_cost(16_000_000_000, GPU)
    assert gemm.bound == "compute"
    assert decode.bound == "memory"
    # A decode step spends far longer reading weights than doing arithmetic.
    assert decode.memory_s / decode.compute_s > 100


def test_batching_amortises_the_weight_read() -> None:
    one = linear_cost(1, 4096, 4096, GPU, weight_bytes=1, activation_bytes=2)
    eight = linear_cost(8, 4096, 4096, GPU, weight_bytes=1, activation_bytes=2)
    assert eight.flops == pytest.approx(8 * one.flops)
    # Eight rows cost far less than eight separate weight reads.
    assert eight.time_s < 8 * one.time_s
    assert eight.time_s / one.time_s < 4


def test_flash_attention_moves_fewer_bytes_than_materialised_scores() -> None:
    flash = attention_cost(4096, 4096, 32, 128, GPU, flash=True)
    naive = attention_cost(4096, 4096, 32, 128, GPU, flash=False)
    assert flash.flops == pytest.approx(naive.flops)
    assert flash.bytes_moved < naive.bytes_moved
    assert naive.time_s >= flash.time_s


def test_kv_bytes_scale_with_precision_and_compression() -> None:
    fp16 = kv_bytes_per_token(40, 1, 128, 16)
    fp8 = kv_bytes_per_token(40, 1, 128, 8)
    fp4 = kv_bytes_per_token(40, 1, 128, 4)
    assert fp16 > fp8 > fp4
    assert fp16 == pytest.approx(2 * fp8)
    compressed = kv_bytes_per_token(40, 1, 128, 4, compression_ratio=2)
    assert compressed < fp4


def test_speculative_speedup_uses_cumulative_acceptance() -> None:
    confidences = [1.0, 1.0, 1.0, 1.0]
    assert expected_accept_length(confidences) == pytest.approx(5.0)
    assert speculative_speedup(confidences, draft_relative_cost=0.1) == pytest.approx(5.0 / 1.4)
    # A weaker draft does not pay for a long verification prefix.
    weak = [0.9, 0.2, 0.1, 0.1]
    assert speculative_speedup(weak, draft_relative_cost=0.1) < speculative_speedup(
        confidences, draft_relative_cost=0.1
    )


def test_unit_economics_and_ttl_storage() -> None:
    assert cost_per_million_tokens(3.0, 3000.0) == pytest.approx(3e6 / (3000 * 3600))
    # Lower utilisation increases the effective cost per token.
    assert cost_per_million_tokens(3.0, 3000.0, utilization=0.5) == pytest.approx(
        2 * cost_per_million_tokens(3.0, 3000.0)
    )
    assert ttl_storage_bytes(1000, 86400, 890) == pytest.approx(1000 * 86400 * 890)
