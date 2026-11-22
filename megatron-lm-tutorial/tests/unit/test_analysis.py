import math

import pytest

from mini_megatron.analysis import (
    H100_SXM,
    activation_memory_bytes,
    attention_score_bytes,
    cluster_hours,
    communication_bytes,
    estimate,
    gpu_hours,
    kv_cache_bytes,
    parameter_count,
    state_memory_per_rank,
    training_flops,
)
from mini_megatron.config import ExperimentConfig


def test_parameter_count_matches_real_model():
    from mini_megatron.parallel.state import initialize_model_parallel
    from mini_megatron.parallel.transformer import TransformerStage

    cfg = ExperimentConfig.from_json("configs/tiny.json")
    initialize_model_parallel(1, 1, 1)
    stage = TransformerStage(cfg.model, cfg.parallel)
    actual = stage.num_parameters()
    counts = parameter_count(cfg.model)
    assert counts["total"] == actual, (counts, actual)
    assert counts["active"] == actual


def test_moe_bias_parameter_count_matches_real_model():
    from mini_megatron.parallel.state import initialize_model_parallel
    from mini_megatron.parallel.transformer import TransformerStage

    cfg = ExperimentConfig.from_json("configs/tiny_moe.json")
    cfg.model.add_bias_linear = True
    cfg.model.add_qkv_bias = True
    initialize_model_parallel(1, 1, 1)
    stage = TransformerStage(cfg.model, cfg.parallel)
    counts = parameter_count(cfg.model)
    assert counts["total"] == stage.num_parameters(), (counts, stage.num_parameters())


def test_gelu_bias_parameter_count_matches_real_model():
    """GELU has two projections; the bias count differs from SwiGLU's three."""
    from mini_megatron.parallel.state import initialize_model_parallel
    from mini_megatron.parallel.transformer import TransformerStage

    cfg = ExperimentConfig.from_json("configs/tiny.json")
    cfg.model.activation = "gelu"
    cfg.model.add_bias_linear = True
    cfg.model.add_qkv_bias = True
    initialize_model_parallel(1, 1, 1)
    stage = TransformerStage(cfg.model, cfg.parallel)
    counts = parameter_count(cfg.model)
    assert counts["total"] == stage.num_parameters(), (counts, stage.num_parameters())


def test_ep_state_memory_and_communication():
    cfg = ExperimentConfig.from_json("configs/tiny_ep2.json")
    ep_state = state_memory_per_rank(cfg.model, cfg.parallel)["total"]
    cfg.parallel.expert_model_parallel_size = 1
    replicated = state_memory_per_rank(cfg.model, cfg.parallel)["total"]
    assert ep_state < replicated
    comm = communication_bytes(
        cfg.model, cfg.train, cfg.parallel, world_size=2
    )
    assert comm["ep_per_step"] == 0  # ep=1
    cfg.parallel.expert_model_parallel_size = 2
    comm = communication_bytes(
        cfg.model, cfg.train, cfg.parallel, world_size=2
    )
    assert comm["ep_per_step"] > 0


def test_moe_active_parameters_smaller_than_total():
    from mini_megatron.analysis import _expert_parameter_count

    cfg = ExperimentConfig.from_json("configs/tiny_moe.json")
    counts = parameter_count(cfg.model)
    # Exactly (E-k) experts per layer are inactive; attention, router and
    # norms stay active on every token.
    inactive = (
        cfg.model.num_layers
        * (cfg.model.num_experts - cfg.model.moe_topk)
        * _expert_parameter_count(cfg.model)
    )
    assert counts["total"] - counts["active"] == inactive
    assert counts["active"] < counts["total"]


def test_state_memory_shards_over_tp_pp_and_zero():
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    base = state_memory_per_rank(cfg.model, cfg.parallel)["total"]
    cfg.parallel.tensor_model_parallel_size = 2
    tp2 = state_memory_per_rank(cfg.model, cfg.parallel)["total"]
    assert math.isclose(tp2, base / 2, rel_tol=1e-9)
    zero = state_memory_per_rank(cfg.model, cfg.parallel, zero_shard=2)["total"]
    assert zero < tp2


def test_activation_recompute_reduces_memory():
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    full = activation_memory_bytes(cfg.model, batch=2, seq_len=32)
    rec = activation_memory_bytes(
        cfg.model, batch=2, seq_len=32, recompute=True
    )
    assert rec["activation"] < full["activation"]


def test_attention_and_kv_shapes():
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    score = attention_score_bytes(cfg.model, batch=2, seq_len=32)
    expected = 2 * cfg.model.num_attention_heads * 32 * 32 * 2
    assert score == expected
    kv = kv_cache_bytes(cfg.model, batch=2, seq_len=32)
    d = cfg.model.hidden_size // cfg.model.num_attention_heads
    expected_kv = (
        2
        * cfg.model.num_layers
        * cfg.model.num_query_groups
        * d
        * 32
        * 2
        * 2
    )
    assert kv == expected_kv


def test_training_flops_and_mfu():
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    n = parameter_count(cfg.model)["active"]
    flops = training_flops(cfg.model, tokens=1000, seq_len=32)
    assert flops["linear"] == 6 * n * 1000
    assert flops["attention"] > 0
    hours = gpu_hours(
        flops=flops["total"], hardware=H100_SXM, utilization=0.4
    )
    expected = flops["total"] / (H100_SXM.peak_flops * 0.4 * 3600.0)
    assert hours == pytest.approx(expected)
    # GPU-hours is a quantity of work: it must not change when the cluster
    # grows.  Wall-clock hours, by contrast, scale as 1/world_size.
    assert gpu_hours(
        flops=flops["total"], hardware=H100_SXM, utilization=0.4
    ) == pytest.approx(hours)
    assert cluster_hours(
        flops=flops["total"], world_size=8, hardware=H100_SXM, utilization=0.4
    ) == pytest.approx(hours / 8)


def test_communication_groups_and_estimate_keys():
    cfg = ExperimentConfig.from_json("configs/tiny_tp2.json")
    comm = communication_bytes(cfg.model, cfg.train, cfg.parallel, world_size=2)
    assert comm["tp_per_step"] > 0
    assert comm["dp_grad_all_reduce"] == 0
    result = estimate(cfg, world_size=2, tokens=1e6)
    assert result["parallel"]["tp"] == 2
    assert result["memory"]["estimated_peak_per_rank"] > 0
    assert result["compute"]["flops"]["total"] > 0


def test_activation_peak_counts_in_flight_micro_batches():
    """1F1B keeps pp-1 micro-batches in flight; GPipe keeps all m.

    The implemented 1F1B warms up ``pp-rank-1`` forwards, then alternates
    B/F; the peak is ``pp-1`` (1 for pp=2), not ``pp``.  GPipe keeps all 8.
    """
    cfg = ExperimentConfig.from_json("configs/tiny_pp2.json")
    cfg.train.micro_batch_size = 1
    cfg.train.global_batch_size = 8
    one = estimate(cfg, world_size=2)["memory"]
    assert one["activation_in_flight_micro_batches"] == 1
    assert one["activation_peak"] == one["activation"]["activation"]

    cfg.parallel.pipeline_schedule = "gpipe"
    gpipe = estimate(cfg, world_size=2)["memory"]
    assert gpipe["activation_in_flight_micro_batches"] == 8
    assert gpipe["activation_peak"] == 8 * gpipe["activation"]["activation"]
    assert gpipe["estimated_peak_per_rank"] > one["estimated_peak_per_rank"]
