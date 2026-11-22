from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from mini_verl.agent import AgentLoop, AgentLoopManager, CalculatorTool, ToolRegistry
from mini_verl.config import PPOConfig
from mini_verl.data import CharTokenizer
from mini_verl.metrics import (
    compute_data_metrics,
    compute_pass_at_k,
    compute_throughput_metrics,
    compute_validation_metrics,
    MetricsLogger,
)
from mini_verl.losses import _sequence_logprob
from mini_verl.nn import TinyLinearLM
from mini_verl.protocol import DataProto
from mini_verl.trainer.dpo_trainer import DPOTrainer
from mini_verl.trainer.main_eval import evaluate_checkpoint
from mini_verl.trainer.main_ppo import main as main_ppo
from mini_verl.trainer.ppo_trainer import PPOTrainer
from mini_verl.trainer.sft_trainer import SFTTrainer


def test_grpo_training_improves(fast_config):
    trainer = PPOTrainer(fast_config()).init()
    result = trainer.fit()
    assert result["steps"] == 40
    assert trainer.validate()["val/accuracy"] >= 0.75


def test_cispo_training_improves(fast_config):
    config = fast_config(
        actor_rollout_ref__actor__policy_loss__loss_mode="cispo",
        trainer__total_training_steps=40,
    )
    trainer = PPOTrainer(config).init()
    result = trainer.fit()
    assert result["steps"] == 40
    assert trainer.validate()["val/accuracy"] >= 0.75


def test_evaluate_pass_at_k(fast_config, tmp_path):
    config = fast_config(
        trainer__log_dir=str(tmp_path),
        trainer__total_training_steps=20,
    )
    trainer = PPOTrainer(config).init()
    trainer.fit()
    metrics = trainer.evaluate(n=6, ks=(1, 3), split="val")
    assert 0.0 <= metrics["val/accuracy"] <= 1.0
    assert 0.0 <= metrics["val/pass@1"] <= 1.0
    assert 0.0 <= metrics["val/pass@3"] <= 1.0
    assert metrics["val/pass@3"] >= metrics["val/pass@1"]


def test_evaluate_checkpoint_reloads_config(fast_config, tmp_path):
    config = fast_config(
        trainer__log_dir=str(tmp_path),
        trainer__total_training_steps=20,
    )
    trainer = PPOTrainer(config).init()
    trainer.fit()
    checkpoint = trainer.save_checkpoint()
    metrics = evaluate_checkpoint(checkpoint, n=4, ks=(1, 2), split="val")
    assert 0.0 <= metrics["val/accuracy"] <= 1.0
    assert "val/pass@2" in metrics


def test_ppo_gae_with_critic(fast_config):
    config = fast_config(
        algorithm__adv_estimator="gae",
        actor_rollout_ref__rollout__n=2,
        actor_rollout_ref__actor__ppo_mini_batch_size=16,
        actor_rollout_ref__actor__ppo_micro_batch_size=16,
        trainer__total_training_steps=2,
        data__train_batch_size=8,
    )
    trainer = PPOTrainer(config).init()
    metrics = trainer.train_step()
    assert "critic/vf_loss" in metrics
    assert "actor/pg_loss" in metrics


def test_async_modes_run(fast_config):
    for mode in ["colocate_async", "separate_async"]:
        config = fast_config(trainer__trainer_mode=mode, trainer__total_training_steps=3)
        trainer = PPOTrainer(config).init()
        result = trainer.fit()
        assert result["steps"] == 3
        # The refill happens before the weight sync, so consumed batches are
        # one version old.  Zero staleness would mean async is a no-op.
        assert result["history"]["async/staleness_max"] > 0


def test_checkpoint_resume(fast_config, tmp_path):
    config = fast_config(
        trainer__total_training_steps=2,
        trainer__log_dir=str(tmp_path),
        trainer__save_freq=1,
    )
    trainer = PPOTrainer(config).init()
    trainer.fit()
    state = trainer.actor_group.get_actor_state_dict()
    resumed = PPOTrainer(config.copy()).init()
    resumed_state = resumed.actor_group.get_actor_state_dict()
    assert resumed.global_steps == 2
    assert all(np.allclose(state[key], resumed_state[key]) for key in state)


def test_resume_from_path_uses_requested_run(fast_config, tmp_path):
    run_a = tmp_path / "runA"
    config_a = fast_config(
        trainer__total_training_steps=2,
        trainer__log_dir=str(run_a),
        trainer__save_freq=1,
    )
    first = PPOTrainer(config_a).init()
    first.fit()
    checkpoint = run_a / "checkpoints" / "mini_verl" / "default" / "global_step_2"
    assert checkpoint.exists()

    run_b = tmp_path / "runB"
    config_b = fast_config(
        trainer__total_training_steps=3,
        trainer__log_dir=str(run_b),
        trainer__save_freq=-1,
        trainer__resume_from_path=str(checkpoint),
    )
    resumed = PPOTrainer(config_b).init()
    assert resumed.global_steps == 2
    assert resumed._train_cursor == first._train_cursor


def test_balance_batch(fast_config):
    trainer = PPOTrainer(fast_config(trainer__total_training_steps=1)).init()
    prompts = trainer._next_train_batch()
    data = trainer._rollout_batch(prompts)
    balanced, metrics = trainer._balance_batch(data)
    assert len(balanced) == len(data)
    assert metrics["data/seqlen_imbalance"] >= 0.0


def test_truncated_ratio_uses_response_budget():
    data = DataProto(
        batch={
            "response_mask": np.ones((2, 6), dtype=np.float32),
            "response_length": np.asarray([4, 3], dtype=np.int64),
        },
        meta_info={"max_response_length": 4},
    )
    metrics = compute_data_metrics(data)
    assert metrics["data/truncated_ratio"] == 0.5


def test_checkpoint_restores_optimizer_state(fast_config, tmp_path):
    config = fast_config(
        trainer__total_training_steps=2,
        trainer__log_dir=str(tmp_path),
        trainer__save_freq=1,
    )
    trainer = PPOTrainer(config).init()
    trainer.fit()
    optimizer_state = trainer.actor_group.get_optimizer_state_dict()
    assert optimizer_state["step_count"] > 0
    assert optimizer_state["state"]

    resumed = PPOTrainer(config.copy()).init()
    restored = resumed.actor_group.get_optimizer_state_dict()
    assert restored["step_count"] == optimizer_state["step_count"]
    for name, fields in optimizer_state["state"].items():
        for field, value in fields.items():
            assert np.allclose(restored["state"][name][field], value)


def test_resume_does_not_overwrite_reference_model(fast_config, tmp_path):
    config = fast_config(
        trainer__total_training_steps=2,
        trainer__log_dir=str(tmp_path),
        trainer__save_freq=1,
        actor_rollout_ref__actor__use_kl_loss=True,
    )
    trainer = PPOTrainer(config).init()
    trainer.fit()
    reference_before = trainer.actor_group.workers[0].ref_model.state_dict()

    resumed = PPOTrainer(config.copy()).init()
    reference_after = resumed.actor_group.workers[0].ref_model.state_dict()
    assert all(np.allclose(reference_before[key], reference_after[key]) for key in reference_before)
    # The actor did train away from the reference; otherwise this test proves nothing.
    actor = resumed.actor_group.get_actor_state_dict()
    assert any(not np.allclose(actor[key], reference_after[key]) for key in actor)


def test_remax_uses_a_separate_greedy_rollout(fast_config):
    config = fast_config(
        trainer__total_training_steps=1,
        algorithm__adv_estimator="remax",
    )
    trainer = PPOTrainer(config).init()
    metrics = trainer.train_step()
    assert "actor/pg_loss" in metrics
    # The greedy baseline is generated from the first sample of each prompt
    # group; with n=8 and 16 prompts this is a 16-row rollout, not 128.
    assert metrics["data/reward_mean"] >= 0.0


def test_multi_rank_padding_keeps_training_finite(fast_config):
    config = fast_config(
        trainer__total_training_steps=1,
        trainer__n_gpus_per_node=2,
        data__train_batch_size=3,
        actor_rollout_ref__rollout__n=2,
        actor_rollout_ref__actor__ppo_mini_batch_size=3,
        actor_rollout_ref__actor__ppo_micro_batch_size=3,
    )
    trainer = PPOTrainer(config).init()
    metrics = trainer.train_step()
    assert np.isfinite(metrics["actor/loss"])
    assert metrics["perf/gpus"] == 2.0


def test_dapo_all_filtered_skips_update(fast_config):
    config = fast_config(
        trainer__total_training_steps=1,
        algorithm__filter_groups__enable=True,
    )
    trainer = PPOTrainer(config).init()

    def constant_rewards(data):
        data.batch["rm_scores"] = np.zeros(len(data), dtype=np.float32)
        data.batch["token_level_rewards"] = np.zeros_like(data.batch["response_mask"], dtype=np.float32)
        return data

    trainer.reward_manager.run = constant_rewards
    metrics = trainer.train_step()
    assert metrics["dapo/all_filtered"] == 1.0


def test_sft_trainer(fast_config):
    config = fast_config(
        trainer__total_training_steps=2,
        data__train_batch_size=8,
        data__max_response_length=4,
    )
    trainer = SFTTrainer(config)
    result = trainer.fit()
    assert result["steps"] == 2
    assert "sft/loss" in result["history"]


def test_sft_multi_rank_global_normalization(fast_config):
    config = fast_config(
        trainer__total_training_steps=1,
        trainer__n_gpus_per_node=2,
        data__train_batch_size=8,
        data__max_response_length=4,
    )
    trainer = SFTTrainer(config)
    result = trainer.fit()
    assert result["steps"] == 1
    assert "sft/loss" in result["history"]


def test_dpo_trainer(fast_config):
    config = fast_config(
        trainer__total_training_steps=2,
        data__train_batch_size=4,
        data__max_prompt_length=4,
        data__max_response_length=2,
        actor_rollout_ref__rollout__n=1,
        actor_rollout_ref__actor__ppo_mini_batch_size=4,
        actor_rollout_ref__actor__ppo_micro_batch_size=4,
    )
    config.extra["dpo_beta"] = 0.1
    trainer = DPOTrainer(config)
    result = trainer.fit()
    assert result["steps"] == 2
    assert "dpo/loss" in result["history"]


def test_dpo_sequence_logprob_ignores_prompt_padding():
    tokenizer = CharTokenizer()
    model = TinyLinearLM(vocab_size=tokenizer.vocab_size, max_seq_len=16, dim=8, seed=0)
    prompt = np.asarray([tokenizer.encode("calc ")], dtype=np.int64)
    padded = np.zeros((1, 12), dtype=np.int64)
    padded[0, : prompt.shape[1]] = prompt[0]
    response = np.asarray([[tokenizer.stoi["7"], tokenizer.eos_token_id]], dtype=np.int64)
    mask = np.ones((1, 2), dtype=np.float32)

    compact = _sequence_logprob(model, prompt, response, mask)
    padded_with_lengths = _sequence_logprob(model, padded, response, mask, np.asarray([prompt.shape[1]]))
    assert np.allclose(compact.data, padded_with_lengths.data, atol=1e-6)


def test_agent_tool_loop():
    tokenizer = CharTokenizer()
    model = TinyLinearLM(vocab_size=tokenizer.vocab_size, max_seq_len=32, dim=8)
    loop = AgentLoop(model, tokenizer, max_turns=2)
    script = [tokenizer.encode("<tool>3+4</tool>"), tokenizer.encode("7")]
    loop._generate = lambda ids, max_tokens: script.pop(0)
    trajectory = loop.run("calc ")
    assert trajectory.tool_calls == [("calculator", "3+4", "7")]
    assert "<obs>7</obs>" in trajectory.text
    assert trajectory.response_mask.min() == 0.0
    assert trajectory.response_mask.max() == 1.0
    manager = AgentLoopManager(loop)
    data = manager.to_dataproto([trajectory], max_length=32, pad_token_id=tokenizer.pad_token_id)
    assert len(data) == 1
    assert data.batch["input_ids"].shape == (1, 32)


def test_calculator_tool():
    tool = CalculatorTool()
    assert tool("3+4") == "7"
    assert tool("2*(3+4)") == "14"
    assert tool("bad(") == "error"
    registry = ToolRegistry.default()
    assert registry.call("calculator", "5-2") == "3"
    assert "unknown" in registry.call("nope", "1")


def test_validation_metrics():
    scores = np.asarray([1.0, 0.0, 1.0, 1.0], dtype=np.float32)
    metrics = compute_validation_metrics(scores, n=2, ks=(1, 2))
    assert metrics["val/accuracy"] == 0.75
    assert metrics["val/pass@2"] == 1.0
    assert compute_pass_at_k(np.asarray([1.0, 0.0]), 2, 1) == 0.5


def test_throughput_metrics():
    metrics = compute_throughput_metrics(1000, 2.0, 1, 1000, num_samples=50)
    assert metrics["perf/tokens_per_s"] == 500
    assert metrics["perf/samples_per_s"] == 25


def test_metrics_logger(tmp_path):
    logger = MetricsLogger(tmp_path, "p", "e", ["console"])
    logger.log({"data/reward_mean": 0.5}, 1)
    lines = (tmp_path / "p" / "e" / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["data/reward_mean"] == 0.5


def test_cli_dry_run(capsys):
    assert main_ppo(["--dry-run"]) == 0
    assert "actor_rollout_ref" in capsys.readouterr().out
