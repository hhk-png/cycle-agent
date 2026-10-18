#!/usr/bin/env python3
"""A deterministic model of Ray Data's streaming execution engine.

Run from the tutorial root:

    python examples/data_execution_lab.py

The lab does not import Ray.  It reproduces the mechanics that decide whether a
Ray Data job on KubeRay finishes or OOMs:

* a bounded queue between the source and the operators (backpressure);
* block-oriented ``map_batches`` / ``filter`` stages;
* a hash shuffle that spills when the in-memory buffer exceeds a budget;
* the difference between streaming peak memory and materializing everything.

The numbers are synthetic but the control flow matches the real executor:
produce -> transform -> shuffle -> write, with at most ``queue_depth`` blocks
in flight.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator


@dataclass
class Block:
    block_id: int
    rows: int
    payload_bytes: int
    partition: int = 0


@dataclass
class Stats:
    blocks_in: int = 0
    rows_in: int = 0
    rows_out: int = 0
    blocks_out: int = 0
    max_inflight: int = 0
    spilled_blocks: int = 0
    spilled_bytes: int = 0
    peak_bytes: int = 0
    shuffle_peak_bytes: int = 0
    partitions: int = 0
    bytes_written: int = 0
    stages: dict[str, int] = field(default_factory=dict)


class BoundedQueue:
    """A tiny bounded channel: ``put`` blocks (returns False) when full."""

    def __init__(self, depth: int):
        self.depth = depth
        self._items: list[Block] = []
        self.max_depth = 0

    def put(self, block: Block) -> bool:
        if len(self._items) >= self.depth:
            return False
        self._items.append(block)
        self.max_depth = max(self.max_depth, len(self._items))
        return True

    def get(self) -> Block | None:
        return self._items.pop(0) if self._items else None

    def __len__(self) -> int:
        return len(self._items)


def partition_of(block: Block, partitions: int) -> int:
    digest = hashlib.sha256(str(block.block_id).encode()).digest()
    return int.from_bytes(digest[:4], "big") % partitions


class StreamingPipeline:
    def __init__(
        self,
        *,
        queue_depth: int = 2,
        object_store_budget: int = 2 * 1024 * 1024,
        shuffle_budget: int = 512 * 1024,
        partitions: int = 4,
        keep_ratio: float = 2 / 3,
        map_cost_per_row: float = 0.000002,
    ):
        self.queue_depth = queue_depth
        self.object_store_budget = object_store_budget
        self.shuffle_budget = shuffle_budget
        self.partitions = partitions
        self.keep_ratio = keep_ratio
        self.map_cost_per_row = map_cost_per_row
        self.stats = Stats(partitions=partitions)

    # -------------------------------------------------------------- operators
    def map_batches(self, block: Block) -> Block:
        # One output row per input row, but a new allocation: this is what
        # counts against the object store while the block is alive.
        self.stats.stages["map"] = self.stats.stages.get("map", 0) + 1
        return Block(block.block_id, block.rows, block.payload_bytes)

    def filter_rows(self, block: Block) -> Block:
        self.stats.stages["filter"] = self.stats.stages.get("filter", 0) + 1
        rows = int(block.rows * self.keep_ratio)
        return Block(block.block_id, rows, int(block.payload_bytes * self.keep_ratio))

    def write(self, block: Block) -> None:
        self.stats.stages["write"] = self.stats.stages.get("write", 0) + 1
        self.stats.rows_out += block.rows
        self.stats.blocks_out += 1
        self.stats.bytes_written += block.payload_bytes

    # ------------------------------------------------------------------- run
    def run(self, blocks: Iterable[Block]) -> Stats:
        queue: BoundedQueue = BoundedQueue(self.queue_depth)
        source: Iterator[Block] = iter(blocks)
        shuffle: dict[int, list[Block]] = {index: [] for index in range(self.partitions)}
        spilled: list[Block] = []
        shuffle_bytes = 0
        inflight_bytes = 0
        done = False

        while not done or len(queue):
            # Source stage: stop when the bounded queue applies backpressure.
            while len(queue) < queue.depth:
                try:
                    block = next(source)
                except StopIteration:
                    done = True
                    break
                self.stats.blocks_in += 1
                self.stats.rows_in += block.rows
                queue.put(block)
                inflight_bytes += block.payload_bytes
                self.stats.peak_bytes = max(self.stats.peak_bytes, inflight_bytes)

            block = queue.get()
            if block is None:
                continue

            mapped = self.map_batches(block)
            filtered = self.filter_rows(mapped)
            # rows are counted at write time, not when the block is produced.
            inflight_bytes = max(0, inflight_bytes - block.payload_bytes)

            partition = partition_of(filtered, self.partitions)
            filtered.partition = partition
            shuffle[partition].append(filtered)
            shuffle_bytes += filtered.payload_bytes
            self.stats.shuffle_peak_bytes = max(self.stats.shuffle_peak_bytes, shuffle_bytes)

            # A real shuffle spills when the in-memory buffer crosses the
            # budget.  Spilling is not free: it serializes and later re-reads.
            if shuffle_bytes > self.shuffle_budget:
                flushed = [item for items in shuffle.values() for item in items]
                self.stats.spilled_blocks += len(flushed)
                self.stats.spilled_bytes += sum(item.payload_bytes for item in flushed)
                # Spill to local disk, then read back in the write stage.
                spilled.extend(flushed)
                for index in shuffle:
                    shuffle[index] = []
                shuffle_bytes = 0

            self.stats.max_inflight = max(self.stats.max_inflight, len(queue) + (1 if block else 0))

        # Final write stage reads spilled blocks first, then one partition at
        # a time.  Spilling changes latency and disk IO, not the result.
        for item in spilled:
            self.write(item)
        for index in range(self.partitions):
            for item in shuffle[index]:
                self.write(item)
        return self.stats


def make_blocks(count: int = 240, rows_per_block: int = 250, bytes_per_row: int = 2048) -> list[Block]:
    return [Block(index, rows_per_block, rows_per_block * bytes_per_row) for index in range(count)]


def materialized_peak(blocks: Iterable[Block]) -> int:
    return sum(block.payload_bytes for block in blocks)


def main() -> int:
    blocks = make_blocks()
    total_input = sum(block.payload_bytes for block in blocks)

    print("== Ray Data streaming execution lab ==")
    print(f"blocks={len(blocks)} rows={sum(b.rows for b in blocks)} input={total_input / 1024 / 1024:.1f} MiB")
    print("queue_depth=2 shuffle_budget=0.5 MiB object_store=2 MiB partitions=4")

    pipeline = StreamingPipeline()
    stats = pipeline.run(iter(blocks))

    print("\n-- counters --")
    print(f"rows in/out      : {stats.rows_in} -> {stats.rows_out}")
    print(f"blocks in/out    : {stats.blocks_in} -> {stats.blocks_out}")
    print(f"max queue depth  : {stats.max_inflight} (bounded)")
    print(f"spilled blocks   : {stats.spilled_blocks} ({stats.spilled_bytes / 1024 / 1024:.2f} MiB)")
    print(f"streaming peak   : {stats.peak_bytes / 1024 / 1024:.2f} MiB")
    print(f"shuffle peak     : {stats.shuffle_peak_bytes / 1024 / 1024:.2f} MiB")
    print(f"materialized peak: {total_input / 1024 / 1024:.2f} MiB")
    print(f"stage calls      : {stats.stages}")

    # Truncation happens per block, so the expected total is per-block, not
    # the ratio applied once to the grand total.
    expected_rows = len(blocks) * int(blocks[0].rows * (2 / 3))
    assert stats.rows_out == expected_rows, (stats.rows_out, expected_rows)
    assert stats.blocks_out == len(blocks)
    assert stats.max_inflight <= 2
    assert stats.peak_bytes < total_input, "streaming must not materialize the whole input"
    assert stats.spilled_blocks > 0, "the small shuffle budget must force a spill"
    assert stats.rows_out <= stats.rows_in

    print("\nOK: streaming execution respected backpressure, spilled under budget, and wrote every retained row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
