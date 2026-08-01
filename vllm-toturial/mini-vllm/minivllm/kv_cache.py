"""Paged KV cache, block allocator, prefix cache and block manager.

This is the heart of the mini-vLLM: a direct, readable re-implementation of
vLLM's ``PagedAttention`` memory management (``vllm/worker/model_runner.py`` +
``vllm/core/block_manager_v1.py``):

* the KV cache lives in **fixed-size physical blocks** (``block_size`` slots);
* every sequence owns a **logical -> physical block table**;
* blocks are shared via reference counting, which enables **prefix caching**
  and **copy-on-write** when a shared block has to be mutated.
"""

from __future__ import annotations

import collections
from typing import Dict, List, Optional

import numpy as np

from .config import CacheConfig, ModelConfig


class NoFreeBlocksError(RuntimeError):
    """Raised when the KV cache is exhausted."""


class KVStore:
    """The raw GPU/CPU memory that holds keys and values.

    Layout per layer: ``(num_blocks, block_size, num_heads, head_dim)``.
    A real vLLM would allocate this from CUDA memory once at startup and never
    move it; we simply allocate NumPy arrays of the same shape.
    """

    def __init__(self, cache_config: CacheConfig, model_config: ModelConfig):
        self.block_size = cache_config.block_size
        self.num_blocks = cache_config.num_gpu_blocks
        self.num_heads = model_config.n_head
        self.head_dim = model_config.head_dim
        self.num_layers = model_config.n_layer
        self.dtype = np.float32
        shape = (self.num_blocks, self.block_size, self.num_heads, self.head_dim)
        # One array pair per transformer layer, exactly like vLLM's
        # ``kv_caches`` list that is passed into each layer's forward.
        self.k_cache: List[np.ndarray] = [np.zeros(shape, dtype=self.dtype)
                                          for _ in range(self.num_layers)]
        self.v_cache: List[np.ndarray] = [np.zeros(shape, dtype=self.dtype)
                                          for _ in range(self.num_layers)]

    def clear(self) -> None:
        for arr in (*self.k_cache, *self.v_cache):
            arr.fill(0.0)


class BlockAllocator:
    """Manages the set of physical blocks and their reference counts.

    Reference-counting is what makes prefix sharing safe: a block is only
    returned to the free list once *every* owner (active sequences plus the
    prefix cache) has released it.
    """

    def __init__(self, num_blocks: int):
        self.num_blocks = num_blocks
        self.free: List[int] = list(range(num_blocks))
        self.ref_count: List[int] = [0] * num_blocks

    def allocate(self) -> int:
        if not self.free:
            raise NoFreeBlocksError("KV cache is full (no free physical blocks)")
        block_id = self.free.pop()
        self.ref_count[block_id] = 1
        return block_id

    def free_block(self, block_id: int) -> None:
        self.ref_count[block_id] -= 1
        if self.ref_count[block_id] < 0:
            raise AssertionError(f"ref_count of block {block_id} went negative")
        if self.ref_count[block_id] == 0:
            self.free.append(block_id)

    def touch(self, block_id: int) -> None:
        """Add one reference to an already-allocated block (prefix sharing)."""
        self.ref_count[block_id] += 1

    @property
    def num_free(self) -> int:
        return len(self.free)


class PrefixCache:
    """LRU cache mapping token-prefixes to physical block lists.

    When a new prompt shares a prefix with an earlier prompt, the shared part
    can skip prefill entirely.  This is vLLM's ``enable_prefix_caching``
    feature (the hash table in ``vllm/core/prefix_caching_block_manager.py``).
    """

    def __init__(self, allocator: BlockAllocator, max_cached_blocks: int):
        self.allocator = allocator
        self.max_cached_blocks = max_cached_blocks
        self._cache: Dict[tuple, List[int]] = {}
        self._lru: "collections.OrderedDict[tuple, None]" = collections.OrderedDict()
        self._size = 0

    def get(self, prefix_tokens: tuple) -> Optional[List[int]]:
        if prefix_tokens in self._cache:
            self._lru.move_to_end(prefix_tokens)
            return list(self._cache[prefix_tokens])
        return None

    def put(self, prefix_tokens: tuple, block_ids: List[int]) -> None:
        if not block_ids or prefix_tokens in self._cache:
            return
        self._cache[prefix_tokens] = list(block_ids)
        self._lru[prefix_tokens] = None
        for b in block_ids:
            # one extra reference held by the cache itself
            self.allocator.touch(b)
        self._size += len(block_ids)
        while self._size > self.max_cached_blocks:
            self._evict_one()

    def _evict_one(self) -> None:
        if not self._lru:
            return
        key, _ = self._lru.popitem(last=False)
        blocks = self._cache.pop(key)
        for b in blocks:
            self.allocator.free_block(b)
        self._size -= len(blocks)


class BlockManager:
    """Owns the KV store, the allocator and every sequence's block table.

    Public API used by the scheduler and the model runner:

    * ``can_allocate(seq, tokens)`` / ``ensure_blocks(seq, tokens)``
    * ``match_prefix(prompt_ids)``      -- longest cached prefix (if enabled)
    * ``register_prefix(seq)``          -- store a finished prompt in the cache
    * ``write_kv(layer, seq, pos, k, v)`` -- store one token's K/V (with COW)
    * ``release(seq)``                  -- free a finished / preempted sequence
    """

    def __init__(self,
                 cache_config: CacheConfig,
                 model_config: ModelConfig,
                 enable_prefix_caching: bool = True):
        self.cache_config = cache_config
        self.block_size = cache_config.block_size
        self.model_config = model_config
        self.kv_store = KVStore(cache_config, model_config)
        self.allocator = BlockAllocator(cache_config.num_gpu_blocks)
        self.block_tables: Dict[int, List[int]] = {}
        self.enable_prefix_caching = enable_prefix_caching
        # give the prefix cache at most half of the physical blocks
        self.prefix_cache = PrefixCache(self.allocator,
                                        max_cached_blocks=cache_config.num_gpu_blocks // 2) \
            if enable_prefix_caching else None

    # ------------------------------------------------------------------ #
    # free-block bookkeeping
    # ------------------------------------------------------------------ #
    @property
    def num_free_blocks(self) -> int:
        return self.allocator.num_free

    @property
    def num_total_blocks(self) -> int:
        return self.allocator.num_blocks

    # ------------------------------------------------------------------ #
    # block table helpers
    # ------------------------------------------------------------------ #
    def get_block_table(self, seq) -> List[int]:
        return self.block_tables.get(seq.seq_id, [])

    def _nblocks(self, num_tokens: int) -> int:
        return (num_tokens + self.block_size - 1) // self.block_size

    # ------------------------------------------------------------------ #
    # prefix caching
    # ------------------------------------------------------------------ #
    def match_prefix(self, prompt_ids: List[int]):
        """Return ``(prefix_len, block_ids)`` for the longest cached prefix."""
        if self.prefix_cache is None or not prompt_ids:
            return 0, []
        prompt_t = tuple(prompt_ids)
        # longest-first search (O(L) lookups, fine for short prompts)
        for L in range(len(prompt_ids), 0, -1):
            blocks = self.prefix_cache.get(prompt_t[:L])
            if blocks is not None:
                return L, blocks
        return 0, []

    def register_prefix(self, seq) -> None:
        """Cache the KV blocks that hold this sequence's *prompt* prefix.

        Only the first ``ceil(prompt_len / block_size)`` blocks belong to the
        prompt; the remaining blocks hold generated tokens and must not be
        shared with a future request.
        """
        if self.prefix_cache is None or not seq.prompt_ids:
            return
        table = self.block_tables.get(seq.seq_id, [])
        if not table:
            return
        n_prompt_blocks = self._nblocks(len(seq.prompt_ids))
        blocks = table[:n_prompt_blocks]
        if not blocks:
            return
        self.prefix_cache.put(tuple(seq.prompt_ids), blocks)

    # ------------------------------------------------------------------ #
    # allocation
    # ------------------------------------------------------------------ #
    def can_allocate(self, seq, num_tokens_needed: int) -> bool:
        needed = self._nblocks(num_tokens_needed)
        existing = len(self.block_tables.get(seq.seq_id, []))
        return (needed - existing) <= self.allocator.num_free

    def attach_shared_blocks(self, seq, block_ids: List[int]) -> None:
        """Attach cached prefix blocks to a sequence (incrementing refs)."""
        for b in block_ids:
            self.allocator.touch(b)
        self.block_tables[seq.seq_id] = list(block_ids)

    def ensure_blocks(self, seq, num_tokens_needed: int) -> None:
        """Grow ``seq``'s block table until it covers ``num_tokens_needed``."""
        table = self.block_tables.setdefault(seq.seq_id, [])
        needed = self._nblocks(num_tokens_needed)
        while len(table) < needed:
            table.append(self.allocator.allocate())

    def release(self, seq) -> None:
        """Free all blocks owned by a sequence (finished or preempted)."""
        table = self.block_tables.pop(seq.seq_id, [])
        for b in table:
            self.allocator.free_block(b)

    # ------------------------------------------------------------------ #
    # KV write (with copy-on-write for shared blocks)
    # ------------------------------------------------------------------ #
    def write_kv(self, layer: int, seq, logical_pos: int,
                 k: np.ndarray, v: np.ndarray) -> None:
        """Store the K/V of one token, copying the block first if it is shared.

        This is the mini version of vLLM's ``copy-on-write`` for shared blocks
        (see ``vllm/core/block_manager_v1.py::_incr_ref_count`` / the COW logic
        in ``vllm/worker/model_runner.py``).
        """
        block_index = logical_pos // self.block_size
        offset = logical_pos % self.block_size
        table = self.block_tables[seq.seq_id]
        phys = table[block_index]

        if self.allocator.ref_count[phys] > 1:
            new_phys = self.allocator.allocate()
            self.kv_store.k_cache[layer][new_phys] = self.kv_store.k_cache[layer][phys]
            self.kv_store.v_cache[layer][new_phys] = self.kv_store.v_cache[layer][phys]
            self.allocator.free_block(phys)          # drop our reference to old block
            self.allocator.ref_count[new_phys] = 1   # allocate() already set this to 1
            table[block_index] = new_phys
            phys = new_phys

        self.kv_store.k_cache[layer][phys, offset] = k
        self.kv_store.v_cache[layer][phys, offset] = v
