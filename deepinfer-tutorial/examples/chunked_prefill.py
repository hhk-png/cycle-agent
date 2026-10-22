"""Compare full prefill, chunked prefill and a prefix-cache hit."""

from __future__ import annotations

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig, LLM, SamplingParams


PROMPT = "DeepInfer is an inference engine for large language models. " * 2


def build(**overrides) -> LLM:
    values = dict(
        num_blocks=128,
        block_size=8,
        max_num_seqs=4,
        max_num_batched_tokens=16,
        max_model_len=160,
        enable_chunked_prefill=True,
    )
    values.update(overrides)
    return LLM(config=EngineConfig(**values))


def main() -> None:
    params = SamplingParams(temperature=0.0, max_tokens=12)
    full = LLM(
        config=EngineConfig(
            num_blocks=128,
            block_size=8,
            max_num_seqs=4,
            max_num_batched_tokens=256,
            max_model_len=160,
            enable_chunked_prefill=False,
        )
    )
    full_text = full.generate(PROMPT, params)[0]
    print("full prefill:", full_text)

    chunked = build()
    first = chunked.generate(PROMPT, params, return_objects=True)[0]
    second = chunked.generate(PROMPT, params, return_objects=True)[0]
    print("chunked prefill:", first.text)
    print("prefix-cache hit tokens:", second.prefix_cache_hit)
    print("same text:", full_text == first.text == second.text)
    print("scheduler:", chunked.engine.scheduler.stats())
    print("block manager:", chunked.engine.block_manager.stats())


if __name__ == "__main__":
    main()
