#!/usr/bin/env python
"""Distributed numerical-equivalence checks.

Run as a subprocess from pytest.  Each case builds a full TP=1/PP=1 reference
inside every worker, then rebuilds the same model under a parallel layout and
compares loss and selected gradients.  The portable queue backend is used so
the test runs on Windows, where stock PyTorch Gloo may be unavailable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

# Pin thread counts before importing torch: CPU GEMM reduction order must be
# stable across the reference and parallel runs, otherwise a tight 1e-5
# tolerance becomes flaky under load.
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
    get_tensor_model_parallel_group,
    initialize_model_parallel,
)
from mini_megatron.parallel.transformer import TransformerStage
from mini_megatron.pipeline.schedules import build_schedule
from mini_megatron.sharding import load_stage_from_full
from mini_megatron.optim.adamw import global_grad_norm


def _max_diff(a: torch.Tensor, b: torch.Tensor) -> float:
    return float((a.detach() - b.detach()).abs().max().item())


def run_case(case: str, world_size: int):
    cfg = ExperimentConfig.from_json(ROOT / "configs" / "tiny.json")
    cfg.model.vocab_size = 128
    cfg.model.max_seq_len = 32
    cfg.model.hidden_size = 64
    cfg.model.ffn_hidden_size = 128
    cfg.train.seq_len = 16
    cfg.train.micro_batch_size = 2
    if case == "tp_learned":
        cfg.model.position_embedding = "learned"
    torch.manual_seed(1234)
    full = TransformerStage(cfg.model, ParallelConfig())
    ids = torch.arange(0, 32, dtype=torch.long).reshape(2, 16) % 128
    labels = (ids + 3) % 128

    ref_out = full(input_ids=ids, labels=labels)
    assert ref_out.loss is not None
    ref_loss = float(ref_out.loss.detach())
    ref_out.loss.backward()
    full_grads = {name: p.grad.detach().clone() for name, p in full.named_parameters() if p.grad is not None}
    ref_norm = torch.sqrt(sum((g.float() ** 2).sum() for g in full_grads.values()))

    result = {"case": case, "ref_loss": ref_loss, "loss_diff": None, "grad_diff": None}

    if case == "tp":
        tp, pp, cp = 2, 1, 1
        parallel = ParallelConfig(
            tensor_model_parallel_size=2,
            pipeline_model_parallel_size=1,
            context_parallel_size=1,
            sequence_parallel=True,
        )
        initialize_model_parallel(tp, pp, cp)
        stage = TransformerStage(cfg.model, parallel)
        load_stage_from_full(stage, full.state_dict())
        out = stage(input_ids=ids, labels=labels)
        out.loss.backward()
        tp_rank = 1 if comm.get_rank() == 1 else 0
        # q_proj is column-parallel: local rows.
        q_name = "layers.0.self_attention.q_proj.weight"
        local_q = stage.layers[0].self_attention.q_proj.weight.grad
        chunk = full_grads[q_name].size(0) // 2
        ref_q = full_grads[q_name].narrow(0, tp_rank * chunk, chunk)
        # out_proj is row-parallel: local columns.
        o_name = "layers.0.self_attention.out_proj.weight"
        local_o = stage.layers[0].self_attention.out_proj.weight.grad
        ref_o = full_grads[o_name].narrow(1, tp_rank * (full_grads[o_name].size(1) // 2), full_grads[o_name].size(1) // 2)
        result["loss_diff"] = abs(float(out.loss.detach()) - ref_loss)
        result["q_diff"] = _max_diff(local_q, ref_q)
        result["o_diff"] = _max_diff(local_o, ref_o)
        result["q_norms"] = [float(local_q.norm()), float(ref_q.norm())]
        result["o_norms"] = [float(local_o.norm()), float(ref_o.norm())]
        result["gradnorm_diff"] = abs(float(global_grad_norm(stage)) - float(ref_norm))
        result["weight_o_diff"] = _max_diff(
            stage.layers[0].self_attention.out_proj.weight,
            full.layers[0].self_attention.out_proj.weight.narrow(1, tp_rank * 32, 32),
        )
        result["grad_diff"] = max(result["q_diff"], result["o_diff"])

    elif case == "tp_learned":
        tp, pp, cp = 2, 1, 1
        parallel = ParallelConfig(
            tensor_model_parallel_size=2,
            pipeline_model_parallel_size=1,
            context_parallel_size=1,
            sequence_parallel=True,
        )
        initialize_model_parallel(tp, pp, cp)
        stage = TransformerStage(cfg.model, parallel)
        load_stage_from_full(stage, full.state_dict())
        out = stage(input_ids=ids, labels=labels)
        out.loss.backward()
        result["loss_diff"] = abs(float(out.loss.detach()) - ref_loss)
        result["position_diff"] = _max_diff(
            stage.position_embedding.weight.grad,
            full_grads["position_embedding.weight"],
        )
        tp_rank = 1 if comm.get_rank() == 1 else 0
        q_ref = full_grads["layers.0.self_attention.q_proj.weight"]
        chunk = q_ref.size(0) // 2
        result["q_diff"] = _max_diff(
            stage.layers[0].self_attention.q_proj.weight.grad,
            q_ref.narrow(0, tp_rank * chunk, chunk),
        )
        result["grad_diff"] = max(result["position_diff"], result["q_diff"])

    elif case == "tp_bias":
        cfg.model.add_bias_linear = True
        cfg.model.add_qkv_bias = True
        torch.manual_seed(1234)
        full_bias = TransformerStage(cfg.model, ParallelConfig())
        ref_bias_out = full_bias(input_ids=ids, labels=labels)
        ref_bias_out.loss.backward()
        bg = {name: p.grad.detach().clone() for name, p in full_bias.named_parameters() if p.grad is not None}
        parallel = ParallelConfig(
            tensor_model_parallel_size=2,
            pipeline_model_parallel_size=1,
            context_parallel_size=1,
            sequence_parallel=True,
        )
        initialize_model_parallel(2, 1, 1)
        stage = TransformerStage(cfg.model, parallel)
        load_stage_from_full(stage, full_bias.state_dict())
        out = stage(input_ids=ids, labels=labels)
        out.loss.backward()
        tp_rank = comm.get_group_rank(get_tensor_model_parallel_group())
        chunk = cfg.model.hidden_size // 2
        result["loss_diff"] = abs(float(out.loss.detach()) - float(ref_bias_out.loss.detach()))
        diffs = []
        diffs.append(_max_diff(stage.layers[0].input_layernorm.weight.grad, bg["layers.0.input_layernorm.weight"]))
        diffs.append(_max_diff(stage.layers[0].self_attention.q_proj.bias.grad, bg["layers.0.self_attention.q_proj.bias"].narrow(0, tp_rank * chunk, chunk)))
        diffs.append(_max_diff(stage.layers[0].self_attention.out_proj.bias.grad, bg["layers.0.self_attention.out_proj.bias"]))
        result["grad_diff"] = max(diffs)
        result["biases"] = diffs
        ref_bias_norm = torch.sqrt(sum((g.float() ** 2).sum() for g in bg.values()))
        result["gradnorm_diff"] = abs(float(global_grad_norm(stage)) - float(ref_bias_norm))

    elif case == "pp":
        tp, pp, cp = 1, 2, 1
        parallel = ParallelConfig(
            tensor_model_parallel_size=1,
            pipeline_model_parallel_size=2,
            context_parallel_size=1,
            sequence_parallel=False,
            pipeline_schedule="1f1b",
        )
        initialize_model_parallel(tp, pp, cp)
        stage = TransformerStage(cfg.model, parallel)
        load_stage_from_full(stage, full.state_dict())
        schedule = build_schedule(stage, parallel, cfg.train)
        schedule.stage.zero_grad(set_to_none=True)
        loss, _ = schedule.forward_backward([(ids, labels)])
        result["loss_diff"] = abs(float(loss.detach()) - ref_loss)
        result["gradnorm_diff"] = abs(float(global_grad_norm(stage)) - float(ref_norm))
        pp_rank = comm.get_group_rank(get_pipeline_model_parallel_group())
        global_layer = 0 if pp_rank == 0 else cfg.model.num_layers // 2
        local_layer = 0
        q_name = f"layers.{global_layer}.self_attention.q_proj.weight"
        local_q = stage.layers[local_layer].self_attention.q_proj.weight.grad
        result["grad_diff"] = _max_diff(local_q, full_grads[q_name])

    elif case == "cp":
        tp, pp, cp = 1, 1, 2
        parallel = ParallelConfig(
            tensor_model_parallel_size=1,
            pipeline_model_parallel_size=1,
            context_parallel_size=2,
            sequence_parallel=False,
        )
        initialize_model_parallel(tp, pp, cp)
        stage = TransformerStage(cfg.model, parallel)
        load_stage_from_full(stage, full.state_dict())
        out = stage(input_ids=ids, labels=labels)
        out.loss.backward()
        from mini_megatron.parallel.state import get_context_parallel_group

        cp_group = get_context_parallel_group()
        pieces = [torch.empty_like(out.logits) for _ in range(comm.get_group_size(cp_group))]
        comm.all_gather(pieces, out.logits, group=cp_group)
        gathered_logits = torch.cat(pieces, dim=1)
        result["logits_diff"] = _max_diff(gathered_logits, ref_out.logits)
        local_q = stage.layers[0].self_attention.q_proj.weight.grad
        cp_group = get_context_parallel_group()
        summed = comm.all_reduce(local_q, op=comm.SUM, group=cp_group)
        norm_grad = comm.all_reduce(
            stage.layers[0].input_layernorm.weight.grad, op=comm.SUM, group=cp_group
        )
        result["loss_diff"] = abs(float(out.loss.detach()) - ref_loss)
        result["q_norms"] = [float(summed.norm()), float(full_grads["layers.0.self_attention.q_proj.weight"].norm())]
        result["norm_diff"] = _max_diff(norm_grad, full_grads["layers.0.input_layernorm.weight"])
        result["norm_norms"] = [float(norm_grad.norm()), float(full_grads["layers.0.input_layernorm.weight"].norm())]
        k_grad = comm.all_reduce(stage.layers[0].self_attention.k_proj.weight.grad, op=comm.SUM, group=cp_group)
        v_grad = comm.all_reduce(stage.layers[0].self_attention.v_proj.weight.grad, op=comm.SUM, group=cp_group)
        result["k_norms"] = [float(k_grad.norm()), float(full_grads["layers.0.self_attention.k_proj.weight"].norm())]
        result["v_norms"] = [float(v_grad.norm()), float(full_grads["layers.0.self_attention.v_proj.weight"].norm())]
        # K/V are gathered across CP and used by every CP query shard; their
        # gradients must therefore include the all-reduce over CP.  Comparing
        # only the norms (as an earlier version did) can miss a wrong gradient
        # direction, so assert element-wise differences too.
        result["k_grad_diff"] = _max_diff(
            k_grad, full_grads["layers.0.self_attention.k_proj.weight"]
        )
        result["v_grad_diff"] = _max_diff(
            v_grad, full_grads["layers.0.self_attention.v_proj.weight"]
        )
        result["grad_diff"] = max(
            _max_diff(summed, full_grads["layers.0.self_attention.q_proj.weight"]),
            result["norm_diff"],
            result["k_grad_diff"],
            result["v_grad_diff"],
        )

    else:
        raise ValueError(f"unknown case: {case}")
    return result


def worker(rank, world_size, case, queues, out_path):
    try:
        comm.initialize_portable(rank, world_size, queues[rank], queues)
        os.environ["OMP_NUM_THREADS"] = "1"
        torch.set_num_threads(1)
        result = run_case(case, world_size)
        if rank == 0:
            Path(out_path).write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        traceback.print_exc()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case", required=True, choices=["tp", "tp_bias", "tp_learned", "pp", "cp"]
    )
    parser.add_argument("--world-size", type=int, default=2)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    mp.set_start_method("spawn", force=True)
    queues = [mp.Queue() for _ in range(args.world_size)]
    mp.spawn(
        worker,
        args=(args.world_size, args.case, queues, args.out),
        nprocs=args.world_size,
        join=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
