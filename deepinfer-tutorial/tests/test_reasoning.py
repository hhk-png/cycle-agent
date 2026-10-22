from __future__ import annotations

import pytest

from mini_deepinfer.reasoning import (
    ReasoningBudget,
    ReasoningTracker,
    ThinkingStreamParser,
    plan_reasoning_capacity,
    reasoning_cost_multiplier,
)


def test_reasoning_tracker_enforces_both_budgets() -> None:
    tracker = ReasoningTracker(
        ReasoningBudget(
            max_reasoning_tokens=4,
            max_answer_tokens=3,
            min_answer_tokens=0,
        )
    )
    assert tracker.add_reasoning(4)
    assert not tracker.add_reasoning(1)
    assert tracker.budget_exceeded
    assert tracker.reasoning_remaining == 0
    assert tracker.add_answer(3)
    assert not tracker.add_answer(1)
    assert tracker.answer_exhausted
    summary = tracker.summary()
    assert summary["reasoning_tokens"] == 5
    assert summary["answer_tokens"] == 4


def test_reasoning_tracker_forces_answer_at_reserve() -> None:
    tracker = ReasoningTracker(
        ReasoningBudget(max_reasoning_tokens=2, max_answer_tokens=8, min_answer_tokens=4)
    )
    tracker.add_reasoning(2)
    tracker.add_answer(5)
    assert tracker.should_force_answer


def test_stream_parser_handles_markers_split_across_chunks() -> None:
    parser = ThinkingStreamParser()
    events = []
    for chunk in ["Hello <thin", "king>step 1", " and step 2</think", "ing>Answer"]:
        events.extend(parser.feed(chunk))
    events.extend(parser.finish())
    channels = [channel for channel, _ in events]
    assert "reasoning" in channels
    assert "content" in channels
    reasoning = "".join(text for channel, text in events if channel == "reasoning")
    content = "".join(text for channel, text in events if channel == "content")
    assert reasoning == "step 1 and step 2"
    assert content == "Hello Answer"


def test_stream_parser_flushes_unclosed_reasoning() -> None:
    parser = ThinkingStreamParser()
    events = parser.feed("<thinking>unfinished")
    events.extend(parser.finish())
    assert ("reasoning", "unfinished") in events


def test_reasoning_capacity_and_cost() -> None:
    capacity = plan_reasoning_capacity(
        qps=10.0,
        prompt_tokens=1000.0,
        reasoning_tokens=900.0,
        answer_tokens=100.0,
        per_user_decode_tokens_per_s=100.0,
        ttft_s=0.4,
        aggregate_decode_tokens_per_s_per_gpu=3000.0,
        gpu_hour_cost=5.57,
    )
    assert capacity.output_tokens_per_request == 1000.0
    assert capacity.latency_s == pytest.approx(10.4)
    assert capacity.concurrency == pytest.approx(104.0)
    assert capacity.kv_bytes == pytest.approx(104.0 * 2000.0 * 890.0)
    assert capacity.decode_gpus == pytest.approx(10.0 * 1000.0 / 3000.0)
    assert capacity.token_multiplier == pytest.approx(10.0)
    assert capacity.cost_multiplier == pytest.approx(10.0)
    assert reasoning_cost_multiplier(reasoning_tokens=900.0, answer_tokens=100.0) == pytest.approx(32.5)


def test_reasoning_budget_validation() -> None:
    with pytest.raises(ValueError):
        ReasoningBudget(max_reasoning_tokens=-1)
    with pytest.raises(ValueError):
        ReasoningBudget(max_answer_tokens=4, min_answer_tokens=5)
