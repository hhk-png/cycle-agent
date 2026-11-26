from __future__ import annotations

import asyncio
import threading

import pytest

from mini_mlflow import tracing
from mini_mlflow.errors import InvalidParameter


@pytest.fixture(autouse=True)
def clean_tracing():
    tracing.reset_tracing()
    yield
    tracing.reset_tracing()


def test_nested_spans_and_persistence(service) -> None:
    with tracing.start_trace("request", inputs={"question": "hi"}, client=service, tags={"env": "test"}) as session:
        with tracing.start_span("retrieve", span_type="RETRIEVER", inputs={"q": "hi"}) as retrieval:
            retrieval.set_outputs([{"doc": "a"}])
            retrieval.set_attributes({"k": 3})
        with tracing.start_span("llm", span_type="LLM") as llm:
            llm.set_outputs({"answer": "hello"})
        session.set_outputs({"answer": "hello"})
    stored = service.get_trace(session.trace.trace_id)
    assert stored.name == "request"
    assert len(stored.spans) == 3
    assert stored.request == {"question": "hi"}
    assert stored.response == {"answer": "hello"}
    assert stored.tags["env"] == "test"
    assert stored.spans[1].parent_span_id == stored.spans[0].span_id
    assert stored.spans[1].attributes["k"] == 3


def test_implicit_trace_and_sink() -> None:
    captured = []
    tracing.set_sink(captured.append)
    with tracing.start_span("work", span_type="TOOL", inputs={"x": 1}) as span:
        span.set_outputs({"y": 2})
    assert len(captured) == 1
    trace = captured[0]
    assert len(trace.spans) == 2
    assert trace.response == {"y": 2}
    assert trace.spans[1].parent_span_id == trace.spans[0].span_id


def test_decorator_success_and_error() -> None:
    captured = []
    tracing.set_sink(captured.append)

    @tracing.trace("add", span_type="TOOL")
    def add(a: int, b: int = 1) -> int:
        return a + b

    assert add(2, b=3) == 5
    assert captured[-1].response == 5
    assert captured[-1].spans[1].inputs == {"a": 2, "b": 3}

    @tracing.trace("fail")
    def fail() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError):
        fail()
    assert captured[-1].status == "ERROR"
    assert captured[-1].spans[-1].events[-1]["name"] == "exception"


def test_async_decorator() -> None:
    captured = []
    tracing.set_sink(captured.append)

    @tracing.trace("async-work")
    async def work(x: int) -> int:
        await asyncio.sleep(0)
        return x * 2

    assert asyncio.run(work(3)) == 6
    assert captured[0].response == 6


def test_trace_context_error_marks_error() -> None:
    captured = []
    tracing.set_sink(captured.append)
    with pytest.raises(RuntimeError):
        with tracing.start_trace("bad"):
            raise RuntimeError("bad")
    assert captured[0].status == "ERROR"


def test_redaction() -> None:
    captured = []
    tracing.set_sink(captured.append)
    with tracing.start_span("secret", inputs={"api_key": "abc", "q": "hello"}):
        pass
    text = str(captured[0].spans[1].inputs)
    assert "abc" not in text
    assert "REDACTED" in text


def test_span_helpers_require_context() -> None:
    with pytest.raises(InvalidParameter):
        tracing.set_span_outputs(1)
    with pytest.raises(InvalidParameter):
        tracing.set_trace_tag("a", 1)
    with pytest.raises(InvalidParameter):
        tracing.add_event("x")


def test_otel_export() -> None:
    captured = []
    tracing.set_sink(captured.append)
    with tracing.start_span("x", span_type="LLM", inputs={"a": 1}) as span:
        span.set_outputs("y")
    payload = tracing.trace_to_otel(captured[0])
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 2
    assert spans[-1]["traceId"] == captured[0].trace_id
    assert any(item["key"] == "mlflow.spanInputs" for item in spans[-1]["attributes"])


def test_trace_filter_and_persistence(service) -> None:
    captured = []
    tracing.set_sink(captured.append)
    with tracing.start_span("prod", attributes={"env": "prod"}):
        pass
    tracing.set_sink(None)
    with tracing.start_trace("stored", client=service):
        pass
    all_traces = service.search_traces(max_results=10)
    assert len(all_traces) == 1
    assert service.search_traces(filter_string='name = "stored"')[0].name == "stored"
    assert service.search_traces(filter_string='status = "ERROR"') == []


def test_threads_do_not_share_span_context() -> None:
    captured = []
    lock = threading.Lock()

    def append(trace):
        with lock:
            captured.append(trace)

    tracing.set_sink(append)
    results = {}
    lock_results = threading.Lock()

    def worker(index: int) -> None:
        with lock_results:
            results[index] = [tracing.get_current_span()]
        with tracing.start_span(f"thread-{index}"):
            with lock_results:
                results[index].append(tracing.get_current_span().name)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results[0][0] is None and results[1][0] is None
    assert results[0][1] == "thread-0"
    assert results[1][1] == "thread-1"
    assert len(captured) == 2


def test_duplicate_trace_rejected() -> None:
    with tracing.start_trace("one"):
        with pytest.raises(InvalidParameter, match="already active"):
            tracing.start_trace("two")


def test_trace_records_active_experiment(monkeypatch, tmp_path) -> None:
    from mini_mlflow.client import MiniClient
    import mini_mlflow as mlflow

    monkeypatch.setenv("MINI_MLFLOW_TRACKING_URI", f"sqlite:///{(tmp_path / 'trace.db').as_posix()}")
    monkeypatch.setenv("MINI_MLFLOW_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    with mlflow.start_run(experiment_name=None) as active:
        with tracing.start_span("inside"):
            pass
    client = MiniClient()
    traces = client.search_traces(experiment_ids=[active.info.experiment_id])
    assert len(traces) == 1
    client.close()


def test_trace_tags_and_attributes_are_redacted() -> None:
    captured = []
    tracing.set_sink(captured.append)
    with tracing.start_trace(
        "secret-trace",
        tags={"api_key": "SECRET"},
        metadata={"authorization": "Bearer SECRET"},
    ):
        with tracing.start_span("tool", attributes={"access_token": "SECRET"}):
            pass
    trace = captured[0]
    assert "SECRET" not in str(trace.tags) + str(trace.request_metadata) + str(trace.spans[-1].attributes)
    assert "REDACTED" in str(trace.tags)


def test_root_output_wins_over_child_output() -> None:
    captured = []
    tracing.set_sink(captured.append)
    with tracing.start_trace("root-output") as session:
        session.set_outputs({"root": 1})
        with tracing.start_span("tool") as span:
            span.set_outputs({"tool": 2})
    assert captured[0].response == {"root": 1}


def test_span_trace_id_mismatch_rejected(service) -> None:
    from mini_mlflow.entities import Span, Trace

    trace = Trace(
        trace_id="a" * 32,
        spans=[
            Span(
                span_id="b" * 16,
                trace_id="c" * 32,
                name="root",
                start_time_ns=1,
                end_time_ns=2,
            )
        ],
    )
    with pytest.raises(InvalidParameter, match="belongs to trace"):
        service.log_trace(trace)


def test_trace_search_scans_beyond_one_batch(service, monkeypatch) -> None:
    import mini_mlflow.tracking as tracking_module

    monkeypatch.setattr(tracking_module, "_TRACE_SCAN_BATCH", 2)
    from mini_mlflow.entities import Span, Trace

    for index in range(5):
        trace = Trace(
            trace_id=f"{index:032x}",
            name=f"trace-{index}",
            status="ERROR" if index == 0 else "OK",
            spans=[
                Span(
                    span_id=f"{index:016x}",
                    trace_id=f"{index:032x}",
                    name="root",
                    start_time_ns=index + 1,
                    end_time_ns=index + 2,
                )
            ],
        )
        service.log_trace(trace)
    found = service.search_traces(filter_string='status = "ERROR"', max_results=10)
    assert [item.name for item in found] == ["trace-0"]


def test_trace_root_output_preferred_on_persist(service) -> None:
    from mini_mlflow.entities import Span, Trace

    trace = Trace(
        trace_id="f" * 32,
        response={"root": 1},
        spans=[
            Span(
                span_id="1" * 16,
                trace_id="f" * 32,
                name="root",
                start_time_ns=1,
                end_time_ns=2,
                outputs={"root": 1},
            ),
            Span(
                span_id="2" * 16,
                trace_id="f" * 32,
                name="child",
                parent_span_id="1" * 16,
                start_time_ns=2,
                end_time_ns=3,
                outputs={"child": 2},
            ),
        ],
    )
    service.log_trace(trace)
    assert service.get_trace("f" * 32).response == {"root": 1}
