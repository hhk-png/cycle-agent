from __future__ import annotations

import numpy as np
import pytest

from mini_verl.protocol import DataProto
from mini_verl.transfer_queue import QueueEmpty, QueueFull, TransferQueue


def make_batch(n: int, start: int = 0) -> DataProto:
    return DataProto(
        batch={"x": np.arange(start, start + n, dtype=np.int64)},
        non_tensor_batch={"uid": np.arange(start, start + n, dtype=np.int64)},
        meta_info={"source": "test"},
    )


def test_transfer_queue_partial_get_and_remainder():
    queue = TransferQueue()
    queue.put(make_batch(3, start=0), version=7, produced_at=10)
    queue.put(make_batch(2, start=3), version=8, produced_at=11)
    data = queue.get(4)
    assert data.batch["x"].tolist() == [0, 1, 2, 3]
    assert data.batch["queue_version"].tolist() == [7, 7, 7, 8]
    assert queue.total_samples() == 1
    rest = queue.get(1)
    assert rest.batch["x"].tolist() == [4]
    assert rest.batch["queue_version"].tolist() == [8]


def test_transfer_queue_capacity_policies():
    queue = TransferQueue(max_batches=1, drop_policy="drop_oldest")
    queue.put(make_batch(2, start=0), version=0)
    queue.put(make_batch(2, start=2), version=1)
    assert queue.qsize() == 1
    assert queue.dropped_samples == 2
    assert queue.get(2).batch["x"].tolist() == [2, 3]

    blocking = TransferQueue(max_batches=1, drop_policy="block")
    blocking.put(make_batch(1, start=0))
    with pytest.raises(QueueFull):
        blocking.put(make_batch(1, start=1))
    assert blocking.stats()["tq/blocked"] == 1.0


def test_transfer_queue_snapshot_restore():
    queue = TransferQueue()
    queue.put(make_batch(3, start=0), version=2, produced_at=5)
    snapshot = queue.snapshot()
    queue.get(2)
    assert queue.total_samples() == 1
    queue.restore(snapshot)
    assert queue.total_samples() == 3
    data = queue.get(1)
    assert data.batch["x"].tolist() == [0]
    assert data.batch["queue_version"].tolist() == [2]


def test_transfer_queue_empty():
    queue = TransferQueue()
    with pytest.raises(QueueEmpty):
        queue.get(1)
    assert queue.try_get(1) is None
