"""Worker base class and simulated collective operations."""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

__all__ = ["Worker", "CollectiveError"]


class CollectiveError(RuntimeError):
    pass


class Worker:
    """One logical process in a worker group.

    In real verl each of these is a Ray actor on a GPU.  MiniVerl keeps them in
    one process but gives each worker its own model replica, rank and a
    reference to the group, so dispatch/collect semantics are identical.
    """

    def __init__(self, rank: int = 0, world_size: int = 1, local_rank: int = 0, group: Any = None, **kwargs):
        self.rank = rank
        self.world_size = world_size
        self.local_rank = local_rank
        self.group = group
        self._failed = False
        self._init_kwargs = kwargs

    # ------------------------------------------------------------------
    # collectives (simulated by the group)
    # ------------------------------------------------------------------
    def all_reduce(self, array: np.ndarray, op: str = "mean") -> np.ndarray:
        if self.group is None:
            return np.asarray(array, dtype=np.float32)
        return self.group._all_reduce(array, op=op)

    def all_gather(self, array: np.ndarray) -> list[np.ndarray]:
        if self.group is None:
            return [np.asarray(array)]
        return self.group._all_gather(array)

    def broadcast(self, array: np.ndarray, src: int = 0) -> np.ndarray:
        if self.group is None:
            return np.asarray(array)
        return self.group._broadcast(array, src=src)

    def barrier(self) -> None:
        if self.group is not None:
            self.group._barrier()

    # ------------------------------------------------------------------
    # failure injection for tests
    # ------------------------------------------------------------------
    def fail_once(self) -> None:
        self._failed = True

    def _maybe_fail(self) -> None:
        if self._failed:
            self._failed = False
            raise RuntimeError(f"worker rank {self.rank} failed (injected)")

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(rank={self.rank}/{self.world_size})"
