from __future__ import annotations

import numpy as np
import pytest

from mini_deepinfer.lora import (
    AdapterRegistry,
    LoRAAdapter,
    LoRAConfig,
    apply_batched,
    make_adapter,
)


def test_adapter_apply_matches_merged_weight() -> None:
    adapter = make_adapter("a", 8, 6, rank=2, seed=1)
    x = np.random.default_rng(0).normal(size=(4, 8)).astype(np.float32)
    base = np.random.default_rng(2).normal(size=(8, 6)).astype(np.float32)
    base_output = x @ base

    direct = adapter.apply("q_proj", x, base_output)
    merged = x @ adapter.merged_weight("q_proj", base)
    assert np.allclose(direct, merged, atol=1e-6)
    assert adapter.project("q_proj", x).shape == (4, 6)


def test_rank_padding_preserves_output() -> None:
    adapter = make_adapter("a", 8, 6, rank=2, alpha=4.0, seed=3)
    padded = adapter.rank_padded(5)
    assert padded.config.rank == 5
    assert padded.config.scaling == pytest.approx(adapter.config.scaling)

    x = np.random.default_rng(4).normal(size=(3, 8)).astype(np.float32)
    base_output = np.zeros((3, 6), dtype=np.float32)
    assert np.allclose(
        adapter.apply("v_proj", x, base_output),
        padded.apply("v_proj", x, base_output),
        atol=1e-6,
    )
    with pytest.raises(ValueError):
        adapter.rank_padded(1)


def test_batched_dispatch_matches_per_row_loop() -> None:
    a = make_adapter("a", 8, 5, rank=2, seed=5)
    b = make_adapter("b", 8, 5, rank=3, seed=6)
    adapters = {"a": a, "b": b}
    x = np.random.default_rng(7).normal(size=(6, 8)).astype(np.float32)
    base_output = np.random.default_rng(8).normal(size=(6, 5)).astype(np.float32)
    ids: list[str | None] = ["a", None, "b", "a", "b", None]

    batched = apply_batched("q_proj", x, base_output, ids, adapters)
    expected = base_output.copy()
    for row, adapter_id in enumerate(ids):
        if adapter_id is not None:
            expected[row] = adapters[adapter_id].apply(
                "q_proj", x[row : row + 1], base_output[row : row + 1]
            )[0]
    assert np.allclose(batched, expected, atol=1e-6)
    assert np.allclose(batched[1], base_output[1])
    assert np.allclose(batched[5], base_output[5])


def test_registry_evicts_lru_but_not_pinned() -> None:
    registry = AdapterRegistry(max_adapters=2)
    a = make_adapter("a", 4, 4, rank=1, seed=1)
    b = make_adapter("b", 4, 4, rank=1, seed=2)
    c = make_adapter("c", 4, 4, rank=1, seed=3)
    registry.register(a)
    registry.register(b)
    registry.get("a")  # a becomes most recently used
    registry.register(c)  # b is the LRU victim
    assert "a" in registry and "c" in registry
    assert "b" not in registry
    assert registry.evictions == 1

    registry.pin("a")
    d = make_adapter("d", 4, 4, rank=1, seed=4)
    registry.register(d)  # c is evicted, a is protected
    assert "a" in registry and "d" in registry
    assert "c" not in registry


def test_registry_rejects_oversized_adapter_and_all_pinned() -> None:
    adapter = make_adapter("a", 32, 32, rank=8, seed=1)
    registry = AdapterRegistry(max_adapters=1, max_bytes=adapter.nbytes - 1)
    with pytest.raises(ValueError):
        registry.register(adapter)

    registry = AdapterRegistry(max_adapters=1)
    registry.register(make_adapter("a", 4, 4, rank=1, seed=1), pin=True)
    with pytest.raises(ValueError):
        registry.register(make_adapter("b", 4, 4, rank=1, seed=2))


def test_adapter_validation() -> None:
    with pytest.raises(ValueError):
        LoRAConfig(rank=0)
    with pytest.raises(ValueError):
        LoRAConfig(rank=2, alpha=0)
    with pytest.raises(ValueError):
        LoRAConfig(rank=2, target_modules=())
    with pytest.raises(ValueError):
        LoRAAdapter(
            adapter_id="bad",
            config=LoRAConfig(rank=2, target_modules=("q_proj",)),
            tensors={},
        )
    with pytest.raises(ValueError):
        LoRAAdapter(
            adapter_id="bad",
            config=LoRAConfig(rank=2, target_modules=("q_proj",)),
            tensors={"q_proj": (np.zeros((4, 3)), np.zeros((3, 4)))},
        )
    adapter = make_adapter("a", 4, 4, rank=1, seed=1)
    with pytest.raises(KeyError):
        adapter.delta("missing")
    with pytest.raises(ValueError):
        adapter.merged_weight("q_proj", np.zeros((3, 3)))
