"""DAPO: dynamic group filtering, clip-higher and the overlong penalty."""

import numpy as np

from _common import bandit_config, header

from mini_verl.algo.core_algos import filter_groups_for_dapo
from mini_verl.rewards import DAPORewardManager
from mini_verl.trainer.ppo_trainer import PPOTrainer


def main() -> None:
    header("DAPO dynamic sampling: drop groups with no learning signal")
    scores = np.asarray([1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0], dtype=np.float32)
    index = np.asarray([0, 0, 1, 1, 2, 2, 2, 2])
    keep, stats = filter_groups_for_dapo(scores, index)
    print("kept indices:", keep.tolist())
    print("stats:", stats)
    assert len(keep) == 4

    config = bandit_config(steps=10, n=8)
    config.algorithm.adv_estimator = "dapo"
    config.algorithm.filter_groups.enable = True
    config.algorithm.overlong_penalty_factor = 0.5
    config.data.max_response_length = 2
    config.reward.reward_manager = "dapo"
    config.actor_rollout_ref.actor.clip_ratio_low = 0.2
    config.actor_rollout_ref.actor.clip_ratio_high = 0.28
    trainer = PPOTrainer(config).init()
    metrics = trainer.train_step()
    print("filtered ratio:", metrics.get("dapo/filtered_ratio", 0.0))
    print("clipfrac:", metrics.get("actor/pg_clipfrac", 0.0), "reward:", metrics.get("train/reward_mean", 0.0))
    manager = DAPORewardManager("bandit")
    print("overlong penalty path:", manager.score("x", "x", response_length=4, max_length=4))


if __name__ == "__main__":
    main()
