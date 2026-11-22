from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini_verl.config import PPOConfig  # noqa: E402


@pytest.fixture
def fast_config():
    def make(**overrides):
        config = PPOConfig()
        config.trainer.logger = []
        config.trainer.val_before_train = False
        config.trainer.total_training_steps = 40
        config.trainer.n_gpus_per_node = 1
        config.trainer.nnodes = 1
        config.data.train_size = 64
        config.data.val_size = 32
        config.data.train_batch_size = 16
        config.data.max_prompt_length = 4
        config.data.max_response_length = 1
        config.actor_rollout_ref.actor.ppo_mini_batch_size = 64
        config.actor_rollout_ref.actor.ppo_micro_batch_size = 64
        config.actor_rollout_ref.rollout.n = 8
        config.algorithm.adv_estimator = "grpo"
        for dotted, value in overrides.items():
            config.set_path(dotted.replace("__", "."), value)
        config.validate()
        return config

    return make


def finite_difference(fn, array: np.ndarray, eps: float = 1e-3) -> np.ndarray:
    array = np.asarray(array, dtype=np.float32)
    grad = np.zeros_like(array)
    it = np.nditer(array, flags=["multi_index"])
    for _ in it:
        index = it.multi_index
        original = array[index]
        array[index] = original + eps
        plus = float(fn(array))
        array[index] = original - eps
        minus = float(fn(array))
        array[index] = original
        grad[index] = (plus - minus) / (2 * eps)
    return grad
