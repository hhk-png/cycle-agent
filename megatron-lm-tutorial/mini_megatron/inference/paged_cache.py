"""A paged KV cache reference implementation.

PagedAttention (vLLM) removes the requirement that a sequence's KV cache be
contiguous.  KV is stored in fixed-size blocks; each sequence owns a block
table that maps logical positions to physical blocks.  Allocation is on
demand, so a short sequence does not reserve ``max_seq_len`` slots and a
finished sequence returns its blocks to a shared pool.

This module implements the data structure only: block allocation, append,
gather and free.  It does not implement the fused paged-attention kernel.  The
tests compare gathered KV against a dense cache, which is the contract any
kernel must satisfy.

Layout per layer: ``[num_blocks, block_size, num_kv_heads, head_dim]``.
A sequence's logical position ``p`` lives in block ``p // block_size`` at slot
``p % block_size``.  The last block is usually partially filled.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import torch


class BlockAllocator:
    """Fixed-size block pool with first-fit free-list allocation."""

    def __init__(self, num_blocks: int, block_size: int) -> None:
        if num_blocks < 1 or block_size < 1:
            raise ValueError("num_blocks and block_size must be positive")
        self.num_blocks = int(num_blocks)
        self.block_size = int(block_size)
        self._free: List[int] = list(range(self.num_blocks))
        self._used: set[int] = set()

    def allocate(self, count: int) -> List[int]:
        if count < 0:
            raise ValueError("count must be non-negative")
        if count > len(self._free):
            raise MemoryError(
                f"out of KV blocks: need {count}, free {len(self._free)}"
            )
        # Pop from the end; order is deterministic and cheap.
        blocks = [self._free.pop() for _ in range(count)]
        self._used.update(blocks)
        return blocks

    def free(self, blocks: List[int]) -> None:
        for block in blocks:
            if block not in self._used:
                raise ValueError(f"block {block} is not allocated")
            self._used.remove(block)
            self._free.append(block)

    @property
    def num_free(self) -> int:
        return len(self._free)

    @property
    def num_used(self) -> int:
        return len(self._used)

    def stats(self) -> Dict[str, int]:
        return {
            "num_blocks": self.num_blocks,
            "block_size": self.block_size,
            "free": self.num_free,
            "used": self.num_used,
        }


class PagedKVCache:
    """Per-sequence block tables over a shared physical KV pool."""

    def __init__(
        self,
        *,
        num_layers: int,
        num_kv_heads: int,
        head_dim: int,
        block_size: int,
        num_blocks: int,
        dtype: torch.dtype = torch.float32,
        device=None,
    ) -> None:
        if num_layers < 1 or num_kv_heads < 1 or head_dim < 1:
            raise ValueError("layer/head/dim must be positive")
        self.num_layers = int(num_layers)
        self.num_kv_heads = int(num_kv_heads)
        self.head_dim = int(head_dim)
        self.block_size = int(block_size)
        self.dtype = dtype
        self.device = device
        self.allocator = BlockAllocator(num_blocks, block_size)

        def _pool() -> torch.Tensor:
            return torch.zeros(
                num_blocks,
                block_size,
                num_kv_heads,
                head_dim,
                dtype=dtype,
                device=device,
            )

        # K and V are separate pools: a paged cache must store both.
        self.storage_k: List[torch.Tensor] = [_pool() for _ in range(num_layers)]
        self.storage_v: List[torch.Tensor] = [_pool() for _ in range(num_layers)]
        self.tables: Dict[int, List[int]] = {}
        self.lengths: Dict[int, int] = {}
        # Per-(sequence, layer) write cursor.  It catches a caller that
        # interleaves chunks of one layer while another layer lags behind.
        self.layer_lengths: Dict[tuple[int, int], int] = {}
        self._next_id = 0

    # ------------------------------------------------------------------ lifecycle
    def create_sequence(self, seq_id: Optional[int] = None) -> int:
        if seq_id is None:
            seq_id = self._next_id
            self._next_id += 1
        if seq_id in self.tables:
            raise ValueError(f"sequence {seq_id} already exists")
        self.tables[seq_id] = []
        self.lengths[seq_id] = 0
        for layer in range(self.num_layers):
            self.layer_lengths[(seq_id, layer)] = 0
        return seq_id

    def free_sequence(self, seq_id: int) -> None:
        if seq_id not in self.tables:
            raise KeyError(seq_id)
        self.allocator.free(self.tables.pop(seq_id))
        self.lengths.pop(seq_id)
        for layer in range(self.num_layers):
            self.layer_lengths.pop((seq_id, layer), None)

    def length(self, seq_id: int) -> int:
        return self.lengths[seq_id]

    # --------------------------------------------------------------------- write
    def _ensure_blocks(self, seq_id: int, needed_tokens: int) -> None:
        current = len(self.tables[seq_id])
        required = (needed_tokens + self.block_size - 1) // self.block_size
        if required > current:
            self.tables[seq_id].extend(self.allocator.allocate(required - current))

    def append(
        self, seq_id: int, layer: int, k: torch.Tensor, v: torch.Tensor
    ) -> None:
        """Append ``k``/``v`` for one layer.

        ``k`` and ``v`` are ``[heads, tokens, dim]`` or
        ``[1, heads, tokens, dim]``.  Tokens must be appended in order; the
        caller is responsible for running all layers with the same token count
        (the tests assert the length bookkeeping).
        """
        if seq_id not in self.tables:
            raise KeyError(seq_id)
        if not 0 <= layer < self.num_layers:
            raise IndexError(layer)
        if k.dim() == 4:
            if k.size(0) != 1:
                raise ValueError("batch dimension must be 1")
            k = k[0]
            v = v[0]
        if k.shape != v.shape or k.dim() != 3:
            raise ValueError("k/v must be [heads, tokens, dim]")
        if k.size(0) != self.num_kv_heads or k.size(-1) != self.head_dim:
            raise ValueError("head count/dim does not match the cache")

        tokens = k.size(1)
        start = self.lengths[seq_id]
        if self.layer_lengths[(seq_id, layer)] != start:
            raise ValueError(
                "layers must be appended in lockstep; finish the previous "
                "token chunk for every layer first"
            )
        end = start + tokens
        self._ensure_blocks(seq_id, end)
        table = self.tables[seq_id]

        for offset in range(tokens):
            pos = start + offset
            block = table[pos // self.block_size]
            slot = pos % self.block_size
            self.storage_k[layer][block, slot].copy_(k[:, offset])
            self.storage_v[layer][block, slot].copy_(v[:, offset])
        self.layer_lengths[(seq_id, layer)] = end
        # The public length advances only after the last layer has written this
        # chunk, so a partial update is never visible to readers.
        if layer == self.num_layers - 1:
            if any(
                self.layer_lengths[(seq_id, other)] != end
                for other in range(self.num_layers)
            ):
                raise ValueError("all layers must cover the same token range")
            self.lengths[seq_id] = end

    # ---------------------------------------------------------------------- read
    def gather(
        self,
        seq_id: int,
        layer: int,
        upto: Optional[int] = None,
        *,
        which: str = "k",
    ) -> torch.Tensor:
        """Return K or V for ``[0, upto)`` as ``[heads, tokens, dim]``."""
        if seq_id not in self.tables:
            raise KeyError(seq_id)
        if which not in {"k", "v"}:
            raise ValueError("which must be 'k' or 'v'")
        length = self.lengths[seq_id]
        upto = length if upto is None else min(int(upto), length)
        if upto < 0:
            raise ValueError("upto must be non-negative")
        if upto == 0:
            return torch.zeros(
                self.num_kv_heads,
                0,
                self.head_dim,
                dtype=self.dtype,
                device=self.device,
            )
        table = self.tables[seq_id]
        blocks = (upto + self.block_size - 1) // self.block_size
        # Each block is [slots, heads, dim]; concatenate on the token axis and
        # then move to the [heads, tokens, dim] convention used by attention.
        pool = self.storage_k if which == "k" else self.storage_v
        pieces = [pool[layer][b] for b in table[:blocks]]
        flat = torch.cat(pieces, dim=0)[:upto]
        return flat.permute(1, 0, 2).contiguous()

    def gather_k(self, seq_id: int, layer: int, upto: Optional[int] = None):
        return self.gather(seq_id, layer, upto, which="k")

    def gather_v(self, seq_id: int, layer: int, upto: Optional[int] = None):
        return self.gather(seq_id, layer, upto, which="v")

    # ----------------------------------------------------------------- accounting
    def memory_bytes(self) -> int:
        per_layer = (
            self.allocator.num_blocks
            * self.block_size
            * self.num_kv_heads
            * self.head_dim
            * torch.tensor([], dtype=self.dtype).element_size()
        )
        # Two pools (K and V).
        return per_layer * self.num_layers * 2

    def fragmentation(self) -> float:
        """Fraction of reserved block slots that are unused.

        A dense cache reserves ``max_seq_len`` per sequence; paged allocation
        only wastes the tail of the last block of each live sequence.
        """
        reserved = self.allocator.num_used * self.block_size
        if reserved == 0:
            return 0.0
        used = sum(self.lengths.values())
        return 1.0 - used / reserved


def dense_kv_gather(k: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
    """Reference: concatenate the full dense K/V for comparison."""
    return torch.cat((k, v), dim=0)
