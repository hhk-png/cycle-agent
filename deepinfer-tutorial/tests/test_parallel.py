from __future__ import annotations

import numpy as np

from mini_deepinfer.block_manager import BlockManager
from mini_deepinfer.cache_engine import PagedKVCache
from mini_deepinfer.model.ops import attention, causal_mask, softmax
from mini_deepinfer.parallel import (
    DecodeDualPathBalancer,
    Expert,
    PrefillTierBalancer,
    context_parallel_attention,
    expert_parallel_moe,
    export_sequence_kv,
    import_sequence_kv,
    tensor_parallel_linear,
    tensor_parallel_mlp,
)
from mini_deepinfer.model.ops import silu
from mini_deepinfer.sampling_params import SamplingParams
from mini_deepinfer.sequence import Sequence


def test_context_parallel_attention_matches_single_rank() -> None:
    rng = np.random.default_rng(0)
    q = rng.normal(size=(8, 4, 16)).astype(np.float32)
    k = rng.normal(size=(8, 4, 16)).astype(np.float32)
    v = rng.normal(size=(8, 4, 16)).astype(np.float32)
    mask = causal_mask(np.arange(8), 8)
    reference = attention(q, k, v, mask)
    for cp_size in (1, 2, 4):
        result, moved = context_parallel_attention(q, k, v, mask, cp_size=cp_size)
        np.testing.assert_allclose(result, reference, atol=1e-6)
        if cp_size == 1:
            assert moved == 0
        else:
            assert moved > 0


def test_expert_parallel_matches_single_rank() -> None:
    rng = np.random.default_rng(1)
    hidden = rng.normal(size=(6, 32)).astype(np.float32)
    gate = (rng.normal(size=(32, 4)) * 0.1).astype(np.float32)
    experts = [
        Expert(
            (rng.normal(size=(32, 48)) * 0.1).astype(np.float32),
            (rng.normal(size=(32, 48)) * 0.1).astype(np.float32),
            (rng.normal(size=(48, 32)) * 0.1).astype(np.float32),
        )
        for _ in range(4)
    ]
    output, stats = expert_parallel_moe(hidden, gate, experts, 2, ep_size=2)
    probs = softmax(hidden @ gate, axis=-1)
    top = np.argsort(-probs, axis=-1)[:, :2]
    weights = np.take_along_axis(probs, top, axis=-1)
    weights /= weights.sum(axis=-1, keepdims=True)
    reference = np.zeros_like(hidden)
    for token in range(6):
        for slot, expert in enumerate(top[token]):
            reference[token] += weights[token, slot] * experts[expert](hidden[token : token + 1])[0]
    np.testing.assert_allclose(output, reference, atol=1e-6)
    assert stats.dispatched == 12


def test_kv_export_import_preserves_decode(model) -> None:
    prompt = [65, 66, 67, 68]
    seq_source = Sequence(1, "a", prompt, SamplingParams(temperature=0.0, max_tokens=4))
    manager_source = BlockManager(32, 8)
    cache_source = PagedKVCache(model.config.n_layers, manager_source, model.config.n_kv_heads, model.config.head_dim)
    logits_source, _, _ = model.forward_paged(seq_source, 0, len(prompt), cache_source)
    seq_source.num_computed_tokens = len(prompt)
    payload = export_sequence_kv(cache_source, seq_source)

    seq_target = Sequence(2, "b", prompt, SamplingParams(temperature=0.0, max_tokens=4))
    manager_target = BlockManager(32, 8)
    cache_target = PagedKVCache(model.config.n_layers, manager_target, model.config.n_kv_heads, model.config.head_dim)
    moved = import_sequence_kv(cache_target, seq_target, payload)
    assert moved > 0
    for layer in range(model.config.n_layers):
        k_source, v_source = cache_source.gather_layer(layer, seq_source)
        k_target, v_target = cache_target.gather_layer(layer, seq_target)
        np.testing.assert_allclose(k_source, k_target)
        np.testing.assert_allclose(v_source, v_target)

    token = int(np.argmax(logits_source[-1]))
    seq_source.append_token(token)
    seq_target.append_token(token)
    logits_a, _, _ = model.forward_paged(seq_source, len(prompt), len(prompt) + 1, cache_source)
    logits_b, _, _ = model.forward_paged(seq_target, len(prompt), len(prompt) + 1, cache_target)
    np.testing.assert_allclose(logits_a, logits_b, atol=1e-6)


def test_tensor_parallel_linear_matches_single_rank() -> None:
    rng = np.random.default_rng(3)
    x = rng.normal(size=(5, 32)).astype(np.float32)
    weight = (rng.normal(size=(32, 48)) * 0.1).astype(np.float32)
    bias = rng.normal(size=48).astype(np.float32)
    reference = x @ weight + bias
    for tp_size in (1, 2, 4):
        column, column_stats = tensor_parallel_linear(
            x, weight, tp_size=tp_size, mode="column", bias=bias
        )
        row, row_stats = tensor_parallel_linear(
            x, weight, tp_size=tp_size, mode="row", bias=bias
        )
        np.testing.assert_allclose(column, reference, atol=1e-6)
        np.testing.assert_allclose(row, reference, atol=1e-6)
        assert len(column_stats) == tp_size
        assert len(row_stats) == tp_size
        assert column_stats[0].shard_shape[-1] == 48 // tp_size
        assert row_stats[0].shard_shape[0] == 32 // tp_size


def test_tensor_parallel_rejects_bad_divisibility() -> None:
    x = np.zeros((2, 30), dtype=np.float32)
    weight = np.zeros((30, 40), dtype=np.float32)
    with np.testing.assert_raises(ValueError):
        # Row parallelism splits the 30 input features across 4 ranks.
        tensor_parallel_linear(x, weight, tp_size=4, mode="row")


def test_tensor_parallel_mlp_matches_single_rank() -> None:
    rng = np.random.default_rng(4)
    x = rng.normal(size=(4, 32)).astype(np.float32)
    w1 = (rng.normal(size=(32, 64)) * 0.1).astype(np.float32)
    w3 = (rng.normal(size=(32, 64)) * 0.1).astype(np.float32)
    w2 = (rng.normal(size=(64, 32)) * 0.1).astype(np.float32)
    reference = (silu(x @ w1) * (x @ w3)) @ w2
    for tp_size in (1, 2, 4):
        result, moved = tensor_parallel_mlp(x, w1, w3, w2, tp_size=tp_size)
        np.testing.assert_allclose(result, reference, atol=1e-6)
        assert moved >= 0


def test_load_balancers() -> None:
    tiers = PrefillTierBalancer()
    assert tiers.tier(100) == 0
    assert tiers.tier(20_000) == 1
    assignment = tiers.assign([100, 100, 20_000, 300_000], 4)
    assert len(assignment) == 4
    assert all(0 <= engine < 4 for engine in assignment)

    # With fewer engines than active tiers, no engine index may fall outside
    # the fleet, and both engines should still receive work.
    sparse = tiers.assign([100, 20_000, 70_000, 300_000], 2)
    assert all(0 <= engine < 2 for engine in sparse)
    assert set(sparse) == {0, 1}

    # A single active tier still uses every engine instead of pinning all
    # requests to engine 0.
    single = tiers.assign([100, 200, 300, 400], 2)
    assert set(single) == {0, 1}

    balancer = DecodeDualPathBalancer()
    ranks = [{"count": 4, "kv_len": 1000}, {"count": 1, "kv_len": 8000}]
    # score = count * (1 + length/4096) + kv_len/8192
    # rank 0: 4 * 1.0244 + 0.1221 = 4.2197
    # rank 1: 1 * 1.0244 + 0.9766 = 2.0010
    assert balancer.choose(ranks, 100) == 1

    # When concurrency is equal, the KV term decides: the rank with the
    # smaller KV footprint wins even for a very long request.
    flat = [{"count": 0, "kv_len": 8000}, {"count": 0, "kv_len": 1000}]
    assert balancer.choose(flat, 1_000_000) == 1

    # When KV is equal, the count term decides.
    busy = [{"count": 8, "kv_len": 2000}, {"count": 1, "kv_len": 2000}]
    assert balancer.choose(busy, 1_000_000) == 1
