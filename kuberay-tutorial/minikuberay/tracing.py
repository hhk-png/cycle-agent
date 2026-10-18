"""Dependency-free spans in the OpenTelemetry data model.

The operator already exposes metrics, events and logs.  This module adds the
fourth observability signal: **traces**.  A trace answers "which reconcile
called which child operation, and where did the time go?" — a question that
metrics (aggregates) and logs (lines without causal order) cannot answer well.

The implementation deliberately mirrors the OpenTelemetry vocabulary:

* a ``trace_id`` groups all spans of one operator round;
* every span has a ``span_id`` and an optional ``parent_span_id``;
* spans carry attributes, timestamped events and a status (``OK``/``ERROR``);
* collection is bounded, because an operator that keeps every span forever is
  itself an incident.

No external SDK is required.  The JSON shape is close enough that a real
OpenTelemetry Collector can consume it with a small translation layer; see
chapter 48 for the mapping table.
"""

from __future__ import annotations

import contextvars
import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from .clock import Clock, RealClock, iso

_current_span: contextvars.ContextVar["Span | None"] = contextvars.ContextVar(
    "minikuberay_current_span", default=None
)


def _span_id() -> str:
    return secrets.token_hex(8)


def _trace_id() -> str:
    return secrets.token_hex(16)


@dataclass
class Span:
    """One timed operation.  Mutable by design: attributes are added while it runs."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_ms: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "UNSET"

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, timestamp: str = "", **attributes: Any) -> None:
        self.events.append(
            {
                "name": name,
                "timestamp": timestamp,
                "attributes": attributes,
            }
        )

    def set_status(self, status: str, description: str = "") -> None:
        self.status = status
        if description:
            self.attributes["status.description"] = description

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": round(self.duration_ms, 3),
            "attributes": dict(self.attributes),
            "events": list(self.events),
            "status": self.status,
        }


class Tracer:
    """Bounded, thread-safe span collector with a context-local parent stack."""

    def __init__(self, clock: Clock | None = None, max_spans: int = 5000):
        self.clock = clock or RealClock()
        self.max_spans = max_spans
        self._spans: deque[Span] = deque(maxlen=max_spans)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ creation
    def start_span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        *,
        parent: Span | None = None,
        trace_id: str | None = None,
    ) -> tuple[Span, contextvars.Token]:
        current = parent or _current_span.get()
        span = Span(
            name=name,
            trace_id=current.trace_id if current else (trace_id or _trace_id()),
            span_id=_span_id(),
            parent_span_id=current.span_id if current else "",
            start_time=iso(self.clock.now()),
            attributes=dict(attributes or {}),
        )
        token = _current_span.set(span)
        # Wall-clock timestamps come from the injectable clock so tests can
        # assert ordering; elapsed time comes from a monotonic high-resolution
        # counter so a ManualClock does not turn every span into 0 ms.
        span.attributes.setdefault("_started_perf", time.perf_counter())
        return span, token

    def finish_span(self, span: Span, token: contextvars.Token, error: BaseException | None = None) -> None:
        started = span.attributes.pop("_started_perf", time.perf_counter())
        span.duration_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        span.end_time = iso(self.clock.now())
        if error is not None:
            span.set_status("ERROR", f"{type(error).__name__}: {error}")
            span.add_event(
                "exception",
                timestamp=span.end_time,
                type=type(error).__name__,
                message=str(error),
            )
        elif span.status == "UNSET":
            span.set_status("OK")
        _current_span.reset(token)
        with self._lock:
            self._spans.append(span)

    @contextmanager
    def span(self, span_name: str, **attributes: Any) -> Iterator[Span]:
        span, token = self.start_span(span_name, attributes)
        try:
            yield span
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            self.finish_span(span, token, exc)
            raise
        else:
            self.finish_span(span, token)

    def add_event(self, name: str, **attributes: Any) -> None:
        current = _current_span.get()
        if current is not None:
            current.add_event(name, timestamp=iso(self.clock.now()), **attributes)

    def set_attribute(self, key: str, value: Any) -> None:
        current = _current_span.get()
        if current is not None:
            current.set_attribute(key, value)

    def set_status(self, status: str, description: str = "") -> None:
        current = _current_span.get()
        if current is not None:
            current.set_status(status, description)

    # -------------------------------------------------------------------- query
    def spans(self, limit: int = 100, *, trace_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._spans)
        if trace_id:
            values = [span for span in values if span.trace_id == trace_id]
        return [span.to_dict() for span in values[-limit:]]

    def traces(self, limit: int = 20) -> list[dict[str, Any]]:
        """Group recent spans into traces, newest first."""
        with self._lock:
            values = list(self._spans)
        grouped: dict[str, list[Span]] = {}
        order: list[str] = []
        for span in values:
            if span.trace_id not in grouped:
                grouped[span.trace_id] = []
                order.append(span.trace_id)
            grouped[span.trace_id].append(span)
        result = []
        for trace_id in reversed(order[-limit:]):
            spans = grouped[trace_id]
            result.append(
                {
                    "trace_id": trace_id,
                    "start_time": min(span.start_time for span in spans),
                    "end_time": max(span.end_time for span in spans),
                    "span_count": len(spans),
                    "error_count": sum(1 for span in spans if span.status == "ERROR"),
                    "root": next((span.name for span in spans if not span.parent_span_id), spans[0].name),
                    "spans": [span.to_dict() for span in spans],
                }
            )
        return result

    def stats(self) -> dict[str, Any]:
        with self._lock:
            values = list(self._spans)
        durations = sorted(span.duration_ms for span in values)
        errors = sum(1 for span in values if span.status == "ERROR")

        def percentile(p: float) -> float:
            if not durations:
                return 0.0
            index = min(len(durations) - 1, max(0, int(round((len(durations) - 1) * p))))
            return round(durations[index], 3)

        return {
            "spans": len(values),
            "errors": errors,
            "error_rate": round(errors / len(values), 6) if values else 0.0,
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
        }

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


def span_tree(spans: list[dict[str, Any]]) -> list[str]:
    """Render span dictionaries as a deterministic text tree.

    This is the format used by ``examples/observability_lab.py`` and is handy
    when the UI is unavailable (for example in a CI log).
    """

    by_parent: dict[str, list[dict[str, Any]]] = {}
    ids = {span.get("span_id") for span in spans}
    for span in spans:
        parent = span.get("parent_span_id") or ""
        if parent not in ids:
            parent = ""
        by_parent.setdefault(parent, []).append(span)

    lines: list[str] = []
    visited: set[str] = set()

    def walk(parent: str, depth: int) -> None:
        for span in sorted(by_parent.get(parent, []), key=lambda item: item.get("start_time", "")):
            span_id = str(span.get("span_id"))
            if span_id in visited:
                continue
            visited.add(span_id)
            marker = "!" if span.get("status") == "ERROR" else " "
            lines.append(
                f"{'  ' * depth}{marker} {span.get('name')} "
                f"{span.get('duration_ms', 0):.1f}ms "
                f"{span.get('attributes', {}).get('controller', '')}".rstrip()
            )
            walk(span_id, depth + 1)

    walk("", 0)
    return lines
