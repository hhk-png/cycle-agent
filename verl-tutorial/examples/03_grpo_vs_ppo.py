"""Compare PPO (critic + GAE) and GRPO (group-relative, no critic)."""

from _common import bandit_config, header, ppo_config

from mini_verl.trainer.ppo_trainer import PPOTrainer


def run(label, config):
    trainer = PPOTrainer(config).init()
    result = trainer.fit()
    validation = trainer.validate()["val/accuracy"]
    print(f"{label}: steps={result['steps']} val_accuracy={validation:.3f}")
    return validation


def main() -> None:
    header("PPO vs GRPO: same task, different variance/cost trade-off")
    ppo = run("PPO/GAE", ppo_config(steps=20))
    grpo = run("GRPO", bandit_config(steps=30, n=8))
    print(f"delta(GRPO - PPO) = {grpo - ppo:+.3f}")
    print("GRPO needs no critic but must sample a group; PPO uses a value baseline.")


if __name__ == "__main__":
    main()
