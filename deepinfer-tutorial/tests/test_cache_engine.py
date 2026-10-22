from __future__ import annotations

import numpy as np

from mini_deepinfer.cache_engine import KVBlockStore


def test_kv_store_roundtrip_and_stats(tmp_path) -> None:
    store = KVBlockStore(tmp_path, capacity=4, ttl_s=100, clock=lambda: 0.0)
    array = np.arange(8, dtype=np.float32).reshape(2, 4)
    store.put("a", array)
    np.testing.assert_array_equal(store.get("a"), array)
    assert store.get("missing") is None
    stats = store.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.5


def test_kv_store_ttl(tmp_path) -> None:
    now = [0.0]
    store = KVBlockStore(tmp_path, capacity=4, ttl_s=10, clock=lambda: now[0])
    store.put("a", np.zeros(2, dtype=np.float32))
    now[0] = 5
    assert store.get("a") is not None
    now[0] = 11
    assert store.get("a") is None
    assert store.stats()["misses"] == 1


def test_kv_store_lru_eviction(tmp_path) -> None:
    store = KVBlockStore(tmp_path, capacity=2, ttl_s=100, clock=lambda: 0.0)
    store.put("a", np.zeros(1, dtype=np.float32))
    store.put("b", np.ones(1, dtype=np.float32))
    assert store.get("a") is not None
    store.put("c", np.full(1, 2, dtype=np.float32))
    assert store.get("b") is None
    assert store.get("a") is not None
    assert store.stats()["evictions"] == 1


def test_kv_store_clear(tmp_path) -> None:
    store = KVBlockStore(tmp_path, capacity=2)
    store.put("a", np.zeros(1, dtype=np.float32))
    store.clear()
    assert store.get("a") is None
