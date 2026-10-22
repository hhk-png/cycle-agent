"""Trace one request through the engine, one scheduler step at a time.

This is the executable companion to appendix G.  It prints only deterministic
fields (token counts, block ids, cache hits, finish reasons); wall-clock
latencies are left to ``benchmark.py`` because they vary between machines.
"""

from __future__ import annotations

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig
from mini_deepinfer.engine import LLMEngine
from mini_deepinfer.sampling_params import SamplingParams

PROMPT = "DeepInfer is an inference engine for large language models."


def main() -> None:
    config = EngineConfig(
        num_blocks=32,
        block_size=16,
        max_num_seqs=4,
        max_num_batched_tokens=128,
        max_model_len=128,
    )
    engine = LLMEngine(config)
    tokenizer = engine.tokenizer
    prompt_ids = tokenizer.encode(PROMPT)

    print(f"prompt: {PROMPT!r}")
    print(f"utf8 bytes={len(PROMPT.encode('utf-8'))} tokens={len(prompt_ids)}")
    print(f"block_size={config.block_size} num_blocks={config.num_blocks}")

    def run(tag: str) -> tuple[list[int], int]:
        seq = engine.add_request(
            prompt_ids,
            SamplingParams(temperature=0, max_tokens=4, seed=1),
            request_id=tag,
        )
        print()
        print(
            f"{tag}: after add_request status={seq.status.name} "
            f"blocks={list(seq.block_table)} prefix_hit={seq.prefix_cache_hit}"
        )
        print(f"{'step':>4} {'phase':<8} {'q_len':>5} {'len':>4} {'blocks':<12} "
              f"{'hit':>3} {'output':<16} finish")
        step = 0
        while not seq.is_finished():
            before = engine.metrics.summary()
            engine.step()
            after = engine.metrics.summary()
            step += 1
            q_prefill = after["prefill_tokens"] - before["prefill_tokens"]
            q_decode = after["decode_tokens"] - before["decode_tokens"]
            phase = "prefill" if q_prefill else "decode"
            q_len = q_prefill + q_decode
            tokens = ",".join(str(t) for t in seq.output_token_ids)
            print(
                f"{step:>4} {phase:<8} {q_len:>5} {seq.length:>4} "
                f"{str(list(seq.block_table)):<12} {seq.prefix_cache_hit:>3} "
                f"{tokens:<16} {seq.finish_reason or '-'}"
            )
        print(
            f"     final status={seq.status.name} finish_reason={seq.finish_reason} "
            f"output={seq.decoded_output(tokenizer)!r} blocks={list(seq.block_table)}"
        )
        return list(seq.output_token_ids), seq.prefix_cache_hit

    first_tokens, _ = run("request-1")
    second_tokens, second_hit = run("request-2")

    stats = engine.block_manager.stats()
    metrics = engine.metrics.summary()
    print()
    print(
        f"second request reused {second_hit} prompt tokens "
        f"({second_hit // config.block_size} full blocks); "
        f"the remaining {len(prompt_ids) - second_hit} tokens were recomputed."
    )
    print(f"identical greedy output: {first_tokens == second_tokens}")
    print(
        f"deterministic totals: steps={metrics['steps']} "
        f"prefill_tokens={metrics['prefill_tokens']} "
        f"decode_tokens={metrics['decode_tokens']} "
        f"output_tokens={metrics['output_tokens']}"
    )
    print(
        f"block manager: allocations={stats['allocations']} frees={stats['frees']} "
        f"cache_hits={stats['cache_hits']} cache_misses={stats['cache_misses']} "
        f"cached_blocks={stats['cached_blocks']} free_blocks={stats['free_blocks']}"
    )

    assert first_tokens == second_tokens
    assert second_hit == len(prompt_ids) // config.block_size * config.block_size
    assert stats["free_blocks"] == config.num_blocks


if __name__ == "__main__":
    main()
