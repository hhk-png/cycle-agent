"""A host/disk KV block store with TTL and LRU eviction."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from _common import add_project_root

add_project_root()

from mini_deepinfer.cache_engine import KVBlockStore


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        now = [0.0]
        store = KVBlockStore(Path(tmp), capacity=3, ttl_s=10, clock=lambda: now[0])
        for index in range(3):
            store.put(f"block-{index}", np.full((2, 2), index, dtype=np.float32))

        # Touch block-0 so block-1 becomes the least-recently-used entry.
        print("get block-0:", store.get("block-0")[0, 0])
        store.put("block-3", np.full((2, 2), 3, dtype=np.float32))
        print("after LRU insert, block-1:", store.get("block-1"))
        print("LRU stats:", store.stats())

        # All remaining entries were written at t=0 with a 10s TTL.
        now[0] = 11
        print("after TTL, block-0:", store.get("block-0"))
        print("TTL stats:", store.stats())
        print("files:", sorted(path.name for path in Path(tmp).glob("*.npy")))


if __name__ == "__main__":
    main()
