#!/usr/bin/env python
"""Numerical-equivalence check for the executable interleaved (VPP) schedule.

Layout: world=2, pp=2, v=2, 4 layers -> global chunks 0/1/2/3, each one layer.
Rank 0 owns chunks 0 and 2, rank 1 owns chunks 1 and 3.  A micro-batch walks
chunks 0->1->2->3, so communication wraps around the pipeline ring.

The reference is the same weights in a plain PP=1 model.  We compare the loss
and every local layer/embedding/output gradient.
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
from mini_megatron.parallel import comm
from mini_megatron.parallel.state import (
    get_pipeline_model_parallel_group,
    get_pipeline_model_parallel_rank,
    initialize_model_parallel,
)
from mini_megatron.parallel.transformer import TransformerStage
from mini_megatron.pipeline.interleaved_exec import InterleavedGPipeExecutor


def _max_diff(a: torch.Tensor, b: torch.Tensor) -> float:
    return float((a.detach() - b.detach()).abs().max().item())


def _micro_batches(cfg):
    base = torch.arange(0, 64, dtype=torch.long).reshape(4, 16) % cfg.model.vocab_size
    labels = (base + 3) % cfg.model.vocab_size
    return [
        (base[0:2], labels[0:2]),
        (base[2:4], labels[2:4]),
    ]


def run_case(world_size: int) -> dict:
    cfg = ExperimentConfig.from_json(ROOT / "configs" / "tiny.json")
    cfg.model.vocab_size = 128
    cfg.model.max_seq_len = 32
    cfg.model.hidden_size = 64
    cfg.model.ffn_hidden_size = 128
    cfg.model.num_layers = 4
    cfg.train.seq_len = 16
    cfg.train.micro_batch_size = 2
    micro_batches = _micro_batches(cfg)

    # PP=1 reference with the same weights and data.
    torch.manual_seed(1234)
    full = TransformerStage(cfg.model, ParallelConfig())
    full.zero_grad(set_to_none=True)
    ref_loss = torch.zeros(())
    for ids, labels in micro_batches:
        out = full(input_ids=ids, labels=labels)
        ref_loss = ref_loss + out.loss.detach() / len(micro_batches)
        (out.loss / len(micro_batches)).backward()
    ref_grads = {
        name: parameter.grad.detach().clone()
        for name, parameter in full.named_parameters()
        if parameter.grad is not None
    }

    parallel = ParallelConfig(
        pipeline_model_parallel_size=world_size,
        pipeline_schedule="1f1b",
    )
    initialize_model_parallel(1, world_size, 1)
    executor = InterleavedGPipeExecutor(
        cfg.model, parallel, cfg.train, chunks_per_rank=2
    )
    pp_rank = get_pipeline_model_parallel_rank()
    layout = executor.layout
    full_state = full.state_dict()

    for chunk in layout.chunks_of_rank(pp_rank):
        stage = executor.chunks[str(chunk)]
        chunk_state = {}
        for local_index, global_layer in enumerate(layout.layers_of_chunk(chunk)):
            prefix = f"layers.{global_layer}."
            for key, value in full_state.items():
                if key.startswith(prefix):
                    chunk_state[f"layers.{local_index}." + key[len(prefix) :]] = value
        if stage.is_first:
            for key, value in full_state.items():
                if key.startswith("embedding.") or key.startswith("position_embedding."):
                    chunk_state[key] = value
        if stage.is_last:
            for key, value in full_state.items():
                if key.startswith("final_norm.") or key.startswith("output_layer."):
                    chunk_state[key] = value
        missing, unexpected = stage.load_state_dict(chunk_state, strict=True)
        if missing or unexpected:
            raise RuntimeError(f"chunk {chunk}: missing={missing}, unexpected={unexpected}")

    executor.zero_grad(set_to_none=True)
    loss, _ = executor.forward_backward(micro_batches)

    diffs = []
    for chunk in layout.chunks_of_rank(pp_rank):
        stage = executor.chunks[str(chunk)]
        global_layers = list(layout.layers_of_chunk(chunk))
        for local_name, parameter in stage.named_parameters():
            if parameter.grad is None:
                continue
            if local_name.startswith("layers."):
                parts = local_name.split(".")
                local_index = int(parts[1])
                global_layer = global_layers[local_index]
                global_name = ".".join(
                    ["layers", str(global_layer)] + parts[2:]
                )
            else:
                global_name = local_name
            if global_name not in ref_grads:
                raise KeyError(f"no reference gradient for {global_name}")
            diffs.append(_max_diff(parameter.grad, ref_grads[global_name]))

    result = {
        "rank": pp_rank,
        "loss_diff": abs(float(loss.detach()) - float(ref_loss)),
        "grad_diff": max(diffs) if diffs else 0.0,
        "num_chunks": len(executor.chunks),
    }
    return result


def worker(rank, world_size, queues, out_path):
    try:
        comm.initialize_portable(rank, world_size, queues[rank], queues)
        torch.set_num_threads(1)
        result = run_case(world_size)
        metrics = torch.tensor([result["loss_diff"], result["grad_diff"]])
        reduced = comm.all_reduce(metrics, op=comm.MAX, group=get_pipeline_model_parallel_group())
        if rank == 0:
            result["loss_diff"] = float(reduced[0])
            result["grad_diff"] = float(reduced[1])
            Path(out_path).write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        traceback.print_exc()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world-size", type=int, default=2)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    mp.set_start_method("spawn", force=True)
    queues = [mp.Queue() for _ in range(args.world_size)]
    mp.spawn(worker, args=(args.world_size, queues, args.out), nprocs=args.world_size, join=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
