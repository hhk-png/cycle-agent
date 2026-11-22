"""Train a scalar reward model, calibrate it, then use it as the PPO reward.

This is the end-to-end loop that Appendix X describes:

    preference pairs -> Bradley-Terry -> val accuracy/ECE
                     -> checkpoint -> ModelRewardManager -> PPO

The RM is intentionally tiny.  The assertion is about the *contract*: the
chosen/rejected direction is learned, the checkpoint reproduces scores, and the
PPO loop consumes `rm_scores` without a rule-based verifier.
"""

from __future__ import annotations

import numpy as np

from _common import header
from mini_verl.config import PPOConfig
from mini_verl.reward_model import ModelRewardManager, RMTrainer, load_reward_model
from mini_verl.rewards import bandit_reward
from mini_verl.rollout import SamplingParams
from mini_verl.trainer.ppo_trainer import PPOTrainer


def main() -> None:
    header("25. Reward model: preference pairs -> Bradley-Terry -> PPO reward")

    rm_config = PPOConfig()
    rm_config.trainer.logger = []
    rm_config.trainer.seed = 7
    rm_config.trainer.log_dir = "outputs/example_rm"
    rm_config.trainer.experiment_name = "bandit"
    rm_config.data.dataset = "bandit"
    rm_config.data.train_size = 128
    rm_config.data.val_size = 32
    rm_config.data.train_batch_size = 16
    rm_config.data.max_prompt_length = 16
    rm_config.data.max_response_length = 8
    rm_config.reward.reward_model = {
        "enable": True,
        "arch": "mlp",
        "hidden_size": 32,
        "pooling": "mean",
        "lr": 0.02,
        "train_batch_size": 16,
        "total_steps": 80,
        "eval_freq": 20,
        "target_accuracy": 0.99,
    }

    rm_trainer = RMTrainer(rm_config)
    result = rm_trainer.fit()
    print(
        f"\nval pairwise accuracy={result['val_accuracy']:.3f} "
        f"ECE={result['val_ece']:.4f} margin={result['val_margin']:.3f}"
    )
    assert result["val_accuracy"] >= 0.9, result
    assert result["val_ece"] < 0.2, result

    checkpoint = rm_trainer.save("outputs/example_rm/bandit_rm")
    loaded, metadata = load_reward_model(checkpoint)
    print(f"checkpoint={checkpoint} arch={metadata['arch']} pooling={metadata['pooling']}")

    # The same input must produce the same score after a save/load round trip.
    sample = np.asarray([[1, 2, 3, 4, 5]], dtype=np.int64)
    with np.errstate(all="ignore"):
        before = rm_trainer.model(sample, np.ones_like(sample, dtype=np.float32)).item()
        after = loaded(sample, np.ones_like(sample, dtype=np.float32)).item()
    assert abs(before - after) < 1e-6
    print(f"score roundtrip: {before:.4f} -> {after:.4f}")

    # Use the RM as the *only* reward in PPO.  The rule verifier is not called
    # by the trainer; it is used below only to report true accuracy.
    ppo_config = PPOConfig()
    ppo_config.trainer.logger = []
    ppo_config.trainer.seed = 7
    ppo_config.trainer.val_before_train = False
    ppo_config.trainer.total_training_steps = 40
    ppo_config.trainer.log_dir = "outputs/example_rm"
    ppo_config.trainer.experiment_name = "ppo_with_rm"
    ppo_config.data.dataset = "bandit"
    ppo_config.data.train_size = 64
    ppo_config.data.val_size = 32
    ppo_config.data.train_batch_size = 16
    ppo_config.data.max_prompt_length = 4
    ppo_config.data.max_response_length = 1
    ppo_config.actor_rollout_ref.rollout.n = 4
    ppo_config.actor_rollout_ref.actor.ppo_mini_batch_size = 64
    ppo_config.reward.reward_model = {
        "enable": True,
        "model_path": str(checkpoint),
        "normalize": "none",
    }

    ppo_trainer = PPOTrainer(ppo_config)
    ppo_result = ppo_trainer.fit()
    print(f"\nPPO steps={ppo_result['steps']} train/reward_mean={ppo_result['history']['train/reward_mean']:.3f}")

    # True accuracy is measured with a rule verifier that the trainer never
    # sees.  This separates "RM score went up" from "the task was solved".
    ppo_trainer._sync_weights()
    prompts = ppo_trainer.val_batches[0]
    was_asleep = ppo_trainer.rollout.model is None
    if was_asleep:
        ppo_trainer.rollout.wake_up()
    data = ppo_trainer.rollout.generate_sequences(
        prompts, SamplingParams(temperature=0.0, do_sample=False, max_tokens=1)
    )
    if was_asleep and ppo_config.actor_rollout_ref.rollout.free_cache_engine:
        ppo_trainer.rollout.sleep()
    accuracy = float(
        np.mean(
            [
                bandit_reward(str(response), str(answer))
                for response, answer in zip(
                    data.non_tensor_batch["response_text"], data.non_tensor_batch["answers"]
                )
            ]
        )
    )
    print(f"true accuracy under the RM-only reward: {accuracy:.3f}")
    assert accuracy >= 0.5, f"policy did not learn from the RM: {accuracy}"

    manager = ModelRewardManager(checkpoint)
    scored = manager.run(data.copy())
    assert scored.batch["rm_scores"].shape == (len(prompts),)
    assert np.isfinite(scored.batch["rm_scores"]).all()
    print("ModelRewardManager produced finite rm_scores; example passed.")


if __name__ == "__main__":
    main()
