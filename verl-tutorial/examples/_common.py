"""Shared helpers for the runnable examples."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini_verl.config import PPOConfig  # noqa: E402


def bandit_config(steps: int = 30, n: int = 8) -> PPOConfig:
    config = PPOConfig()
    config.trainer.logger = []
    config.trainer.val_before_train = False
    config.trainer.total_training_steps = steps
    config.trainer.n_gpus_per_node = 1
    config.trainer.nnodes = 1
    config.data.train_size = 64
    config.data.val_size = 32
    config.data.train_batch_size = 16
    config.data.max_prompt_length = 4
    config.data.max_response_length = 1
    config.actor_rollout_ref.rollout.n = n
    config.actor_rollout_ref.actor.ppo_mini_batch_size = 64
    config.actor_rollout_ref.actor.ppo_micro_batch_size = 64
    config.algorithm.adv_estimator = "grpo"
    return config


def ppo_config(steps: int = 20) -> PPOConfig:
    config = bandit_config(steps=steps, n=2)
    config.algorithm.adv_estimator = "gae"
    config.actor_rollout_ref.actor.ppo_mini_batch_size = 16
    config.actor_rollout_ref.actor.ppo_micro_batch_size = 16
    config.data.train_batch_size = 8
    return config


def header(title: str) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)
