"""Content-addressed paged KV blocks.

This is the part of a serving engine that turns a flat KV cache into a page
table.  A block is the unit of allocation, prefix sharing and eviction.  A
block may be referenced by several sequences (prefix cache hit); writers use
copy-on-write when they need to extend a shared partial block.
"""

from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass

from .errors import OutOfBlocksError


def compute_block_hash(prev_hash: int, token_ids: tuple[int, ...]) -> int:
    payload = struct.pack(f"<{len(token_ids)}I", *[int(t) & 0xFFFFFFFF for t in token_ids])
    digest = hashlib.blake2b(
        prev_hash.to_bytes(8, "little") + payload, digest_size=8
    ).digest()
    return int.from_bytes(digest, "little")


@dataclass
class Block:
    block_id: int
    ref_count: int = 0
    block_hash: int | None = None
    token_ids: tuple[int, ...] = ()
    last_used: float = 0.0

    @property
    def cached(self) -> bool:
        return self.block_hash is not None


class BlockManager:
    def __init__(self, num_blocks: int, block_size: int, *, enable_prefix_cache: bool = True) -> None:
        if num_blocks < 1:
            raise ValueError("num_blocks must be positive")
        self.num_blocks = num_blocks
        self.block_size = block_size
        self.enable_prefix_cache = enable_prefix_cache
        self.blocks = [Block(i) for i in range(num_blocks)]
        self._hash_to_block: dict[int, int] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.evictions = 0
        self.allocations = 0
        self.frees = 0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def num_free_blocks(self) -> int:
        """Blocks the allocator may hand out.

        A cached block with ``ref_count == 0`` is "free" in this sense: the
        prefix cache is an optimisation, and the allocator may evict it.
        """
        return sum(1 for b in self.blocks if b.ref_count == 0)

    def num_resident_blocks(self) -> int:
        """Blocks that currently hold KV data, cached or referenced.

        This is the physical occupancy signal.  Counting only referenced
        blocks hides prefix-cached KV: after a request finishes, its blocks
        stay resident and can consume most of HBM while every "used" gauge
        drops to zero.
        """
        return sum(1 for b in self.blocks if b.ref_count > 0 or b.cached)

    def num_used_blocks(self) -> int:
        """Alias for :meth:`num_resident_blocks`, kept for callers."""
        return self.num_resident_blocks()

    def num_cached_blocks(self) -> int:
        return sum(1 for b in self.blocks if b.ref_count == 0 and b.cached)

    def can_allocate(self, num_blocks: int) -> bool:
        return self.num_free_blocks() >= num_blocks

    def get_block(self, block_id: int) -> Block:
        return self.blocks[block_id]

    # ------------------------------------------------------------------
    # Allocation
    # ------------------------------------------------------------------
    def _take_free_block(self) -> Block:
        candidates = [b for b in self.blocks if b.ref_count == 0]
        if not candidates:
            raise OutOfBlocksError("no free KV blocks")
        # Evict the least recently used block first.  A cached block is also
        # evictable: the prefix cache is an optimisation, not a guarantee.
        victim = min(candidates, key=lambda b: (b.last_used, b.block_id))
        if victim.cached:
            self.evictions += 1
            if self._hash_to_block.get(victim.block_hash) == victim.block_id:
                self._hash_to_block.pop(victim.block_hash, None)
                # Duplicate hashes are possible (the same prefix registered
                # by more than one sequence).  Re-point the index instead of
                # losing the surviving copy.
                duplicate = self._find_cached(victim.block_hash)
                if duplicate is not None:
                    self._hash_to_block[victim.block_hash] = duplicate
        victim.block_hash = None
        victim.token_ids = ()
        victim.ref_count = 1
        victim.last_used = time.perf_counter()
        self.allocations += 1
        return victim

    def allocate(self, num_blocks: int) -> list[int]:
        if num_blocks < 0:
            raise ValueError("num_blocks must be non-negative")
        if not self.can_allocate(num_blocks):
            raise OutOfBlocksError(
                f"requested {num_blocks} blocks, only {self.num_free_blocks()} free"
            )
        return [self._take_free_block().block_id for _ in range(num_blocks)]

    def allocate_single(self) -> int:
        return self.allocate(1)[0]

    def share(self, block_id: int) -> None:
        block = self.blocks[block_id]
        if block.ref_count < 0:
            raise ValueError("invalid block reference count")
        # A cached but unreferenced block is pinned by the first sharer.
        block.ref_count += 1
        block.last_used = time.perf_counter()

    def free(self, block_ids: list[int]) -> None:
        for block_id in block_ids:
            block = self.blocks[block_id]
            if block.ref_count <= 0:
                raise ValueError(f"double free of block {block_id}")
            block.ref_count -= 1
            block.last_used = time.perf_counter()
            if block.ref_count == 0:
                self.frees += 1

    # ------------------------------------------------------------------
    # Prefix cache
    # ------------------------------------------------------------------
    def _find_cached(self, block_hash: int, *, exclude: int | None = None) -> int | None:
        for block in self.blocks:
            if block.block_id == exclude:
                continue
            if block.cached and block.block_hash == block_hash:
                return block.block_id
        return None

    def lookup(self, block_hash: int) -> int | None:
        if not self.enable_prefix_cache:
            return None
        block_id = self._hash_to_block.get(block_hash)
        if block_id is not None:
            block = self.blocks[block_id]
            if block.block_hash == block_hash and block.cached:
                self.cache_hits += 1
                return block_id
            # Stale index entry (the block was evicted and reused).
            self._hash_to_block.pop(block_hash, None)
        # The index may have been dropped when one of several duplicate
        # blocks was evicted; fall back to a scan before declaring a miss.
        duplicate = self._find_cached(block_hash)
        if duplicate is not None:
            self._hash_to_block[block_hash] = duplicate
            self.cache_hits += 1
            return duplicate
        self.cache_misses += 1
        return None

    def register(self, block_id: int, block_hash: int, token_ids: tuple[int, ...]) -> None:
        if not self.enable_prefix_cache:
            return
        block = self.blocks[block_id]
        block.block_hash = block_hash
        block.token_ids = tuple(token_ids)
        block.last_used = time.perf_counter()
        self._hash_to_block[block_hash] = block_id

    def prefix_hashes(
        self, token_ids: list[int], max_tokens: int | None = None
    ) -> list[tuple[int, tuple[int, ...]]]:
        """Return hashes for every complete block in a prefix.

        The hash of a block includes the hash of the previous block, so two
        sequences only share a block when their *entire* prefix matches.
        """
        limit = len(token_ids) if max_tokens is None else min(len(token_ids), max_tokens)
        result: list[tuple[int, tuple[int, ...]]] = []
        prev = 0
        for start in range(0, limit - self.block_size + 1, self.block_size):
            chunk = tuple(token_ids[start : start + self.block_size])
            prev = compute_block_hash(prev, chunk)
            result.append((prev, chunk))
        return result

    def match_prefix(self, token_ids: list[int]) -> tuple[int, list[int], int]:
        """Match the longest full-block prefix.

        Returns ``(num_tokens, block_ids, last_hash)``.  The returned block ids
        have already been referenced once for the caller.
        """
        if not self.enable_prefix_cache:
            return 0, [], 0
        matched: list[int] = []
        prev = 0
        for chunk_start in range(0, len(token_ids) - self.block_size + 1, self.block_size):
            chunk = tuple(token_ids[chunk_start : chunk_start + self.block_size])
            prev = compute_block_hash(prev, chunk)
            block_id = self.lookup(prev)
            if block_id is None:
                break
            block = self.blocks[block_id]
            if block.token_ids != chunk:
                self._hash_to_block.pop(prev, None)
                break
            self.share(block_id)
            matched.append(block_id)
        return len(matched) * self.block_size, matched, prev

    def stats(self) -> dict[str, int]:
        return {
            "blocks": self.num_blocks,
            # ``free_blocks`` is allocatable capacity (cached blocks may be
            # evicted); ``resident_blocks`` is physical HBM occupancy.
            "free_blocks": self.num_free_blocks(),
            "allocatable_blocks": self.num_free_blocks(),
            "used_blocks": self.num_used_blocks(),
            "resident_blocks": self.num_resident_blocks(),
            "cached_blocks": self.num_cached_blocks(),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "evictions": self.evictions,
            "allocations": self.allocations,
            "frees": self.frees,
        }
