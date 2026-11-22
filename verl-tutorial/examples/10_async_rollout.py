"""Versioned replay buffer and off-policy staleness control."""

import numpy as np

from _common import bandit_config, header

from mini_verl.async_policy import ReplayBuffer
from mini_verl.protocol import DataProto
from mini_verl.trainer.ppo_trainer import PPOTrainer


def main() -> None:
    header("Async rollout: staleness thresholds and replay-buffer drops")
    batch = DataProto(batch={"x": np.arange(4)}, meta_info={})
    buffer = ReplayBuffer(max_off_policy_threshold=2, strategy="drop")
    for version in range(5):
        buffer.add(batch, version=version, produced_at=version)
    data, metrics = buffer.sample(4, current_version=5)
    print("sampled:", len(data), "metrics:", metrics)

    config = bandit_config(steps=4, n=8)
    config.trainer.trainer_mode = "colocate_async"
    config.trainer.max_off_policy_threshold = 2
    trainer = PPOTrainer(config).init()
    result = trainer.fit()
    print("async trainer steps:", result["steps"])
    print("async metrics:", {k: round(v, 3) for k, v in result["last"].items() if k.startswith("async/")})


if __name__ == "__main__":
    main()
