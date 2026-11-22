"""SFT and DPO on the same worker/engine stack as PPO."""

from _common import bandit_config, header

from mini_verl.trainer.dpo_trainer import DPOTrainer
from mini_verl.trainer.sft_trainer import SFTTrainer


def main() -> None:
    header("SFT and DPO: supervised and preference objectives reuse the engine")
    sft_config = bandit_config(steps=5, n=1)
    sft_config.trainer.total_training_steps = 5
    sft_config.data.train_batch_size = 8
    sft = SFTTrainer(sft_config)
    sft_result = sft.fit()
    print("SFT:", {k: round(v, 4) for k, v in sft_result["history"].items()})

    dpo_config = bandit_config(steps=5, n=1)
    dpo_config.trainer.total_training_steps = 5
    dpo_config.data.train_batch_size = 8
    dpo_config.data.max_response_length = 2
    dpo_config.actor_rollout_ref.actor.ppo_mini_batch_size = 8
    dpo_config.actor_rollout_ref.actor.ppo_micro_batch_size = 8
    dpo_config.extra["dpo_beta"] = 0.1
    dpo = DPOTrainer(dpo_config)
    dpo_result = dpo.fit()
    print("DPO:", {k: round(v, 4) for k, v in dpo_result["history"].items()})


if __name__ == "__main__":
    main()
