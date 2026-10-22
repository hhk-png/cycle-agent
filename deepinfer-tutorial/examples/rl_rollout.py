"""RL rollout planning: groups, advantages, cost and policy-version checks."""

from __future__ import annotations

import numpy as np

from _common import add_project_root

add_project_root()

from mini_deepinfer.rl_rollout import (
    KVNamespace,
    PartialRollout,
    WeightVersion,
    estimate_rollout_cost,
    group_advantages,
    plan_groups,
    rollout_throughput,
)


def main() -> None:
    prompts = [("math-0", 1200), ("math-1", 800), ("code-0", 2400), ("chat-0", 400)]
    waves = plan_groups(
        prompts, group_size=4, output_budget=256, max_batch_tokens=8000
    )
    print("rollout waves:")
    for index, wave in enumerate(waves):
        print(
            f"  wave {index}: "
            + ", ".join(
                f"{plan.prompt_id}(prompt={plan.prompt_tokens}, reserved={plan.reserved_tokens})"
                for plan in wave
            )
        )

    rewards = [0.2, 0.8, 0.5, 1.0]
    advantages = group_advantages(rewards)
    print(f"rewards: {rewards}")
    print(f"advantages: {np.round(advantages, 4).tolist()}")
    print(f"advantage mean={advantages.mean():.6f} std={advantages.std():.6f}")

    cost = estimate_rollout_cost(
        group_size=4,
        prompt_tokens=1200,
        output_tokens=256,
        prefill_tokens_per_s=60_000,
        decode_tokens_per_s=3000,
        kv_bytes_per_token=890,
        kv_bandwidth_gbs=1000,
        gpu_hour_cost=5.57,
    )
    print(
        "group cost: "
        f"prefill_tokens={cost.prefill_tokens} decode_tokens={cost.decode_tokens} "
        f"kv_token_steps={cost.kv_token_steps} "
        f"gpu_seconds={cost.gpu_seconds:.4f} cost_usd={cost.cost_usd:.6f}"
    )

    throughput = rollout_throughput(
        group_size=4,
        prompt_tokens=1200,
        output_tokens=256,
        prefill_tokens_per_s=60_000,
        decode_tokens_per_s=3000,
        concurrency=8,
    )
    print("throughput:")
    for key, value in throughput.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.4f}")
        else:
            print(f"  {key}: {value}")

    version = WeightVersion("policy", step=100)
    next_version = version.bump()
    old_kv = KVNamespace("policy", version.step, "tokenizer-v1", "template-v1")
    new_kv = KVNamespace("policy", next_version.step, "tokenizer-v1", "template-v1")
    print(
        "policy version: "
        f"old={version.step} new={next_version.step} "
        f"stale_at_trainer_102={version.staleness(102)} "
        f"kv_key_changed={old_kv.key('p0') != new_kv.key('p0')}"
    )

    rollout = PartialRollout("math-0", budget=8)
    print(f"partial: advanced={rollout.advance(5)} remaining={rollout.remaining}")
    rollout.suspend()
    print(f"partial: suspended={rollout.suspended} advanced_while_suspended={rollout.advance(3)}")
    rollout.resume()
    print(f"partial: resumed advanced={rollout.advance(3)} finished={rollout.finished}")


if __name__ == "__main__":
    main()
