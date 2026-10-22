from __future__ import annotations

import numpy as np

from mini_deepinfer.multimodal import (
    ImageEmbeddingCache,
    ImageTokenPlan,
    estimate_ve_gpus,
    pack_vision_batches,
    plan_epd_split,
)


def test_image_token_plan_accounts_for_unshuffle_and_pooling() -> None:
    plan = ImageTokenPlan(
        height=1024,
        width=1024,
        patch_size=16,
        unshuffle=3,
        pool=2,
        bytes_per_token=890,
    )
    assert plan.raw_patches == 4096
    assert plan.unshuffled_tokens == 484
    assert plan.llm_tokens == 121
    assert plan.kv_bytes == 121 * 890
    assert plan.ve_flops > 0
    assert plan.as_dict()["llm_tokens"] == 121


def test_image_plan_scales_with_resolution() -> None:
    small = ImageTokenPlan(height=256, width=256, pool=2)
    large = ImageTokenPlan(height=1024, width=1024, pool=2)
    assert large.llm_tokens > small.llm_tokens
    assert large.kv_bytes == large.llm_tokens * large.bytes_per_token


def test_image_cache_hit_ttl_and_lru() -> None:
    now = [0.0]
    cache = ImageEmbeddingCache(capacity=2, ttl_s=10.0, clock=lambda: now[0])
    image_a = np.zeros((4, 4, 3), dtype=np.float32)
    image_b = np.ones((4, 4, 3), dtype=np.float32)
    image_c = np.full((4, 4, 3), 2.0, dtype=np.float32)
    cache.put(image_a, np.ones(8))
    assert cache.get(image_a) is not None
    assert cache.get(image_b) is None
    assert cache.hits == 1 and cache.misses == 1

    cache.put(image_b, np.ones(8))
    cache.put(image_c, np.ones(8))  # evicts the least recently used entry
    assert len(cache.entries) == 2
    assert cache.evictions == 1

    now[0] = 100.0
    assert cache.get(image_c) is None
    assert cache.stats()["entries"] == 0


def test_image_cache_key_covers_preprocessing_config() -> None:
    image = np.zeros((2, 2, 3), dtype=np.float32)
    key_a = ImageEmbeddingCache.content_key(image, config="pool=1")
    key_b = ImageEmbeddingCache.content_key(image, config="pool=2")
    assert key_a != key_b


def test_vision_batch_packing_respects_budget() -> None:
    counts = [96, 100, 480, 500, 960, 1000]
    plan = pack_vision_batches(counts, max_batch_tokens=1200, bucket_width=64)
    seen = sorted(index for batch in plan.batches for index in batch)
    assert seen == list(range(len(counts)))
    for batch in plan.batches:
        padded = max(counts[index] for index in batch) * len(batch)
        assert padded <= 1200
    assert plan.real_tokens == sum(counts)
    assert 0.0 <= plan.waste_ratio < 1.0


def test_vision_capacity_and_epd_split() -> None:
    gpus = estimate_ve_gpus(
        10.0,
        100.0,
        tokens_per_second_per_gpu=10_000.0,
        headroom=1.3,
    )
    assert abs(gpus - 0.13) < 1e-9
    split = plan_epd_split(
        request_qps=5.0,
        images_per_request=1.0,
        tokens_per_image=121.0,
        ve_tokens_per_second_per_gpu=10_000.0,
        prefill_tokens_per_second_per_gpu=60_000.0,
        non_cached_tokens_per_request=4096.0,
        decode_tokens_per_second_per_gpu=14_000.0,
        output_tokens_per_request=512.0,
        headroom=1.3,
    )
    assert split["vision_gpus"] > 0
    assert split["prefill_gpus"] > 0
    assert split["decode_gpus"] > 0
    assert split["normalised_vision"] >= 1.0
