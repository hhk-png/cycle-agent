"""A small throughput/latency sweep.

The numbers are CPU teaching numbers, not GPU performance claims.  The useful
part is the shape of the experiment and the fact that every number is measured
by the same engine that serves the examples.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig, LLM, SamplingParams


def _latency_summary(outputs) -> tuple[float, float]:
    """TTFT and TPOT from the request objects, not from re-encoded text."""
    ttfts = [
        output.metrics.first_token_latency * 1000
        for output in outputs
        if output.metrics.first_token_latency is not None
    ]
    tpots = []
    for output in outputs:
        if output.output_tokens > 1 and output.metrics.e2e_latency is not None:
            first = output.metrics.first_token_latency or 0.0
            tpots.append(
                (output.metrics.e2e_latency - first)
                / (output.output_tokens - 1)
                * 1000
            )
    return (
        statistics.mean(ttfts) if ttfts else 0.0,
        statistics.mean(tpots) if tpots else 0.0,
    )


def run(
    batch_size: int,
    max_tokens: int,
    prompt_repeat: int,
    *,
    warmup: int = 1,
    repeat: int = 3,
) -> dict:
    llm = LLM(
        config=EngineConfig(
            num_blocks=512,
            max_num_seqs=max(8, batch_size),
            max_num_batched_tokens=1024,
            max_model_len=256,
            enable_prefix_cache=False,
        )
    )
    prompts = [
        ("DeepInfer is an inference engine. " * prompt_repeat).strip()
        for _ in range(batch_size)
    ]
    params = SamplingParams(temperature=0.0, max_tokens=max_tokens)
    for _ in range(max(0, warmup)):
        llm.generate(prompts, params, return_objects=True)

    elapsed_samples: list[float] = []
    outputs = []
    for _ in range(max(1, repeat)):
        started = time.perf_counter()
        outputs = llm.generate(prompts, params, return_objects=True)
        elapsed_samples.append(time.perf_counter() - started)

    elapsed = statistics.median(elapsed_samples)
    tokens = sum(output.output_tokens for output in outputs)
    ttft_mean, tpot_mean = _latency_summary(outputs)
    return {
        "batch_size": batch_size,
        "elapsed_s": round(elapsed, 4),
        "output_tokens": tokens,
        "output_tokens_per_s": round(tokens / elapsed, 1),
        "ttft_mean_ms": round(ttft_mean, 2),
        "tpot_mean_ms": round(tpot_mean, 2),
        "steps": llm.engine.metrics.steps // (max(0, warmup) + max(1, repeat)),
        "runs": len(elapsed_samples),
        "warmup": max(0, warmup),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--max-tokens", type=int, default=24)
    parser.add_argument("--prompt-repeat", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    results = []
    for batch_size in args.batch_sizes:
        result = run(
            batch_size,
            args.max_tokens,
            args.prompt_repeat,
            warmup=args.warmup,
            repeat=args.repeat,
        )
        results.append(result)
        print(json.dumps(result))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"saved {args.output}")


if __name__ == "__main__":
    main()
