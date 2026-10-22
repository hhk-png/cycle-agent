from __future__ import annotations

import pytest

from mini_deepinfer.pipeline import (
    DecodePerfCurve,
    ShapeSpecializedRunner,
    StageTimings,
    simulate_zero_overhead,
)


def test_zero_overhead_speedup() -> None:
    timings = StageTimings(schedule_ms=1.2, forward_ms=8.0, sample_ms=0.8, post_ms=0.6)
    result = simulate_zero_overhead(100, timings)
    assert result.synchronous_ms == pytest.approx(100 * 10.6)
    assert result.overlapped_ms < result.synchronous_ms
    assert 1.0 < result.speedup < 2.0
    assert 0.0 <= result.bubble_fraction < 1.0


def test_prepare_input_counts_toward_cpu_time() -> None:
    timings = StageTimings(
        schedule_ms=1.2,
        prepare_input_ms=0.4,
        forward_ms=8.0,
        sample_ms=0.8,
        post_ms=0.6,
    )
    assert timings.cpu_ms == pytest.approx(2.2)
    result = simulate_zero_overhead(100, timings)
    assert result.synchronous_ms == pytest.approx(1100.0)
    assert result.overlapped_ms == pytest.approx(882.2)


def test_shape_specialized_runner_cache() -> None:
    runner = ShapeSpecializedRunner()
    calls = []
    fn = lambda: calls.append(1)
    assert runner.run((1, 8), fn) is None
    assert runner.run((1, 8), fn) is None
    assert runner.run((2, 8), fn) is None
    assert calls == [1, 1, 1]
    stats = runner.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 2


def test_decode_perf_curve_interpolation() -> None:
    curve = DecodePerfCurve([(1, 10.0), (8, 18.0), (64, 60.0)])
    assert curve.predict_ms(1) == 10.0
    assert curve.predict_ms(8) == 18.0
    assert 10.0 < curve.predict_ms(4) < 18.0
    assert curve.predict_ms(100) == 60.0
    assert curve.best_batch_size(20.0) == 8
