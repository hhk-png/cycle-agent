"""Numerical primitives shared by training and inference.

Everything is float32 NumPy.  The code favours clarity over speed; the
production kernels (FlashMLA, DeepGEMM, TileLang) are discussed in the
tutorial and are not reimplemented here.
"""

from __future__ import annotations

import numpy as np


def rms_norm(x: np.ndarray, weight: np.ndarray, eps: float) -> tuple[np.ndarray, np.ndarray]:
    """RMSNorm over the last axis.

    Returns the normalized tensor and ``1/rms`` so the backward pass can reuse
    it.
    """
    rms = np.sqrt(np.mean(np.square(x), axis=-1, keepdims=True) + eps)
    return (x / rms) * weight, 1.0 / rms


def softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = x - np.max(x, axis=axis, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.sum(exp, axis=axis, keepdims=True)


def silu(x: np.ndarray) -> np.ndarray:
    return x * (1.0 / (1.0 + np.exp(-x)))


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def rope_tables(
    max_seq_len: int, head_dim: int, theta: float
) -> tuple[np.ndarray, np.ndarray]:
    """Cosine/sine tables for interleaved RoPE, shape ``[S, head_dim/2]``."""
    if head_dim % 2:
        raise ValueError("RoPE requires an even head_dim")
    inv_freq = theta ** (-np.arange(0, head_dim, 2, dtype=np.float32) / head_dim)
    positions = np.arange(max_seq_len, dtype=np.float32)[:, None]
    angles = positions * inv_freq[None, :]
    return np.cos(angles).astype(np.float32), np.sin(angles).astype(np.float32)


def apply_rope(
    x: np.ndarray,
    positions: np.ndarray,
    cos: np.ndarray,
    sin: np.ndarray,
) -> np.ndarray:
    """Rotate adjacent pairs of the last dimension.

    ``x`` is ``[T, H, D]``; ``positions`` is ``[T]``.
    """
    c = cos[positions][:, None, :]
    s = sin[positions][:, None, :]
    even = x[..., 0::2]
    odd = x[..., 1::2]
    out = np.empty_like(x)
    out[..., 0::2] = even * c - odd * s
    out[..., 1::2] = even * s + odd * c
    return out


def apply_rope_backward(
    grad: np.ndarray,
    positions: np.ndarray,
    cos: np.ndarray,
    sin: np.ndarray,
) -> np.ndarray:
    """Inverse rotation (transpose of :func:`apply_rope`)."""
    c = cos[positions][:, None, :]
    s = sin[positions][:, None, :]
    even = grad[..., 0::2]
    odd = grad[..., 1::2]
    out = np.empty_like(grad)
    out[..., 0::2] = even * c + odd * s
    out[..., 1::2] = -even * s + odd * c
    return out


def causal_mask(
    query_positions: np.ndarray,
    key_length: int,
    window: int | None = None,
) -> np.ndarray:
    """Boolean ``[Q, T]`` mask; True means the key may be attended."""
    q = query_positions[:, None]
    k = np.arange(key_length)[None, :]
    mask = k <= q
    if window is not None and window > 0:
        mask &= k > (q - window)
    return mask


def attention(
    q: np.ndarray,
    k: np.ndarray,
    v: np.ndarray,
    mask: np.ndarray,
) -> np.ndarray:
    """Single-sequence multi-head attention.

    q: ``[Q, H, D]``; k/v: ``[T, H, D]``; mask: ``[Q, T]`` boolean.
    Returns ``[Q, H, D]``.
    """
    scale = 1.0 / np.sqrt(q.shape[-1])
    scores = np.einsum("qhd,thd->hqt", q, k, optimize=True) * scale
    scores = np.where(mask[None, :, :], scores, -np.inf)
    probs = softmax(scores, axis=-1)
    return np.einsum("hqt,thd->qhd", probs, v, optimize=True)


def expand_kv(k: np.ndarray, v: np.ndarray, n_heads: int) -> tuple[np.ndarray, np.ndarray]:
    """Repeat GQA K/V heads to the query head count."""
    n_kv_heads = k.shape[1]
    if n_heads == n_kv_heads:
        return k, v
    if n_heads % n_kv_heads:
        raise ValueError("n_heads must be divisible by n_kv_heads")
    repeat = n_heads // n_kv_heads
    return (
        np.repeat(k, repeat, axis=1),
        np.repeat(v, repeat, axis=1),
    )


def log_softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = x - np.max(x, axis=axis, keepdims=True)
    return shifted - np.log(np.sum(np.exp(shifted), axis=axis, keepdims=True))
