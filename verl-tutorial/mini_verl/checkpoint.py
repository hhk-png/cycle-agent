"""Checkpoint manager, TP merge and a dependency-free safetensors export."""

from __future__ import annotations

import json
import re
import shutil
import struct
from pathlib import Path
from typing import Any

import numpy as np

from .config import PPOConfig
from .data import CharTokenizer
from .sharding import tp_gather_state_dict

__all__ = [
    "CheckpointManager",
    "write_safetensors",
    "read_safetensors",
    "export_hf_like",
]

_STEP = re.compile(r"global_step_(\d+)$")


def write_safetensors(path: str | Path, tensors: dict[str, np.ndarray]) -> None:
    """Write the safetensors container format without external dependencies.

    Layout: 8-byte little-endian uint64 header length, JSON header padded with
    spaces to an 8-byte boundary, then raw little-endian tensor bytes.
    """
    header: dict[str, Any] = {}
    payload = bytearray()
    offset = 0
    for name, tensor in tensors.items():
        array = np.ascontiguousarray(tensor, dtype=np.float32)
        data = array.tobytes(order="C")
        header[name] = {
            "dtype": "F32",
            "shape": list(array.shape),
            "data_offsets": [offset, offset + len(data)],
        }
        payload.extend(data)
        offset += len(data)
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    padding = (8 - len(encoded) % 8) % 8
    encoded += b" " * padding
    with open(path, "wb") as handle:
        handle.write(struct.pack("<Q", len(encoded)))
        handle.write(encoded)
        handle.write(payload)


def read_safetensors(path: str | Path) -> dict[str, np.ndarray]:
    with open(path, "rb") as handle:
        length = struct.unpack("<Q", handle.read(8))[0]
        header = json.loads(handle.read(length).decode("utf-8"))
        payload = handle.read()
    tensors = {}
    for name, spec in header.items():
        start, end = spec["data_offsets"]
        array = np.frombuffer(payload[start:end], dtype=np.float32).reshape(spec["shape"]).copy()
        tensors[name] = array
    return tensors


def _write_optimizer(path: Path, payload: dict[str, Any]) -> None:
    """Serialize an ``Optimizer.state_dict`` into a single npz file.

    Keys are ``step_count`` plus ``param::field`` for each moment tensor.  The
    parameter names are kept as npz keys, which is enough to restore AdamW
    moments without pickle.
    """

    arrays: dict[str, np.ndarray] = {"__step_count__": np.asarray([int(payload.get("step_count", 0))])}
    for name, fields in payload.get("state", {}).items():
        for field, value in fields.items():
            arrays[f"{name}::{field}"] = np.asarray(value, dtype=np.float32)
    np.savez(path, **arrays)


def _read_optimizer(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    arrays = np.load(path)
    state: dict[str, dict[str, np.ndarray]] = {}
    for key in arrays.files:
        if key == "__step_count__":
            continue
        name, field = key.split("::", 1)
        state.setdefault(name, {})[field] = arrays[key]
    return {"step_count": int(arrays["__step_count__"][0]), "state": state}


class CheckpointManager:
    def __init__(self, root: str | Path, max_to_keep: int = 2):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_to_keep = max_to_keep

    def step_dir(self, step: int) -> Path:
        return self.root / f"global_step_{step}"

    def save(
        self,
        step: int,
        actor_state: dict[str, np.ndarray],
        critic_state: dict[str, np.ndarray] | None = None,
        config: PPOConfig | None = None,
        tokenizer: CharTokenizer | None = None,
        metrics: dict[str, float] | None = None,
        actor_optimizer: dict[str, Any] | None = None,
        critic_optimizer: dict[str, Any] | None = None,
        extra_state: dict[str, Any] | None = None,
    ) -> Path:
        directory = self.step_dir(step)
        directory.mkdir(parents=True, exist_ok=True)
        np.savez(directory / "actor.npz", **{f"actor.{k}": v for k, v in actor_state.items()})
        if critic_state is not None:
            np.savez(directory / "critic.npz", **{f"critic.{k}": v for k, v in critic_state.items()})
        if actor_optimizer is not None:
            _write_optimizer(directory / "actor_optimizer.npz", actor_optimizer)
        if critic_optimizer is not None:
            _write_optimizer(directory / "critic_optimizer.npz", critic_optimizer)
        if config is not None:
            config.to_yaml(directory / "config.yaml")
        if tokenizer is not None:
            tokenizer.save(str(directory / "tokenizer.json"))
        state = {"step": step, "metrics": metrics or {}}
        if extra_state:
            state.update(extra_state)
        (directory / "trainer_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        self.prune()
        return directory

    def list_checkpoints(self) -> list[int]:
        steps = []
        for child in self.root.iterdir():
            match = _STEP.match(child.name)
            if match and child.is_dir():
                steps.append(int(match.group(1)))
        return sorted(steps)

    def latest(self) -> int | None:
        steps = self.list_checkpoints()
        return steps[-1] if steps else None

    def load(self, step: int) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray] | None, dict[str, Any]]:
        directory = self.step_dir(step)
        if not directory.exists():
            raise FileNotFoundError(directory)
        actor_arrays = np.load(directory / "actor.npz")
        actor = {key[len("actor.") :]: actor_arrays[key] for key in actor_arrays.files}
        critic = None
        critic_path = directory / "critic.npz"
        if critic_path.exists():
            critic_arrays = np.load(critic_path)
            critic = {key[len("critic.") :]: critic_arrays[key] for key in critic_arrays.files}
        state_path = directory / "trainer_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        return actor, critic, state

    def load_optimizers(self, step: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        directory = self.step_dir(step)
        return (
            _read_optimizer(directory / "actor_optimizer.npz"),
            _read_optimizer(directory / "critic_optimizer.npz"),
        )

    def load_config(self, step: int) -> PPOConfig:
        from .config import load_config

        return load_config(self.step_dir(step) / "config.yaml")

    def prune(self) -> None:
        if self.max_to_keep <= 0:
            # 0 means "keep all" rather than "delete the checkpoint just saved".
            return
        steps = self.list_checkpoints()
        for step in steps[: max(0, len(steps) - self.max_to_keep)]:
            shutil.rmtree(self.step_dir(step), ignore_errors=True)

    def export_hf_like(
        self,
        step: int,
        path: str | Path,
        tp_shards: list[dict[str, np.ndarray]] | None = None,
    ) -> Path:
        if tp_shards is not None:
            actor = tp_gather_state_dict(tp_shards)
        else:
            actor, _, _ = self.load(step)
        result = export_hf_like(actor, path, config_path=self.step_dir(step) / "config.yaml")
        tokenizer_path = self.step_dir(step) / "tokenizer.json"
        if tokenizer_path.exists():
            shutil.copyfile(tokenizer_path, result / "tokenizer.json")
        return result


def export_hf_like(state: dict[str, np.ndarray], path: str | Path, config_path: str | Path | None = None) -> Path:
    """Export a HuggingFace-shaped directory.

    The weights are real safetensors, the tokenizer is MiniVerl's character
    tokenizer, and the config records the architecture.  This is *not* a
    drop-in HF checkpoint for a production model; it is the same export shape
    that verl's model merger produces, scaled down for inspection.
    """
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    write_safetensors(directory / "model.safetensors", state)
    if config_path is not None and Path(config_path).exists():
        shutil.copyfile(config_path, directory / "config.yaml")
    manifest = {
        "format": "mini_verl",
        "tensors": sorted(state),
        "num_parameters": int(sum(value.size for value in state.values())),
        "note": "MiniVerl export; not a production HF checkpoint",
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return directory
