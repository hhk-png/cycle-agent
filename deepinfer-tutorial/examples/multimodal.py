"""Image token budgeting, embedding caching and vision batching."""

from __future__ import annotations

import numpy as np

from _common import add_project_root

add_project_root()

from mini_deepinfer.multimodal import (
    ImageEmbeddingCache,
    ImageTokenPlan,
    estimate_ve_gpus,
    pack_vision_batches,
    plan_epd_split,
)
from mini_deepinfer.vision import TinyViT, TinyViTConfig


def main() -> None:
    plan = ImageTokenPlan(
        height=1024,
        width=1024,
        patch_size=16,
        unshuffle=3,
        pool=2,
        bytes_per_token=890,
    )
    print("1024x1024 image:")
    print("  raw patches        :", plan.raw_patches)
    print("  after unshuffle    :", plan.unshuffled_tokens)
    print("  LLM tokens         :", plan.llm_tokens)
    print("  KV bytes           :", plan.kv_bytes)
    print("  KV MiB             :", round(plan.kv_bytes / 1024**2, 3))

    encoder = TinyViT(TinyViTConfig(image_size=16, patch_size=4, out_dim=64))
    cache = ImageEmbeddingCache(capacity=2, ttl_s=60.0)
    rng = np.random.default_rng(0)
    image_a = rng.normal(size=(16, 16, 3)).astype(np.float32)
    image_b = rng.normal(size=(16, 16, 3)).astype(np.float32)
    embedding_a = encoder.encode(image_a)
    cache.put(image_a, embedding_a)
    cached = cache.get(image_a)
    print("embedding shape      :", embedding_a.shape)
    print("cache hit            :", cached is not None)
    print("cache hit rate       :", cache.hit_rate)
    print("different image key  :", cache.content_key(image_a) != cache.content_key(image_b))

    counts = [plan.llm_tokens, 100, 121, 480, 484, 960]
    packed = pack_vision_batches(counts, max_batch_tokens=1024, bucket_width=64)
    print("vision batches       :", packed.batches)
    print("padding waste        :", round(packed.waste_ratio, 3))

    print("VE GPUs for 10 img/s  :", round(estimate_ve_gpus(
        10.0, plan.llm_tokens, tokens_per_second_per_gpu=10_000.0
    ), 3))
    split = plan_epd_split(
        request_qps=5.0,
        images_per_request=1.0,
        tokens_per_image=plan.llm_tokens,
        ve_tokens_per_second_per_gpu=10_000.0,
        prefill_tokens_per_second_per_gpu=60_000.0,
        non_cached_tokens_per_request=4096.0,
        decode_tokens_per_second_per_gpu=14_000.0,
        output_tokens_per_request=512.0,
    )
    print("EPD split            :")
    for key, value in split.items():
        print(f"  {key}: {value:.3f}")


if __name__ == "__main__":
    main()
