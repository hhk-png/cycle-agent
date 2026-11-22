#!/usr/bin/env python
"""Cross-platform launcher for tiny multi-process runs.

Examples:
    python scripts/run_distributed.py --world-size 2 --config configs/tiny_tp2.json
    python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2.json --steps 2

On Windows the ``spawn`` start method is mandatory; on Linux both spawn and
fork work, but we always use spawn for reproducibility.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torch
import torch.multiprocessing as mp


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def worker(
    rank: int,
    world_size: int,
    master_port: int,
    config: str,
    steps: int | None,
    checkpoint_dir: str | None,
    mode: str,
    queues,
    auto_resume: bool,
    checkpoint: str | None,
    prompt: str | None,
    max_new_tokens: int | None,
    jsonl: str | None,
) -> None:
    from mini_megatron.parallel import comm

    comm.initialize_portable(rank, world_size, queues[rank], queues)
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = str(master_port)
    os.environ["RANK"] = str(rank)
    os.environ["WORLD_SIZE"] = str(world_size)
    os.environ["LOCAL_RANK"] = str(rank)
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    torch.set_num_threads(1)
    try:
        from mini_megatron.cli import main

        argv = [mode, "--config", config]
        if steps is not None:
            argv += ["--steps", str(steps)]
        if checkpoint_dir is not None:
            argv += ["--checkpoint-dir", checkpoint_dir]
        if auto_resume:
            argv += ["--auto-resume"]
        if checkpoint:
            argv += ["--checkpoint", checkpoint]
        if prompt:
            argv += ["--prompt", prompt]
        if max_new_tokens is not None:
            argv += ["--max-new-tokens", str(max_new_tokens)]
        if jsonl:
            argv += ["--jsonl", jsonl]
        code = main(argv)
        if code:
            raise SystemExit(code)
    except Exception:
        traceback.print_exc()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world-size", type=int, default=2)
    parser.add_argument("--config", default="configs/tiny_tp2.json")
    parser.add_argument("--steps", type=int, default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--mode", default="train")
    parser.add_argument("--master-port", type=int, default=None)
    parser.add_argument("--auto-resume", action="store_true")
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--max-new-tokens", dest="max_new_tokens", type=int, default=None)
    parser.add_argument("--jsonl", default=None, help="append rank-0 training records here")
    args = parser.parse_args()

    os.environ.setdefault("PYTHONPATH", str(ROOT))
    port = args.master_port or _free_port()
    mp.set_start_method("spawn", force=True)
    queues = [mp.Queue() for _ in range(args.world_size)]
    mp.spawn(
        worker,
        args=(
            args.world_size,
            port,
            args.config,
            args.steps,
            args.checkpoint_dir,
            args.mode,
            queues,
            args.auto_resume,
            args.checkpoint,
            args.prompt,
            args.max_new_tokens,
            args.jsonl,
        ),
        nprocs=args.world_size,
        join=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
