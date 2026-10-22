from __future__ import annotations

import json

import pytest

from mini_deepinfer.tracing import Trace, Tracer


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_nested_spans_and_trace_duration() -> None:
    clock = FakeClock()
    tracer = Tracer(clock=clock)
    trace = tracer.start("trace-1", tenant="acme")
    with tracer.span(trace, "prefill", tokens=128):
        clock.advance(0.01)
        with tracer.span(trace, "attention"):
            clock.advance(0.02)
    clock.advance(0.005)
    with tracer.span(trace, "sample"):
        clock.advance(0.002)
    assert len(trace.spans) == 3
    assert trace.duration_s == pytest.approx(0.037)
    names = [span.name for span in trace.spans]
    assert names == ["attention", "prefill", "sample"]
    assert trace.by_name()["prefill"][0].attributes == (("tokens", "128"),)


def test_summary_reports_percentiles() -> None:
    clock = FakeClock()
    tracer = Tracer(clock=clock)
    for index in range(10):
        trace = tracer.start(f"t{index}")
        with tracer.span(trace, "step"):
            clock.advance(0.001 * (index + 1))
    summary = tracer.summary()
    assert summary["traces"] == 10
    assert summary["spans"]["step"]["count"] == 10
    assert summary["spans"]["step"]["mean_ms"] == pytest.approx(5.5)
    assert summary["spans"]["step"]["p95_ms"] >= 9.0
    assert summary["trace_p95_ms"] >= 9.0


def test_trace_ring_is_bounded() -> None:
    tracer = Tracer(max_traces=3, clock=FakeClock())
    for index in range(5):
        tracer.start(f"t{index}")
    recent = tracer.recent(10)
    assert [trace.trace_id for trace in recent] == ["t2", "t3", "t4"]


def test_jsonl_export(tmp_path) -> None:
    tracer = Tracer(clock=FakeClock())
    trace = tracer.start("trace-export")
    trace.record("noop", 0.0, 0.001, phase="test")
    path = tmp_path / "trace.jsonl"
    assert tracer.export_jsonl(path) == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    assert payload["trace_id"] == "trace-export"
    assert payload["spans"][0]["name"] == "noop"


def test_invalid_tracer_configuration() -> None:
    with pytest.raises(ValueError):
        Tracer(max_traces=0)


def test_empty_trace_summary_is_safe() -> None:
    trace = Trace(trace_id="empty")
    assert trace.duration_s == 0.0
    tracer = Tracer(clock=FakeClock())
    summary = tracer.summary()
    assert summary["traces"] == 0
    assert summary["trace_p95_ms"] == 0.0
