from __future__ import annotations

import numpy as np
import pytest

from mini_deepinfer.rl_rollout import (
    GroupPlan,
    KVNamespace,
    PartialRollout,
    RolloutConfig,
    WeightVersion,
    estimate_rollout_cost,
    group_advantages,
    plan_groups,
    rollout_throughput,
)


def test_plan_groups_respects_budget_and_keeps_groups_intact() -> None:
    prompts = [("p0", 100), ("p1", 200), ("p2", 50)]
    waves = plan_groups(
        prompts, group_size=4, output_budget=100, max_batch_tokens=1000
    )
    assert all(isinstance(group, GroupPlan) for wave in waves for group in wave)
    assert sum(len(wave) for wave in waves) == 3
    assert all(
        sum(group.reserved_tokens for group in wave) <= 1000 for wave in waves
    )
    # Every prompt appears exactly once.
    ids = [group.prompt_id for wave in waves for group in wave]
    assert sorted(ids) == ["p0", "p1", "p2"]


def test_plan_groups_gives_oversized_group_its_own_wave() -> None:
    waves = plan_groups(
        [("big", 900), ("small", 10)],
        group_size=4,
        output_budget=100,
        max_batch_tokens=500,
    )
    big_waves = [wave for wave in waves if any(group.prompt_id == "big" for group in wave)]
    assert len(big_waves) == 1
    assert len(big_waves[0]) == 1


def test_group_advantages_are_normalized() -> None:
    rewards = [1.0, 2.0, 3.0, 4.0]
    advantages = group_advantages(rewards)
    assert advantages.shape == (4,)
    assert abs(float(advantages.mean())) < 1e-9
    assert abs(float(advantages.std()) - 1.0) < 1e-6
    # The best sample must have a positive advantage.
    assert advantages[-1] > 0 > advantages[0]


def test_group_advantages_zero_for_identical_rewards() -> None:
    advantages = group_advantages([2.5, 2.5, 2.5])
    assert np.allclose(advantages, 0.0)


def test_rollout_cost_splits_prefill_decode_and_kv() -> None:
    shared = estimate_rollout_cost(
        group_size=8,
        prompt_tokens=1000,
        output_tokens=100,
        prefill_tokens_per_s=60_000,
        decode_tokens_per_s=3000,
        kv_bytes_per_token=890,
        kv_bandwidth_gbs=1000,
        gpu_hour_cost=5.57,
    )
    unshared = estimate_rollout_cost(
        group_size=8,
        prompt_tokens=1000,
        output_tokens=100,
        prefill_tokens_per_s=60_000,
        decode_tokens_per_s=3000,
        kv_bytes_per_token=890,
        kv_bandwidth_gbs=1000,
        gpu_hour_cost=5.57,
        share_prefix=False,
    )
    assert shared.prefill_tokens == 1000
    assert unshared.prefill_tokens == 8000
    assert shared.decode_tokens == 800
    assert unshared.cost_usd > shared.cost_usd
    assert shared.kv_token_steps == unshared.kv_token_steps > 0


def test_weight_version_staleness_and_kv_namespace() -> None:
    version = WeightVersion("policy", step=10)
    assert version.bump().step == 11
    assert version.staleness(13) == 3
    assert version.acceptable(11, max_staleness_steps=1)
    assert not version.acceptable(12, max_staleness_steps=1)

    old = KVNamespace("policy", 10, "tok", "tpl")
    new = KVNamespace("policy", 11, "tok", "tpl")
    key = old.key("prefix")
    assert key == old.key("prefix")
    assert key != old.key("other")
    assert key != new.key("prefix")
    assert not old.matches(new)


def test_partial_rollout_suspends_and_resumes() -> None:
    rollout = PartialRollout("p0", budget=10)
    assert rollout.advance(4) == 4
    rollout.suspend()
    assert rollout.advance(3) == 0
    rollout.resume()
    assert rollout.advance(10) == 6
    assert rollout.finished
    with pytest.raises(ValueError):
        rollout.resume()


def test_rollout_throughput_reports_bottleneck() -> None:
    result = rollout_throughput(
        group_size=4,
        prompt_tokens=1000,
        output_tokens=100,
        prefill_tokens_per_s=60_000,
        decode_tokens_per_s=1000,
        concurrency=2,
    )
    assert result["bottleneck"] == "decode"
    assert result["samples_per_s"] > 0
    assert result["tokens_per_s"] == pytest.approx(result["samples_per_s"] * 100)


def test_rollout_config_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        RolloutConfig(group_size=0)
    with pytest.raises(ValueError):
        RolloutConfig(temperature=0.0)
