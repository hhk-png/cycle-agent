"""Train the same bandit task with vanilla PPO clipping and CISPO.

CISPO detaches the clipped importance weight and keeps the score-function
gradient.  On a four-action bandit both objectives learn; the point of the
example is to show that the selector is a real code path, not a renamed
vanilla loss.
"""

import numpy as np

from _common import bandit_config, header

from mini_verl.algo.core_algos import compute_policy_loss, compute_policy_loss_cispo
from mini_verl.autograd import Tensor
from mini_verl.trainer.ppo_trainer import PPOTrainer


def compare_on_fixed_batch() -> None:
    """One token where rho = e^10 is far outside the trust region."""
    old = Tensor(np.zeros((1, 1), dtype=np.float32))
    new = Tensor(np.full((1, 1), 10.0, dtype=np.float32))
    advantage = np.ones((1, 1), dtype=np.float32)
    mask = np.ones((1, 1), dtype=np.float32)
    ppo, _ = compute_policy_loss(old, new, advantage, mask, clip_ratio=0.2)
    cispo, metrics = compute_policy_loss_cispo(old, new, advantage, mask, clip_ratio=0.2)
    print(f"fixed batch: ppo_loss={ppo.item():.3f} cispo_loss={cispo.item():.3f}")
    print(
        "cispo clips the *weight* to 1.2 but keeps log pi in the objective, "
        f"so its loss is -1.2 * log pi = {cispo.item():.3f}; clipfrac={metrics['actor/cispo_clipfrac']:.0f}"
    )


def run(mode: str, steps: int = 40) -> float:
    config = bandit_config(steps=steps, n=8)
    config.actor_rollout_ref.actor.policy_loss.loss_mode = mode
    trainer = PPOTrainer(config).init()
    result = trainer.fit()
    metrics = trainer.validate()
    print(
        f"{mode:>7}: steps={result['steps']} "
        f"val_accuracy={metrics['val/accuracy']:.3f} "
        f"train_reward={result['history'].get('data/reward_mean', float('nan')):.3f}"
    )
    return float(metrics["val/accuracy"])


def main() -> None:
    header("CISPO vs vanilla PPO: clip the weight, keep the score-function gradient")
    compare_on_fixed_batch()
    vanilla = run("vanilla")
    cispo = run("cispo")
    print(f"delta(cispo - vanilla) = {cispo - vanilla:+.3f}")
    print("Both are policy gradients; only CISPO keeps a gradient when rho is clipped.")


if __name__ == "__main__":
    main()
