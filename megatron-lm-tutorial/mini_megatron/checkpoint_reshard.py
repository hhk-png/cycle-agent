"""Merge and re-shard model-only checkpoints between TP/PP layouts.

Production Megatron uses a distributed checkpoint with a global metadata index
and a resharding planner that also handles optimizer state, expert parallel
layout and arbitrary parallel orders.  This module implements the *model
weight* half of that problem for the mini layout:

    rank-local shards -> global (TP=1, PP=1) state dict -> new rank-local shards

The module implements both halves of the simple (non-ZeRO) case:

* model weights and AdamW ``exp_avg``/``exp_avg_sq``/``step`` are resharded;
* ZeRO/FSDP optimizer shards and expert-parallel MoE shards are rejected with a
  clear error (their state is layout-bound in a way this teaching tool does not
  model);
* layers must be evenly divisible across pipeline stages.

The utility is useful for (a) understanding what "resharding" actually
permutes, and (b) converting a checkpoint between small TP/PP layouts during
experiments.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import torch

from .config import ExperimentConfig, ParallelConfig
from .sharding import make_stage_state


_COLUMN_SUFFIXES = (
    "out_proj.weight",
    "down_proj.weight",
    "fc2.weight",
)
_ROW_SUFFIXES = (
    "q_proj.weight",
    "k_proj.weight",
    "v_proj.weight",
    "gate_proj.weight",
    "up_proj.weight",
    "fc1.weight",
)
_ROW_BIAS_SUFFIXES = (
    "q_proj.bias",
    "k_proj.bias",
    "v_proj.bias",
    "gate_proj.bias",
    "up_proj.bias",
    "fc1.bias",
)


def _shard_dim(key: str) -> int | None:
    """Return the tensor-parallel shard dimension, or None if replicated."""
    if key in {"embedding.weight", "output_layer.weight"}:
        return 0
    if key.endswith(_COLUMN_SUFFIXES):
        return -1
    if key.endswith(_ROW_SUFFIXES) or key.endswith(_ROW_BIAS_SUFFIXES):
        return 0
    return None


def merge_tp_shards(
    key: str, shards: Iterable[torch.Tensor], tp_size: int
) -> torch.Tensor:
    shards = list(shards)
    if len(shards) != tp_size:
        raise ValueError(f"{key}: expected {tp_size} shards, got {len(shards)}")
    dim = _shard_dim(key)
    if dim is None:
        # Replicated parameter: every shard must be identical.
        first = shards[0]
        for other in shards[1:]:
            if not torch.equal(first, other):
                raise ValueError(f"{key}: replicated shards differ across TP ranks")
        return first.clone()
    return torch.cat(shards, dim=dim)


def _global_layer_key(key: str, pp_rank: int, layers_per_stage: int) -> str:
    if not key.startswith("layers."):
        return key
    parts = key.split(".")
    local = int(parts[1])
    global_layer = pp_rank * layers_per_stage + local
    return ".".join(["layers", str(global_layer)] + parts[2:])


def full_state_from_stage_states(
    stage_states: Mapping[Tuple[int, int], Mapping[str, torch.Tensor]],
    *,
    pp_size: int,
    tp_size: int,
    num_layers: int,
) -> Dict[str, torch.Tensor]:
    """Invert ``make_stage_state`` for one DP/CP replica.

    ``stage_states[(pp_rank, tp_rank)]`` is the local ``state_dict`` of that
    stage.  The result is a full TP=1/PP=1 state dict with global layer names.
    """
    if num_layers % pp_size != 0:
        raise ValueError("num_layers must be divisible by pp_size")
    layers_per_stage = num_layers // pp_size

    # global_key -> {tp_rank: tensor}
    collected: Dict[str, Dict[int, torch.Tensor]] = {}
    for pp_rank in range(pp_size):
        for tp_rank in range(tp_size):
            key = (pp_rank, tp_rank)
            if key not in stage_states:
                raise KeyError(f"missing stage state for pp={pp_rank}, tp={tp_rank}")
            for local_key, tensor in stage_states[key].items():
                global_key = _global_layer_key(local_key, pp_rank, layers_per_stage)
                collected.setdefault(global_key, {})[tp_rank] = tensor

    full: Dict[str, torch.Tensor] = {}
    for global_key, shards in collected.items():
        if len(shards) != tp_size:
            raise ValueError(
                f"{global_key}: found {len(shards)} TP shards, expected {tp_size}"
            )
        full[global_key] = merge_tp_shards(
            global_key, [shards[i] for i in range(tp_size)], tp_size
        )
    return full


def load_full_state_from_checkpoint(
    directory: str | Path,
    config: ExperimentConfig,
    *,
    dp_size: int,
) -> Dict[str, torch.Tensor]:
    """Read a mini checkpoint directory and reconstruct full model weights.

    DP and CP replicas hold identical weights, so only ``dp=0, cp=0`` is read
    for each ``(pp, tp)`` coordinate.
    """
    directory = Path(directory)
    if directory.is_file() and directory.name == "latest":
        directory = directory.parent / directory.read_text(encoding="utf-8").strip()
    elif directory.is_dir() and (directory / "latest").exists() and not (
        directory / "metadata.json"
    ).exists():
        directory = directory / (directory / "latest").read_text(encoding="utf-8").strip()

    metadata_path = directory / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"missing checkpoint metadata: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    tp = config.parallel.tensor_model_parallel_size
    pp = config.parallel.pipeline_model_parallel_size
    cp = config.parallel.context_parallel_size
    if metadata.get("tensor_model_parallel_size") != tp:
        raise ValueError("checkpoint TP does not match the source config")
    if metadata.get("pipeline_model_parallel_size") != pp:
        raise ValueError("checkpoint PP does not match the source config")
    if metadata.get("context_parallel_size") != cp:
        raise ValueError("checkpoint CP does not match the source config")
    if metadata.get("data_parallel_size") != dp_size:
        raise ValueError(
            f"checkpoint DP={metadata.get('data_parallel_size')} does not match "
            f"the supplied dp_size={dp_size}"
        )
    if metadata.get("expert_model_parallel_size", 1) > 1:
        raise NotImplementedError(
            "expert-parallel weight resharding is not implemented; "
            "restore with the original EP layout first"
        )

    states: Dict[Tuple[int, int], Mapping[str, torch.Tensor]] = {}
    for pp_rank in range(pp):
        for tp_rank in range(tp):
            # dp=0, cp=0 for this (pp, tp) coordinate.
            rank = ((pp_rank * dp_size + 0) * cp + 0) * tp + tp_rank
            payload = torch.load(
                directory / f"rank_{rank:05d}.pt",
                map_location="cpu",
                weights_only=False,
            )
            # Expert-replicated MoE (EP=1) is still a normal TP/PP layout and
            # is handled by ``make_stage_state``.  Only a checkpoint that was
            # actually written with EP>1 needs the rejection above.
            states[(pp_rank, tp_rank)] = payload["model"]
    return full_state_from_stage_states(
        states, pp_size=pp, tp_size=tp, num_layers=config.model.num_layers
    )


def stage_states_for_layout(
    full_state: Mapping[str, torch.Tensor],
    *,
    config: ExperimentConfig,
) -> Dict[Tuple[int, int], Dict[str, torch.Tensor]]:
    """Slice a full state dict into ``(pp, tp) -> state_dict`` for a layout."""
    tp = config.parallel.tensor_model_parallel_size
    pp = config.parallel.pipeline_model_parallel_size
    if config.model.num_layers % pp != 0:
        raise ValueError("num_layers must be divisible by PP")
    out: Dict[Tuple[int, int], Dict[str, torch.Tensor]] = {}
    for pp_rank in range(pp):
        for tp_rank in range(tp):
            out[(pp_rank, tp_rank)] = make_stage_state(
                dict(full_state),
                pp_rank=pp_rank,
                pp_size=pp,
                tp_rank=tp_rank,
                tp_size=tp,
                num_layers=config.model.num_layers,
            )
    return out


def reshard_full_state(
    full_state: Mapping[str, torch.Tensor], target: ExperimentConfig
) -> Dict[Tuple[int, int], Dict[str, torch.Tensor]]:
    """Convert a full state dict to the target TP/PP layout (weights only).

    Expert-replicated MoE (``expert_model_parallel_size == 1``) is a normal
    TP/PP layout: the experts live on every rank and are sharded by the same
    row/column rules as dense MLPs.  Only a target with EP>1 would require
    expert redistribution, which this teaching tool does not implement.
    """
    if target.parallel.expert_model_parallel_size > 1:
        raise NotImplementedError(
            "expert-parallel resharding is not implemented; convert to an "
            "EP=1 layout first or use a production distributed checkpoint"
        )
    return stage_states_for_layout(full_state, config=target)


def save_model_only_checkpoint(
    directory: str | Path,
    *,
    stage_states: Mapping[Tuple[int, int], Mapping[str, torch.Tensor]],
    config: ExperimentConfig,
    dp_size: int,
    step: int = 0,
) -> Path:
    """Write a model-only checkpoint (no optimizer state) for experiments."""
    directory = Path(directory) / f"step_{step:06d}"
    directory.mkdir(parents=True, exist_ok=True)
    tp = config.parallel.tensor_model_parallel_size
    pp = config.parallel.pipeline_model_parallel_size
    cp = config.parallel.context_parallel_size
    for (pp_rank, tp_rank), state in stage_states.items():
        for dp_rank in range(dp_size):
            for cp_rank in range(cp):
                rank = ((pp_rank * dp_size + dp_rank) * cp + cp_rank) * tp + tp_rank
                torch.save(
                    {
                        "format": "mini-megatron/model-only-v1",
                        "step": step,
                        "rank": rank,
                        "model": dict(state),
                    },
                    directory / f"rank_{rank:05d}.pt",
                )
    manifest = {
        "format": "mini-megatron/model-only-v1",
        "step": step,
        "tensor_model_parallel_size": tp,
        "pipeline_model_parallel_size": pp,
        "context_parallel_size": cp,
        "expert_model_parallel_size": config.parallel.expert_model_parallel_size,
        "data_parallel_size": dp_size,
        "files": sorted(p.name for p in directory.glob("rank_*.pt")),
        "note": "model weights only; no optimizer/RNG state",
    }
    (directory / "metadata.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (directory.parent / "latest").write_text(directory.name + "\n", encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# Optimizer state (AdamW) resharding
# ---------------------------------------------------------------------------
# A standard PyTorch AdamW state_dict stores entries under integer indices into
# the optimizer's flat parameter order.  ``build_optimizer`` creates that order
# by splitting model.parameters() into "decay" (2-D) and "no-decay" (1-D or
# trailing-dim-1) groups, preserving model order inside each group.  The helpers
# below recover names from that order, merge TP shards, and split into a new
# TP/PP layout.  ZeRO-1 shards are intentionally rejected: their ``owned`` list
# is a different index space and needs the model to be reconstructed first.


def _is_no_decay(tensor: torch.Tensor) -> bool:
    return tensor.ndim <= 1 or tensor.shape[-1] == 1


def _state_aliases(
    model_state: Mapping[str, torch.Tensor],
) -> Tuple[Dict[str, torch.Tensor], Dict[str, str]]:
    """Canonicalise tied parameters (same storage) to one name.

    ``state_dict`` exposes both ``embedding.weight`` and ``output_layer.weight``
    when they are tied, but ``build_optimizer`` sees one ``Parameter``.  The
    optimizer order must therefore deduplicate by storage, not by key.
    """
    canonical: Dict[str, torch.Tensor] = {}
    aliases: Dict[str, str] = {}
    seen: Dict[Tuple[int, Tuple[int, ...], torch.dtype, str], str] = {}
    for name, tensor in model_state.items():
        key = (tensor.data_ptr(), tuple(tensor.shape), tensor.dtype, str(tensor.device))
        if key in seen:
            aliases[name] = seen[key]
        else:
            seen[key] = name
            canonical[name] = tensor
    return canonical, aliases


def _param_order_from_state(model_state: Mapping[str, torch.Tensor]) -> List[str]:
    """Parameter order used by ``optim.build_optimizer`` for this state dict.

    Tied embedding/output parameters are deduplicated by storage.  Every key in
    the mini model state dict is a parameter (no persistent buffers), which
    keeps the mapping unambiguous.
    """
    canonical, _ = _state_aliases(model_state)
    names = list(canonical.keys())
    decay = [name for name in names if not _is_no_decay(canonical[name])]
    no_decay = [name for name in names if _is_no_decay(canonical[name])]
    return decay + no_decay


def optimizer_state_by_name(
    optimizer_state: Mapping[str, Any],
    model_state: Mapping[str, torch.Tensor],
) -> Dict[str, Dict[str, Any]]:
    """Convert a standard AdamW state_dict into ``{param_name: entry}``."""
    if not isinstance(optimizer_state, Mapping) or (
        "state" not in optimizer_state or "param_groups" not in optimizer_state
    ):
        raise ValueError(
            "expected a standard AdamW state_dict; flat distributed-optimizer "
            "and ZeRO shards must be restored with their original layout"
        )
    order = _param_order_from_state(model_state)
    index_to_name: Dict[int, str] = {}
    for group in optimizer_state.get("param_groups", []):
        for index in group.get("params", []):
            if not 0 <= int(index) < len(order):
                raise ValueError(f"optimizer index {index} outside parameter order")
            index_to_name[int(index)] = order[int(index)]
    result: Dict[str, Dict[str, Any]] = {}
    if not optimizer_state.get("state"):
        return {name: {} for name in order}
    for index, entry in optimizer_state.get("state", {}).items():
        name = index_to_name.get(int(index))
        if name is None:
            raise ValueError(f"optimizer state index {index} has no parameter")
        result[name] = {
            key: (value.clone() if torch.is_tensor(value) else value)
            for key, value in entry.items()
        }
    missing = [name for name in order if name not in result]
    if missing:
        raise ValueError(f"optimizer state missing parameters: {missing[:5]}")
    return result


def _merge_optimizer_tensor(
    name: str, shards: Iterable[torch.Tensor], tp_size: int
) -> torch.Tensor:
    shards = list(shards)
    if len(shards) != tp_size:
        raise ValueError(f"{name}: expected {tp_size} shards, got {len(shards)}")
    dim = _shard_dim(name)
    if dim is None:
        first = shards[0]
        for other in shards[1:]:
            if not torch.allclose(first, other, atol=1e-6, rtol=1e-5):
                raise ValueError(f"{name}: replicated optimizer shards differ")
        return first.clone()
    return torch.cat(shards, dim=dim)


def merge_optimizer_state_by_name(
    stage_states: Mapping[Tuple[int, int], Mapping[str, Dict[str, Any]]],
    *,
    pp_size: int,
    tp_size: int,
    num_layers: int,
) -> Dict[str, Dict[str, Any]]:
    """Invert per-(pp, tp) name-keyed optimizer states into a full state."""
    if num_layers % pp_size != 0:
        raise ValueError("num_layers must be divisible by pp_size")
    layers_per_stage = num_layers // pp_size
    collected: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for pp_rank in range(pp_size):
        for tp_rank in range(tp_size):
            key = (pp_rank, tp_rank)
            if key not in stage_states:
                raise KeyError(f"missing optimizer state for pp={pp_rank}, tp={tp_rank}")
            for local_name, entry in stage_states[key].items():
                global_name = _global_layer_key(local_name, pp_rank, layers_per_stage)
                collected.setdefault(global_name, {})[tp_rank] = entry

    merged: Dict[str, Dict[str, Any]] = {}
    for global_name, entries in collected.items():
        if len(entries) != tp_size:
            raise ValueError(
                f"{global_name}: found {len(entries)} TP shards, expected {tp_size}"
            )
        ordered = [entries[i] for i in range(tp_size)]
        keys = set(ordered[0])
        for entry in ordered[1:]:
            if set(entry) != keys:
                raise ValueError(f"{global_name}: optimizer entries have different keys")
        entry_out: Dict[str, Any] = {}
        for state_key in keys:
            values = [entry[state_key] for entry in ordered]
            if state_key in {"exp_avg", "exp_avg_sq"}:
                entry_out[state_key] = _merge_optimizer_tensor(
                    global_name, values, tp_size
                )
            elif torch.is_tensor(values[0]) and values[0].numel() == 1:
                first = values[0]
                for other in values[1:]:
                    if not torch.equal(first, other):
                        raise ValueError(
                            f"{global_name}.{state_key}: scalar state differs"
                        )
                entry_out[state_key] = first.clone()
            else:
                # Unknown optimizer state: only safe to merge if replicated.
                first = values[0]
                for other in values[1:]:
                    if torch.is_tensor(first):
                        if not torch.allclose(first, other, atol=1e-6, rtol=1e-5):
                            raise ValueError(
                                f"{global_name}.{state_key}: shards differ"
                            )
                    elif first != other:
                        raise ValueError(
                            f"{global_name}.{state_key}: shards differ"
                        )
                entry_out[state_key] = (
                    first.clone() if torch.is_tensor(first) else first
                )
        merged[global_name] = entry_out
    return merged


def _infer_num_layers(
    stage_model_states: Mapping[Tuple[int, int], Mapping[str, torch.Tensor]]
) -> int:
    pp_size = max(pp for pp, _ in stage_model_states) + 1
    per_stage = 0
    for (pp_rank, _), state in stage_model_states.items():
        if pp_rank != 0:
            continue
        layer_ids = {
            int(key.split(".")[1])
            for key in state
            if key.startswith("layers.")
        }
        per_stage = max(per_stage, len(layer_ids))
    if per_stage == 0:
        raise ValueError("cannot infer num_layers from stage states")
    return per_stage * pp_size


def split_optimizer_state_by_name(
    full_state: Mapping[str, Mapping[str, Any]],
    stage_model_states: Mapping[Tuple[int, int], Mapping[str, torch.Tensor]],
    *,
    tp_size: int,
) -> Dict[Tuple[int, int], Dict[str, Dict[str, Any]]]:
    """Slice a full name-keyed optimizer state into a TP/PP layout.

    ``stage_model_states[(pp, tp)]`` provides the local parameter names and
    identifies the global name, exactly like ``make_stage_state`` does for
    weights.
    """
    pp_size = max(pp for pp, _ in stage_model_states) + 1
    layers_per_stage = _infer_num_layers(stage_model_states) // pp_size
    out: Dict[Tuple[int, int], Dict[str, Dict[str, Any]]] = {}
    for (pp_rank, tp_rank), local_model in stage_model_states.items():
        local_out: Dict[str, Dict[str, Any]] = {}
        canonical_local, _ = _state_aliases(local_model)
        for local_name in canonical_local:
            global_name = _global_layer_key(local_name, pp_rank, layers_per_stage)
            if global_name not in full_state:
                raise KeyError(f"missing optimizer state for {global_name}")
            entry = full_state[global_name]
            local_entry: Dict[str, Any] = {}
            for state_key, value in entry.items():
                if state_key in {"exp_avg", "exp_avg_sq"}:
                    dim = _shard_dim(global_name)
                    if dim is None:
                        local_entry[state_key] = value.clone()
                    else:
                        chunks = torch.chunk(value, tp_size, dim=dim)
                        local_entry[state_key] = chunks[tp_rank].clone()
                else:
                    local_entry[state_key] = (
                        value.clone() if torch.is_tensor(value) else value
                    )
            local_out[local_name] = local_entry
        out[(pp_rank, tp_rank)] = local_out
    return out


def optimizer_state_dict_from_by_name(
    by_name: Mapping[str, Mapping[str, Any]],
    model_state: Mapping[str, torch.Tensor],
    template: Mapping[str, Any],
) -> Dict[str, Any]:
    """Convert a name-keyed optimizer state back to AdamW's state_dict form."""
    order = _param_order_from_state(model_state)
    decay = [name for name in order if not _is_no_decay(model_state[name])]
    state: Dict[int, Dict[str, Any]] = {}
    for index, name in enumerate(order):
        if name not in by_name:
            raise KeyError(f"missing optimizer state for {name}")
        state[index] = dict(by_name[name])
    if "param_groups" in template:
        base = dict(template["param_groups"][0])
    else:
        base = dict(template)
    base.pop("params", None)
    groups = [dict(base, params=list(range(len(decay))))]
    if len(order) > len(decay):
        no_decay = dict(base, weight_decay=0.0)
        no_decay["params"] = list(range(len(decay), len(order)))
        groups.append(no_decay)
    return {"state": state, "param_groups": groups}


def load_full_optimizer_from_checkpoint(
    directory: str | Path,
    config: ExperimentConfig,
    *,
    dp_size: int,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Read a non-ZeRO checkpoint and merge its optimizer state across TP/PP.

    Returns ``(name_keyed_state, param_group_template)``.  Raises for ZeRO and
    expert-parallel checkpoints, whose state cannot be merged by this simple
    index mapping.
    """
    directory = Path(directory)
    if directory.is_file() and directory.name == "latest":
        directory = directory.parent / directory.read_text(encoding="utf-8").strip()
    elif directory.is_dir() and (directory / "latest").exists() and not (
        directory / "metadata.json"
    ).exists():
        directory = directory / (directory / "latest").read_text(encoding="utf-8").strip()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    tp = config.parallel.tensor_model_parallel_size
    pp = config.parallel.pipeline_model_parallel_size
    cp = config.parallel.context_parallel_size
    for key, expected in (
        ("tensor_model_parallel_size", tp),
        ("pipeline_model_parallel_size", pp),
        ("context_parallel_size", cp),
        ("data_parallel_size", dp_size),
    ):
        if metadata.get(key) != expected:
            raise ValueError(
                f"checkpoint {key}={metadata.get(key)} does not match {expected}"
            )
    if metadata.get("expert_model_parallel_size", 1) > 1:
        raise NotImplementedError(
            "expert-parallel optimizer resharding is not implemented; "
            "restore with the original EP layout first"
        )

    stage_states: Dict[Tuple[int, int], Dict[str, Dict[str, Any]]] = {}
    template: Dict[str, Any] = {}
    for pp_rank in range(pp):
        for tp_rank in range(tp):
            rank = ((pp_rank * dp_size + 0) * cp + 0) * tp + tp_rank
            payload = torch.load(
                directory / f"rank_{rank:05d}.pt",
                map_location="cpu",
                weights_only=False,
            )
            optimizer_state = payload["optimizer"]
            if isinstance(optimizer_state, dict) and (
                "owned" in optimizer_state
                or optimizer_state.get("format") == "mini-distributed-optimizer/v1"
                or "master_shard" in optimizer_state
            ):
                raise NotImplementedError(
                    "ZeRO-1/flat distributed-optimizer resharding is not "
                    "implemented; restore with the original DP size first"
                )
            # EP=1 experts are replicated and shard exactly like dense MLPs;
            # the EP>1 case was rejected from the checkpoint metadata above.
            stage_states[(pp_rank, tp_rank)] = optimizer_state_by_name(
                optimizer_state, payload["model"]
            )
            if not template:
                template = optimizer_state.get("param_groups", [{}])[0]
    merged = merge_optimizer_state_by_name(
        stage_states,
        pp_size=pp,
        tp_size=tp,
        num_layers=config.model.num_layers,
    )
    return merged, dict(template)
