"""Paged KV cache and a small host-side KV block store.

The GPU cache is represented by NumPy arrays; ``KVBlockStore`` models the
3FS-backed persistent cache described in DeepInfer's deployment notes.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from pathlib import Path
import numpy as np

from .block_manager import BlockManager
from .errors import OutOfBlocksError
from .sequence import Sequence


class PagedKVCache:
    """A page table over per-layer K/V arrays.

    Shape of each layer: ``[num_blocks, block_size, n_kv_heads, head_dim]``.
    The implementation gathers blocks per sequence before attention.  A real
    engine would keep the page table on the device and let the attention
    kernel gather directly; doing it in NumPy keeps the teaching code clear.
    """

    def __init__(
        self,
        num_layers: int,
        block_manager: BlockManager,
        n_kv_heads: int,
        head_dim: int,
        *,
        dtype: np.dtype = np.float32,
    ) -> None:
        self.block_manager = block_manager
        self.num_layers = num_layers
        self.block_size = block_manager.block_size
        self.n_kv_heads = n_kv_heads
        self.head_dim = head_dim
        self.dtype = np.dtype(dtype)
        shape = (block_manager.num_blocks, self.block_size, n_kv_heads, head_dim)
        self.k = np.zeros((num_layers, *shape), dtype=self.dtype)
        self.v = np.zeros((num_layers, *shape), dtype=self.dtype)

    # ------------------------------------------------------------------
    # Capacity and copy-on-write
    # ------------------------------------------------------------------
    def required_blocks(self, num_tokens: int) -> int:
        return (num_tokens + self.block_size - 1) // self.block_size

    def _cow_if_shared(self, seq: Sequence, block_index: int) -> None:
        block_id = seq.block_table[block_index]
        block = self.block_manager.blocks[block_id]
        if block.ref_count <= 1:
            return
        new_id = self.block_manager.allocate_single()
        self.k[:, new_id] = self.k[:, block_id]
        self.v[:, new_id] = self.v[:, block_id]
        self.block_manager.free([block_id])
        seq.block_table[block_index] = new_id

    def ensure_capacity(self, seq: Sequence, target_tokens: int) -> None:
        """Allocate pages for ``target_tokens`` total tokens.

        A partial last page that is shared with another sequence must be
        copied before new tokens are appended.  Prefix-cache pages are full,
        so they are never written through.
        """
        if target_tokens <= 0:
            return
        needed = self.required_blocks(target_tokens)
        missing = needed - len(seq.block_table)
        if missing > 0:
            seq.block_table.extend(self.block_manager.allocate(missing))
        # The last page may receive more tokens.  A full page is immutable;
        # a partial page shared with another sequence needs copy-on-write.
        if target_tokens % self.block_size != 0:
            last_index = needed - 1
            if last_index >= 0 and self.block_manager.blocks[seq.block_table[last_index]].ref_count > 1:
                self._cow_if_shared(seq, last_index)

    # ------------------------------------------------------------------
    # Read/write
    # ------------------------------------------------------------------
    def write_layer(
        self,
        layer: int,
        seq: Sequence,
        start_pos: int,
        k: np.ndarray,
        v: np.ndarray,
    ) -> None:
        """Write one layer's K/V; ``k``/``v`` are ``[n, Hkv, D]``."""
        n = k.shape[0]
        self.ensure_capacity(seq, start_pos + n)
        for offset in range(n):
            pos = start_pos + offset
            block_index = pos // self.block_size
            block_offset = pos % self.block_size
            block_id = seq.block_table[block_index]
            if self.block_manager.blocks[block_id].ref_count > 1:
                self._cow_if_shared(seq, block_index)
                block_id = seq.block_table[block_index]
            self.k[layer, block_id, block_offset] = k[offset]
            self.v[layer, block_id, block_offset] = v[offset]

    def gather(self, seq: Sequence, end_pos: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Return all layers as ``[n_layers, T, Hkv, D]``.

        ``gather_layer`` is the hot-path helper used by the model; this method
        is useful for tests, debugging and KV offload.
        """
        end = seq.num_computed_tokens if end_pos is None else end_pos
        if end <= 0:
            return (
                np.zeros((self.num_layers, 0, self.n_kv_heads, self.head_dim), dtype=self.dtype),
                np.zeros((self.num_layers, 0, self.n_kv_heads, self.head_dim), dtype=self.dtype),
            )
        needed = self.required_blocks(end)
        if needed > len(seq.block_table):
            raise OutOfBlocksError("sequence has fewer pages than requested tokens")
        ks: list[np.ndarray] = []
        vs: list[np.ndarray] = []
        remaining = end
        for block_id in seq.block_table[:needed]:
            take = min(self.block_size, remaining)
            ks.append(self.k[:, block_id, :take])
            vs.append(self.v[:, block_id, :take])
            remaining -= take
            if remaining <= 0:
                break
        # [L, block, H, D] concatenated along the token axis.
        return np.concatenate(ks, axis=2), np.concatenate(vs, axis=2)

    def gather_layer(
        self, layer: int, seq: Sequence, end_pos: int | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        end = seq.num_computed_tokens if end_pos is None else end_pos
        if end <= 0:
            return (
                np.zeros((0, self.n_kv_heads, self.head_dim), dtype=self.dtype),
                np.zeros((0, self.n_kv_heads, self.head_dim), dtype=self.dtype),
            )
        needed = self.required_blocks(end)
        if needed > len(seq.block_table):
            raise OutOfBlocksError("sequence has fewer pages than requested tokens")
        ks: list[np.ndarray] = []
        vs: list[np.ndarray] = []
        remaining = end
        for block_id in seq.block_table[:needed]:
            take = min(self.block_size, remaining)
            ks.append(self.k[layer, block_id, :take])
            vs.append(self.v[layer, block_id, :take])
            remaining -= take
            if remaining <= 0:
                break
        return np.concatenate(ks, axis=0), np.concatenate(vs, axis=0)

    def free_sequence(self, seq: Sequence) -> None:
        if seq.block_table:
            self.block_manager.free(seq.block_table)
            seq.block_table = []

    def memory_bytes(self) -> int:
        """Allocated capacity, not the bytes currently referenced by requests."""
        return int(self.k.nbytes + self.v.nbytes)

    def used_bytes(self) -> int:
        """Bytes physically holding KV, including unreferenced cached blocks.

        A prefix-cached block with ``ref_count == 0`` is still resident in
        HBM.  Reporting it as free makes a nearly full cache look empty.
        """
        total = self.k.nbytes + self.v.nbytes
        per_block = total // max(1, self.block_manager.num_blocks)
        return int(per_block * self.block_manager.num_resident_blocks())

    def occupancy(self) -> float:
        return self.block_manager.num_resident_blocks() / self.block_manager.num_blocks


class KVBlockStore:
    """A tiny content-addressed KV store with TTL and LRU eviction.

    It models the *interface* of the 3FS-backed KV cache: blocks are written
    asynchronously, fetched by hash, and reclaimed by an LRU/GC policy.  The
    storage backend is a local directory so the behaviour is testable.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        capacity: int = 2048,
        ttl_s: float = 3600.0,
        clock=time.monotonic,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.capacity = capacity
        self.ttl_s = ttl_s
        self.clock = clock
        self._entries: OrderedDict[str, tuple[Path, float]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.evictions = 0

    @staticmethod
    def key_for(block_hash: int, layer: int) -> str:
        return f"{block_hash:016x}-{layer}"

    def put(self, key: str, array: np.ndarray) -> None:
        with self._lock:
            path = self.root / f"{key}.npy"
            np.save(path, array)
            self._entries[key] = (path, self.clock())
            self._entries.move_to_end(key)
            self.writes += 1
            self._evict()

    def get(self, key: str) -> np.ndarray | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            path, created = entry
            if self.clock() - created > self.ttl_s:
                self._entries.pop(key, None)
                path.unlink(missing_ok=True)
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return np.load(path)

    def _evict(self) -> None:
        while len(self._entries) > self.capacity:
            _, (path, _) = self._entries.popitem(last=False)
            path.unlink(missing_ok=True)
            self.evictions += 1

    def stats(self) -> dict[str, float | int]:
        total = self.hits + self.misses
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": (self.hits / total) if total else 0.0,
            "writes": self.writes,
            "evictions": self.evictions,
        }

    def clear(self) -> None:
        with self._lock:
            for path, _ in self._entries.values():
                path.unlink(missing_ok=True)
            self._entries.clear()
