"""Paged KV cache: gather equals dense, allocator reuse, fragmentation."""
from __future__ import annotations

import torch

from mini_megatron.inference.paged_cache import BlockAllocator, PagedKVCache


def _cache(num_blocks: int = 8, block_size: int = 4, layers: int = 2):
    return PagedKVCache(
        num_layers=layers,
        num_kv_heads=2,
        head_dim=3,
        block_size=block_size,
        num_blocks=num_blocks,
    )


def _append_chunk(cache, seq_id, chunk_id, tokens, layers):
    k = torch.randn(layers, 2, tokens, 3)
    v = torch.randn(layers, 2, tokens, 3)
    for layer in range(layers):
        cache.append(seq_id, layer, k[layer].unsqueeze(0), v[layer].unsqueeze(0))
    return k, v


def test_paged_gather_matches_dense_across_block_boundaries():
    cache = _cache(block_size=4, layers=2)
    seq = cache.create_sequence()
    k_ref = [torch.zeros(2, 0, 3) for _ in range(2)]
    v_ref = [torch.zeros(2, 0, 3) for _ in range(2)]
    # 3 + 4 + 1 tokens crosses two block boundaries and leaves a partial tail.
    for chunk_id, tokens in enumerate((3, 4, 1)):
        k, v = _append_chunk(cache, seq, chunk_id, tokens, 2)
        for layer in range(2):
            k_ref[layer] = torch.cat((k_ref[layer], k[layer]), dim=1)
            v_ref[layer] = torch.cat((v_ref[layer], v[layer]), dim=1)
            assert torch.allclose(cache.gather_k(seq, layer), k_ref[layer])
            assert torch.allclose(cache.gather_v(seq, layer), v_ref[layer])
    assert cache.length(seq) == 8
    assert len(cache.tables[seq]) == 2
    # Partial gather does not depend on the block tail.
    assert torch.allclose(cache.gather(seq, 0, upto=5), k_ref[0][:, :5])


def test_sequences_do_not_share_blocks_and_release_reuses_them():
    cache = _cache(num_blocks=4, block_size=4)
    a = cache.create_sequence()
    _append_chunk(cache, a, 0, 8, 2)
    assert cache.allocator.num_used == 2

    b = cache.create_sequence()
    k, v = _append_chunk(cache, b, 0, 4, 2)
    assert cache.allocator.num_used == 3
    assert torch.allclose(cache.gather(b, 0), k[0])
    assert torch.allclose(cache.gather_v(b, 0), v[0])

    cache.free_sequence(a)
    assert cache.allocator.num_used == 1
    c = cache.create_sequence()
    _append_chunk(cache, c, 0, 8, 2)
    assert cache.allocator.num_used == 3
    # A's blocks were recycled, not leaked.
    assert cache.allocator.stats()["free"] == 1


def test_out_of_blocks_raises_instead_of_corrupting():
    cache = _cache(num_blocks=1, block_size=2)
    seq = cache.create_sequence()
    _append_chunk(cache, seq, 0, 2, 2)
    try:
        _append_chunk(cache, seq, 1, 2, 2)
    except MemoryError:
        pass
    else:
        raise AssertionError("expected block exhaustion")


def test_fragmentation_and_memory_accounting():
    cache = _cache(num_blocks=8, block_size=4, layers=2)
    seq = cache.create_sequence()
    _append_chunk(cache, seq, 0, 3, 2)
    # One 4-slot block reserved, 3 slots used.
    assert cache.allocator.num_used == 1
    assert abs(cache.fragmentation() - 0.25) < 1e-9
    # layers, blocks, slots, heads, dim, fp32, and both K and V pools
    expected = 2 * 2 * 8 * 4 * 2 * 3 * 4
    assert cache.memory_bytes() == expected


def test_layer_lockstep_is_enforced():
    cache = _cache(layers=2)
    seq = cache.create_sequence()
    k = torch.randn(1, 2, 2, 3)
    cache.append(seq, 0, k, k)
    try:
        cache.append(seq, 0, k, k)  # layer 1 has not caught up
    except ValueError:
        pass
    else:
        raise AssertionError("expected a lockstep error")


def test_allocator_rejects_double_free():
    alloc = BlockAllocator(2, 4)
    blocks = alloc.allocate(1)
    alloc.free(blocks)
    try:
        alloc.free(blocks)
    except ValueError:
        pass
    else:
        raise AssertionError("expected double free to raise")


def test_gather_validates_which_even_for_empty_range():
    cache = _cache(layers=1)
    seq = cache.create_sequence()
    try:
        cache.gather(seq, 0, upto=0, which="bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("expected an invalid cache selector to raise")
