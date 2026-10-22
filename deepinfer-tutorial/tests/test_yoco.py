from __future__ import annotations

import pytest

from mini_deepinfer.yoco import (
    DEFAULT_KEEP_TOKENS,
    plan_decoder_replay,
    plan_encoder_hit,
    replay_cost_us,
)


def test_complete_swa_state_accepts_the_block_hit() -> None:
    plan = plan_encoder_hit(1024, 1024)
    assert plan.effective_hit_tokens == 1024
    assert plan.replay_tokens == 0
    assert not plan.needs_replay


def test_swa_covering_the_tail_window_accepts_the_hit() -> None:
    plan = plan_encoder_hit(1024, 1024 - 128)
    assert plan.effective_hit_tokens == 1024
    assert plan.replay_tokens == 0


def test_missing_swa_replays_only_one_window() -> None:
    plan = plan_encoder_hit(1024, 0)
    assert plan.block_hit_tokens == 1024
    assert plan.effective_hit_tokens == 1024 - 128
    assert plan.replay_tokens == 128
    assert plan.needs_replay


def test_block_hit_is_rounded_down_to_a_block_boundary() -> None:
    plan = plan_encoder_hit(1000, 1000, block_size=128)
    assert plan.block_hit_tokens == 896
    assert plan.effective_hit_tokens == 896


def test_no_block_hit_needs_no_replay() -> None:
    plan = plan_encoder_hit(0, 0)
    assert plan.effective_hit_tokens == 0
    assert plan.replay_tokens == 0
    assert not plan.needs_replay


def test_decoder_replay_is_bounded_by_keep_tokens() -> None:
    plan = plan_decoder_replay(100_000)
    assert plan.replay_tokens == DEFAULT_KEEP_TOKENS
    assert plan.effective_hit_tokens == 100_000 - DEFAULT_KEEP_TOKENS
    short = plan_decoder_replay(100)
    assert short.replay_tokens == 100
    assert short.effective_hit_tokens == 0


def test_keep_tokens_must_cover_window_plus_backtracking() -> None:
    with pytest.raises(ValueError):
        plan_decoder_replay(100, window=128, backtracking=6, keep_tokens=100)


def test_replay_cost_is_linear() -> None:
    assert replay_cost_us(140, layers=20, microseconds_per_token_layer=0.5) == pytest.approx(1400)
    with pytest.raises(ValueError):
        replay_cost_us(-1, layers=20, microseconds_per_token_layer=0.5)
