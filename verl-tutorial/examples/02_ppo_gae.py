"""PPO with a critic: GAE, clipped value loss and actor/critic updates."""

from _common import header, ppo_config

from mini_verl.trainer.ppo_trainer import PPOTrainer


def main() -> None:
    header("PPO + GAE: the critic learns values while the actor learns actions")
    config = ppo_config(steps=20)
    trainer = PPOTrainer(config).init()
    print(f"critic enabled: {trainer.use_critic}")
    for step in range(5):
        metrics = trainer.train_step()
        print(
            f"step {step + 1}: reward={metrics.get('train/reward_mean', 0):.3f} "
            f"pg_loss={metrics.get('actor/pg_loss', 0):.4f} "
            f"vf_loss={metrics.get('critic/vf_loss', 0):.4f} "
            f"clipfrac={metrics.get('actor/pg_clipfrac', 0):.3f}"
        )
        trainer.global_steps += 1
    print("validation:", trainer.validate())


if __name__ == "__main__":
    main()
