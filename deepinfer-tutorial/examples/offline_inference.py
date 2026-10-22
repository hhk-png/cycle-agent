"""Offline batched generation, the mini analogue of examples/offline_inference.py."""

from __future__ import annotations

import argparse

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig, LLM, SamplingParams


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=24)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    llm = LLM(
        config=EngineConfig(
            num_blocks=256,
            max_num_seqs=8,
            max_num_batched_tokens=512,
            max_model_len=256,
        )
    )
    prompts = [
        "DeepInfer is",
        "Once upon a time",
        "The cat sat on the mat.",
        "A small robot lived at the edge of a village.",
    ]
    params = [
        SamplingParams(temperature=args.temperature, max_tokens=args.max_tokens, seed=args.seed + i)
        for i in range(len(prompts))
    ]
    outputs = llm.generate(prompts, params)
    for prompt, output in zip(prompts, outputs):
        print(f"PROMPT: {prompt}")
        print(f"OUTPUT: {output}")
        print()

    stats = llm.engine.stats()
    print("metrics:")
    for key, value in stats["metrics"].items():
        print(f"  {key}: {value}")
    print("scheduler:", stats["scheduler"])
    print("block manager:", stats["block_manager"])


if __name__ == "__main__":
    main()
