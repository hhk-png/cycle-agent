"""Sharded, rank-local checkpoints.

A production Megatron checkpoint is a distributed checkpoint with a global
metadata index, per-shard tensors and optional resharding between TP/PP
layouts.  The mini version saves one file per global rank and refuses to load
into a different parallel layout.  The important engineering ideas are still
visible: model shards, optimizer shards, RNG state, step, and a manifest.
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any, Dict, Optional

import torch

from .config import ExperimentConfig
from .parallel import comm


CHECKPOINT_FORMAT = "mini-megatron/v1"


def _rank() -> int:
    return comm.get_rank()


def _barrier() -> None:
    comm.barrier()


def _optimizer_state(optimizer) -> Dict[str, Any]:
    if hasattr(optimizer, "state_dict"):
        return optimizer.state_dict()
    raise TypeError("optimizer must implement state_dict()")


def _load_optimizer_state(optimizer, state: Dict[str, Any]) -> None:
    if hasattr(optimizer, "load_state_dict"):
        optimizer.load_state_dict(state)
        return
    raise TypeError("optimizer must implement load_state_dict()")


def save_checkpoint(
    directory: str | Path,
    *,
    stage: torch.nn.Module,
    optimizer,
    step: int,
    config: ExperimentConfig,
    dp_size: int,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    directory = Path(directory) / f"step_{step:06d}"
    directory.mkdir(parents=True, exist_ok=True)
    rank = _rank()
    payload = {
        "format": CHECKPOINT_FORMAT,
        "step": step,
        "rank": rank,
        "world_size": comm.get_world_size(),
        "dp_size": dp_size,
        "config": config.to_dict(),
        "model": stage.state_dict(),
        "optimizer": _optimizer_state(optimizer),
        "python_rng": random.getstate(),
        "torch_rng": torch.get_rng_state(),
        "extra": extra or {},
    }
    if torch.cuda.is_available():
        payload["cuda_rng"] = torch.cuda.get_rng_state_all()
    torch.save(payload, directory / f"rank_{rank:05d}.pt")

    _barrier()
    if rank == 0:
        manifest = {
            "format": CHECKPOINT_FORMAT,
            "step": step,
            "world_size": payload["world_size"],
            "tensor_model_parallel_size": config.parallel.tensor_model_parallel_size,
            "pipeline_model_parallel_size": config.parallel.pipeline_model_parallel_size,
            "context_parallel_size": config.parallel.context_parallel_size,
            "expert_model_parallel_size": config.parallel.expert_model_parallel_size,
            "data_parallel_size": dp_size,
            "files": sorted(p.name for p in directory.glob("rank_*.pt")),
        }
        (directory / "metadata.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        (Path(directory).parent / "latest").write_text(
            directory.name + "\n", encoding="utf-8"
        )
    _barrier()
    return directory


def load_checkpoint(
    directory: str | Path,
    *,
    stage: torch.nn.Module,
    optimizer,
    config: ExperimentConfig,
    dp_size: int,
    strict: bool = True,
) -> Dict[str, Any]:
    directory = Path(directory)
    if directory.is_file() and directory.name == "latest":
        directory = directory.parent / directory.read_text(encoding="utf-8").strip()
    elif (
        directory.is_dir()
        and not (directory / "metadata.json").exists()
        and (directory / "latest").exists()
    ):
        target = (directory / "latest").read_text(encoding="utf-8").strip()
        directory = directory / target

    metadata_path = directory / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"missing checkpoint metadata: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected = {
        "tensor_model_parallel_size": config.parallel.tensor_model_parallel_size,
        "pipeline_model_parallel_size": config.parallel.pipeline_model_parallel_size,
        "context_parallel_size": config.parallel.context_parallel_size,
        "expert_model_parallel_size": config.parallel.expert_model_parallel_size,
        "data_parallel_size": dp_size,
    }
    for key, value in expected.items():
        # Checkpoints written before EP support have no expert key; they are
        # equivalent to expert_model_parallel_size=1.
        actual = metadata.get(
            key, 1 if key == "expert_model_parallel_size" else None
        )
        if actual != value:
            raise ValueError(
                f"checkpoint {key}={actual} does not match runtime {value}; "
                "use a resharding converter instead of loading in place"
            )
    rank = _rank()
    payload = torch.load(
        directory / f"rank_{rank:05d}.pt", map_location="cpu", weights_only=False
    )
    stage.load_state_dict(payload["model"], strict=strict)
    _load_optimizer_state(optimizer, payload["optimizer"])
    try:
        random.setstate(payload["python_rng"])
    except (KeyError, TypeError):
        pass
    if "torch_rng" in payload:
        torch.set_rng_state(payload["torch_rng"])
    if "cuda_rng" in payload and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(payload["cuda_rng"])
    return {
        "step": payload["step"],
        "extra": payload.get("extra", {}),
    }


def resolve_checkpoint(path: str | Path) -> Path:
    path = Path(path)
    if path.is_file() and path.name == "latest":
        target = path.read_text(encoding="utf-8").strip()
        return path.parent / target
    if path.is_dir() and (path / "latest").exists():
        target = (path / "latest").read_text(encoding="utf-8").strip()
        return path / target
    return path
