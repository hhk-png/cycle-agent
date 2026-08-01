"""Command-line entry points for mini-vLLM.

Mirrors the surface of the real ``vllm`` CLI::

    python -m minivllm serve   --model artifacts/tinygpt --host 0.0.0.0 --port 8000
    python -m minivllm chat    --model artifacts/tinygpt
    python -m minivllm demo    --model artifacts/tinygpt
    python -m minivllm spec    --model artifacts/tinygpt
    python -m minivllm quant   --model artifacts/tinygpt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import SamplingParams
from .checkpoint import load_checkpoint


def _load(args):
    model, cfg, tokenizer = load_checkpoint(args.model)
    if getattr(args, "quantize", None) == "int8":
        from .quantize import quantize_model
        model, stats = quantize_model(model)
        cfg.quantize = "int8"
        print("[cli] int8 quantization:", {k: round(v, 4) for k, v in stats.items()})
    return model, cfg, tokenizer


def cmd_serve(args) -> None:
    model, cfg, tokenizer = _load(args)
    from .api_server import create_app
    from .async_engine import AsyncLLMEngine
    import uvicorn

    engine = AsyncLLMEngine(cfg, tokenizer=tokenizer, model=model)
    app = create_app(engine, tokenizer)
    print(f"[cli] serving model '{args.model}' on http://{args.host}:{args.port}")
    print(f"[cli] OpenAI-compatible endpoints: /v1/completions, /v1/chat/completions")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    finally:
        engine.stop()


def cmd_chat(args) -> None:
    model, cfg, tokenizer = _load(args)
    from .engine import LLMEngine

    engine = LLMEngine(cfg, tokenizer=tokenizer, model=model)
    print("[cli] mini-vLLM chat. Type 'exit' or Ctrl-C to quit.\n")
    history: list = []
    while True:
        try:
            user = input("You> ")
        except (EOFError, KeyboardInterrupt):
            break
        if user.strip().lower() in ("exit", "quit"):
            break
        history.append(f"user: {user}")
        prompt = "\n".join(history) + "\nassistant:"
        out = engine.generate(prompt, SamplingParams(
            temperature=args.temperature, max_tokens=args.max_tokens))
        reply = out.outputs[0].text
        print(f"Bot> {reply}\n")
        history.append(f"assistant: {reply}")


def cmd_demo(args) -> None:
    """Show prefill/decode + continuous batching with several concurrent prompts."""
    model, cfg, tokenizer = _load(args)
    from .engine import LLMEngine

    engine = LLMEngine(cfg, tokenizer=tokenizer, model=model)
    prompts = args.prompts or [
        "vLLM is a",
        "The KV cache",
        "The scheduler",
        "Prefix caching",
    ]
    print(f"[cli] adding {len(prompts)} requests (continuous batching demo)\n")
    for p in prompts:
        engine.add_request(p, SamplingParams(temperature=0.8, max_tokens=args.max_tokens))

    prev = {}
    step_no = 0
    while engine.has_pending():
        step_no += 1
        outputs = engine.step()
        print(f"--- step {step_no} ---")
        for o in outputs:
            text = o.outputs[0].text
            delta = text[len(prev.get(o.request_id, "")):]
            prev[o.request_id] = text
            mark = f" [finished: {o.outputs[0].finish_reason}]" if o.finished else ""
            print(f"  req#{o.request_id}: {delta!r}{mark}")
    print(f"\n[cli] done in {step_no} engine steps")
    for o in engine.seqs.values():
        print(f"  req#{o.seq_id}: prompt={tokenizer.decode(o.prompt_ids)!r} "
              f"-> output={tokenizer.decode(o.output_ids)!r}")


def cmd_spec(args) -> None:
    model, cfg, tokenizer = _load(args)
    from .data import CORPUS
    from .kv_cache import BlockManager
    from .speculative import BigramDraftModel, speculative_generate

    prompt = args.prompt
    prompt_ids = tokenizer.encode(prompt)
    draft = BigramDraftModel(tokenizer, CORPUS)
    bm = BlockManager(cfg.cache, cfg.model, enable_prefix_caching=False)
    res = speculative_generate(model, tokenizer, bm, draft, prompt_ids,
                               max_new_tokens=args.max_tokens,
                               num_speculative_tokens=args.num_speculative)
    print(f"[cli] prompt: {prompt!r}")
    print(f"[cli] output: {tokenizer.decode(res['output_ids'])!r}")
    accept_rate = (res["accepted_count"] / res["draft_count"]) if res["draft_count"] else 0
    print(f"[cli] draft tokens: {res['draft_count']}, accepted: {res['accepted_count']} "
          f"({accept_rate:.1%}), target forwards: {res['target_forwards']}, "
          f"steps: {res['steps']}")


def cmd_quant(args) -> None:
    model, cfg, tokenizer = _load(args)
    from .quantize import quantize_model

    qmodel, stats = quantize_model(model)
    print("[cli] int8 weight-only quantization")
    print(f"  original : {stats['original_bytes']:>8.0f} bytes")
    print(f"  quantized: {stats['quantized_bytes']:>8.0f} bytes "
          f"({stats['compression_ratio']:.2f}x)")
    print(f"  max abs error: {stats['max_abs_error']:.5f}")
    print("  -> the dequantized weights are loaded into a new engine for a demo")

    from .engine import LLMEngine
    e1 = LLMEngine(cfg, tokenizer=tokenizer, model=model)
    e2 = LLMEngine(cfg, tokenizer=tokenizer, model=qmodel)
    p = SamplingParams(temperature=0.0, max_tokens=30)
    for eng, tag in ((e1, "float32"), (e2, "int8(dequantized)")):
        out = eng.generate("vLLM is", p)
        print(f"  [{tag:>19}] {out.outputs[0].text!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minivllm", description="mini-vLLM")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--model", type=str, default="artifacts/tinygpt",
                       help="path to a checkpoint directory")
        p.add_argument("--quantize", choices=["int8"], default=None)
        p.add_argument("--max-tokens", type=int, default=30)
        p.add_argument("--temperature", type=float, default=0.8)

    p = sub.add_parser("serve", help="run the OpenAI-compatible server")
    add_common(p)
    p.add_argument("--host", type=str, default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--log-level", type=str, default="warning")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("chat", help="interactive chat")
    add_common(p)
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("demo", help="continuous-batching demo")
    add_common(p)
    p.add_argument("--prompts", nargs="*", default=None)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("spec", help="speculative decoding demo")
    add_common(p)
    p.add_argument("--num-speculative", type=int, default=5)
    p.add_argument("--prompt", type=str, default="vLLM is")
    p.set_defaults(func=cmd_spec)

    p = sub.add_parser("quant", help="show int8 quantization effect")
    add_common(p)
    p.set_defaults(func=cmd_quant)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as e:
        print(f"[cli] error: {e}", file=sys.stderr)
        print("[cli] run `python scripts/train.py` first to create a checkpoint",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
