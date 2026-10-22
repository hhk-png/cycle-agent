"""Request-scoped spans: a small observability primitive.

Metrics answer "how many" and "how slow"; traces answer "where did the time
go for this request".  A serving engine needs both.  This module keeps the
smallest useful subset:

* a trace is a collection of named spans with start/end timestamps;
* spans can be nested by convention (``decode.forward`` inside ``decode.step``);
* a tracer keeps a bounded ring of recent traces and computes percentile
  summaries over the spans it has seen;
* traces export as JSON lines, which is what most collectors ingest.

The implementation is intentionally synchronous and dependency-free.  A
production engine would propagate W3C trace context across processes and
sample traces at the collector, not in the hot path.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Span:
    name: str
    start: float
    end: float
    attributes: tuple[tuple[str, str], ...] = ()

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "duration_ms": self.duration_s * 1000.0,
            "attributes": dict(self.attributes),
        }


@dataclass
class Trace:
    trace_id: str
    spans: list[Span] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)

    def record(
        self,
        name: str,
        start: float,
        end: float,
        **attributes: object,
    ) -> Span:
        span = Span(
            name=name,
            start=float(start),
            end=float(end),
            attributes=tuple(sorted((str(k), str(v)) for k, v in attributes.items())),
        )
        self.spans.append(span)
        return span

    @property
    def duration_s(self) -> float:
        if not self.spans:
            return 0.0
        return max(span.end for span in self.spans) - min(span.start for span in self.spans)

    def by_name(self) -> dict[str, list[Span]]:
        result: dict[str, list[Span]] = {}
        for span in self.spans:
            result.setdefault(span.name, []).append(span)
        return result

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "duration_ms": self.duration_s * 1000.0,
            "attributes": dict(self.attributes),
            "spans": [span.to_dict() for span in self.spans],
        }


class Tracer:
    def __init__(self, *, max_traces: int = 256, clock=time.perf_counter) -> None:
        if max_traces < 1:
            raise ValueError("max_traces must be >= 1")
        self._max_traces = max_traces
        self._clock = clock
        self._lock = threading.Lock()
        self._traces: list[Trace] = []
        self._span_durations: dict[str, list[float]] = {}

    def start(self, trace_id: str | None = None, **attributes: object) -> Trace:
        trace = Trace(
            trace_id=trace_id or uuid.uuid4().hex,
            attributes={str(k): str(v) for k, v in attributes.items()},
        )
        with self._lock:
            self._traces.append(trace)
            if len(self._traces) > self._max_traces:
                del self._traces[: len(self._traces) - self._max_traces]
        return trace

    @contextmanager
    def span(self, trace: Trace, name: str, **attributes: object):
        started = self._clock()
        try:
            yield
        finally:
            ended = self._clock()
            span = trace.record(name, started, ended, **attributes)
            with self._lock:
                self._span_durations.setdefault(name, []).append(span.duration_s)

    def finish(self, trace: Trace) -> Trace:
        """Mark a trace complete.  Kept for API symmetry with async tracers."""
        return trace

    def recent(self, n: int = 10) -> list[Trace]:
        with self._lock:
            return list(self._traces[-max(0, n) :])

    def summary(self) -> dict:
        with self._lock:
            traces = list(self._traces)
            durations = {name: list(values) for name, values in self._span_durations.items()}
        trace_durations = sorted(trace.duration_s for trace in traces)

        def percentile(values: list[float], q: float) -> float:
            if not values:
                return 0.0
            index = min(len(values) - 1, max(0, int(round((len(values) - 1) * q))))
            return values[index]

        spans = {}
        for name, values in durations.items():
            ordered = sorted(values)
            spans[name] = {
                "count": len(ordered),
                "mean_ms": (sum(ordered) / len(ordered)) * 1000.0 if ordered else 0.0,
                "p95_ms": percentile(ordered, 0.95) * 1000.0,
                "max_ms": (ordered[-1] if ordered else 0.0) * 1000.0,
            }
        return {
            "traces": len(traces),
            "trace_p50_ms": percentile(trace_durations, 0.50) * 1000.0,
            "trace_p95_ms": percentile(trace_durations, 0.95) * 1000.0,
            "spans": spans,
        }

    def export_jsonl(self, path: str | Path) -> int:
        traces = self.recent(len(self._traces))
        with Path(path).open("w", encoding="utf-8") as handle:
            for trace in traces:
                handle.write(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n")
        return len(traces)
