#!/usr/bin/env python
"""Compare ZeRO-1 with plain AdamW under the same DP step."""
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
from mini_megatron.optim.adamw import (
    ZeRO1Optimizer,
    all_reduce_gradients,
    build_optimizer,
)
from mini_megatron.parallel import comm
from mini_megatron.parallel.state import (
    get_data_parallel_group,
    get_data_parallel_rank,
    get_data_parallel_world_size,
    initialize_model_parallel,
)
from mini_megatron.parallel.transformer import TransformerStage


def worker(rank, world, queues, out_path):
    try:
        comm.initialize_portable(rank, world, queues[rank], queues)
        torch.set_num_threads(1)
        cfg = ExperimentConfig.from_json(ROOT / "configs" / "tiny.json")
        cfg.train.seq_len = 8
        cfg.model.max_seq_len = 32
        cfg.parallel.distributed_optimizer = True
        initialize_model_parallel(1, 1, 1)  # world=2 => DP=2

        torch.manual_seed(77)
        a = TransformerStage(cfg.model, cfg.parallel)
        torch.manual_seed(77)
        b = TransformerStage(cfg.model, cfg.parallel)
        b.load_state_dict(a.state_dict())

        opt_a = build_optimizer(a, cfg.train)
        opt_b = build_optimizer(b, cfg.train)
        zero = ZeRO1Optimizer(b, opt_b, group=get_data_parallel_group())

        ds = SyntheticTokenDataset(cfg.model.vocab_size, cfg.train.seq_len, 256, seed=5)
        provider = BatchProvider(
            ds,
            micro_batch_size=2,
            grad_accumulation_steps=1,
            data_parallel_rank=get_data_parallel_rank(),
            data_parallel_size=get_data_parallel_world_size(),
            seq_len=cfg.train.seq_len,
            seed=9,
        )
        x, y = provider.get_micro_batches(0)[0]

        opt_a.zero_grad(set_to_none=True)
        zero.zero_grad()
        loss_a = a(input_ids=x, labels=y).loss
        loss_b = b(input_ids=x, labels=y).loss
        loss_a.backward()
        loss_b.backward()
        all_reduce_gradients(a, get_data_parallel_group())
        all_reduce_gradients(b, get_data_parallel_group())
        opt_a.step()
        zero.step()

        max_diff = max(
            float((pa.detach() - pb.detach()).abs().max())
            for pa, pb in zip(a.parameters(), b.parameters())
        )
        if rank == 0:
            Path(out_path).write_text(
                json.dumps({"max_diff": max_diff, "loss_diff": abs(float(loss_a.detach() - loss_b.detach()))})
            )
    except Exception:
        traceback.print_exc()
        raise


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    args = p.parse_args()
    mp.set_start_method("spawn", force=True)
    qs = [mp.Queue() for _ in range(2)]
    mp.spawn(worker, args=(2, qs, args.out), nprocs=2, join=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
