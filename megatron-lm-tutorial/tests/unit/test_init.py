"""Tests for TP/PP-aware parameter initialisation.

The important properties:

* a sharded weight on TP rank 0 and TP rank 1 must be a different slice of the
  same conceptual matrix, not two copies of the same random block;
* corresponding local layers on different PP stages must not be identical;
* replicated parameters must match across TP ranks (same DP/PP position).
"""
from __future__ import annotations

import torch

from mini_megatron.parallel import layers


def _set_tp(monkeypatch, rank: int, world: int = 2) -> None:
    monkeypatch.setattr(layers, "get_tensor_model_parallel_world_size", lambda: world)
    monkeypatch.setattr(layers, "get_tensor_model_parallel_rank", lambda: rank)


def _set_pp(monkeypatch, rank: int, world: int = 2) -> None:
    monkeypatch.setattr(layers, "get_pipeline_model_parallel_world_size", lambda: world)
    monkeypatch.setattr(layers, "get_pipeline_model_parallel_rank", lambda: rank)


def test_tp_ranks_get_different_shards(monkeypatch):
    _set_pp(monkeypatch, rank=0, world=1)
    _set_tp(monkeypatch, rank=0)
    layers.set_model_parallel_init_seed(1234)
    rank0 = layers.ColumnParallelLinear(8, 8, init_method_std=0.02)

    _set_tp(monkeypatch, rank=1)
    layers.set_model_parallel_init_seed(1234)
    rank1 = layers.ColumnParallelLinear(8, 8, init_method_std=0.02)

    assert rank0.weight.shape == rank1.weight.shape == (4, 8)
    assert not torch.allclose(rank0.weight, rank1.weight)


def test_same_rank_reset_is_reproducible(monkeypatch):
    _set_pp(monkeypatch, rank=0, world=1)
    _set_tp(monkeypatch, rank=0)
    layers.set_model_parallel_init_seed(1234)
    first = layers.ColumnParallelLinear(8, 8, init_method_std=0.02)

    layers.set_model_parallel_init_seed(1234)
    second = layers.ColumnParallelLinear(8, 8, init_method_std=0.02)

    assert torch.allclose(first.weight, second.weight)


def test_counter_advances_between_layers(monkeypatch):
    _set_pp(monkeypatch, rank=0, world=1)
    _set_tp(monkeypatch, rank=0)
    layers.set_model_parallel_init_seed(1234)
    first = layers.ColumnParallelLinear(8, 8, init_method_std=0.02)
    second = layers.ColumnParallelLinear(8, 8, init_method_std=0.02)

    assert not torch.allclose(first.weight, second.weight)


def test_replicated_parameters_match_across_tp_but_not_pp(monkeypatch):
    _set_pp(monkeypatch, rank=0)
    layers.set_model_parallel_init_seed(1234)
    _set_tp(monkeypatch, rank=0)
    tp0 = layers.LearnedPositionEmbedding(8, 8, init_method_std=0.02)
    layers.set_model_parallel_init_seed(1234)
    _set_tp(monkeypatch, rank=1)
    tp1 = layers.LearnedPositionEmbedding(8, 8, init_method_std=0.02)
    assert torch.allclose(tp0.weight, tp1.weight)

    _set_pp(monkeypatch, rank=1)
    _set_tp(monkeypatch, rank=0)
    layers.set_model_parallel_init_seed(1234)
    pp1 = layers.LearnedPositionEmbedding(8, 8, init_method_std=0.02)
    assert not torch.allclose(tp0.weight, pp1.weight)
