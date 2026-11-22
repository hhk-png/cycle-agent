"""Command-line entry points.

Single process:
    python -m mini_megatron.cli train --config configs/tiny.json

Distributed (Linux/macOS or Windows with the provided launch script):
    torchrun --standalone --nproc_per_node=2 -m mini_megatron.cli train \
        --config configs/tiny_tp2.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import torch

from .analysis import HARDWARE_PRESETS, estimate, format_estimate
from .checkpoint import resolve_checkpoint
from .config import ExperimentConfig
from .generation import generate
from .parallel import comm
from .parallel.layers import set_model_parallel_init_seed
from .parallel.state import (
    destroy_model_parallel,
    get_rank,
    initialize_model_parallel,
)
from .parallel.transformer import TransformerStage
from .training import Trainer


def setup_distributed(backend: Optional[str] = None) -> int:
    world_size = int(os.environ.get("WORLD_SIZE", "1"))
    if world_size > 1 and not comm.is_initialized():
        if "MASTER_ADDR" not in os.environ:
            os.environ["MASTER_ADDR"] = "127.0.0.1"
        if "MASTER_PORT" not in os.environ:
            os.environ["MASTER_PORT"] = "29500"
        backend = backend or ("nccl" if torch.cuda.is_available() else "gloo")
        comm.initialize_torch(backend=backend, init_method="env://")
    return world_size


def load_config(path: str, args) -> ExperimentConfig:
    config = ExperimentConfig.from_json(path)
    if getattr(args, "steps", None):
        config.train.max_steps = args.steps
    if getattr(args, "seq_len", None):
        config.train.seq_len = args.seq_len
    if getattr(args, "checkpoint_dir", None) is not None:
        # An explicit empty string disables checkpointing; do not treat it as
        # "argument absent" and fall back to the config default.
        config.train.checkpoint_dir = args.checkpoint_dir
    if getattr(args, "seed", None) is not None:
        config.train.seed = args.seed
    if getattr(args, "data_path", None):
        config.train.data = "bin"
        config.train.data_path = args.data_path
    if getattr(args, "jsonl", None):
        config.train.log_jsonl = args.jsonl
    return config


def _init_runtime(config: ExperimentConfig) -> int:
    world_size = setup_distributed()
    config.validate(world_size)
    initialize_model_parallel(
        config.parallel.tensor_model_parallel_size,
        config.parallel.pipeline_model_parallel_size,
        config.parallel.context_parallel_size,
        config.parallel.expert_model_parallel_size,
    )
    # Seed before the stage is constructed so every model-parallel peer and
    # every schedule variant starts from identical weights.
    torch.manual_seed(config.train.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(config.train.seed)
    # Sharded layers consume a TP-rank-offset stream; replicated layers keep
    # the global seed above.  This must happen before TransformerStage is built.
    set_model_parallel_init_seed(config.train.seed)
    return world_size


def _shutdown() -> None:
    destroy_model_parallel()
    comm.destroy()


def run_train(args) -> int:
    config = load_config(args.config, args)
    _init_runtime(config)
    try:
        stage = TransformerStage(config.model, config.parallel)
        trainer = Trainer(config, stage=stage)
        if get_rank() == 0:
            print(
                json.dumps(
                    {
                        "event": "start",
                        "world_size": trainer.world_size,
                        "dp_size": trainer.dp_size,
                        "grad_accum": trainer.grad_accum,
                        "parameters_this_stage": stage.num_parameters(),
                    }
                ),
                flush=True,
            )
        resume = args.resume
        if resume is None and args.auto_resume:
            candidate = Path(config.train.checkpoint_dir) / "latest"
            if candidate.exists():
                resume = str(candidate)
        trainer.fit(resume=resume)
        if get_rank() == 0 and trainer.history:
            print(
                json.dumps(
                    {"event": "final", **trainer.history[-1]},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        return 0
    finally:
        _shutdown()


def _load_model_for_generation(config: ExperimentConfig, checkpoint: str) -> TransformerStage:
    import copy

    # Sequence parallel shards the activation sequence; autoregressive decoding
    # appends one token at a time, so the length is rarely divisible by TP.
    # Sequence parallel is a training-memory optimisation and is not needed for
    # this single-request decoder, so build the inference stage without it.
    inference_parallel = copy.copy(config.parallel)
    inference_parallel.sequence_parallel = False
    stage = TransformerStage(config.model, inference_parallel)
    path = resolve_checkpoint(checkpoint)
    rank = get_rank()
    payload = torch.load(path / f"rank_{rank:05d}.pt", map_location="cpu", weights_only=False)
    stage.load_state_dict(payload["model"])
    return stage


def run_generate(args) -> int:
    config = load_config(args.config, args)
    _init_runtime(config)
    try:
        if config.parallel.pipeline_model_parallel_size > 1:
            raise SystemExit("generation currently supports pipeline_model_parallel_size=1")
        stage = _load_model_for_generation(config, args.checkpoint)
        stage.to(config.train.device)
        prompt = [int(x) for x in args.prompt.split(",") if x.strip()]
        if not prompt:
            prompt = [0]
        input_ids = torch.tensor([prompt], dtype=torch.long)
        seed = args.seed if args.seed is not None else config.train.seed
        generator = torch.Generator().manual_seed(seed)
        out = generate(
            stage,
            input_ids,
            max_new_tokens=args.max_new_tokens,
            temperature=args.temperature,
            top_k=args.top_k,
            top_p=args.top_p,
            eos_token_id=args.eos,
            generator=generator,
        )
        if get_rank() == 0:
            print(json.dumps({"prompt": prompt, "tokens": out[0].tolist()}))
        return 0
    finally:
        _shutdown()


def run_benchmark(args) -> int:
    config = load_config(args.config, args)
    config.train.max_steps = max(1, args.steps or 5)
    config.train.checkpoint_dir = ""
    config.train.eval_interval = 0
    _init_runtime(config)
    try:
        trainer = Trainer(config)
        history = trainer.fit(save_final=False)
        if get_rank() == 0:
            print(json.dumps({"event": "benchmark", "history": history}, ensure_ascii=False))
        return 0
    finally:
        _shutdown()


def run_info(args) -> int:
    config = load_config(args.config, args)
    world = int(os.environ.get("WORLD_SIZE", "1"))
    config.validate(world)
    print(
        json.dumps(
            {
                "world_size": world,
                "tensor_parallel": config.parallel.tensor_model_parallel_size,
                "pipeline_parallel": config.parallel.pipeline_model_parallel_size,
                "context_parallel": config.parallel.context_parallel_size,
                "data_parallel": world
                // (
                    config.parallel.tensor_model_parallel_size
                    * config.parallel.pipeline_model_parallel_size
                    * config.parallel.context_parallel_size
                ),
                "model": config.model.__dict__,
                "train": config.train.__dict__,
            },
            indent=2,
        )
    )
    return 0


def run_estimate(args) -> int:
    config = load_config(args.config, args)
    hardware = HARDWARE_PRESETS[args.gpu]
    if args.world_size is not None:
        world = args.world_size
    elif "WORLD_SIZE" in os.environ:
        world = int(os.environ["WORLD_SIZE"])
    else:
        # Default to one data-parallel replica; the estimator is about the
        # model-parallel layout unless the user asks for a bigger world.
        world = (
            config.parallel.tensor_model_parallel_size
            * config.parallel.pipeline_model_parallel_size
            * config.parallel.context_parallel_size
        )
    config.validate(world)
    result = estimate(
        config,
        world_size=world,
        tokens=args.tokens,
        hardware=hardware,
        utilization=args.mfu,
        zero_shard=max(1, args.zero_shard),
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_estimate(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mini-megatron")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--config", default="configs/tiny.json")
        p.add_argument("--steps", type=int, default=None)
        p.add_argument("--seq-len", dest="seq_len", type=int, default=None)
        p.add_argument("--checkpoint-dir", dest="checkpoint_dir", default=None)
        p.add_argument("--seed", type=int, default=None)
        p.add_argument("--data-path", dest="data_path", default=None)
        p.add_argument("--jsonl", default=None)

    train = sub.add_parser("train")
    common(train)
    train.add_argument("--resume", default=None)
    train.add_argument("--auto-resume", action="store_true")
    train.set_defaults(func=run_train)

    gen = sub.add_parser("generate")
    common(gen)
    gen.add_argument("--checkpoint", required=True)
    gen.add_argument("--prompt", default="0,1,2,3")
    gen.add_argument("--max-new-tokens", dest="max_new_tokens", type=int, default=16)
    gen.add_argument("--temperature", type=float, default=0.8)
    gen.add_argument("--top-k", dest="top_k", type=int, default=20)
    gen.add_argument("--top-p", dest="top_p", type=float, default=0.95)
    gen.add_argument("--eos", type=int, default=None)
    gen.set_defaults(func=run_generate)

    bench = sub.add_parser("benchmark")
    common(bench)
    bench.set_defaults(func=run_benchmark)

    info = sub.add_parser("info")
    common(info)
    info.set_defaults(func=run_info)

    est = sub.add_parser(
        "estimate",
        help="First-order parameter/memory/FLOP/communication estimate",
    )
    common(est)
    est.add_argument("--tokens", type=float, default=0.0,
                     help="training token budget (e.g. 1e12)")
    est.add_argument("--world-size", dest="world_size", type=int, default=None,
                     help="total ranks; default = tp*pp*cp (DP=1)")
    est.add_argument("--gpu", choices=sorted(HARDWARE_PRESETS), default="h100")
    est.add_argument("--mfu", type=float, default=0.4)
    est.add_argument("--zero-shard", dest="zero_shard", type=int, default=1,
                     help="effective ZeRO sharding of optimizer state")
    est.add_argument("--json", action="store_true")
    est.set_defaults(func=run_estimate)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
