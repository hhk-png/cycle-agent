from __future__ import annotations

import numpy as np
import pytest

from mini_verl.config import EngineConfig, OptimConfig
from mini_verl.engine import AdamW, EngineRegistry, FSDPEngine, clip_gradients, estimate_memory
from mini_verl.losses import sft_loss_fn
from mini_verl.nn import TinyLinearLM
from mini_verl.parallel import (
    ColumnParallelLinear,
    PipelineSchedule,
    RowParallelLinear,
    kv_cache_bytes,
    moe_route,
    pipeline_forward,
    sequence_parallel_mlp,
    ulysses_merge,
    ulysses_split,
)
from mini_verl.sharding import (
    CheckpointEngineManager,
    fsdp_gather,
    fsdp_shard,
    tp_gather_state_dict,
    tp_shard_state_dict,
)


def test_adamw_reduces_quadratic():
    from mini_verl.autograd import Parameter
    from mini_verl.nn import Module

    class Quadratic(Module):
        def __init__(self):
            super().__init__()
            self.w = Parameter(np.asarray([3.0], dtype=np.float32))

        def forward(self):
            return (self.w - 1.0) * (self.w - 1.0)

    model = Quadratic()
    optim = AdamW(model, OptimConfig(lr=0.1, total_training_steps=100))
    for _ in range(200):
        model.zero_grad()
        model().backward()
        optim.step_named(model.get_gradients() if hasattr(model, "get_gradients") else {"w": model.w.grad})
    assert abs(float(model.w.data[0]) - 1.0) < 0.1


def test_clip_gradients():
    grads, norm = clip_gradients({"a": np.asarray([3.0, 4.0])}, 1.0)
    assert norm == 5.0
    assert np.linalg.norm(grads["a"]) == pytest.approx(1.0)
    _, norm = clip_gradients({"a": np.asarray([0.3, 0.4])}, 1.0)
    assert norm == pytest.approx(0.5)


def test_engine_train_batch_reduces_loss():
    model = TinyLinearLM(vocab_size=10, max_seq_len=8, dim=8, seed=0)
    engine = EngineRegistry.new("fsdp", model, OptimConfig(lr=0.05, total_training_steps=100), EngineConfig())
    ids = np.asarray([[1, 2, 3, 4]] * 4, dtype=np.int64)
    mask = np.asarray([[0, 0, 1, 1]] * 4, dtype=np.float32)
    batch = {"input_ids": ids, "response_mask": mask}
    losses = [engine.train_batch(batch, sft_loss_fn)["loss"] for _ in range(30)]
    assert losses[-1] < losses[0]


def test_engine_checkpoint_roundtrip(tmp_path):
    model = TinyLinearLM(vocab_size=10, max_seq_len=8, dim=8, seed=0)
    engine = EngineRegistry.new("fsdp", model, OptimConfig(), EngineConfig())
    ids = np.asarray([[1, 2, 3]], dtype=np.int64)
    mask = np.asarray([[0, 1, 1]], dtype=np.float32)
    for _ in range(3):
        engine.train_batch({"input_ids": ids, "response_mask": mask}, sft_loss_fn)
    engine.save_checkpoint(str(tmp_path), "actor")
    expected = model.state_dict()
    other = EngineRegistry.new("fsdp", TinyLinearLM(vocab_size=10, max_seq_len=8, dim=8, seed=1), OptimConfig(), EngineConfig())
    other.load_checkpoint(str(tmp_path), "actor")
    assert all(np.allclose(other.model.state_dict()[key], value) for key, value in expected.items())


def test_engine_offload_and_memory():
    engine = EngineRegistry.new("fsdp", TinyLinearLM(10, 8, 8), OptimConfig(), EngineConfig(param_offload=True))
    assert engine.is_param_offload_enabled
    engine.to("cpu")
    assert engine.offloaded
    report = engine.memory_bytes()
    assert set(report) == {"params", "grads", "optimizer"}


def test_estimate_memory():
    report = estimate_memory(1_000_000, world_size=8, zero_stage=3, activation_tokens=128)
    assert report["total_gib"] > 0
    assert report["optimizer_gib"] < report["params_gib"] * 10


def test_column_row_parallel_equivalence():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(3, 8)).astype(np.float32)
    w = rng.normal(size=(8, 12)).astype(np.float32)
    b = rng.normal(size=(12,)).astype(np.float32)
    column = ColumnParallelLinear(w, b, tp_size=3)
    assert np.allclose(column.forward(x), x @ w + b, atol=1e-5)
    w2 = rng.normal(size=(12, 6)).astype(np.float32)
    b2 = rng.normal(size=(6,)).astype(np.float32)
    row = RowParallelLinear(w2, b2, tp_size=3)
    hidden = x @ w + b
    assert np.allclose(row.forward(hidden), hidden @ w2 + b2, atol=1e-5)


def test_sequence_parallel_and_ulysses():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(2, 6, 4)).astype(np.float32)
    chunks = ulysses_split(x, 3)
    assert [chunk.shape[1] for chunk in chunks] == [2, 2, 2]
    assert np.allclose(ulysses_merge(chunks, 3), x)
    w1 = rng.normal(size=(4, 8)).astype(np.float32)
    b1 = np.zeros(8, dtype=np.float32)
    w2 = rng.normal(size=(8, 4)).astype(np.float32)
    b2 = np.zeros(4, dtype=np.float32)
    out = sequence_parallel_mlp(x, w1, b1, w2, b2, 3)
    expected = np.maximum(x @ w1 + b1, 0) @ w2 + b2
    assert np.allclose(out, expected, atol=1e-5)


def test_pipeline():
    stages = [lambda x: x + 1, lambda x: x * 2]
    micro = [np.asarray([1]), np.asarray([2])]
    outputs = pipeline_forward(stages, micro)
    assert [int(item[0]) for item in outputs] == [4, 6]
    schedule = PipelineSchedule(num_microbatches=8, num_stages=4)
    assert 0 < schedule.bubble_ratio() < 1
    assert len(schedule.timeline()) == 8 * 4 * 2


def test_moe_route_capacity():
    logits = np.asarray([[3.0, 1.0, 0.0], [0.0, 2.0, 1.0], [0.0, 0.0, 4.0]], dtype=np.float32)
    outputs = np.arange(3 * 3 * 2, dtype=np.float32).reshape(3, 3, 2) + 1
    result = moe_route(logits, top_k=2, capacity_factor=0.5, expert_outputs=outputs)
    assert result["expert_counts"].sum() <= 3 * 2
    assert result["dropped_tokens"][0] >= 0
    assert result["output"].shape == (3, 2)


def test_kv_cache_bytes():
    assert kv_cache_bytes(1, 10, 2, 2, 4, 2) == 1 * 10 * 2 * 2 * 2 * 4 * 2


def test_fsdp_shard_gather():
    state = {"a": np.arange(8, dtype=np.float32), "b": np.arange(6, dtype=np.float32)}
    shards = fsdp_shard(state, 4)
    restored = fsdp_gather(shards)
    assert np.allclose(restored["a"], state["a"])
    assert np.allclose(restored["b"], state["b"])


def test_tp_shard_gather():
    state = {
        "blocks.0.attn.c_attn.weight": np.arange(24, dtype=np.float32).reshape(4, 6),
        "blocks.0.attn.c_proj.weight": np.arange(24, dtype=np.float32).reshape(6, 4),
        "tok_emb.weight": np.arange(20, dtype=np.float32).reshape(10, 2),
    }
    shards = tp_shard_state_dict(state, 2)
    restored = tp_gather_state_dict(shards)
    assert all(np.allclose(restored[key], value) for key, value in state.items())


def test_tp_gather_keeps_equal_zero_shards():
    state = {"lm_head.weight": np.zeros((4, 2), dtype=np.float32)}
    shards = tp_shard_state_dict(state, 2)
    restored = tp_gather_state_dict(shards)
    assert restored["lm_head.weight"].shape == (4, 2)


def test_tp_shard_gather_non_divisible_is_replicated():
    state = {"fc_in.weight": np.arange(15, dtype=np.float32).reshape(3, 5)}
    shards = tp_shard_state_dict(state, 2)
    assert all(shard["fc_in.weight"].shape == (3, 5) for shard in shards)
    restored = tp_gather_state_dict(shards)
    assert restored["fc_in.weight"].shape == (3, 5)
    assert np.allclose(restored["fc_in.weight"], state["fc_in.weight"])


class _DummyRollout:
    def __init__(self):
        self.state = None

    def update_weights(self, state):
        self.state = {key: value.copy() for key, value in state.items()}

    def update_weights_partial(self, state):
        if self.state is None:
            self.state = {}
        for key, value in state.items():
            self.state[key] = value.copy()


def test_checkpoint_engine_delta():
    state = {"a": np.arange(10, dtype=np.float32), "b": np.arange(10, dtype=np.float32)}
    rollout = _DummyRollout()
    manager = CheckpointEngineManager("delta", replicas=2)
    first = manager.update_weights(state, [rollout])
    assert first.bytes_sent > 0
    state["a"] = state["a"] + 1
    second = manager.update_weights(state, [rollout])
    assert second.bytes_sent < first.bytes_sent
    assert second.changed_ratio < 1.0
    assert np.allclose(rollout.state["a"], state["a"])
