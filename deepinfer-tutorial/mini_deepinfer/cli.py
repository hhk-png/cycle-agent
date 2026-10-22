"""Command-line entry points: ``python -m mini_deepinfer.cli ...``."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .config import EngineConfig
from .llm import LLM
from .sampling_params import SamplingParams
from .server import EngineServer


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.lstrip("-").isdigit():
        return default
    return int(raw)


def _model_paths_from_env() -> tuple[str | None, str | None]:
    """Resolve an optional model directory, matching the container layout."""
    directory = os.environ.get("MINI_DEEPINFER_MODEL_DIR")
    if not directory:
        return None, None
    base = Path(directory)
    model = base / "tiny_lm.npz"
    tokenizer = base / "vocab.json"
    return (
        str(model) if model.exists() else None,
        str(tokenizer) if tokenizer.exists() else None,
    )


def make_llm(args) -> LLM:
    model_path, tokenizer_path = _model_paths_from_env()
    config = EngineConfig(
        block_size=args.block_size,
        num_blocks=args.num_blocks,
        max_num_seqs=args.max_num_seqs,
        max_num_batched_tokens=args.max_num_batched_tokens,
        max_model_len=args.max_model_len,
        model_path=model_path,
        tokenizer_path=tokenizer_path,
        enable_speculative=args.speculative,
        speculative_tokens=args.draft_tokens,
        confidence_threshold=args.confidence_threshold,
        enable_prefix_cache=not args.no_prefix_cache,
        enable_chunked_prefill=not args.no_chunked_prefill,
    )
    draft = None
    if args.speculative:
        draft = Path(__file__).resolve().parent / "assets" / "draft_lm.npz"
    return LLM(config=config, draft_model=draft)


def cmd_generate(args) -> None:
    llm = make_llm(args)
    params = SamplingParams(
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        max_tokens=args.max_tokens,
        seed=args.seed,
        so_args=json.loads(args.so_args) if args.so_args else None,
    )
    started = time.perf_counter()
    text = llm.generate(args.prompt, params)[0]
    elapsed = time.perf_counter() - started
    print(text)
    print(f"\n[{elapsed * 1000:.1f} ms]")
    if args.stats:
        print(json.dumps(llm.engine.stats(), indent=2, ensure_ascii=False))


def cmd_chat(args) -> None:
    llm = make_llm(args)
    messages = [{"role": "user", "content": args.message}]
    params = SamplingParams(temperature=args.temperature, max_tokens=args.max_tokens, seed=args.seed)
    print(llm.generate_chat(messages, params)[0])


def cmd_serve(args) -> None:
    llm = make_llm(args)
    app = EngineServer(llm, model_name=args.model_name)
    server = app.start(args.host, args.port)
    host, port = server.server_address
    print(f"serving on http://{host}:{port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        app.stop()


def cmd_bench(args) -> None:
    llm = make_llm(args)
    prompts = [
        "DeepInfer is an inference engine. " * args.prompt_repeat
        for _ in range(args.batch_size)
    ]
    params = SamplingParams(temperature=0.0, max_tokens=args.max_tokens)
    started = time.perf_counter()
    outputs = llm.generate(prompts, params)
    elapsed = time.perf_counter() - started
    tokens = sum(len(llm.engine.tokenizer.encode(text)) for text in outputs)
    summary = llm.engine.metrics.summary()
    result = {
        "batch_size": args.batch_size,
        "elapsed_s": elapsed,
        "output_tokens": tokens,
        "output_tokens_per_s": tokens / elapsed if elapsed else 0.0,
        "ttft_mean_ms": summary["ttft_mean_ms"],
        "tpot_mean_ms": summary["tpot_mean_ms"],
        "steps": summary["steps"],
    }
    print(json.dumps(result, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mini-deepinfer CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--num-blocks", type=int, default=_env_int("MINI_DEEPINFER_NUM_BLOCKS", 256))
        p.add_argument("--block-size", type=int, default=_env_int("MINI_DEEPINFER_BLOCK_SIZE", 16))
        p.add_argument(
            "--max-num-seqs",
            type=int,
            default=_env_int("MINI_DEEPINFER_MAX_NUM_SEQS", 8),
        )
        p.add_argument(
            "--max-num-batched-tokens",
            type=int,
            default=_env_int("MINI_DEEPINFER_MAX_NUM_BATCHED_TOKENS", 512),
        )
        p.add_argument(
            "--max-model-len",
            type=int,
            default=_env_int("MINI_DEEPINFER_MAX_MODEL_LEN", 256),
        )
        p.add_argument("--speculative", action="store_true")
        p.add_argument("--draft-tokens", type=int, default=4)
        p.add_argument("--confidence-threshold", type=float, default=0.55)
        p.add_argument("--no-prefix-cache", action="store_true")
        p.add_argument("--no-chunked-prefill", action="store_true")

    gen = sub.add_parser("generate")
    common(gen)
    gen.add_argument("prompt")
    gen.add_argument("--max-tokens", type=int, default=32)
    gen.add_argument("--temperature", type=float, default=0.8)
    gen.add_argument("--top-p", type=float, default=1.0)
    gen.add_argument("--top-k", type=int, default=-1)
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--so-args", type=str, default=None)
    gen.add_argument("--stats", action="store_true")
    gen.set_defaults(func=cmd_generate)

    chat = sub.add_parser("chat")
    common(chat)
    chat.add_argument("message")
    chat.add_argument("--max-tokens", type=int, default=32)
    chat.add_argument("--temperature", type=float, default=0.8)
    chat.add_argument("--seed", type=int, default=0)
    chat.set_defaults(func=cmd_chat)

    serve = sub.add_parser("serve")
    common(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--model-name", default="mini-deepinfer")
    serve.set_defaults(func=cmd_serve)

    bench = sub.add_parser("bench")
    common(bench)
    bench.add_argument("--batch-size", type=int, default=8)
    bench.add_argument("--max-tokens", type=int, default=32)
    bench.add_argument("--prompt-repeat", type=int, default=1)
    bench.add_argument("--output", default=None)
    bench.set_defaults(func=cmd_bench)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
