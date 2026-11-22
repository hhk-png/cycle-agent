"""A small producer/consumer data plane modeled after verl's TransferQueue.

The production system (``verl/utils/transferqueue_utils.py`` plus the
``transfer_queue`` config tree) separates *trajectory production* from
*training consumption*: an AgentLoop/rollouter puts finished trajectories into
a queue, and the trainer pulls enough samples for one update.  This module keeps
the observable contract:

* entries carry a policy version and production timestamp;
* ``put`` respects a capacity policy (drop oldest or reject);
* ``get`` can return a partial batch and leaves the remainder queued;
* snapshots let a restarted trainer resume without duplicating samples.

It is intentionally in-process: MiniVerl teaches the semantics, not the
transport (Ray object store / Mooncake / shared memory).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from .protocol import DataProto

__all__ = ["QueueEmpty", "QueueFull", "QueueEntry", "TransferQueue"]


class QueueEmpty(RuntimeError):
    pass


class QueueFull(RuntimeError):
    pass


@dataclass
class QueueEntry:
    data: DataProto
    version: int = 0
    produced_at: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def copy(self) -> "QueueEntry":
        return QueueEntry(
            data=self.data.copy(),
            version=self.version,
            produced_at=self.produced_at,
            metadata=dict(self.metadata),
        )


class TransferQueue:
    def __init__(self, max_batches: int = 0, drop_policy: str = "drop_oldest"):
        if drop_policy not in {"drop_oldest", "block"}:
            raise ValueError("drop_policy must be 'drop_oldest' or 'block'")
        self.max_batches = max(0, int(max_batches))
        self.drop_policy = drop_policy
        self.entries: list[QueueEntry] = []
        self.produced_samples = 0
        self.consumed_samples = 0
        self.dropped_samples = 0
        self.put_calls = 0
        self.get_calls = 0
        self.blocked = 0

    # ------------------------------------------------------------------
    # producer side
    # ------------------------------------------------------------------
    def put(
        self,
        data: DataProto,
        version: int = 0,
        produced_at: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self.put_calls += 1
        if self.max_batches and len(self.entries) >= self.max_batches:
            if self.drop_policy == "block":
                self.blocked += 1
                raise QueueFull(f"queue holds {len(self.entries)} batches (max={self.max_batches})")
            evicted = self.entries.pop(0)
            self.dropped_samples += len(evicted.data)
        self.entries.append(
            QueueEntry(data=data, version=version, produced_at=produced_at, metadata=dict(metadata or {}))
        )
        self.produced_samples += len(data)
        return len(data)

    # ------------------------------------------------------------------
    # consumer side
    # ------------------------------------------------------------------
    def qsize(self) -> int:
        return len(self.entries)

    def total_samples(self) -> int:
        return sum(len(entry.data) for entry in self.entries)

    def get(self, batch_size: int | None = None) -> DataProto:
        """Pull ``batch_size`` samples (or one whole entry when omitted).

        Raises ``QueueEmpty`` instead of blocking.  A trainer that wants to wait
        should retry at its own cadence, which is exactly the ``sleep/resume``
        loop used by the fully-async trainer.
        """

        self.get_calls += 1
        if not self.entries:
            raise QueueEmpty("queue is empty")
        if batch_size is None:
            entry = self.entries.pop(0)
            self.consumed_samples += len(entry.data)
            return entry.data
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.total_samples() < batch_size:
            raise QueueEmpty(f"need {batch_size} samples, have {self.total_samples()}")

        selected: list[DataProto] = []
        versions: list[int] = []
        produced_at: list[int] = []
        while self.entries and sum(len(item) for item in selected) < batch_size:
            entry = self.entries.pop(0)
            take = min(len(entry.data), batch_size - sum(len(item) for item in selected))
            selected.append(entry.data[:take])
            versions.extend([entry.version] * take)
            produced_at.extend([entry.produced_at] * take)
            if take < len(entry.data):
                remainder = entry.copy()
                remainder.data = entry.data[take:]
                self.entries.insert(0, remainder)
        for item in selected:
            for key in ("queue_version", "queue_produced_at"):
                item.batch.pop(key, None)
        data = DataProto.concat(selected)
        data.batch["queue_version"] = np.asarray(versions, dtype=np.int64)
        data.batch["queue_produced_at"] = np.asarray(produced_at, dtype=np.int64)
        self.consumed_samples += len(data)
        return data

    def try_get(self, batch_size: int | None = None) -> DataProto | None:
        try:
            return self.get(batch_size)
        except QueueEmpty:
            return None

    def clear(self) -> None:
        self.dropped_samples += self.total_samples()
        self.entries.clear()

    # ------------------------------------------------------------------
    # snapshot / restore
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "entries": [entry.copy() for entry in self.entries],
            "produced_samples": self.produced_samples,
            "consumed_samples": self.consumed_samples,
            "dropped_samples": self.dropped_samples,
            "put_calls": self.put_calls,
            "get_calls": self.get_calls,
            "max_batches": self.max_batches,
            "drop_policy": self.drop_policy,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.entries = [entry.copy() for entry in snapshot.get("entries", [])]
        self.produced_samples = int(snapshot.get("produced_samples", 0))
        self.consumed_samples = int(snapshot.get("consumed_samples", 0))
        self.dropped_samples = int(snapshot.get("dropped_samples", 0))
        self.put_calls = int(snapshot.get("put_calls", 0))
        self.get_calls = int(snapshot.get("get_calls", 0))
        self.max_batches = int(snapshot.get("max_batches", self.max_batches))
        self.drop_policy = str(snapshot.get("drop_policy", self.drop_policy))

    def stats(self) -> dict[str, float]:
        return {
            "tq/batches": float(len(self.entries)),
            "tq/samples": float(self.total_samples()),
            "tq/produced_samples": float(self.produced_samples),
            "tq/consumed_samples": float(self.consumed_samples),
            "tq/dropped_samples": float(self.dropped_samples),
            "tq/put_calls": float(self.put_calls),
            "tq/get_calls": float(self.get_calls),
            "tq/blocked": float(self.blocked),
        }

    # ------------------------------------------------------------------
    # convenience
    # ------------------------------------------------------------------
    def extend(self, entries: Iterable[DataProto], version: int = 0) -> int:
        total = 0
        for data in entries:
            total += self.put(data, version=version)
        return total

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"TransferQueue(batches={len(self.entries)}, samples={self.total_samples()}, "
            f"max_batches={self.max_batches}, policy={self.drop_policy})"
        )
