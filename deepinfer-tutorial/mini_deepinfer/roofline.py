"""Back-of-the-envelope performance models.

The engine in this repository runs on NumPy, so it cannot measure GPU
behaviour.  What it can do is make the arithmetic behind the tutorial's
optimisation arguments executable: FLOPs, bytes moved, the ridge point of a
device, the cost of materialising an attention matrix, the expected speedup of
speculative decoding, and the unit cost of a generated token.

Every function returns a small dataclass so that examples and tests can assert
on the individual terms instead of parsing printed text.  The hardware numbers
used in the examples are illustrative; replace them with a measured peak and a
measured memcpy bandwidth before using the model for a real decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Hardware:
    """A device's two roofline limits.

    ``peak_flops`` is the sustained rate for the relevant precision (dense,
    without sparsity).  ``memory_bw`` is the achievable HBM bandwidth, not the
    datasheet peak.  Both are expressed in SI units.
    """

    name: str
    peak_flops: float
    memory_bw: float
    sram_bytes: int = 0
    notes: str = ""

    def __post_init__(self) -> None:
        if self.peak_flops <= 0 or self.memory_bw <= 0:
            raise ValueError("peak_flops and memory_bw must be positive")

    @property
    def ridge_point(self) -> float:
        """Arithmetic intensity (FLOP/byte) at which the two limits meet."""
        return self.peak_flops / self.memory_bw


@dataclass(frozen=True)
class Cost:
    """Time lower bounds for one operation and the limiting resource."""

    flops: float
    bytes_moved: float
    compute_s: float
    memory_s: float

    @property
    def time_s(self) -> float:
        return max(self.compute_s, self.memory_s)

    @property
    def intensity(self) -> float:
        return self.flops / self.bytes_moved if self.bytes_moved else math.inf

    @property
    def bound(self) -> str:
        return "compute" if self.compute_s >= self.memory_s else "memory"

    @property
    def achieved_tflops(self) -> float:
        return self.flops / self.time_s / 1e12 if self.time_s else 0.0


def arithmetic_intensity(flops: float, bytes_moved: float) -> float:
    if bytes_moved <= 0:
        raise ValueError("bytes_moved must be positive")
    return flops / bytes_moved


def gemm_cost(
    m: int,
    n: int,
    k: int,
    hardware: Hardware,
    *,
    input_bytes: int = 2,
    output_bytes: int = 2,
    reuse_a: bool = False,
    reuse_b: bool = False,
) -> Cost:
    """Cost of ``C[m,n] = A[m,k] @ B[k,n]``.

    ``reuse_a`` / ``reuse_b`` say that one operand is already resident (for
    example a weight matrix read once per decode step).  The default charges
    both operands and the output.  Real kernels add tiling traffic; this is a
    lower bound.
    """
    if min(m, n, k) <= 0:
        raise ValueError("matrix dimensions must be positive")
    flops = 2.0 * m * n * k
    moved = m * n * output_bytes
    if not reuse_a:
        moved += m * k * input_bytes
    if not reuse_b:
        moved += k * n * input_bytes
    return Cost(
        flops=flops,
        bytes_moved=float(moved),
        compute_s=flops / hardware.peak_flops,
        memory_s=moved / hardware.memory_bw,
    )


def linear_cost(
    batch: int,
    in_features: int,
    out_features: int,
    hardware: Hardware,
    *,
    weight_bytes: int = 1,
    activation_bytes: int = 2,
) -> Cost:
    """One token-parallel linear layer over a batch of rows.

    The weight matrix is read once and reused across the batch, which is the
    whole point of batching a decode step.  The output and activation traffic
    is charged per row.
    """
    if batch <= 0:
        raise ValueError("batch must be positive")
    flops = 2.0 * batch * in_features * out_features
    moved = in_features * out_features * weight_bytes
    moved += batch * (in_features + out_features) * activation_bytes
    return Cost(
        flops=flops,
        bytes_moved=float(moved),
        compute_s=flops / hardware.peak_flops,
        memory_s=moved / hardware.memory_bw,
    )


def decode_step_cost(
    active_params: int,
    hardware: Hardware,
    *,
    batch: int = 1,
    weight_bytes: int = 1,
    kv_bytes: int = 0,
    activation_bytes: int = 0,
) -> Cost:
    """A bandwidth-first lower bound for one decode step.

    Each active weight is read once per step and reused across the batch.
    ``kv_bytes`` is the total KV traffic for the batch (read by attention).
    ``activation_bytes`` counts other traffic you want to charge explicitly;
    it defaults to zero because the dominant term is the weight read.
    """
    if active_params <= 0 or batch <= 0:
        raise ValueError("active_params and batch must be positive")
    flops = 2.0 * active_params * batch
    moved = active_params * weight_bytes + kv_bytes + activation_bytes
    return Cost(
        flops=flops,
        bytes_moved=float(moved),
        compute_s=flops / hardware.peak_flops,
        memory_s=moved / hardware.memory_bw,
    )


def attention_cost(
    q_len: int,
    kv_len: int,
    n_heads: int,
    head_dim: int,
    hardware: Hardware,
    *,
    dtype_bytes: int = 2,
    causal: bool = True,
    flash: bool = True,
) -> Cost:
    """Prefill-style attention cost with and without score materialisation.

    ``flash=False`` models the naive path that writes the ``[H, Q, K]`` score
    matrix to HBM and reads it back for softmax.  ``flash=True`` keeps the
    scores in SRAM and only moves Q, K, V and the output, which is the traffic
    argument behind FlashAttention/FlashMLA.
    """
    if min(q_len, kv_len, n_heads, head_dim) <= 0:
        raise ValueError("attention dimensions must be positive")
    pair_count = q_len * kv_len
    if causal:
        # Roughly half of the rectangle is needed; diagonal handling is noise
        # at the scale this model is used for.
        pair_count //= 2
    flops = 2.0 * 2.0 * pair_count * n_heads * head_dim
    qkv_bytes = (q_len + 2 * kv_len + q_len) * n_heads * head_dim * dtype_bytes
    score_bytes = 2 * pair_count * n_heads * dtype_bytes if not flash else 0
    moved = qkv_bytes + score_bytes
    return Cost(
        flops=flops,
        bytes_moved=float(moved),
        compute_s=flops / hardware.peak_flops,
        memory_s=moved / hardware.memory_bw,
    )


def kv_bytes_per_token(
    num_layers: int,
    n_kv_heads: int,
    head_dim: int,
    bits: int,
    *,
    compression_ratio: int = 1,
    scale_bytes: int = 0,
    scale_group: int = 32,
) -> float:
    """KV bytes per token including per-group scales.

    This mirrors :func:`mini_deepinfer.quantization.kv_bytes_per_token`; it is
    repeated here so the performance model has no dependency on the
    quantisation lab and can be read on its own.
    """
    if num_layers <= 0 or n_kv_heads <= 0 or head_dim <= 0:
        raise ValueError("layer and head dimensions must be positive")
    if bits not in (4, 8, 16, 32):
        raise ValueError("bits must be one of 4, 8, 16, 32")
    if compression_ratio < 1 or scale_group < 1:
        raise ValueError("compression_ratio and scale_group must be >= 1")
    values = 2.0 * num_layers * n_kv_heads * head_dim / compression_ratio
    data = values * bits / 8.0
    scales = (values / scale_group) * scale_bytes
    return data + scales


def expected_accept_length(confidences: list[float] | tuple[float, ...]) -> float:
    """``E = 1 + c1 + c1*c2 + ...`` tokens advanced per target step.

    This includes the target model's own token, so it is one larger than
    ``speculative.expected_accepted_length`` for the same confidences.
    """
    total = 1.0
    product = 1.0
    for confidence in confidences:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidences must be in [0, 1]")
        product *= confidence
        total += product
    return total


def speculative_speedup(
    confidences: list[float] | tuple[float, ...],
    *,
    draft_relative_cost: float,
) -> float:
    """Expected tokens per unit cost relative to plain target decoding.

    A step verifies ``k`` drafts at cost ``1 + k * r`` and returns
    ``1 + c1 + c1*c2 + ...`` tokens on average.
    """
    if draft_relative_cost < 0:
        raise ValueError("draft_relative_cost must be non-negative")
    k = len(confidences)
    if k == 0:
        return 1.0
    return expected_accept_length(confidences) / (1.0 + k * draft_relative_cost)


def cost_per_million_tokens(
    gpu_hour_cost: float,
    output_tokens_per_s: float,
    *,
    utilization: float = 1.0,
) -> float:
    """Fully-loaded cost of one million output tokens (USD)."""
    if gpu_hour_cost < 0 or output_tokens_per_s <= 0:
        raise ValueError("cost must be non-negative and throughput positive")
    if not 0.0 < utilization <= 1.0:
        raise ValueError("utilization must be in (0, 1]")
    effective_tokens = output_tokens_per_s * utilization
    return gpu_hour_cost * 1e6 / (effective_tokens * 3600.0)


def ttl_storage_bytes(
    tokens_per_second: float,
    ttl_seconds: float,
    bytes_per_token: float,
) -> float:
    """Steady-state KV storage for a cache with the given hit population."""
    if min(tokens_per_second, ttl_seconds, bytes_per_token) < 0:
        raise ValueError("inputs must be non-negative")
    return tokens_per_second * ttl_seconds * bytes_per_token
