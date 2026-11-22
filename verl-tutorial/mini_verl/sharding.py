"""Weight layouts, resharding and checkpoint-engine simulation.

Training layout in MiniVerl is replicated dense (FSDP-like semantics via
gradient all-reduce).  Rollout may be TP-sharded.  This module makes the
reshard explicit and measures the bytes that a real 3D-HybridEngine-style
transfer would move.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

__all__ = [
    "SyncStats",
    "flatten_state_dict",
    "unflatten_state_dict",
    "fsdp_shard",
    "fsdp_gather",
    "tp_shard_state_dict",
    "tp_gather_state_dict",
    "CheckpointEngineManager",
]


@dataclass
class SyncStats:
    backend: str = "naive"
    tensors_sent: int = 0
    bytes_sent: int = 0
    changed_ratio: float = 0.0
    sync_time_ms: float = 0.0
    version: int = 0

    def as_dict(self) -> dict[str, float]:
        return {
            "sync/bytes": float(self.bytes_sent),
            "sync/tensors": float(self.tensors_sent),
            "sync/changed_ratio": float(self.changed_ratio),
            "sync/time_ms": float(self.sync_time_ms),
            "sync/version": float(self.version),
        }


def flatten_state_dict(state: dict[str, np.ndarray]) -> tuple[np.ndarray, list[tuple[str, tuple[int, ...], int, int]]]:
    flat = np.concatenate([np.asarray(value).reshape(-1) for _, value in sorted(state.items())])
    layout = []
    offset = 0
    for name, value in sorted(state.items()):
        size = int(np.asarray(value).size)
        layout.append((name, tuple(np.asarray(value).shape), offset, offset + size))
        offset += size
    return flat, layout


def unflatten_state_dict(flat: np.ndarray, layout: list[tuple[str, tuple[int, ...], int, int]]) -> dict[str, np.ndarray]:
    return {name: np.asarray(flat[start:end]).reshape(shape) for name, shape, start, end in layout}


def fsdp_shard(state: dict[str, np.ndarray], world_size: int) -> list[dict[str, np.ndarray]]:
    """Flat-shard every tensor across ranks, as FSDP2 ``fully_shard`` does."""
    if world_size < 1:
        raise ValueError("world_size must be >= 1")
    flat, layout = flatten_state_dict(state)
    if flat.size % world_size:
        padded = np.pad(flat, (0, world_size - flat.size % world_size))
    else:
        padded = flat
    shards = []
    per = padded.size // world_size
    for rank in range(world_size):
        shard = padded[rank * per : (rank + 1) * per]
        shards.append({"__flat__": shard, "__layout__": np.asarray(layout, dtype=object)})
    return shards


def fsdp_gather(shards: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    flat = np.concatenate([shard["__flat__"] for shard in shards])
    layout = [tuple(item) for item in shards[0]["__layout__"]]
    return unflatten_state_dict(flat, layout)


def _split_axis(name: str, shape: tuple[int, ...]) -> int | None:
    if len(shape) != 2:
        return None
    if "c_attn" in name or "fc_in" in name or "qkv" in name or "gate" in name:
        return 1
    if "c_proj" in name or "fc_out" in name or "lm_head" in name or "value_head" in name:
        return 0
    return 1


def tp_shard_state_dict(state: dict[str, np.ndarray], tp_size: int) -> list[dict[str, np.ndarray]]:
    """Megatron-style TP shards.

    Column-parallel layers split the output dim; row-parallel layers split the
    input dim.  Non-2D tensors are replicated.  The point is the layout, not a
    universal model converter: real verl uses mbridge/DTensor for this step.
    """
    shards = [dict() for _ in range(tp_size)]
    replicated_names: list[str] = []
    for name, value in state.items():
        value = np.asarray(value)
        axis = _split_axis(name, value.shape)
        if axis is None or value.shape[axis] % tp_size:
            replicated_names.append(name)
            for rank in range(tp_size):
                shards[rank][name] = value.copy()
            continue
        parts = np.split(value, tp_size, axis=axis)
        for rank in range(tp_size):
            shards[rank][name] = np.ascontiguousarray(parts[rank])
    # Always write the marker, even when empty.  Its presence means "this shard
    # dict was produced by tp_shard_state_dict, so do not guess replication
    # from equal values" — a genuinely split zero tensor has equal shards too.
    marker = np.asarray(replicated_names, dtype=object)
    for rank in range(tp_size):
        shards[rank]["__tp_replicated__"] = marker
    return shards


def tp_gather_state_dict(shards: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    if not shards:
        raise ValueError("no shards")
    merged = {}
    replicated = set(np.asarray(shards[0].get("__tp_replicated__", [])).tolist())
    for name in shards[0]:
        if name == "__tp_replicated__":
            continue
        values = [shard[name] for shard in shards]
        if name in replicated:
            merged[name] = values[0].copy()
            continue
        # Backwards-compatible fallback for shard dicts without the marker.
        if "__tp_replicated__" not in shards[0] and all(
            value.shape == values[0].shape and np.array_equal(values[0], value) for value in values[1:]
        ):
            merged[name] = values[0].copy()
            continue
        axis = _split_axis(name, values[0].shape)
        if axis is None:
            merged[name] = values[0].copy()
        else:
            merged[name] = np.concatenate(values, axis=axis)
    return merged


class CheckpointEngineManager:
    """Push actor weights to rollout replicas.

    Backends mirror the names in verl v0.9.1's ``checkpoint_engine``:
    ``naive`` (per-tensor copy), ``nccl`` (full broadcast), ``delta`` (only
    changed tensors) and ``delta_sharded`` (element-granular accounting with
    index overhead; no full gather in the model).
    """

    def __init__(self, backend: str = "naive", replicas: int = 1):
        if backend not in {"naive", "nccl", "delta", "delta_sharded"}:
            raise ValueError(f"unknown checkpoint engine backend {backend!r}")
        self.backend = backend
        self.replicas = max(1, replicas)
        self._previous: dict[str, np.ndarray] | None = None
        self.version = 0
        self.last_stats = SyncStats(backend=backend)

    def update_weights(self, state: dict[str, np.ndarray], rollout_engines: Iterable) -> SyncStats:
        self.version += 1
        total_bytes = sum(np.asarray(value).nbytes for value in state.values())
        delta_mode = self.backend in {"delta", "delta_sharded"} and self._previous is not None
        if delta_mode:
            payload: dict[str, np.ndarray] = {}
            changed_elements = 0
            for name, value in state.items():
                current = np.asarray(value)
                previous = self._previous.get(name) if self._previous is not None else None
                if previous is None or not np.array_equal(current, previous):
                    payload[name] = current
                    if previous is not None:
                        changed_elements += int(np.count_nonzero(current != previous))
                    else:
                        changed_elements += int(current.size)
            if self.backend == "delta":
                # Tensor-granular: every changed tensor crosses the wire whole.
                bytes_sent = sum(np.asarray(value).nbytes for value in payload.values())
            else:
                # Element-granular: only changed values, plus a modest index
                # overhead.  This is the accounting that makes sparse updates
                # (LoRA, optimizer deltas) look qualitatively different.
                index_overhead = 1.25
                bytes_sent = max(1, int(changed_elements * np.dtype(np.float32).itemsize * index_overhead))
                # Element metadata cannot cost more than sending the full state.
                bytes_sent = min(bytes_sent, total_bytes)
            changed_ratio = min(1.0, bytes_sent / max(total_bytes, 1))
            send_state = payload
        else:
            payload = state
            bytes_sent = total_bytes
            changed_ratio = 1.0
            send_state = state

        for engine in rollout_engines:
            if delta_mode:
                engine.update_weights_partial(send_state)
            else:
                engine.update_weights(send_state)
        self._previous = {name: value.copy() for name, value in state.items()}
        # NCCL is modeled as one broadcast; naive pays a per-tensor copy per
        # replica.  The absolute times are deliberately synthetic.
        time_ms = 0.05 * total_bytes / 1e6 if self.backend in {"nccl", "delta", "delta_sharded"} else 0.2 * total_bytes / 1e6
        self.last_stats = SyncStats(
            backend=self.backend,
            tensors_sent=len(payload),
            bytes_sent=bytes_sent * self.replicas,
            changed_ratio=changed_ratio,
            sync_time_ms=time_ms * self.replicas,
            version=self.version,
        )
        return self.last_stats

    def sleep(self, rollout_engines: Iterable) -> None:
        for engine in rollout_engines:
            engine.sleep()

    def wake_up(self, rollout_engines: Iterable) -> None:
        for engine in rollout_engines:
            engine.wake_up()
