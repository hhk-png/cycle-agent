"""Reasoning budgets, dual-channel streaming and cost accounting."""

from __future__ import annotations

from _common import add_project_root

add_project_root()

from mini_deepinfer.reasoning import (
    ReasoningBudget,
    ReasoningTracker,
    ThinkingStreamParser,
    plan_reasoning_capacity,
    reasoning_cost_multiplier,
)


def main() -> None:
    budget = ReasoningBudget(
        max_reasoning_tokens=4096,
        max_answer_tokens=1024,
        min_answer_tokens=128,
    )
    tracker = ReasoningTracker(budget)
    for _ in range(4096):
        tracker.add_reasoning(1)
    print("reasoning used       :", tracker.reasoning_tokens)
    print("force answer         :", tracker.should_force_answer)
    print("answer reserve       :", tracker.budget.min_answer_tokens)

    parser = ThinkingStreamParser()
    chunks = [
        "I should compare the two options. <thin",
        "king>The first is cheaper, the second is faster.",
        " Therefore pick the second.</think",
        "ing>The second option is the better trade-off.",
    ]
    print("stream events:")
    for chunk in chunks:
        for channel, text in parser.feed(chunk):
            print(f"  {channel:9s} | {text}")
    for channel, text in parser.finish():
        print(f"  {channel:9s} | {text}")

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
    print("capacity:")
    for key, value in capacity.as_dict().items():
        print(f"  {key}: {value:.4f}")
    print("token multiplier     :", round(capacity.token_multiplier, 2))
    print("cost multiplier      :", round(capacity.cost_multiplier, 2))
    print("simple cost multiplier:", round(
        reasoning_cost_multiplier(reasoning_tokens=900.0, answer_tokens=100.0), 2
    ))


if __name__ == "__main__":
    main()
