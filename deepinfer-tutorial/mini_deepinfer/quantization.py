"""Low-precision mechanism labs: FP8/FP4 quantize-dequantize and byte math.

These are reference implementations, not optimized kernels.  They make the
scale layout and the error/byte trade-off visible without requiring a GPU.
"""

from __future__ import annotations

import numpy as np

# E2M1 positive magnitudes; the sign bit gives the full 16-value codebook.
E2M1_POSITIVE = np.array([0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0], dtype=np.float32)


def quantize_fp8(
    x: np.ndarray, *, per_channel: bool = True, eps: float = 1e-8
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Symmetric FP8-like quantize/dequantize using E4M3's range.

    The returned codes are int8 in [-127, 127]; the scale maps codes back to
    the original range.  This is a numerical lab, not IEEE E4M3 encoding.
    """
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        raise ValueError("cannot quantize an empty array")
    if per_channel and x.ndim > 1:
        axes = tuple(range(x.ndim - 1))
        amax = np.max(np.abs(x), axis=axes, keepdims=True)
    else:
        amax = np.max(np.abs(x), keepdims=True)
    scale = np.maximum(amax / 127.0, eps)
    codes = np.clip(np.round(x / scale), -127, 127).astype(np.int8)
    return codes, scale.astype(np.float32), (codes.astype(np.float32) * scale)


def quantize_fp4(
    x: np.ndarray, *, block_size: int = 16, eps: float = 1e-8
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MXFP4-style block quantization using the E2M1 codebook.

    Each block of ``block_size`` contiguous values shares one power-of-two
    scale.  Codes are 0..15: the low 3 bits are the magnitude index and the
    high bit is the sign.
    """
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        raise ValueError("cannot quantize an empty array")
    flat = x.reshape(-1)
    padded = (-flat.size) % block_size
    if padded:
        flat = np.concatenate([flat, np.zeros(padded, dtype=np.float32)])
    blocks = flat.reshape(-1, block_size)
    amax = np.max(np.abs(blocks), axis=1, keepdims=True)
    # Choose a power-of-two scale so the block maximum maps near 6.0.
    scale = np.power(2.0, np.ceil(np.log2(np.maximum(amax / 6.0, eps))))
    normalized = blocks / scale
    magnitude = np.abs(normalized)
    index = np.argmin(np.abs(magnitude[..., None] - E2M1_POSITIVE), axis=-1)
    sign = (normalized < 0).astype(np.uint8)
    codes = (sign << 3) | index.astype(np.uint8)
    decoded = E2M1_POSITIVE[index] * np.where(sign.astype(bool), -1.0, 1.0)
    dequant = (decoded * scale).reshape(-1)[: flat.size - padded]
    return codes.reshape(-1)[: flat.size - padded], scale.reshape(-1), dequant


def quantization_error(x: np.ndarray, dequant: np.ndarray) -> dict[str, float]:
    x = np.asarray(x, dtype=np.float32)
    error = np.abs(x - dequant)
    return {
        "max_abs": float(np.max(error)),
        "mean_abs": float(np.mean(error)),
        "relative_l2": float(np.linalg.norm(x - dequant) / max(np.linalg.norm(x), 1e-12)),
    }


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
    """Estimate KV bytes per token including per-group scales."""
    values = 2 * num_layers * n_kv_heads * head_dim / max(1, compression_ratio)
    data = values * bits / 8.0
    scales = (values / max(1, scale_group)) * scale_bytes
    return data + scales
