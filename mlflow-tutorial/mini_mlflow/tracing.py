"""Tracing for chains, tools, retrieval and LLM calls.

Concepts mirror OpenTelemetry and MLflow Tracing:

* a **trace** is one user-visible operation (a request, an evaluation case);
* a **span** is a timed node inside it, with a parent relationship;
* spans carry typed attributes, inputs/outputs, events and status.

The implementation uses ``ContextVar`` for context propagation.  That makes
async tasks safe by default and also makes the limitation explicit: threads do
not inherit the active span, so a background thread must create its own trace
or be passed an explicit parent.  Auto-instrumentation for OpenAI/LangChain is
outside this mini implementation; the manual API and OTel export are enough to
demonstrate the data model.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import time
import uuid
import warnings
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .entities import Span, Trace
from .errors import InvalidParameter
from .utils import json_safe, new_id, now_ms, redact, truncate_text

_ACTIVE_TRACE: ContextVar["_TraceState | None"] = ContextVar("mini_mlflow_trace", default=None)
_CURRENT_SPAN: ContextVar[Span | None] = ContextVar("mini_mlflow_span", default=None)
_GLOBAL_SINK: Callable[[Trace], None] | None = None
_MAX_PAYLOAD = int(os.environ.get("MINI_MLFLOW_TRACE_MAX_PAYLOAD_BYTES", str(256 * 1024)))


def _bounded(value: Any) -> Any:
    if value is None:
        return None
    prepared = json_safe(redact(value))
    encoded = json.dumps(prepared, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= _MAX_PAYLOAD:
        return prepared
    return {"_truncated": True, "preview": truncate_text(encoded, _MAX_PAYLOAD // 2)}


def _redacted_pairs(mapping: Mapping[str, Any] | None) -> dict[str, str]:
    """Redact sensitive keys before stringifying trace tags/metadata."""
    if not mapping:
        return {}
    result: dict[str, str] = {}
    for key, value in mapping.items():
        redacted = redact({str(key): value})
        rendered = next(iter(redacted.values()))
        if isinstance(rendered, (dict, list)):
            rendered = json.dumps(json_safe(rendered), ensure_ascii=False, sort_keys=True)
        result[str(key)] = truncate_text(str(rendered), min(_MAX_PAYLOAD, 8192))
    return result


@dataclass
class _TraceState:
    trace: Trace
    root_span: Span
    client: Any = None
    sink: Callable[[Trace], None] | None = None
    auto: bool = False
    flushed: bool = False
    token: Any = None


class SpanContext:
    def __init__(self, span: Span, state: _TraceState, *, finish_trace: bool = False) -> None:
        self.span = span
        self.state = state
        self.finish_trace = finish_trace
        self._token = None
        self._closed = False

    def __enter__(self) -> "SpanContext":
        self._token = _CURRENT_SPAN.set(self.span)
        return self

    def set_outputs(self, outputs: Any) -> None:
        self.span.outputs = _bounded(outputs)
        # The trace response is the root/explicit output. If the root has no
        # output yet, use the first completed child as a best-effort fallback;
        # a later explicit trace/root output still wins.
        if self.state.root_span.outputs is None and self.state.trace.response is None:
            self.state.trace.response = self.span.outputs

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        for key, value in attributes.items():
            self.span.attributes[str(key)] = _bounded(value)

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        self.span.events.append(
            {
                "name": str(name),
                "timestamp_ns": time.time_ns(),
                "attributes": _bounded(dict(attributes or {})),
            }
        )

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._closed:
            return False
        if exc is not None:
            self.span.status = "ERROR"
            self.span.events.append(
                {
                    "name": "exception",
                    "timestamp_ns": time.time_ns(),
                    "attributes": {
                        "exception.type": type(exc).__name__,
                        "exception.message": truncate_text(str(exc), 4000),
                    },
                }
            )
            self.state.trace.status = "ERROR"
        self.span.end_time_ns = time.time_ns()
        if self._token is not None:
            try:
                _CURRENT_SPAN.reset(self._token)
            except ValueError:
                _CURRENT_SPAN.set(None)
        self._closed = True
        if self.finish_trace and self.state.auto:
            if self.span.outputs is not None:
                self.state.trace.response = self.span.outputs
            self._finish_auto_trace()
        return False

    def _finish_auto_trace(self) -> None:
        if self.state.flushed:
            return
        if self.state.root_span.end_time_ns is None:
            self.state.root_span.end_time_ns = time.time_ns()
        self.state.trace.status = "ERROR" if any(span.status == "ERROR" for span in self.state.trace.spans) else "OK"
        _flush(self.state)
        _ACTIVE_TRACE.set(None)


class TraceContext:
    def __init__(self, state: _TraceState) -> None:
        self.state = state
        self.trace = state.trace

    def __enter__(self) -> "TraceContext":
        self._token = _ACTIVE_TRACE.set(self.state)
        self._span_token = _CURRENT_SPAN.set(self.state.root_span)
        return self

    def set_outputs(self, outputs: Any) -> None:
        self.state.root_span.outputs = _bounded(outputs)
        self.trace.response = self.state.root_span.outputs

    def add_tag(self, key: str, value: Any) -> None:
        self.trace.tags[str(key)] = str(value)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None:
            self.state.root_span.status = "ERROR"
            self.state.trace.status = "ERROR"
            self.state.root_span.events.append(
                {
                    "name": "exception",
                    "timestamp_ns": time.time_ns(),
                    "attributes": {
                        "exception.type": type(exc).__name__,
                        "exception.message": truncate_text(str(exc), 4000),
                    },
                }
            )
        if self.state.root_span.end_time_ns is None:
            self.state.root_span.end_time_ns = time.time_ns()
        if any(span.status == "ERROR" for span in self.state.trace.spans):
            self.state.trace.status = "ERROR"
        _flush(self.state)
        try:
            _CURRENT_SPAN.reset(self._span_token)
        except ValueError:
            _CURRENT_SPAN.set(None)
        try:
            _ACTIVE_TRACE.reset(self._token)
        except ValueError:
            _ACTIVE_TRACE.set(None)
        return False


def _experiment_id() -> str:
    try:
        from .fluent import get_active_run

        active = get_active_run()
        if active is not None:
            return active.run.info.experiment_id
    except Exception:  # noqa: BLE001 - tracing must not break the app
        pass
    return os.environ.get("MINI_MLFLOW_EXPERIMENT_ID", "0")


def _begin_trace(
    name: str,
    *,
    experiment_id: str | None = None,
    inputs: Any = None,
    tags: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    client: Any = None,
    sink: Callable[[Trace], None] | None = None,
    auto: bool = False,
) -> _TraceState:
    current = _ACTIVE_TRACE.get()
    if current is not None and not auto:
        raise InvalidParameter(
            "a trace is already active; use start_span() to add a child span rather than nesting traces"
        )
    trace_id = new_id()
    root = Span(
        span_id=uuid.uuid4().hex[:16],
        trace_id=trace_id,
        name=str(name),
        parent_span_id=None,
        span_type="CHAIN",
        start_time_ns=time.time_ns(),
    )
    trace = Trace(
        trace_id=trace_id,
        name=str(name),
        experiment_id=str(experiment_id or _experiment_id()),
        timestamp=now_ms(),
        tags=_redacted_pairs(tags),
        request_metadata=_redacted_pairs(metadata),
        request=_bounded(inputs),
        spans=[root],
    )
    root.inputs = trace.request
    state = _TraceState(trace=trace, root_span=root, client=client, sink=sink, auto=auto)
    return state


def start_trace(
    name: str = "trace",
    *,
    experiment_id: str | None = None,
    inputs: Any = None,
    tags: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    client: Any = None,
    sink: Callable[[Trace], None] | None = None,
) -> TraceContext:
    state = _begin_trace(
        name,
        experiment_id=experiment_id,
        inputs=inputs,
        tags=tags,
        metadata=metadata,
        client=client,
        sink=sink,
        auto=False,
    )
    return TraceContext(state)


def start_span(
    name: str,
    *,
    span_type: str = "UNKNOWN",
    inputs: Any = None,
    attributes: Mapping[str, Any] | None = None,
    client: Any = None,
    sink: Callable[[Trace], None] | None = None,
) -> SpanContext:
    state = _ACTIVE_TRACE.get()
    created_trace = False
    if state is None:
        state = _begin_trace(name, inputs=inputs, client=client, sink=sink, auto=True)
        state.token = _ACTIVE_TRACE.set(state)
        created_trace = True
    parent = None if created_trace else _CURRENT_SPAN.get()
    if created_trace:
        # The implicit root already started; use it as the parent so the trace
        # has a single root and the named span remains the meaningful child.
        parent = state.root_span
    span = Span(
        span_id=uuid.uuid4().hex[:16],
        trace_id=state.trace.trace_id,
        name=str(name),
        parent_span_id=parent.span_id if parent else None,
        span_type=str(span_type),
        start_time_ns=time.time_ns(),
        inputs=_bounded(inputs),
        attributes=dict(_bounded(dict(attributes or {})) or {}),
    )
    state.trace.spans.append(span)
    if created_trace:
        if inputs is not None:
            state.trace.request = _bounded(inputs)
            state.root_span.inputs = state.trace.request
        return SpanContext(span, state, finish_trace=True)
    return SpanContext(span, state)


def get_current_trace() -> Trace | None:
    state = _ACTIVE_TRACE.get()
    return state.trace if state else None


def get_current_span() -> Span | None:
    return _CURRENT_SPAN.get()


def set_trace_tag(key: str, value: Any) -> None:
    state = _ACTIVE_TRACE.get()
    if state is None:
        raise InvalidParameter("no active trace")
    state.trace.tags[str(key)] = str(value)


def set_span_outputs(outputs: Any) -> None:
    span = _CURRENT_SPAN.get()
    if span is None:
        raise InvalidParameter("no active span")
    span.outputs = _bounded(outputs)
    state = _ACTIVE_TRACE.get()
    if state is not None:
        state.trace.response = span.outputs


def set_span_attributes(attributes: Mapping[str, Any]) -> None:
    span = _CURRENT_SPAN.get()
    if span is None:
        raise InvalidParameter("no active span")
    span.attributes.update(dict(_bounded(dict(attributes)) or {}))


def add_event(name: str, attributes: Mapping[str, Any] | None = None) -> None:
    span = _CURRENT_SPAN.get()
    if span is None:
        raise InvalidParameter("no active span")
    span.events.append(
        {
            "name": str(name),
            "timestamp_ns": time.time_ns(),
            "attributes": _bounded(dict(attributes or {})),
        }
    )


def _flush(state: _TraceState) -> None:
    if state.flushed:
        return
    state.flushed = True
    trace = state.trace
    trace.status = "ERROR" if any(span.status == "ERROR" for span in trace.spans) else trace.status
    if trace.response is None:
        root = next((span for span in trace.spans if span.parent_span_id is None), None)
        if root is not None and root.outputs is not None:
            trace.response = root.outputs
        else:
            for span in reversed(trace.spans):
                if span.outputs is not None:
                    trace.response = span.outputs
                    break
    try:
        if state.client is not None:
            state.client.log_trace(trace)
        elif state.sink is not None:
            state.sink(trace)
        elif _GLOBAL_SINK is not None:
            _GLOBAL_SINK(trace)
        else:
            from .fluent import get_default_client

            get_default_client().log_trace(trace)
    except Exception as exc:  # noqa: BLE001 - observability must not break business logic
        warnings.warn(f"failed to export trace {trace.trace_id}: {exc}", RuntimeWarning, stacklevel=3)


def set_sink(sink: Callable[[Trace], None] | None) -> None:
    global _GLOBAL_SINK
    _GLOBAL_SINK = sink


def reset_tracing() -> None:
    global _GLOBAL_SINK
    _GLOBAL_SINK = None
    _ACTIVE_TRACE.set(None)
    _CURRENT_SPAN.set(None)


def _bound_call_arguments(func: Callable[..., Any], args: Sequence[Any], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        result = dict(bound.arguments)
        if "self" in result:
            result["self"] = f"<{type(result['self']).__name__}>"
        return result
    except (TypeError, ValueError):
        return {"args": list(args), "kwargs": dict(kwargs)}


def trace(
    name: str | None = None,
    *,
    span_type: str = "CHAIN",
    capture_inputs: bool = True,
    capture_outputs: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that creates a span (and implicit trace) per call."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        trace_name = name or func.__qualname__

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                inputs = _bound_call_arguments(func, args, kwargs) if capture_inputs else None
                with start_span(trace_name, span_type=span_type, inputs=inputs) as span:
                    result = await func(*args, **kwargs)
                    if capture_outputs:
                        span.set_outputs(result)
                    return result
            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            inputs = _bound_call_arguments(func, args, kwargs) if capture_inputs else None
            with start_span(trace_name, span_type=span_type, inputs=inputs) as span:
                result = func(*args, **kwargs)
                if capture_outputs:
                    span.set_outputs(result)
                return result
        return wrapper
    return decorator


def trace_to_otel(trace: Trace) -> dict[str, Any]:
    """Convert to an OTLP/JSON-shaped payload (no exporter dependency)."""

    def any_value(value: Any) -> dict[str, Any]:
        if isinstance(value, bool):
            return {"boolValue": value}
        if isinstance(value, int):
            return {"intValue": str(value)}
        if isinstance(value, float):
            return {"doubleValue": value}
        if isinstance(value, (dict, list)):
            return {"stringValue": json.dumps(value, ensure_ascii=False)}
        return {"stringValue": "" if value is None else str(value)}

    spans = []
    for span in trace.spans:
        events = [
            {
                "timeUnixNano": str(event.get("timestamp_ns", 0)),
                "name": str(event.get("name", "event")),
                "attributes": [
                    {"key": str(key), "value": any_value(value)}
                    for key, value in (event.get("attributes") or {}).items()
                ],
            }
            for event in span.events
        ]
        attributes = [{"key": "mlflow.spanType", "value": {"stringValue": span.span_type}}]
        if span.inputs is not None:
            attributes.append({"key": "mlflow.spanInputs", "value": any_value(span.inputs)})
        if span.outputs is not None:
            attributes.append({"key": "mlflow.spanOutputs", "value": any_value(span.outputs)})
        attributes.extend(
            {"key": str(key), "value": any_value(value)}
            for key, value in span.attributes.items()
        )
        spans.append(
            {
                "traceId": trace.trace_id,
                "spanId": span.span_id,
                "parentSpanId": span.parent_span_id or "",
                "name": span.name,
                "kind": 1,
                "startTimeUnixNano": str(span.start_time_ns),
                "endTimeUnixNano": str(span.end_time_ns or span.start_time_ns),
                "attributes": attributes,
                "events": events,
                "status": {"code": 2 if span.status == "ERROR" else 1, "message": span.status},
            }
        )
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "mini-mlflow"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "mini_mlflow.tracing", "version": "1.0.0"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }
