"""A small throughput benchmark for the MiniVerl training loop."""

import time

from _common import bandit_config, header

from mini_verl.engine import estimate_memory
from mini_verl.trainer.ppo_trainer import PPOTrainer


def main() -> None:
    header("Benchmark: steps/s, response tokens/s and a memory estimate")
    config = bandit_config(steps=20, n=8)
    trainer = PPOTrainer(config).init()
    started = time.perf_counter()
    result = trainer.fit()
    elapsed = time.perf_counter() - started
    tokens = result["steps"] * config.data.train_batch_size * config.actor_rollout_ref.rollout.n
    print(f"steps={result['steps']} elapsed_s={elapsed:.2f}")
    print(f"steps/s={result['steps'] / elapsed:.2f} response_tokens/s={tokens / elapsed:.0f}")
    print("final validation:", trainer.validate())
    params = sum(p.size for p in trainer.actor_group.workers[0].actor.parameters())
    print("policy parameters:", params)
    print("memory estimate (GiB):", {k: round(v, 3) for k, v in estimate_memory(params).items()})


if __name__ == "__main__":
    main()
