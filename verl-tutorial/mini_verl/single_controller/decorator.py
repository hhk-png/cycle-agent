"""Dispatch decorators mirroring ``verl.single_controller.base.decorator``."""

from __future__ import annotations

import enum
import functools
from typing import Callable

__all__ = ["Dispatch", "register", "get_dispatch_mode"]


class Dispatch(enum.Enum):
    RANK_ZERO = "rank_zero"
    ONE_TO_ALL = "one_to_all"
    ALL_TO_ALL = "all_to_all"
    DP_COMPUTE = "dp_compute"
    DP_COMPUTE_PROTO = "dp_compute_proto"


def register(
    dispatch_mode: Dispatch = Dispatch.ALL_TO_ALL,
    blocking: bool = True,
    sync_gradients: bool = False,
):
    """Attach dispatch metadata to a worker method.

    ``blocking=False`` is accepted for API parity with verl's async RPCs; the
    local worker group still returns a future object that callers can ``.get()``.

    ``sync_gradients=True`` marks a data-parallel training RPC.  Each worker
    runs forward/backward on its own chunk and leaves gradients pending; after
    all workers return, the group averages gradients, applies the optimizer
    step on every replica and merges the post-step metrics.  This two-phase
    shape is the in-process analogue of NCCL all-reduce inside a Ray worker
    group.
    """

    def decorator(fn: Callable):
        fn._dispatch_mode = dispatch_mode  # type: ignore[attr-defined]
        fn._blocking = blocking  # type: ignore[attr-defined]
        fn._sync_gradients = sync_gradients  # type: ignore[attr-defined]
        return fn

    return decorator


def get_dispatch_mode(fn: Callable) -> Dispatch:
    return getattr(fn, "_dispatch_mode", Dispatch.ALL_TO_ALL)
