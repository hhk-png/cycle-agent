"""TransferQueue: versioned producer/consumer, partial batches and snapshots."""

import numpy as np

from _common import header

from mini_verl.data import CharTokenizer, build_dataset, collate_fn
from mini_verl.transfer_queue import QueueEmpty, TransferQueue


def main() -> None:
    header("TransferQueue: stream trajectories into the trainer")
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 12, seed=0)
    queue = TransferQueue(max_batches=4, drop_policy="drop_oldest")

    first = collate_fn(dataset.samples[:6], tokenizer, 4, 1)
    second = collate_fn(dataset.samples[6:12], tokenizer, 4, 1)
    queue.put(first, version=3, produced_at=10)
    queue.put(second, version=4, produced_at=11)
    print("queued batches:", queue.qsize(), "samples:", queue.total_samples())

    batch = queue.get(8)
    print("pulled input_ids:", batch.batch["input_ids"].shape)
    print("queue_version:", np.unique(batch.batch["queue_version"]).tolist())
    print("left in queue:", queue.total_samples())

    snapshot = queue.snapshot()
    rest = queue.get(4)
    print("remaining pulled:", len(rest))
    with_empty = queue.try_get(1)
    print("try_get on empty:", with_empty)

    queue.restore(snapshot)
    print("after restore:", queue.total_samples(), "stats:", queue.stats())

    try:
        TransferQueue().get(1)
    except QueueEmpty as exc:
        print("empty queue raises:", type(exc).__name__)


if __name__ == "__main__":
    main()
