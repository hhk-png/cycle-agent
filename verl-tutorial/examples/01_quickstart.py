"""A complete MiniVerl run: data -> rollout -> reward -> GRPO -> validation."""

from _common import bandit_config, header

from mini_verl.trainer.ppo_trainer import PPOTrainer


def main() -> None:
    header("MiniVerl quickstart: GRPO on a four-action contextual bandit")
    config = bandit_config(steps=30, n=8)
    trainer = PPOTrainer(config).init()
    result = trainer.fit()
    validation = trainer.validate()
    print(f"steps={result['steps']}")
    print(f"final training reward={result['last'].get('train/reward_mean', 0):.3f}")
    print(f"validation accuracy={validation['val/accuracy']:.3f}")
    print("expected: >= 0.75 once the policy has separated the four contexts")


if __name__ == "__main__":
    main()
