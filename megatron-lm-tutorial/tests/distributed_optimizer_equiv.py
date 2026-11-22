#!/usr/bin/env python
"""Compare the flat DP-sharded optimizer with plain AdamW under DP=2.

Both models start from identical weights and see the same per-rank batches.
The reference path all-reduces gradients and steps AdamW on every rank; the
flat path reduce-scatters into one shard per rank and all-gathers the updated
parameters.  After each step the full parameter vectors must agree.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torch
import torch.multiprocessing as mp

from mini_megatron.config import ExperimentConfig
from mini_megatron.data.dataset import BatchProvider, SyntheticTokenDataset
from mini_megatron.optim.adamw import all_reduce_gradients, build_optimizer
from mini_megatron.optim.distributed_optimizer import DistributedOptimizer
from mini_megatron.parallel import comm
from mini_megatron.parallel.state import (
    get_data_parallel_group,
    get_data_parallel_rank,
    get_data_parallel_world_size,
    initialize_model_parallel,
)
from mini_megatron.parallel.transformer import TransformerStage


def worker(rank, world, queues, out_path, steps):
    try:
        comm.initialize_portable(rank, world, queues[rank], queues)
        torch.set_num_threads(1)
        cfg = ExperimentConfig.from_json(ROOT / "configs" / "tiny.json")
        cfg.train.seq_len = 8
        cfg.model.max_seq_len = 32
        cfg.parallel.distributed_optimizer = True
        cfg.parallel.distributed_optimizer_style = "flat"
        initialize_model_parallel(1, 1, 1)  # world=2 => DP=2

        torch.manual_seed(77)
        ref = TransformerStage(cfg.model, cfg.parallel)
        torch.manual_seed(77)
        flat = TransformerStage(cfg.model, cfg.parallel)
        flat.load_state_dict(ref.state_dict())

        ref_opt = build_optimizer(ref, cfg.train)
        flat_opt = DistributedOptimizer(
            flat,
            lr=cfg.train.lr,
            betas=(cfg.train.adam_beta1, cfg.train.adam_beta2),
            eps=cfg.train.adam_eps,
            weight_decay=cfg.train.weight_decay,
            group=get_data_parallel_group(),
        )

        ds = SyntheticTokenDataset(cfg.model.vocab_size, cfg.train.seq_len, 512, seed=5)
        provider = BatchProvider(
            ds,
            micro_batch_size=2,
            grad_accumulation_steps=1,
            data_parallel_rank=get_data_parallel_rank(),
            data_parallel_size=get_data_parallel_world_size(),
            seq_len=cfg.train.seq_len,
            seed=9,
        )

        records = []
        for step in range(steps):
            x, y = provider.get_micro_batches(step)[0]
            ref_opt.zero_grad(set_to_none=True)
            flat_opt.zero_grad()

            loss_ref = ref(input_ids=x, labels=y).loss
            loss_flat = flat(input_ids=x, labels=y).loss
            loss_ref.backward()
            loss_flat.backward()

            all_reduce_gradients(ref, get_data_parallel_group())
            flat_opt.prepare()
            norm_flat = float(flat_opt.grad_norm())
            # The reference norm is the same quantity for DP-averaged grads.
            ref_norm = torch.zeros((), dtype=torch.float32)
            for p in ref.parameters():
                if p.grad is not None:
                    ref_norm = ref_norm + p.grad.detach().float().pow(2).sum()
            # Each rank holds the full DP-averaged gradient, so the mean of the
            # per-rank squared norms is the global squared norm.
            ref_norm = float(
                (comm.all_reduce(ref_norm, op=comm.SUM) / world).sqrt()
            )

            ref_opt.step()
            flat_opt.step()

            max_diff = max(
                float((pa.detach() - pb.detach()).abs().max())
                for pa, pb in zip(ref.parameters(), flat.parameters())
            )
            records.append(
                {
                    "step": step,
                    "loss_diff": abs(float(loss_ref.detach() - loss_flat.detach())),
                    "param_max_diff": max_diff,
                    "grad_norm_diff": abs(ref_norm - norm_flat),
                }
            )

        if rank == 0:
            Path(out_path).write_text(
                json.dumps(
                    {
                        "steps": records,
                        "loss_diff": max(r["loss_diff"] for r in records),
                        "param_max_diff": max(r["param_max_diff"] for r in records),
                        "grad_norm_diff": max(r["grad_norm_diff"] for r in records),
                    },
                    indent=2,
                )
            )
    except Exception:
        traceback.print_exc()
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--steps", type=int, default=3)
    args = parser.parse_args()
    mp.set_start_method("spawn", force=True)
    queues = [mp.Queue() for _ in range(2)]
    mp.spawn(worker, args=(2, queues, args.out, args.steps), nprocs=2, join=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
