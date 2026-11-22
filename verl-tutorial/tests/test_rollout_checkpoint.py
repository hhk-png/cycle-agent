from __future__ import annotations

import numpy as np
import pytest

from mini_verl.async_policy import ReplayBuffer, compute_rollout_correction
from mini_verl.autograd import Tensor
from mini_verl.checkpoint import CheckpointManager, export_hf_like, read_safetensors, write_safetensors
from mini_verl.config import ModelConfig, PPOConfig, RolloutConfig
from mini_verl.data import CharTokenizer, build_dataset, collate_fn
from mini_verl.protocol import DataProto
from mini_verl.rollout import (
    NaiveRollout,
    PDDisaggregatedRollout,
    SamplingParams,
    TPRollout,
    build_rollout,
)
from mini_verl.sharding import CheckpointEngineManager


def make_batch():
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 4, seed=0)
    batch = collate_fn(dataset.samples, tokenizer, 4, 1)
    return tokenizer, batch


def test_naive_rollout_generation():
    tokenizer, batch = make_batch()
    config = RolloutConfig(prompt_length=4, response_length=1, allowed_chars="xyzw", n=1)
    model_config = ModelConfig(arch="linear", hidden_size=8, max_seq_len=8)
    rollout = build_rollout(config, tokenizer, seed=0, model_config=model_config)
    out = rollout.generate_sequences(batch)
    assert len(out) == 4
    assert out.batch["input_ids"].shape == batch.batch["input_ids"].shape
    assert np.all(out.batch["response_mask"].sum(axis=-1) == 1)
    for text in out.non_tensor_batch["response_text"]:
        assert text == "" or text[0] in "xyzw"


class _FixedLogitModel:
    def __init__(self, logits: np.ndarray):
        self.logits = logits

    def __call__(self, ids):
        return Tensor(self.logits, requires_grad=False)


def test_rollout_log_probs_follow_behavior_temperature():
    tokenizer = CharTokenizer()
    config = RolloutConfig(prompt_length=1, response_length=1, temperature=0.5, do_sample=True)
    logits = np.asarray([[[0.0, 1.0, 0.5, -1.0]]], dtype=np.float32)
    rollout = NaiveRollout(_FixedLogitModel(logits), tokenizer, config, seed=0, model_config=config)
    prompts = DataProto(
        batch={
            "input_ids": np.asarray([[1, 0]], dtype=np.int64),
            "attention_mask": np.ones((1, 2), dtype=np.float32),
            "response_mask": np.zeros((1, 2), dtype=np.float32),
            "prompt_length": np.asarray([1], dtype=np.int64),
        },
        non_tensor_batch={"uid": np.asarray([1], dtype=np.int64)},
    )
    out = rollout.generate_sequences(prompts)
    token = int(out.batch["input_ids"][0, 1])
    adjusted = logits[0, 0] / 0.5
    expected = adjusted - np.log(np.exp(adjusted - adjusted.max()).sum()) - adjusted.max()
    assert out.batch["rollout_log_probs"][0, 1] == pytest.approx(expected[token], abs=1e-5)


def test_rollout_deterministic_with_seed():
    tokenizer, batch = make_batch()
    config = RolloutConfig(prompt_length=4, response_length=1, temperature=1.0, n=1)
    model_config = ModelConfig(arch="linear", hidden_size=8, max_seq_len=8)
    a = build_rollout(config, tokenizer, seed=7, model_config=model_config).generate_sequences(batch)
    b = build_rollout(config, tokenizer, seed=7, model_config=model_config).generate_sequences(batch)
    assert a.non_tensor_batch["response_text"].tolist() == b.non_tensor_batch["response_text"].tolist()


def test_rollout_sleep_wake_and_prefix_cache():
    tokenizer, batch = make_batch()
    config = RolloutConfig(prompt_length=4, response_length=1, allowed_chars="xyzw")
    model_config = ModelConfig(arch="linear", hidden_size=8, max_seq_len=8)
    rollout = build_rollout(config, tokenizer, seed=0, model_config=model_config)
    first = rollout.generate_sequences(batch)
    second = rollout.generate_sequences(batch)
    assert second.meta_info["prefix_hits"] >= 0
    rollout.sleep()
    assert rollout.model is None
    with pytest.raises(RuntimeError):
        rollout.generate_sequences(batch)
    rollout.wake_up()
    assert rollout.model is not None
    state = rollout.model.state_dict()
    rollout.update_weights(state)
    assert rollout.version >= 1


def test_pd_disaggregation_accounts_for_kv_transfer():
    tokenizer, batch = make_batch()
    config = RolloutConfig(
        prompt_length=4,
        response_length=1,
        allowed_chars="xyzw",
        disaggregation={
            "enabled": True,
            "prefill_replicas": 2,
            "decode_replicas": 1,
            "transfer_backend": "nixl",
        },
    )
    model_config = ModelConfig(arch="linear", hidden_size=8, num_layers=1, num_heads=2, max_seq_len=8)
    rollout = build_rollout(config, tokenizer, seed=0, model_config=model_config)
    assert isinstance(rollout, PDDisaggregatedRollout)
    out = rollout.generate_sequences(batch)
    assert out.meta_info["pd/prefill_tokens"] == float(batch.batch["prompt_length"].sum())
    kv_bytes = out.meta_info["pd/kv_transfer_bytes"]
    assert kv_bytes > 0
    # 400 Gbps is 400e9 bits/s, so a byte costs 8/400e9 seconds.
    expected_ms = kv_bytes * 8.0 / (400.0 * 1e9) * 1000.0
    assert out.meta_info["pd/transfer_ms"] == pytest.approx(expected_ms)
    assert out.meta_info["pd/prefill_replicas"] == 2.0


def test_pd_matches_naive_sampling_with_same_seed():
    tokenizer, batch = make_batch()
    model_config = ModelConfig(arch="linear", hidden_size=8, num_layers=1, num_heads=2, max_seq_len=8)
    naive = build_rollout(
        RolloutConfig(prompt_length=4, response_length=1, allowed_chars="xyzw"),
        tokenizer,
        seed=11,
        model_config=model_config,
    )
    pd = build_rollout(
        RolloutConfig(
            prompt_length=4,
            response_length=1,
            allowed_chars="xyzw",
            disaggregation={"enabled": True},
        ),
        tokenizer,
        seed=11,
        model_config=model_config,
    )
    assert naive.generate_sequences(batch).non_tensor_batch["response_text"].tolist() == pd.generate_sequences(
        batch
    ).non_tensor_batch["response_text"].tolist()


def test_delta_checkpoint_engine_applies_partial_update():
    tokenizer, _ = make_batch()
    config = RolloutConfig(prompt_length=4, response_length=1, allowed_chars="xyzw")
    model_config = ModelConfig(arch="linear", hidden_size=8, max_seq_len=8)
    rollout = build_rollout(config, tokenizer, seed=0, model_config=model_config)
    state = rollout.model.state_dict()
    manager = CheckpointEngineManager("delta", replicas=1)

    first = manager.update_weights(state, [rollout])
    changed_name = next(iter(state))
    state[changed_name] = state[changed_name] + 1.0
    second = manager.update_weights(state, [rollout])

    assert second.bytes_sent < first.bytes_sent
    assert rollout.version == 2
    assert np.allclose(rollout.model.state_dict()[changed_name], state[changed_name])


def test_tp_rollout_reshard():
    tokenizer, batch = make_batch()
    config = RolloutConfig(prompt_length=4, response_length=1, tensor_model_parallel_size=2)
    model_config = ModelConfig(arch="linear", hidden_size=8, max_seq_len=8)
    rollout = build_rollout(config, tokenizer, seed=0, model_config=model_config)
    assert isinstance(rollout, TPRollout)
    state = {key: np.zeros_like(value) for key, value in rollout.model.state_dict().items()}
    rollout.update_weights(state)
    assert rollout.last_reshard_bytes > 0


def test_checkpoint_manager_roundtrip(tmp_path):
    config = PPOConfig()
    tokenizer = CharTokenizer()
    manager = CheckpointManager(tmp_path, max_to_keep=2)
    actor = {"w": np.arange(4, dtype=np.float32)}
    critic = {"v": np.arange(3, dtype=np.float32)}
    for step in range(3):
        manager.save(step, actor, critic, config=config, tokenizer=tokenizer, metrics={"a": step})
    assert manager.list_checkpoints() == [1, 2]
    loaded_actor, loaded_critic, state = manager.load(2)
    assert np.allclose(loaded_actor["w"], actor["w"])
    assert np.allclose(loaded_critic["v"], critic["v"])
    assert state["step"] == 2
    loaded_config = manager.load_config(2)
    assert loaded_config.algorithm.adv_estimator == config.algorithm.adv_estimator


def test_safetensors_roundtrip(tmp_path):
    tensors = {"a": np.arange(6, dtype=np.float32).reshape(2, 3), "b": np.zeros((1,), dtype=np.float32)}
    path = tmp_path / "model.safetensors"
    write_safetensors(path, tensors)
    loaded = read_safetensors(path)
    assert set(loaded) == set(tensors)
    assert np.allclose(loaded["a"], tensors["a"])


def test_export_hf_like(tmp_path):
    directory = export_hf_like({"w": np.zeros((2, 2), dtype=np.float32)}, tmp_path / "export")
    assert (directory / "model.safetensors").exists()
    assert (directory / "manifest.json").exists()


def test_replay_buffer_drop_and_keep():
    batch = DataProto(batch={"x": np.arange(4)}, meta_info={})
    dropped = ReplayBuffer(max_off_policy_threshold=1, strategy="drop")
    dropped.add(batch, version=0, produced_at=0)
    with pytest.raises(IndexError):
        dropped.sample(2, current_version=5)
    dropped.add(batch, version=4, produced_at=1)
    data, metrics = dropped.sample(2, current_version=5)
    assert len(data) == 2
    assert metrics["async/staleness_mean"] == 1.0

    kept = ReplayBuffer(max_off_policy_threshold=1, strategy="keep")
    kept.add(batch, version=0, produced_at=0)
    data, metrics = kept.sample(2, current_version=5)
    assert len(data) == 2
    assert metrics["async/staleness_max"] == 5
    # The batch keeps the producing version, not the consumption version.
    assert np.all(data.batch["rollout_version"] == 0)
    assert np.all(data.batch["staleness"] == 5)


def test_rollout_correction():
    mask = np.ones((2, 3), dtype=np.float32)
    data = DataProto(batch={"response_mask": mask, "rollout_log_probs": np.zeros((2, 3), dtype=np.float32)})
    weights, metrics = compute_rollout_correction(data, np.zeros((2, 3), dtype=np.float32))
    assert weights.shape == mask.shape
    assert np.allclose(weights, 1.0)
    assert metrics["rollout_is/weight_mean"] == pytest.approx(1.0)
