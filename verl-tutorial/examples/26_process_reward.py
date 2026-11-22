"""Train a process reward model (PRM), inspect step scores, then use it in PPO.

Part A uses the two-character "copy" task (prompt ``ax=`` -> ``ax``) because it
has multiple steps: the same trajectory can be judged by ``last`` (optimistic),
``min`` (conservative), ``mean`` (diluted) or ``product`` (probability that
every step is correct).  Part B uses the single-token contextual bandit for the
PPO integration, because MiniVerl's byte-sized policy is intentionally tuned for
that action space; the PRM still scores a real prefix (action + EOS) and the
manager still performs aggregation and scatter.

The example checks five contracts:

1. step labels and prefix construction are correct;
2. a trained PRM separates a corrupted trajectory from its positive twin;
3. a checkpoint reproduces step scores;
4. ``ProcessRewardManager`` combines dense PRM shaping with a verifier;
5. the combined reward plugs into PPO without a special trainer branch.
"""

from __future__ import annotations

import numpy as np

from _common import header
from mini_verl.config import PPOConfig
from mini_verl.data import CharTokenizer, build_dataset
from mini_verl.prm import (
    AGGREGATIONS,
    PRMTrainer,
    ProcessRewardManager,
    aggregate_step_probs,
    build_step_samples,
    load_prm,
)
from mini_verl.rewards import bandit_reward
from mini_verl.rollout import SamplingParams
from mini_verl.trainer.ppo_trainer import PPOTrainer


def _score_sample(manager: ProcessRewardManager, sample) -> np.ndarray:
    """Score one full trajectory and return its per-step probabilities."""

    response = [token for step in sample.steps for token in step]
    input_ids = np.asarray([sample.prompt_ids + response], dtype=np.int64)
    attention_mask = np.ones(input_ids.shape, dtype=np.float32)
    response_mask = np.zeros(input_ids.shape, dtype=np.float32)
    response_mask[:, len(sample.prompt_ids) :] = 1.0
    boundaries = np.cumsum([len(step) for step in sample.steps]) - 1
    _, step_scores, _ = manager.score_steps(
        input_ids,
        attention_mask,
        response_mask=response_mask,
        step_boundaries=[boundaries],
    )
    return step_scores[0]


def _copy_prm() -> ProcessRewardManager:
    """Multi-step PRM on the copy task, used for the aggregation table."""

    tokenizer = CharTokenizer()
    dataset = build_dataset("copy", 8, seed=0)
    samples = build_step_samples(
        dataset,
        tokenizer,
        max_prompt_length=16,
        max_response_length=4,
        step_tokens=1,
        corruptions_per_sample=1,
        label_mode="local",
        seed=0,
    )
    positive, corrupted = samples[0], samples[1]
    print(
        f"positive: labels={positive.labels} steps={positive.num_steps} | "
        f"corrupted: labels={corrupted.labels} steps={corrupted.num_steps}"
    )
    assert positive.trajectory_label == 1 and corrupted.trajectory_label == 0
    assert len(positive.labels) == len(corrupted.labels)

    config = PPOConfig()
    config.trainer.logger = []
    config.trainer.seed = 3
    config.trainer.log_dir = "outputs/example_prm"
    config.trainer.experiment_name = "copy"
    config.data.dataset = "copy"
    config.data.train_size = 128
    config.data.val_size = 32
    config.data.train_batch_size = 32
    config.data.max_prompt_length = 16
    config.data.max_response_length = 4
    config.reward.prm = {
        "enable": True,
        "arch": "mlp",
        "hidden_size": 32,
        "pooling": "mean",
        "step_tokens": 1,
        # ``suffix`` teaches "an earlier mistake stays wrong"; with only
        # ``local`` labels the PRM can score a later correct token highly even
        # when the prefix is already off track.  Several corruptions keep the
        # wrong-token distribution close to the rollout.
        "label_mode": "suffix",
        "corruptions_per_sample": 8,
        "aggregation": "min",
        "lr": 0.02,
        "train_batch_size": 32,
        "total_steps": 100,
        "eval_freq": 20,
        "target_accuracy": 0.96,
        "patience": 5,
    }

    trainer = PRMTrainer(config)
    result = trainer.fit()
    print(
        f"\ncopy PRM step accuracy={result['step_accuracy']:.3f} "
        f"trajectory accuracy={result['trajectory_accuracy']:.3f} "
        f"Brier={result['brier']:.4f}"
    )
    assert result["step_accuracy"] >= 0.85, result
    assert result["trajectory_accuracy"] >= 0.75, result

    checkpoint = trainer.save("outputs/example_prm/copy_prm")
    model, metadata = load_prm(checkpoint)
    print(f"checkpoint={checkpoint} step_tokens={metadata['step_tokens']}")

    manager = ProcessRewardManager(
        checkpoint,
        aggregation="min",
        step_tokens=int(metadata["step_tokens"]),
    )
    positive_scores = _score_sample(manager, positive)
    corrupted_scores = _score_sample(manager, corrupted)
    print("\nstep probabilities")
    print(f"  positive : {np.round(positive_scores, 4).tolist()}")
    print(f"  corrupted: {np.round(corrupted_scores, 4).tolist()}")
    print("\naggregation            positive   corrupted")
    for method in AGGREGATIONS:
        print(
            f"  {method:<10} {aggregate_step_probs(positive_scores, method):>10.4f} "
            f"{aggregate_step_probs(corrupted_scores, method):>11.4f}"
        )
    # The corrupted prefix must be detected by the conservative aggregation.
    # ``last`` may look healthy with local labels, which is the point.
    assert aggregate_step_probs(corrupted_scores, "min") < 0.5
    assert aggregate_step_probs(positive_scores, "min") > 0.5
    return manager


def _bandit_prm_for_ppo() -> str:
    """Single-token PRM used by the PPO integration below."""

    config = PPOConfig()
    config.trainer.logger = []
    config.trainer.seed = 3
    config.trainer.log_dir = "outputs/example_prm"
    config.trainer.experiment_name = "bandit"
    config.data.dataset = "bandit"
    config.data.train_size = 128
    config.data.val_size = 32
    config.data.train_batch_size = 32
    config.data.max_prompt_length = 8
    config.data.max_response_length = 4
    config.reward.prm = {
        "enable": True,
        "arch": "mlp",
        "hidden_size": 32,
        "pooling": "mean",
        "step_tokens": 1,
        "label_mode": "suffix",
        "corruptions_per_sample": 4,
        "aggregation": "min",
        "lr": 0.02,
        "train_batch_size": 32,
        "total_steps": 60,
        "eval_freq": 20,
        "target_accuracy": 0.95,
        "patience": 4,
    }
    trainer = PRMTrainer(config)
    result = trainer.fit()
    print(
        f"\nbandit PRM step accuracy={result['step_accuracy']:.3f} "
        f"trajectory accuracy={result['trajectory_accuracy']:.3f}"
    )
    assert result["step_accuracy"] >= 0.85, result
    assert result["trajectory_accuracy"] >= 0.8, result
    return str(trainer.save("outputs/example_prm/bandit_prm"))


def main() -> None:
    header("26. Process reward model: step labels -> prefix scores -> PPO reward")
    _copy_prm()
    checkpoint = _bandit_prm_for_ppo()

    # Use the PRM as dense shaping on top of the verifiable bandit reward.
    # A PRM is a learned proxy, not a verifier: PRM-only PPO can reward-hack
    # the prefix scorer with a high-scoring but wrong sequence.
    ppo_config = PPOConfig()
    ppo_config.trainer.logger = []
    ppo_config.trainer.seed = 7
    ppo_config.trainer.val_before_train = False
    ppo_config.trainer.total_training_steps = 60
    ppo_config.trainer.log_dir = "outputs/example_prm"
    ppo_config.trainer.experiment_name = "ppo_with_prm"
    ppo_config.data.dataset = "bandit"
    ppo_config.data.train_size = 64
    ppo_config.data.val_size = 16
    ppo_config.data.train_batch_size = 16
    ppo_config.data.max_prompt_length = 4
    ppo_config.data.max_response_length = 1
    ppo_config.actor_rollout_ref.rollout.n = 4
    ppo_config.actor_rollout_ref.actor.ppo_mini_batch_size = 64
    ppo_config.reward.prm = {
        "enable": True,
        "model_path": checkpoint,
        "aggregation": "min",
        "step_tokens": 1,
        "outcome_reward_fn": "bandit",
        "outcome_weight": 1.0,
        "prm_weight": 0.1,
    }

    ppo_trainer = PPOTrainer(ppo_config)
    ppo_result = ppo_trainer.fit()
    print(
        f"\nPPO steps={ppo_result['steps']} "
        f"train/reward_mean={ppo_result['history']['train/reward_mean']:.3f}"
    )

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
    print(f"true accuracy under PRM shaping + verifier: {accuracy:.3f}")
    assert accuracy >= 0.5, f"policy did not learn: {accuracy}"

    manager = ProcessRewardManager(
        checkpoint,
        aggregation="min",
        step_tokens=1,
        outcome_reward_fn="bandit",
        outcome_weight=1.0,
        prm_weight=0.1,
    )
    scored = manager.run(data.copy())
    assert scored.batch["rm_scores"].shape == (len(prompts),)
    assert np.isfinite(scored.batch["rm_scores"]).all()
    assert "prm_step_scores" in scored.non_tensor_batch
    assert "prm_outcome_scores" in scored.batch
    print("ProcessRewardManager produced finite, combined scores; example passed.")


if __name__ == "__main__":
    main()
