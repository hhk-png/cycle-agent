"""Regression tests for bugs found by the independent code audit.

Each test pins a behavior that previously looked plausible but was wrong:
DPO normalization across ranks, reward failure/timeout handling, IS
normalization, delta accounting, LoRA target parsing, cache invalidation and
checkpoint pruning.
"""

from __future__ import annotations

import time

import numpy as np

from mini_verl.async_policy import compute_rollout_correction
from mini_verl.checkpoint import CheckpointManager
from mini_verl.config import ModelConfig, RolloutConfig
from mini_verl.data import CharTokenizer, build_dataset, collate_fn
from mini_verl.lora import apply_lora
from mini_verl.nn import TinyLM
from mini_verl.protocol import DataProto
from mini_verl.rewards import RewardLoopManager, RewardManager
from mini_verl.rollout import build_rollout
from mini_verl.sharding import CheckpointEngineManager
from mini_verl.trainer.dpo_trainer import DPOTrainer


def _batch(n: int = 4) -> DataProto:
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", n, seed=0)
    return collate_fn(dataset.samples, tokenizer, 4, 1)


def test_dpo_gradient_matches_across_world_sizes(fast_config):
    """DPO's global pair normalization must survive DP padding/averaging."""

    def run(world: int) -> dict[str, np.ndarray]:
        config = fast_config(
            trainer__n_gpus_per_node=world,
            trainer__total_training_steps=1,
            data__train_batch_size=8,
            data__train_size=32,
            data__max_response_length=2,
            actor_rollout_ref__actor__ppo_mini_batch_size=8,
            actor_rollout_ref__actor__ppo_micro_batch_size=8,
        )
        config.extra["dpo_beta"] = 0.1
        trainer = DPOTrainer(config)
        trainer.fit()
        return trainer.group.get_actor_state_dict()

    single = run(1)
    multi = run(3)  # 8 pairs, world 3: padding + unequal real pair counts
    assert set(single) == set(multi)
    for key in single:
        # Float32 accumulation differs by a few 1e-4 across ranks; the old
        # local-normalization bug produced ~2e-2 differences.
        assert np.allclose(single[key], multi[key], atol=1e-3, rtol=1e-3), key


def test_reward_exception_in_positional_fallback_returns_zero():
    def bad(response, answer):  # no **kwargs: first call raises TypeError
        raise ValueError("verifier bug")

    manager = RewardManager(bad)
    assert manager.score("answer", "42") == 0.0


def test_reward_loop_timeout_is_counted():
    def slow(response, answer):
        time.sleep(0.05)
        return 1.0

    batch = _batch(1)
    batch.non_tensor_batch["response_text"] = np.asarray(["x"], dtype=object)
    batch.non_tensor_batch["answers"] = np.asarray(["x"], dtype=object)
    loop = RewardLoopManager(slow, max_workers=1, timeout=0.001)
    out = loop.run(batch)
    assert loop.timeouts == 1
    assert out.batch["rm_scores"][0] == 0.0


def test_rollout_correction_keeps_uniform_shift():
    batch = _batch(2)
    batch.batch["response_mask"] = np.ones((2, 2), dtype=np.float32)
    batch.batch["rollout_log_probs"] = np.zeros((2, 2), dtype=np.float32)
    current = np.full((2, 2), 0.5, dtype=np.float32)
    weights, _ = compute_rollout_correction(batch, current)
    # A uniform behavior/current shift must not be normalized away.
    assert np.allclose(weights, np.exp(0.5), atol=1e-5)


def test_delta_sharded_bytes_never_exceed_full_sync():
    state = {"a": np.zeros(10, dtype=np.float32)}
    manager = CheckpointEngineManager("delta_sharded")
    first = manager.update_weights(state, [])
    second = manager.update_weights({"a": np.ones(10, dtype=np.float32)}, [])
    assert second.bytes_sent <= first.bytes_sent
    assert second.changed_ratio <= 1.0


def test_lora_string_target_injects_adapters():
    model = TinyLM(vocab_size=12, max_seq_len=8, dim=16, n_layer=1, n_head=2, seed=0)
    replaced = apply_lora(model, rank=4, alpha=8, targets="fc_in")
    assert replaced
    assert all(path.endswith("fc_in") for path in replaced)


def test_prefix_cache_invalidated_on_weight_update():
    tokenizer = CharTokenizer()
    batch = _batch(4)
    config = RolloutConfig(
        prompt_length=4,
        response_length=1,
        allowed_chars="xyzw",
        enable_prefix_caching=True,
    )
    model_config = ModelConfig(arch="linear", hidden_size=8, num_layers=1, num_heads=2, max_seq_len=8)
    rollout = build_rollout(config, tokenizer, seed=0, model_config=model_config)
    rollout.generate_sequences(batch)
    assert rollout.prefix_cache
    rollout.update_weights(rollout.model.state_dict())
    assert not rollout.prefix_cache


def test_checkpoint_prune_zero_keeps_all(tmp_path):
    manager = CheckpointManager(tmp_path, max_to_keep=0)
    state = {"w": np.zeros(3, dtype=np.float32)}
    manager.save(1, state)
    manager.save(2, state)
    manager.prune()
    assert manager.list_checkpoints() == [1, 2]
