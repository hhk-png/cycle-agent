"""Resource pool and local worker group with verl-style dispatch modes.

The real ``RayWorkerGroup`` turns a Python method call into N remote calls,
splits ``DataProto`` across ranks, and concatenates the results.  MiniVerl does
exactly the same dispatch/collect logic in-process.  The only deliberate
difference is transport: no serialization, no Ray, no NCCL.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np

from ..protocol import DataProto, pad_dataproto_to_divisor, unpad_dataproto
from .decorator import Dispatch, get_dispatch_mode
from .worker import Worker

__all__ = ["ResourcePool", "LocalWorkerGroup", "LocalFuture"]


@dataclass
class ResourcePool:
    """A tensor of process counts, one entry per node.

    ``ResourcePool([2, 2])`` means 2 workers on each of 2 nodes (world size 4).
    ``n_gpus_per_node`` is kept for validation and memory accounting.
    """

    process_on_nodes: list[int]
    n_gpus_per_node: int = 8
    max_colocate_count: int = 10

    def __post_init__(self) -> None:
        if not self.process_on_nodes:
            raise ValueError("process_on_nodes must not be empty")
        if any(count <= 0 for count in self.process_on_nodes):
            raise ValueError("every node must host at least one process")

    @property
    def world_size(self) -> int:
        return sum(self.process_on_nodes)

    @property
    def store(self) -> list[int]:
        return list(self.process_on_nodes)

    def local_world_size_list(self) -> list[int]:
        return [count for count in self.process_on_nodes for _ in range(count)]

    def local_rank_list(self) -> list[int]:
        return [rank for count in self.process_on_nodes for rank in range(count)]

    @classmethod
    def single_node(cls, world_size: int, n_gpus_per_node: int | None = None) -> "ResourcePool":
        return cls([world_size], n_gpus_per_node=n_gpus_per_node or world_size)


class LocalFuture:
    """Tiny future used by ``blocking=False`` RPCs."""

    def __init__(self, value: Any = None, error: Exception | None = None):
        self._value = value
        self._error = error
        self._event = threading.Event()
        self._event.set()

    @classmethod
    def completed(cls, value: Any) -> "LocalFuture":
        return cls(value=value)

    def get(self) -> Any:
        self._event.wait()
        if self._error is not None:
            raise self._error
        return self._value


class LocalWorkerGroup:
    def __init__(
        self,
        resource_pool: ResourcePool,
        worker_cls: type[Worker],
        init_kwargs: dict[str, Any] | None = None,
        name: str = "worker_group",
    ) -> None:
        self.resource_pool = resource_pool
        self.name = name
        self.workers: list[Worker] = []
        local_sizes = resource_pool.local_world_size_list()
        local_ranks = resource_pool.local_rank_list()
        for rank in range(resource_pool.world_size):
            worker = worker_cls(
                rank=rank,
                world_size=resource_pool.world_size,
                local_rank=local_ranks[rank],
                group=self,
                worker_group_name=name,
                **(init_kwargs or {}),
            )
            self.workers.append(worker)
        # Workers need the group to exist before they can be constructed in some
        # subclasses; attach afterwards as well.
        for worker in self.workers:
            worker.group = self

    @property
    def world_size(self) -> int:
        return len(self.workers)

    # ------------------------------------------------------------------
    # dispatch surface
    # ------------------------------------------------------------------
    def __getattr__(self, item: str):
        if item.startswith("_") or item in {"workers", "resource_pool", "name"}:
            raise AttributeError(item)
        prototype = self.workers[0]
        if not hasattr(prototype, item):
            raise AttributeError(item)
        bound = getattr(prototype, item)
        if not callable(bound):
            raise AttributeError(item)
        dispatch_mode = get_dispatch_mode(getattr(type(prototype), item))
        sync_gradients = bool(getattr(getattr(type(prototype), item), "_sync_gradients", False))

        def call(*args, **kwargs):
            return self._dispatch(item, dispatch_mode, sync_gradients, *args, **kwargs)

        call.__name__ = item  # type: ignore[attr-defined]
        return call

    def _call_one(self, worker: Worker, method: str, *args, **kwargs):
        worker._maybe_fail()
        return getattr(worker, method)(*args, **kwargs)

    def _dispatch(self, method: str, mode: Dispatch, sync_gradients: bool, *args, **kwargs):
        if mode is Dispatch.RANK_ZERO:
            return self._call_one(self.workers[0], method, *args, **kwargs)

        if mode is Dispatch.ONE_TO_ALL:
            outputs = [self._call_one(worker, method, *args, **kwargs) for worker in self.workers]
            return outputs

        if mode in (Dispatch.ALL_TO_ALL, Dispatch.DP_COMPUTE):
            outputs = []
            for rank, worker in enumerate(self.workers):
                rank_args = _take_rank(args, rank, self.world_size)
                rank_kwargs = _take_rank_kwargs(kwargs, rank, self.world_size)
                outputs.append(self._call_one(worker, method, *rank_args, **rank_kwargs))
            if sync_gradients:
                self._sync_gradients_and_step()
                outputs = self._merge_post_step_metrics(outputs)
            return outputs

        if mode is Dispatch.DP_COMPUTE_PROTO:
            # Pad so every rank gets exactly the same number of samples.
            padded_args, pad = _pad_args(args, self.world_size)
            padded_kwargs, pad_kw = _pad_kwargs(kwargs, self.world_size)
            pad = max(pad, pad_kw)
            chunks_per_arg = [_chunk_dataproto(arg, self.world_size) for arg in padded_args]
            chunks_per_kwarg = {key: _chunk_dataproto(value, self.world_size) for key, value in padded_kwargs.items()}

            outputs = []
            for rank, worker in enumerate(self.workers):
                rank_args = tuple(chunks[rank] for chunks in chunks_per_arg)
                rank_kwargs = {key: chunks[rank] for key, chunks in chunks_per_kwarg.items()}
                outputs.append(self._call_one(worker, method, *rank_args, **rank_kwargs))

            if sync_gradients:
                self._sync_gradients_and_step()
                outputs = self._merge_post_step_metrics(outputs)

            collected = _collect(outputs)
            if isinstance(collected, DataProto):
                collected = unpad_dataproto(collected, pad)
            return collected

        raise ValueError(f"unsupported dispatch mode {mode}")

    # ------------------------------------------------------------------
    # gradient synchronisation (simulated all-reduce)
    # ------------------------------------------------------------------
    def _sync_gradients_and_step(self) -> None:
        trainers = [worker for worker in self.workers if hasattr(worker, "engine")]
        if len(trainers) != len(self.workers):
            raise RuntimeError("sync_gradients requires every worker to expose an engine")
        named_grads: list[dict[str, np.ndarray]] = [worker.engine.get_gradients() for worker in trainers]
        keys = set(named_grads[0])
        for grads in named_grads[1:]:
            if set(grads) != keys:
                raise RuntimeError("workers disagree on parameter names during gradient sync")
        averaged: dict[str, np.ndarray] = {}
        for key in keys:
            stack = np.stack([grads[key] for grads in named_grads], axis=0)
            averaged[key] = stack.mean(axis=0)
        for worker in trainers:
            worker.engine.set_gradients(averaged)
            worker.finalize_update()

    def _merge_post_step_metrics(self, outputs: list[Any]) -> list[Any]:
        merged: list[Any] = []
        for output, worker in zip(outputs, self.workers):
            metrics = dict(output or {})
            metrics.update(worker.post_update_metrics())
            merged.append(metrics)
        return merged

    # ------------------------------------------------------------------
    # collectives used by tests and by the parallel simulator
    # ------------------------------------------------------------------
    def all_reduce_values(self, values: list[np.ndarray], op: str = "mean") -> np.ndarray:
        if len(values) != self.world_size:
            raise ValueError(f"expected {self.world_size} values, got {len(values)}")
        stack = np.stack([np.asarray(value, dtype=np.float32) for value in values], axis=0)
        if op == "mean":
            return stack.mean(axis=0)
        if op == "sum":
            return stack.sum(axis=0)
        if op == "max":
            return stack.max(axis=0)
        raise ValueError(op)

    def all_gather_values(self, values: list[np.ndarray]) -> list[np.ndarray]:
        if len(values) != self.world_size:
            raise ValueError(f"expected {self.world_size} values, got {len(values)}")
        return [np.asarray(value) for value in values]

    # Placeholders so Worker.all_reduce has a well-defined no-op behaviour in
    # sequential in-process execution.  Real collectives happen in
    # ``_sync_gradients_and_step`` above.
    def _all_reduce(self, array: np.ndarray, op: str = "mean") -> np.ndarray:
        return np.asarray(array, dtype=np.float32)

    def _all_gather(self, array: np.ndarray) -> list[np.ndarray]:
        return [np.asarray(array)]

    def _broadcast(self, array: np.ndarray, src: int = 0) -> np.ndarray:
        return np.asarray(array)

    def _barrier(self) -> None:
        return None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def execute_all_sync(self, method: str, *args, **kwargs) -> list[Any]:
        return self._dispatch(method, Dispatch.ONE_TO_ALL, False, *args, **kwargs)

    def execute_rank_zero_sync(self, method: str, *args, **kwargs) -> Any:
        return self._dispatch(method, Dispatch.RANK_ZERO, False, *args, **kwargs)

    def execute_all_async(self, method: str, *args, **kwargs) -> LocalFuture:
        return LocalFuture.completed(self.execute_all_sync(method, *args, **kwargs))

    def __len__(self) -> int:
        return len(self.workers)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"LocalWorkerGroup(name={self.name}, world_size={len(self.workers)})"


# ----------------------------------------------------------------------
# argument surgery
# ----------------------------------------------------------------------


def _take_rank(args: tuple, rank: int, world_size: int) -> tuple:
    result = []
    for arg in args:
        if isinstance(arg, (list, tuple)) and len(arg) == world_size:
            result.append(arg[rank])
        else:
            result.append(arg)
    return tuple(result)


def _take_rank_kwargs(kwargs: dict, rank: int, world_size: int) -> dict:
    result = {}
    for key, value in kwargs.items():
        if isinstance(value, (list, tuple)) and len(value) == world_size:
            result[key] = value[rank]
        else:
            result[key] = value
    return result


def _pad_args(args: tuple, world_size: int) -> tuple[list[Any], int]:
    padded = []
    pad = 0
    for arg in args:
        if isinstance(arg, DataProto):
            arg, this_pad = pad_dataproto_to_divisor(arg, world_size)
            pad = max(pad, this_pad)
        padded.append(arg)
    return padded, pad


def _pad_kwargs(kwargs: dict, world_size: int) -> tuple[dict[str, Any], int]:
    padded = {}
    pad = 0
    for key, value in kwargs.items():
        if isinstance(value, DataProto):
            value, this_pad = pad_dataproto_to_divisor(value, world_size)
            pad = max(pad, this_pad)
        padded[key] = value
    return padded, pad


def _chunk_dataproto(value: Any, world_size: int) -> list[Any]:
    if isinstance(value, DataProto):
        return value.chunk(world_size)
    return [value for _ in range(world_size)]


def _collect(outputs: list[Any]) -> Any:
    if not outputs:
        return []
    if all(isinstance(output, DataProto) for output in outputs):
        return DataProto.concat(outputs)
    return outputs
