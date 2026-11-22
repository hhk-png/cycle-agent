"""Educational TP/PP sharding of a full (TP=1, PP=1) state dict.

Production Megatron uses a distributed checkpoint with a global metadata
index and a resharding planner.  For the mini model a full state dict is small
enough to slice in memory.  This utility is used by the equivalence tests and
is also a useful template for understanding which weight dimension is sharded
by each parallel layer:

* column-parallel layers shard output rows;
* row-parallel layers shard input columns;
* embeddings and the vocabulary projection shard vocabulary rows.
"""
from __future__ import annotations

from typing import Dict

import torch


def _rows(tensor: torch.Tensor, rank: int, world: int) -> torch.Tensor:
    if tensor.size(0) % world != 0:
        raise ValueError(f"row dimension {tensor.size(0)} not divisible by {world}")
    chunk = tensor.size(0) // world
    return tensor.narrow(0, rank * chunk, chunk).clone()


def _cols(tensor: torch.Tensor, rank: int, world: int) -> torch.Tensor:
    if tensor.size(-1) % world != 0:
        raise ValueError(f"column dimension {tensor.size(-1)} not divisible by {world}")
    chunk = tensor.size(-1) // world
    return tensor.narrow(-1, rank * chunk, chunk).clone()


def _tp_shard(key: str, tensor: torch.Tensor, tp_rank: int, tp_size: int) -> torch.Tensor:
    if tp_size == 1:
        return tensor.clone()
    if key.endswith("out_proj.weight") or key.endswith("down_proj.weight") or key.endswith("fc2.weight"):
        return _cols(tensor, tp_rank, tp_size)
    if key.endswith("out_proj.bias") or key.endswith("down_proj.bias") or key.endswith("fc2.bias"):
        return tensor.clone()
    if key.endswith("q_proj.weight") or key.endswith("k_proj.weight") or key.endswith(
        "v_proj.weight"
    ):
        return _rows(tensor, tp_rank, tp_size)
    if key.endswith("q_proj.bias") or key.endswith("k_proj.bias") or key.endswith(
        "v_proj.bias"
    ):
        return _rows(tensor, tp_rank, tp_size)
    if key.endswith("gate_proj.weight") or key.endswith("up_proj.weight") or key.endswith("fc1.weight"):
        return _rows(tensor, tp_rank, tp_size)
    if key.endswith("gate_proj.bias") or key.endswith("up_proj.bias") or key.endswith("fc1.bias"):
        return _rows(tensor, tp_rank, tp_size)
    if key in {"embedding.weight", "output_layer.weight"}:
        return _rows(tensor, tp_rank, tp_size)
    # LayerNorm, router, position embeddings: replicated.  Note that
    # ``position_embedding.weight`` must not match the vocabulary-embedding
    # suffix rule above; it has shape [max_seq_len, hidden] and is replicated.
    return tensor.clone()


def make_stage_state(
    full_state: Dict[str, torch.Tensor],
    *,
    pp_rank: int,
    pp_size: int,
    tp_rank: int,
    tp_size: int,
    num_layers: int,
) -> Dict[str, torch.Tensor]:
    if num_layers % pp_size != 0:
        raise ValueError("num_layers must be divisible by pp_size")
    per_stage = num_layers // pp_size
    start = pp_rank * per_stage
    end = start + per_stage

    out: Dict[str, torch.Tensor] = {}
    for key, value in full_state.items():
        if key.startswith("layers."):
            parts = key.split(".")
            global_layer = int(parts[1])
            if not (start <= global_layer < end):
                continue
            local_key = ".".join(["layers", str(global_layer - start)] + parts[2:])
            out[local_key] = _tp_shard(key, value, tp_rank, tp_size)
        elif key.startswith("embedding.") or key.startswith("position_embedding."):
            if pp_rank == 0:
                out[key] = _tp_shard(key, value, tp_rank, tp_size)
        elif key.startswith("final_norm.") or key.startswith("output_layer."):
            if pp_rank == pp_size - 1:
                out[key] = _tp_shard(key, value, tp_rank, tp_size)
        else:
            # Any future global parameter is replicated by default.
            out[key] = _tp_shard(key, value, tp_rank, tp_size)
    # Preserve tied embedding/output aliasing so optimizer-state helpers can
    # deduplicate them by storage (build_optimizer sees one Parameter).
    if (
        "embedding.weight" in out
        and "output_layer.weight" in out
        and torch.equal(out["embedding.weight"], out["output_layer.weight"])
    ):
        out["output_layer.weight"] = out["embedding.weight"]
    return out


def load_stage_from_full(stage, full_state: Dict[str, torch.Tensor]) -> None:
    """Load a TP=1/PP=1 state dict into the current TP/PP stage."""
    from .parallel.state import (
        get_pipeline_model_parallel_rank,
        get_pipeline_model_parallel_world_size,
        get_tensor_model_parallel_rank,
        get_tensor_model_parallel_world_size,
    )

    sharded = make_stage_state(
        full_state,
        pp_rank=get_pipeline_model_parallel_rank(),
        pp_size=get_pipeline_model_parallel_world_size(),
        tp_rank=get_tensor_model_parallel_rank(),
        tp_size=get_tensor_model_parallel_world_size(),
        num_layers=stage.config.num_layers,
    )
    missing, unexpected = stage.load_state_dict(sharded, strict=False)
    if unexpected:
        raise ValueError(f"unexpected keys while loading sharded state: {unexpected}")
    if missing:
        raise ValueError(f"missing keys while loading sharded state: {missing}")
