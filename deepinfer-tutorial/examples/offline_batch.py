"""Offline batch planning, execution and throughput report."""

from __future__ import annotations

import argparse

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig, LLM
from mini_deepinfer.offline_batch import BatchItem, plan_waves, run_offline_batch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-tokens", type=int, default=8)
    parser.add_argument("--max-batch-size", type=int, default=4)
    parser.add_argument("--max-batched-tokens", type=int, default=256)
    args = parser.parse_args()

    prompts = [
        ("doc-0", "DeepInfer is", 0),
        ("doc-1", "Once upon a time", 0),
        ("doc-2", "The cat sat on the mat.", 1),
        ("doc-3", "A small robot lived at the edge of a village.", 2),
        ("doc-4", "Prefill and decode have different bottlenecks.", 0),
        ("doc-5", "KV cache turns recomputation into a read.", 1),
        ("doc-6", "Continuous batching keeps the GPU busy.", 3),
        ("doc-7", "Speculative decoding proposes and verifies.", 0),
    ]
    items = [
        BatchItem(
            request_id=request_id,
            prompt=prompt,
            max_tokens=args.max_tokens,
            temperature=0.0,
            seed=index,
            priority=priority,
            deadline_s=30.0,
        )
        for index, (request_id, prompt, priority) in enumerate(prompts)
    ]

    waves = plan_waves(
        items,
        max_batch_size=args.max_batch_size,
        max_batched_tokens=args.max_batched_tokens,
    )
    print("waves:")
    for index, wave in enumerate(waves):
        ids = [items[i].request_id for i in wave.indices]
        print(f"  wave {index}: {ids} estimated_tokens={wave.estimated_tokens}")

    llm = LLM(
        config=EngineConfig(
            num_blocks=128,
            block_size=16,
            max_num_seqs=args.max_batch_size,
            max_num_batched_tokens=args.max_batched_tokens,
            max_model_len=256,
        )
    )
    report = run_offline_batch(
        llm,
        items,
        max_batch_size=args.max_batch_size,
        max_batched_tokens=args.max_batched_tokens,
        waves=waves,
    )
    print("results:")
    for result in report.results:
        print(
            f"  {result.request_id}: output_tokens={result.output_tokens} "
            f"latency={result.latency_s * 1000:.1f}ms "
            f"finish={result.finish_reason} deadline_met={result.deadline_met}"
        )
    print("report:")
    for key, value in report.as_dict().items():
        if isinstance(value, float):
            print(f"  {key}: {value:.4f}")
        else:
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
