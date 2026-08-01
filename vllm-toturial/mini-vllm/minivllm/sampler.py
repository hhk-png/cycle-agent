"""Logits post-processing and token sampling.

The order of operations matches vLLM's ``Sampler``
(``vllm/model_executor/layers/sampler.py``): temperature -> top-k -> top-p
-> softmax -> sample.  Greedy decoding (temperature == 0) bypasses the
randomness entirely, which makes tests deterministic.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

from .config import SamplingParams


def _softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    x = x - np.max(x, axis=axis, keepdims=True)
    e = np.exp(x)
    return e / np.sum(e, axis=axis, keepdims=True)


def apply_temperature(logits: np.ndarray, temperature: float) -> np.ndarray:
    if temperature == 0:
        return logits
    return logits / temperature


def apply_top_k(logits: np.ndarray, top_k: int) -> np.ndarray:
    if top_k <= 0 or top_k >= logits.shape[-1]:
        return logits
    kth = np.partition(logits, -top_k, axis=-1)[..., -top_k:][..., 0:1]
    return np.where(logits < kth, -np.inf, logits)


def apply_top_p(logits: np.ndarray, top_p: float) -> np.ndarray:
    if top_p >= 1.0:
        return logits
    sorted_idx = np.argsort(-logits, axis=-1)
    sorted_logits = np.take_along_axis(logits, sorted_idx, axis=-1)
    probs = _softmax(sorted_logits)
    cum = np.cumsum(probs, axis=-1)
    # remove tokens whose cumulative probability exceeds the nucleus
    remove = (cum - probs) > top_p
    filtered = np.where(remove, -np.inf, sorted_logits)
    out = np.empty_like(logits)
    np.put_along_axis(out, sorted_idx, filtered, axis=-1)
    return out


def sample_token(logits: np.ndarray,
                 params: SamplingParams,
                 rng: np.random.Generator) -> Tuple[np.ndarray, np.ndarray]:
    """Sample one token per row.

    Args:
        logits: ``(B, vocab)`` raw logits for the next token.
        params: sampling parameters (shared across the batch here; a real
                engine allows per-request params).
        rng:    numpy generator for reproducible sampling.

    Returns:
        ``(token_ids, logprobs)`` each of shape ``(B,)``.  ``logprobs`` is the
        natural log-probability of the chosen token.
    """
    logits = np.asarray(logits, dtype=np.float64)
    vocab = logits.shape[-1]

    if params.temperature == 0:
        tokens = np.argmax(logits, axis=-1)
        # logprob of the greedy token
        p = _softmax(logits)
        logprobs = np.log(np.take_along_axis(p, tokens[:, None], axis=-1)[:, 0])
        return tokens, logprobs

    logits = apply_temperature(logits, params.temperature)
    logits = apply_top_k(logits, params.top_k)
    logits = apply_top_p(logits, params.top_p)

    probs = _softmax(logits)
    probs = probs / probs.sum(axis=-1, keepdims=True)  # re-normalize after filtering

    if rng is not None:
        flat = probs.reshape(-1, vocab)
        idx = rng.multinomial(1, flat).argmax(axis=-1).reshape(probs.shape[:-1])
    else:
        idx = np.argmax(probs, axis=-1)
    tokens = idx.astype(np.int64)
    logprobs = np.log(np.take_along_axis(probs, tokens[..., None], axis=-1)[..., 0])
    return tokens, logprobs
