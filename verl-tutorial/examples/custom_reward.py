"""A custom verifiable reward used by examples/13_custom_reward.py."""

from __future__ import annotations


def length_aware_reward(response: str, answer: str, **_) -> float:
    correct = 1.0 if response.strip().startswith(answer.strip()) else 0.0
    # Small shaping term: shorter correct responses are easier to verify and
    # cheaper to serve.  Keep the correctness term dominant.
    brevity = 0.1 if correct and len(response.strip()) <= len(answer) + 2 else 0.0
    return correct + brevity
