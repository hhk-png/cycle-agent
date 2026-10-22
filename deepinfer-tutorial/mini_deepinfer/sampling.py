"""Sampling utilities with per-sequence deterministic RNGs."""

from __future__ import annotations

import numpy as np

from .model.ops import log_softmax, softmax
from .sampling_params import SamplingParams


def apply_top_k_top_p(probs: np.ndarray, top_k: int, top_p: float) -> np.ndarray:
    """Return a renormalized distribution after top-k/top-p truncation."""
    probs = probs.astype(np.float64, copy=True)
    if top_k > 0 and top_k < probs.size:
        cutoff = np.partition(probs, -top_k)[-top_k]
        probs[probs < cutoff] = 0.0
    if top_p < 1.0:
        order = np.argsort(-probs)
        sorted_probs = probs[order]
        cumulative = np.cumsum(sorted_probs)
        # Keep the first token that crosses the threshold; always keep one.
        keep = cumulative - sorted_probs < top_p
        if not np.any(keep):
            keep[0] = True
        kept = np.zeros_like(probs, dtype=bool)
        kept[order[keep]] = True
        probs[~kept] = 0.0
    total = probs.sum()
    if total <= 0:
        # Degenerate distribution (for example an over-constrained grammar).
        probs[:] = 0.0
        probs[0] = 1.0
        return probs
    return probs / total


def distribution(
    logits: np.ndarray,
    params: SamplingParams,
    mask: np.ndarray | None = None,
) -> np.ndarray:
    """Convert logits to a sampling distribution."""
    logits = np.asarray(logits, dtype=np.float64)
    if mask is not None:
        logits = np.where(mask, logits, -np.inf)
    if not np.any(np.isfinite(logits)):
        raise ValueError("all tokens are masked; the grammar cannot be satisfied")
    if params.greedy:
        probs = np.zeros_like(logits)
        probs[int(np.argmax(logits))] = 1.0
        return probs
    scaled = logits / max(params.temperature, 1e-5)
    probs = softmax(scaled, axis=-1)
    return apply_top_k_top_p(probs, params.top_k, params.top_p)


def sample_token(
    logits: np.ndarray,
    params: SamplingParams,
    rng: np.random.Generator,
    mask: np.ndarray | None = None,
) -> tuple[int, float, list[tuple[int, float]]]:
    """Sample one token, returning ``(id, logprob, top_logprobs)``."""
    probs = distribution(logits, params, mask)
    if params.greedy:
        token = int(np.argmax(probs))
    else:
        token = int(rng.choice(probs.size, p=probs))
    logprob = float(np.log(max(probs[token], 1e-300)))
    top: list[tuple[int, float]] = []
    if params.top_logprobs > 0:
        k = min(params.top_logprobs, probs.size)
        order = np.argsort(-probs)[:k]
        top = [(int(i), float(np.log(max(probs[i], 1e-300)))) for i in order]
    return token, logprob, top


def target_probs(
    logits: np.ndarray,
    params: SamplingParams,
    mask: np.ndarray | None = None,
) -> np.ndarray:
    """Distribution used by speculative acceptance.

    The same top-k/top-p (and grammar mask) must be applied to the draft and
    the target, otherwise ``q`` and ``p`` have different support and the
    accept/reject rule no longer preserves the target distribution.  This
    helper therefore applies the full shaping pipeline, not just temperature.
    """
    logits = np.asarray(logits, dtype=np.float64)
    if mask is not None:
        logits = np.where(mask, logits, -np.inf)
    if params.greedy:
        probs = np.zeros_like(logits)
        probs[int(np.argmax(logits))] = 1.0
        return probs
    probs = softmax(logits / max(params.temperature, 1e-5), axis=-1)
    return apply_top_k_top_p(probs, params.top_k, params.top_p)


def logprob_of(logits: np.ndarray, token: int) -> float:
    return float(log_softmax(np.asarray(logits, dtype=np.float64))[token])
