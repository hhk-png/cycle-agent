#!/usr/bin/env python
"""Flat optimizer under TP: the clip norm must be global, not TP-local.

World=4, TP=2, DP=2.  The reference path uses ``global_grad_norm`` (which
reduces over the whole world); the flat path must report the same norm and
produce the same parameters when clipping is active.  This is the regression
for the bug where ``DistributedOptimizer.grad_norm`` only reduced over DP and
ignored the disjoint TP shards (and PP stages).
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

from mini_megatron.config import ExperimentConfig, ParallelConfig
from mini_megatron.data.dataset import BatchProvider, SyntheticTokenDataset
from mini_megatron.optim.adamw import (
    all_reduce_gradients,
    build_optimizer,
    clip_gradients,
    global_grad_norm,
)
from mini_megatron.optim.distributed_optimizer import DistributedOptimizer
from mini_megatron.parallel import comm
from mini_megatron.parallel.layers import set_model_parallel_init_seed
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
        cfg.train.seq_len = 16
        cfg.model.max_seq_len = 32
        cfg.parallel = ParallelConfig(
            tensor_model_parallel_size=2,
            sequence_parallel=True,
            distributed_optimizer=True,
            distributed_optimizer_style="flat",
        )
        initialize_model_parallel(2, 1, 1, 1)
        dp_group = get_data_parallel_group()

        ds = SyntheticTokenDataset(
            cfg.model.vocab_size, cfg.train.seq_len, 512, seed=5
        )
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

        records = []
        for clip in (0.0, 0.01):
            set_model_parallel_init_seed(1234)
            torch.manual_seed(1234)
            ref = TransformerStage(cfg.model, cfg.parallel)
            set_model_parallel_init_seed(1234)
            torch.manual_seed(1234)
            flat = TransformerStage(cfg.model, cfg.parallel)
            flat.load_state_dict(ref.state_dict())
            ref_opt = build_optimizer(ref, cfg.train)
            flat_opt = DistributedOptimizer(
                flat,
                lr=cfg.train.lr,
                betas=(cfg.train.adam_beta1, cfg.train.adam_beta2),
                eps=cfg.train.adam_eps,
                weight_decay=cfg.train.weight_decay,
                group=dp_group,
            )
            ref_opt.zero_grad(set_to_none=True)
            flat_opt.zero_grad()
            loss_ref = ref(input_ids=x, labels=y).loss
            loss_flat = flat(input_ids=x, labels=y).loss
            loss_ref.backward()
            loss_flat.backward()

            # The reference path: DP average, then global (TP*DP) norm/clip.
            all_reduce_gradients(ref, dp_group)
            norm_ref = float(global_grad_norm(ref))
            clip_gradients(ref, clip)

            # The flat path owns DP reduction internally; its norm must still
            # cover the TP shards and apply the same clip coefficient.
            flat_opt.prepare()
            norm_flat = float(flat_opt.clip_grad_norm(clip))

            ref_opt.step()
            flat_opt.step()
            param_diff = max(
                float((a.detach() - b.detach()).abs().max())
                for a, b in zip(ref.parameters(), flat.parameters())
            )
            records.append(
                {
                    "clip": clip,
                    "norm_ref": norm_ref,
                    "norm_flat": norm_flat,
                    "norm_diff": abs(norm_ref - norm_flat),
                    "param_diff": param_diff,
                    "loss_diff": abs(
                        float(loss_ref.detach() - loss_flat.detach())
                    ),
                }
            )

        # Every rank must agree that the norm and the clipped update match.
        for record in records:
            assert record["norm_diff"] < 1e-5, record
            assert record["param_diff"] < 1e-6, record
            assert record["loss_diff"] < 1e-6, record

        if rank == 0:
            Path(out_path).write_text(
                json.dumps({"rank": rank, "records": records}, indent=2),
                encoding="utf-8",
            )
    except Exception:
        traceback.print_exc()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    mp.set_start_method("spawn", force=True)
    queues = [mp.Queue() for _ in range(4)]
    mp.spawn(worker, args=(4, queues, args.out), nprocs=4, join=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
