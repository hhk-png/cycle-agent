"""Long-context and agent-style prefix reuse on the toy engine.

The script builds several sessions that share a long system/document prefix and
compares a run with prefix caching against one without it.  The absolute token
counts are small because the toy model is limited to 512 positions; the point is
the ratio, not the wall-clock time.
"""

from __future__ import annotations

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig, LLM, SamplingParams
from mini_deepinfer.economics import capacity_plan
from mini_deepinfer.roofline import kv_bytes_per_token, ttl_storage_bytes
from mini_deepinfer.yoco import plan_encoder_hit


SHARED_BYTES = 320
TAIL_BYTES = 32


def build_prompts(count: int) -> tuple[str, list[str]]:
    shared = ("The quick brown fox jumps over the lazy dog. " * 8)[:SHARED_BYTES]
    prompts = []
    for index in range(count):
        tail = f" session {index:02d} question ".ljust(TAIL_BYTES, "x")[:TAIL_BYTES]
        prompts.append(shared + tail)
    return shared, prompts


def run(cache: bool) -> dict:
    config = EngineConfig(
        block_size=16,
        num_blocks=256,
        max_num_seqs=8,
        max_num_batched_tokens=512,
        max_model_len=512,
        enable_prefix_cache=cache,
    )
    llm = LLM(config=config)
    try:
        _, prompts = build_prompts(4)
        # Run sessions one after another, as an agent would: each turn can
        # reuse the prefix registered by the previous turn.
        for prompt in prompts:
            llm.generate(prompt, SamplingParams(max_tokens=8, temperature=0.0))
        return llm.engine.stats()
    finally:
        llm.close()


def main() -> None:
    shared, prompts = build_prompts(4)
    print(f"shared prefix: {len(shared)} bytes; each prompt: {len(prompts[0])} bytes")
    print(f"sessions: {len(prompts)}")

    cached = run(True)
    uncached = run(False)
    print("\nwith prefix cache")
    print(f"  prefill tokens: {cached['metrics']['prefill_tokens']}")
    print(f"  cache hits:     {cached['block_manager']['cache_hits']}")
    print(f"  cached blocks:  {cached['block_manager']['cached_blocks']}")
    print("without prefix cache")
    print(f"  prefill tokens: {uncached['metrics']['prefill_tokens']}")
    print(f"  cache hits:     {uncached['block_manager']['cache_hits']}")

    saved = uncached["metrics"]["prefill_tokens"] - cached["metrics"]["prefill_tokens"]
    ratio = saved / max(1, uncached["metrics"]["prefill_tokens"])
    print(f"\nprefill tokens saved: {saved} ({ratio:.0%})")

    print("\nlong-context capacity at the published 890 B/token")
    per_token = 890.0
    for context in (77_000, 131_072, 1_048_576):
        kv_mb = context * per_token / (1024**2)
        print(f"  {context:>9,} tokens -> {kv_mb:8.1f} MiB per request")

    plan = capacity_plan(
        qps=100.0,
        average_stay_s=30.0,
        average_context_tokens=77_000,
        kv_bytes_per_token=per_token,
        redundancy=0.25,
    )
    print("\nagent-style capacity plan (100 QPS, 30 s stay, 77K context)")
    print(f"  concurrency: {plan['concurrency']:.0f}")
    print(f"  KV:          {plan['kv_gib']:.1f} GiB ({plan['kv_bytes_with_redundancy'] / 1024**3:.1f} GiB with 25% redundancy)")

    ttl = ttl_storage_bytes(
        tokens_per_second=50_000,
        ttl_seconds=3 * 24 * 3600,
        bytes_per_token=per_token,
    )
    print(f"  3-day KV ingest at 50K tok/s: {ttl / 1e12:.2f} TB (before replicas/EC)")

    hit = plan_encoder_hit(block_hit_tokens=131_072, swa_cached_tokens=0)
    print("\nSWA bounded replay")
    print(f"  block hit: {hit.block_hit_tokens}, effective hit: {hit.effective_hit_tokens}, replay: {hit.replay_tokens}")

    print("\nWhat to notice")
    print("1. The shared prefix is charged once with caching and once per session without it.")
    print("2. A hit is not free: KV still has to be fetched and paged into HBM.")
    print("3. At 1M tokens the KV footprint dominates concurrency; length buckets matter.")
    print("4. SWA hit and block KV hit are different; a block hit may still need replay.")


if __name__ == "__main__":
    main()
