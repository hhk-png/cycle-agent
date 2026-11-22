#!/usr/bin/env python
"""Numerical-equivalence check for expert parallelism (EP).

The reference is the expert-replicated MoE (``ep=1``).  Every worker builds the
same reference weights, then rank ``r`` loads only the experts it owns into the
EP model and processes its own data shard.  Dispatch/combine run over the
portable backend, so the test exercises the real all-to-all path on CPU.

What is compared:

* local loss and logits (each rank's own tokens);
* the *global* auxiliary loss (EP statistics are all-reduced);
* expert gradients against a reference run on the concatenated global batch,
  scaled by ``1/ep`` because an expert sums contributions from every EP rank;
* router/dense gradients against the global batch after averaging over EP.

Run from pytest as a subprocess (Windows uses spawn).
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
    get_expert_parallel_group,
    initialize_model_parallel,
)
from mini_megatron.parallel.transformer import TransformerStage


def _make_ids(rank: int) -> torch.Tensor:
    base = torch.arange(0, 32, dtype=torch.long).reshape(2, 16) % 96
    return (base + 11 * rank) % 96


def _max_diff(a: torch.Tensor, b: torch.Tensor) -> float:
    return float((a.detach() - b.detach()).abs().max().item())


def run_case(world_size: int) -> dict:
    cfg = ExperimentConfig.from_json(ROOT / "configs" / "tiny_moe.json")
    cfg.model.vocab_size = 96
    cfg.model.max_seq_len = 32
    cfg.model.hidden_size = 64
    cfg.model.ffn_hidden_size = 128
    cfg.model.num_layers = 2
    cfg.model.num_experts = 4
    cfg.model.moe_topk = 2
    cfg.model.moe_capacity_factor = 100.0  # no dropping: clean equivalence
    cfg.model.moe_aux_loss_coeff = 1.0  # make the aux gradient visible
    cfg.train.seq_len = 16
    cfg.train.micro_batch_size = 2

    # Reference weights and reference gradients.  Both are computed before the
    # EP groups exist, so every rank sees exactly the same full model.
    torch.manual_seed(1234)
    full = TransformerStage(cfg.model, ParallelConfig())
    ids_local = _make_ids(comm.get_rank())
    labels_local = (ids_local + 3) % cfg.model.vocab_size
    ids_global = torch.cat([_make_ids(r) for r in range(world_size)], dim=0)
    labels_global = (ids_global + 3) % cfg.model.vocab_size

    ref_local = full(input_ids=ids_local, labels=labels_local)
    ref_local_logits = ref_local.logits.detach().clone()
    full.zero_grad(set_to_none=True)
    ref_local.loss.backward()
    local_router_grad = full.layers[0].mlp.router.weight.grad.detach().clone()
    local_q_grad = full.layers[0].self_attention.q_proj.weight.grad.detach().clone()

    full.zero_grad(set_to_none=True)
    ref_global = full(input_ids=ids_global, labels=labels_global)
    ref_global.loss.backward()
    global_router_grad = full.layers[0].mlp.router.weight.grad.detach().clone()
    global_q_grad = full.layers[0].self_attention.q_proj.weight.grad.detach().clone()
    global_grads = {
        name: parameter.grad.detach().clone()
        for name, parameter in full.named_parameters()
        if parameter.grad is not None
    }
    global_expert_grads = {
        e: global_grads[f"layers.0.mlp.experts.{e}.down_proj.weight"]
        for e in range(cfg.model.num_experts)
    }
    ref_loss = float(ref_local.loss.detach())
    ref_aux = float(ref_global.aux_loss.detach())

    # Separate reference run whose objective includes the auxiliary loss, so
    # the router gradient tests the aux path (not just the CE path).
    full.zero_grad(set_to_none=True)
    ref_aux_run = full(input_ids=ids_global, labels=labels_global)
    (ref_aux_run.loss + ref_aux_run.aux_loss).backward()
    global_router_grad_with_aux = (
        full.layers[0].mlp.router.weight.grad.detach().clone()
    )

    # Build the EP model and load each rank's expert shard plus the shared
    # weights from the reference state dict.
    parallel = ParallelConfig(expert_model_parallel_size=world_size)
    initialize_model_parallel(1, 1, 1, expert_model_parallel_size=world_size)
    ep_rank = comm.get_group_rank(get_expert_parallel_group())
    ep_size = comm.get_group_size(get_expert_parallel_group())
    epr = cfg.model.num_experts // ep_size

    torch.manual_seed(1234)
    ep_stage = TransformerStage(cfg.model, parallel)
    full_state = full.state_dict()
    ep_state = {}
    for key, value in full_state.items():
        if ".mlp.experts." in key:
            global_expert = int(key.split(".")[4])
            owner = global_expert // epr
            if owner != ep_rank:
                continue
            local_expert = global_expert - owner * epr
            new_key = key.replace(
                f"experts.{global_expert}.", f"experts.{local_expert}."
            )
            ep_state[new_key] = value
        else:
            ep_state[key] = value
    missing, unexpected = ep_stage.load_state_dict(ep_state, strict=False)
    if missing or unexpected:
        raise RuntimeError(f"missing={missing}, unexpected={unexpected}")

    ep_group = get_expert_parallel_group()
    out = ep_stage(input_ids=ids_local, labels=labels_local)
    out.loss.backward()
    ep_aux = float(out.aux_loss.detach())

    # Capture CE-only gradients before zeroing for the aux-gradient run.
    router_ce_avg = comm.all_reduce(
        ep_stage.layers[0].mlp.router.weight.grad.detach().clone(),
        op=comm.SUM,
        group=ep_group,
    ) / ep_size
    q_ce_avg = comm.all_reduce(
        ep_stage.layers[0].self_attention.q_proj.weight.grad.detach().clone(),
        op=comm.SUM,
        group=ep_group,
    ) / ep_size
    expert_diffs = []
    for local_expert in range(epr):
        global_expert = ep_rank * epr + local_expert
        grad = ep_stage.layers[0].mlp.experts[local_expert].down_proj.weight.grad.detach()
        # The expert sums the local mean-loss gradients of every EP rank;
        # divide by EP to express the global mean, matching the Trainer.
        expert_diffs.append(_max_diff(grad / ep_size, global_expert_grads[global_expert]))

    # Every local parameter: experts are compared against the global expert
    # after the 1/ep scaling; dense/router parameters are averaged over EP.
    all_grad_diffs = []
    for name, parameter in ep_stage.named_parameters():
        if parameter.grad is None:
            continue
        if ".mlp.experts." in name:
            parts = name.split(".")
            layer = parts[1]
            local_expert = int(parts[4])
            global_expert = ep_rank * epr + local_expert
            global_name = ".".join(
                ["layers", layer, "mlp", "experts", str(global_expert)]
                + parts[5:]
            )
            all_grad_diffs.append(
                _max_diff(parameter.grad / ep_size, global_grads[global_name])
            )
        else:
            averaged = comm.all_reduce(
                parameter.grad.detach(), op=comm.SUM, group=ep_group
            ) / ep_size
            all_grad_diffs.append(_max_diff(averaged, global_grads[name]))

    # Aux-loss gradient: objective = CE + aux.  The router gradient must match
    # the concatenated-global-batch reference, not be under-scaled by EP.
    ep_stage.zero_grad(set_to_none=True)
    out_aux_run = ep_stage(input_ids=ids_local, labels=labels_local)
    (out_aux_run.loss + out_aux_run.aux_loss).backward()
    router_aux_avg = comm.all_reduce(
        ep_stage.layers[0].mlp.router.weight.grad,
        op=comm.SUM,
        group=ep_group,
    ) / ep_size

    result = {
        "rank": ep_rank,
        "loss_diff": abs(float(out.loss.detach()) - ref_loss),
        "logits_diff": _max_diff(out.logits, ref_local_logits),
        "aux_diff": abs(ep_aux - ref_aux),
        "router_diff": _max_diff(router_ce_avg, global_router_grad),
        "q_diff": _max_diff(q_ce_avg, global_q_grad),
        "expert_diff": max(expert_diffs),
        "all_grad_diff": max(all_grad_diffs) if all_grad_diffs else 0.0,
        "aux_grad_diff": _max_diff(router_aux_avg, global_router_grad_with_aux),
        "local_router_norm": float(local_router_grad.norm()),
        "local_q_norm": float(local_q_grad.norm()),
        "ep_aux": ep_aux,
        "ref_aux": ref_aux,
    }
    result["grad_diff"] = max(
        result["router_diff"],
        result["q_diff"],
        result["expert_diff"],
        result["all_grad_diff"],
        result["aux_grad_diff"],
    )
    return result


def worker(rank, world_size, queues, out_path):
    try:
        comm.initialize_portable(rank, world_size, queues[rank], queues)
        torch.set_num_threads(1)
        result = run_case(world_size)
        metrics = torch.tensor(
            [
                result["loss_diff"],
                result["logits_diff"],
                result["aux_diff"],
                result["grad_diff"],
            ]
        )
        reduced = comm.all_reduce(metrics, op=comm.MAX)
        if rank == 0:
            result["loss_diff"] = float(reduced[0])
            result["logits_diff"] = float(reduced[1])
            result["aux_diff"] = float(reduced[2])
            result["grad_diff"] = float(reduced[3])
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
    mp.spawn(
        worker,
        args=(args.world_size, queues, args.out),
        nprocs=args.world_size,
        join=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
