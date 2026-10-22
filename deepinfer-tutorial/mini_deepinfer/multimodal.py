"""Multimodal serving arithmetic: pixels -> patches -> LLM tokens.

This module does not implement DeepSeek-ViT.  It implements the parts of a
multimodal serving path that are easy to get wrong and easy to measure:

* how many LLM tokens an image costs after pixel-unshuffle and pooling;
* how much KV those tokens occupy;
* how to key an image-embedding cache;
* how to pack variable-resolution images into vision batches without wasting
  most of the batch on padding;
* how many vision-encoder GPUs a request rate needs.

The tiny ViT in :mod:`mini_deepinfer.vision` provides the actual encoder used by
the accompanying example; this module provides the capacity model around it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


@dataclass(frozen=True)
class ImageTokenPlan:
    """Static cost of one image at a given preprocessing configuration."""

    height: int
    width: int
    patch_size: int = 16
    unshuffle: int = 3
    pool: int = 1
    bytes_per_token: int = 890
    d_model: int = 5120
    n_layers: int = 32

    def __post_init__(self) -> None:
        for name in ("height", "width", "patch_size", "unshuffle", "pool"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")

    @property
    def raw_patches(self) -> int:
        return _ceil_div(self.height, self.patch_size) * _ceil_div(self.width, self.patch_size)

    @property
    def unshuffled_tokens(self) -> int:
        step = self.patch_size * self.unshuffle
        return _ceil_div(self.height, step) * _ceil_div(self.width, step)

    @property
    def llm_tokens(self) -> int:
        return _ceil_div(self.unshuffled_tokens, self.pool * self.pool) if self.pool > 1 else self.unshuffled_tokens

    @property
    def kv_bytes(self) -> int:
        return self.llm_tokens * self.bytes_per_token

    @property
    def ve_flops(self) -> float:
        """A deliberately rough encoder estimate.

        Attention is quadratic in the token count and the MLP is linear in
        ``d_model``.  The point is the shape of the curve, not a benchmark.
        """
        tokens = float(self.unshuffled_tokens)
        per_layer = 4.0 * tokens * tokens * self.d_model + 4.0 * tokens * self.d_model * self.d_model
        return 2.0 * self.n_layers * per_layer

    def as_dict(self) -> dict[str, float | int]:
        return {
            "height": self.height,
            "width": self.width,
            "raw_patches": self.raw_patches,
            "unshuffled_tokens": self.unshuffled_tokens,
            "llm_tokens": self.llm_tokens,
            "kv_bytes": self.kv_bytes,
            "ve_flops": self.ve_flops,
        }


@dataclass
class CacheEntry:
    embedding: np.ndarray
    expires_at: float
    last_used: float
    size_bytes: int


@dataclass
class ImageEmbeddingCache:
    """Content-addressed cache for vision embeddings.

    The key covers the image bytes, the preprocessing configuration and the
    encoder identity.  Two images that differ only in one pixel therefore
    miss, which is the correct default for exact reuse.  Perceptual or
    downscaled keys are a product decision, not an implementation detail.
    """

    capacity: int = 64
    ttl_s: float = 600.0
    clock: Any = None
    entries: dict[str, CacheEntry] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    evictions: int = 0

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity must be positive")
        if self.ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if self.clock is None:
            import time

            self.clock = time.monotonic

    @staticmethod
    def content_key(image: np.ndarray, *, config: str = "", encoder: str = "tiny-vit") -> str:
        payload = np.ascontiguousarray(np.asarray(image, dtype=np.float32)).tobytes()
        digest = hashlib.sha256(payload)
        digest.update(b"|")
        digest.update(encoder.encode("utf-8"))
        digest.update(b"|")
        digest.update(config.encode("utf-8"))
        return digest.hexdigest()

    def _purge_expired(self) -> None:
        now = self.clock()
        expired = [key for key, entry in self.entries.items() if entry.expires_at <= now]
        for key in expired:
            del self.entries[key]

    def _evict_one(self) -> None:
        if not self.entries:
            return
        victim = min(self.entries, key=lambda key: self.entries[key].last_used)
        del self.entries[victim]
        self.evictions += 1

    def put(
        self,
        image: np.ndarray,
        embedding: np.ndarray,
        *,
        config: str = "",
        encoder: str = "tiny-vit",
    ) -> str:
        self._purge_expired()
        key = self.content_key(image, config=config, encoder=encoder)
        while len(self.entries) >= self.capacity:
            self._evict_one()
        now = self.clock()
        copy = np.array(embedding, copy=True)
        self.entries[key] = CacheEntry(
            embedding=copy,
            expires_at=now + self.ttl_s,
            last_used=now,
            size_bytes=int(copy.nbytes),
        )
        return key

    def get(
        self,
        image: np.ndarray,
        *,
        config: str = "",
        encoder: str = "tiny-vit",
    ) -> np.ndarray | None:
        key = self.content_key(image, config=config, encoder=encoder)
        entry = self.entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        now = self.clock()
        if entry.expires_at <= now:
            del self.entries[key]
            self.misses += 1
            return None
        entry.last_used = now
        self.hits += 1
        return entry.embedding

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict[str, float | int]:
        self._purge_expired()
        return {
            "entries": len(self.entries),
            "capacity": self.capacity,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": self.hit_rate,
            "bytes": sum(entry.size_bytes for entry in self.entries.values()),
        }


@dataclass
class VisionBatchPlan:
    batches: list[list[int]]
    real_tokens: int
    padded_tokens: int

    @property
    def waste_ratio(self) -> float:
        return 0.0 if self.padded_tokens == 0 else 1.0 - self.real_tokens / self.padded_tokens


def pack_vision_batches(
    token_counts: list[int],
    *,
    max_batch_tokens: int,
    bucket_width: int = 64,
) -> VisionBatchPlan:
    """Pack images into vision batches, grouping similar sizes first.

    A vision encoder pads every image in a batch to the largest spatial shape,
    so mixing a 4K screenshot with a thumbnail wastes almost the whole batch.
    Sorting by a coarse bucket and then by size keeps the padding bounded.
    """
    if max_batch_tokens < 1:
        raise ValueError("max_batch_tokens must be positive")
    if bucket_width < 1:
        raise ValueError("bucket_width must be positive")
    if any(count < 1 for count in token_counts):
        raise ValueError("token counts must be positive")
    order = sorted(
        range(len(token_counts)),
        key=lambda index: (token_counts[index] // bucket_width, token_counts[index]),
    )
    batches: list[list[int]] = []
    current: list[int] = []
    current_max = 0
    for index in order:
        count = token_counts[index]
        new_max = max(current_max, count)
        if current and new_max * (len(current) + 1) > max_batch_tokens:
            batches.append(current)
            current = [index]
            current_max = count
        else:
            current.append(index)
            current_max = new_max
    if current:
        batches.append(current)
    real = sum(token_counts)
    padded = sum(max(token_counts[index] for index in batch) * len(batch) for batch in batches)
    return VisionBatchPlan(batches=batches, real_tokens=real, padded_tokens=padded)


def estimate_ve_gpus(
    images_per_second: float,
    tokens_per_image: float,
    *,
    tokens_per_second_per_gpu: float,
    headroom: float = 1.3,
) -> float:
    """Capacity estimate for the vision-encoder pool."""
    if images_per_second < 0:
        raise ValueError("images_per_second must be non-negative")
    if tokens_per_second_per_gpu <= 0:
        raise ValueError("tokens_per_second_per_gpu must be positive")
    if headroom < 1.0:
        raise ValueError("headroom must be at least 1")
    required = images_per_second * tokens_per_image * headroom
    return required / tokens_per_second_per_gpu


def plan_epd_split(
    *,
    request_qps: float,
    images_per_request: float,
    tokens_per_image: float,
    ve_tokens_per_second_per_gpu: float,
    prefill_tokens_per_second_per_gpu: float,
    non_cached_tokens_per_request: float,
    decode_tokens_per_second_per_gpu: float,
    output_tokens_per_request: float,
    headroom: float = 1.3,
) -> dict[str, float]:
    """Estimate the relative size of the E, P and D pools.

    The result is expressed in GPU count and in ratios.  It is a first-order
    capacity model: it assumes the three pools are independent and that the
    request rate is steady.
    """
    if request_qps < 0:
        raise ValueError("request_qps must be non-negative")
    ve = estimate_ve_gpus(
        request_qps * images_per_request,
        tokens_per_image,
        tokens_per_second_per_gpu=ve_tokens_per_second_per_gpu,
        headroom=headroom,
    )
    prefill = request_qps * non_cached_tokens_per_request * headroom / prefill_tokens_per_second_per_gpu
    decode = request_qps * output_tokens_per_request * headroom / decode_tokens_per_second_per_gpu
    smallest = min(value for value in (ve, prefill, decode) if value > 0) if any(
        value > 0 for value in (ve, prefill, decode)
    ) else 1.0
    return {
        "vision_gpus": ve,
        "prefill_gpus": prefill,
        "decode_gpus": decode,
        "e_to_p": ve / prefill if prefill else 0.0,
        "p_to_d": prefill / decode if decode else 0.0,
        "normalised_vision": ve / smallest,
        "normalised_prefill": prefill / smallest,
        "normalised_decode": decode / smallest,
    }
