"""Train, save a checkpoint, resume, and compare the actor weights."""

import tempfile
from pathlib import Path

import numpy as np

from _common import bandit_config, header

from mini_verl.trainer.ppo_trainer import PPOTrainer


def main() -> None:
    header("Checkpoint and resume: optimizer state rides along with the model")
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        config = bandit_config(steps=5, n=8)
        config.trainer.log_dir = str(directory)
        config.trainer.save_freq = 1
        trainer = PPOTrainer(config).init()
        trainer.fit()
        before = trainer.actor_group.get_actor_state_dict()
        print("checkpoints:", trainer.checkpoint_manager.list_checkpoints())

        resumed = PPOTrainer(config.copy()).init()
        after = resumed.actor_group.get_actor_state_dict()
        equal = all(np.allclose(before[key], after[key]) for key in before)
        print("resumed global step:", resumed.global_steps)
        print("actor weights identical:", equal)


if __name__ == "__main__":
    main()
