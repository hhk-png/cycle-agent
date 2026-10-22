from __future__ import annotations

import pytest

from mini_deepinfer.metrics import EngineMetrics


def test_last_decode_throughput_ignores_prefill_steps() -> None:
    metrics = EngineMetrics()
    metrics.record_step(prefill_tokens=16, decode_tokens=0, elapsed=0.5)
    assert metrics.summary()["last_decode_tokens_per_s"] == 0.0

    metrics.record_step(prefill_tokens=0, decode_tokens=2, elapsed=0.01)
    assert metrics.summary()["last_decode_tokens_per_s"] == pytest.approx(200.0)

    # A later prefill must not pair its long elapsed time with the previous
    # decode token count.
    metrics.record_step(prefill_tokens=64, decode_tokens=0, elapsed=2.0)
    assert metrics.summary()["last_decode_tokens_per_s"] == pytest.approx(200.0)


def test_last_decode_throughput_can_be_zero_before_any_decode() -> None:
    metrics = EngineMetrics()
    metrics.record_step(prefill_tokens=8, decode_tokens=0, elapsed=0.1)
    assert metrics.summary()["last_decode_tokens_per_s"] == 0.0
