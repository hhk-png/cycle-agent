from __future__ import annotations

import numpy as np
import pytest

from mini_deepinfer.block_manager import BlockManager, compute_block_hash
from mini_deepinfer.cache_engine import PagedKVCache
from mini_deepinfer.errors import OutOfBlocksError
from mini_deepinfer.sampling_params import SamplingParams
from mini_deepinfer.sequence import Sequence


def make_seq(seq_id: int, tokens: list[int]) -> Sequence:
    return Sequence(seq_id, str(seq_id), tokens, SamplingParams(max_tokens=4))


def test_hash_chain_depends_on_prefix() -> None:
    first = compute_block_hash(0, (1, 2, 3, 4))
    second = compute_block_hash(first, (5, 6, 7, 8))
    same = compute_block_hash(compute_block_hash(0, (1, 2, 3, 4)), (5, 6, 7, 8))
    assert second == same
    assert compute_block_hash(0, (1, 2, 3, 5)) != first


def test_allocate_free_and_eviction() -> None:
    manager = BlockManager(4, block_size=2, enable_prefix_cache=False)
    blocks = manager.allocate(3)
    assert manager.num_free_blocks() == 1
    manager.free(blocks[:2])
    assert manager.num_free_blocks() == 3
    with pytest.raises(OutOfBlocksError):
        manager.allocate(4)
    assert manager.allocate(1)


def test_duplicate_hash_survives_eviction_of_one_copy() -> None:
    manager = BlockManager(4, block_size=4)
    blocks = manager.allocate(4)
    first, second = blocks[0], blocks[1]
    token_ids = (1, 2, 3, 4)
    block_hash = compute_block_hash(0, token_ids)
    manager.register(first, block_hash, token_ids)
    manager.register(second, block_hash, token_ids)
    manager.free([first])  # second still holds a referenced copy
    manager.allocate(1)  # evicts the unreferenced first copy
    hit = manager.lookup(block_hash)
    assert hit == second


def test_prefix_cache_match_and_share() -> None:
    manager = BlockManager(8, block_size=4)
    tokens = [10, 11, 12, 13, 14, 15]
    first = make_seq(1, tokens)
    first.block_table = manager.allocate(2)
    hashes = manager.prefix_hashes(tokens)
    manager.register(first.block_table[0], hashes[0][0], hashes[0][1])
    manager.free(first.block_table)

    second = make_seq(2, tokens)
    hit, blocks, _ = manager.match_prefix(tokens)
    assert hit == 4
    assert len(blocks) == 1
    assert manager.get_block(blocks[0]).ref_count == 1
    manager.free(blocks)


def test_paged_cache_write_and_gather() -> None:
    manager = BlockManager(8, block_size=4)
    cache = PagedKVCache(2, manager, n_kv_heads=1, head_dim=2)
    seq = make_seq(3, [0] * 6)
    k = np.arange(12, dtype=np.float32).reshape(6, 1, 2)
    v = k + 100
    cache.write_layer(0, seq, 0, k, v)
    got_k, got_v = cache.gather_layer(0, seq, 6)
    np.testing.assert_allclose(got_k, k)
    np.testing.assert_allclose(got_v, v)
    assert cache.required_blocks(6) == 2


def test_copy_on_write_protects_shared_block() -> None:
    manager = BlockManager(8, block_size=4)
    cache = PagedKVCache(1, manager, n_kv_heads=1, head_dim=2)
    seq_a = make_seq(4, [0] * 4)
    cache.ensure_capacity(seq_a, 4)
    k = np.ones((4, 1, 2), dtype=np.float32)
    cache.write_layer(0, seq_a, 0, k, k)
    block = seq_a.block_table[0]
    manager.register(block, 123, (0, 0, 0, 0))
    manager.share(block)
    seq_b = make_seq(5, [0] * 4)
    seq_b.block_table = [block]
    # Simulate an append into the shared partial block.
    seq_b.block_table = [block]
    cache._cow_if_shared(seq_b, 0)
    assert seq_b.block_table[0] != block
    assert manager.get_block(block).ref_count == 1
    np.testing.assert_allclose(cache.k[0, seq_b.block_table[0]], np.ones((4, 1, 2)))


def test_allow_eviction_of_cached_blocks() -> None:
    manager = BlockManager(2, block_size=2)
    blocks = manager.allocate(2)
    hashes = manager.prefix_hashes([1, 2, 3, 4])
    manager.register(blocks[0], hashes[0][0], hashes[0][1])
    manager.free(blocks)
    manager.allocate(2)  # both cached pages may be evicted
    assert manager.evictions >= 1


def test_cached_blocks_are_allocatable_but_still_resident() -> None:
    manager = BlockManager(4, block_size=2)
    blocks = manager.allocate(2)
    hashes = manager.prefix_hashes([1, 2, 3, 4])
    manager.register(blocks[0], hashes[0][0], hashes[0][1])
    manager.free(blocks)

    # Both blocks can be handed out again, but the cached block still holds
    # KV data until the allocator actually evicts it.
    assert manager.num_free_blocks() == 4
    assert manager.num_resident_blocks() == 1
    assert manager.num_used_blocks() == 1
    assert manager.stats()["resident_blocks"] == 1
    assert manager.stats()["cached_blocks"] == 1

    manager.allocate(4)
    assert manager.num_resident_blocks() == 4
    assert manager.evictions >= 1


def test_cache_occupancy_counts_resident_cached_bytes() -> None:
    manager = BlockManager(4, block_size=2)
    cache = PagedKVCache(1, manager, n_kv_heads=1, head_dim=1)
    blocks = manager.allocate(2)
    hashes = manager.prefix_hashes([1, 2, 3, 4])
    manager.register(blocks[0], hashes[0][0], hashes[0][1])
    manager.free(blocks)

    # One cached block is physically resident even though nothing references it.
    assert cache.occupancy() == pytest.approx(1 / 4)
    assert cache.used_bytes() == cache.memory_bytes() // 4
