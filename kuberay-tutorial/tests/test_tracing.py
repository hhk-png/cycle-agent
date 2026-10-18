from __future__ import annotations

import pytest

from minikuberay import constants as C
from minikuberay.apiserver.server import ApiServer
from minikuberay.cli.main import main
from minikuberay.kube.client import HTTPClient
from minikuberay.tracing import Tracer, span_tree
from tests.conftest import Harness, cluster_spec


def test_operator_records_nested_round_and_reconcile_spans(harness: Harness) -> None:
    harness.apply(C.KIND_RAYCLUSTER, "traced", cluster_spec(workers=1, min_replicas=1))
    harness.settle(20)

    spans = harness.operator.tracer.spans(limit=2000)
    rounds = [span for span in spans if span["name"] == "operator.round"]
    reconciles = [span for span in spans if span["name"] == "reconcile"]
    assert rounds, "every operator round must produce a root span"
    assert reconciles, "every controller must produce a reconcile span"

    round_ids = {span["span_id"] for span in rounds}
    nested = [span for span in reconciles if span["parent_span_id"] in round_ids]
    assert nested, "reconcile spans must be children of the operator round"

    cluster_spans = [span for span in reconciles if span["attributes"].get("kind") == C.KIND_RAYCLUSTER]
    assert cluster_spans
    assert cluster_spans[0]["attributes"]["controller"] == "raycluster"
    assert cluster_spans[0]["attributes"]["name"] == "traced"
    assert cluster_spans[0]["status"] in ("OK", "UNSET")
    # The context manager closes spans and assigns a real duration.
    assert all(span["end_time"] for span in spans)
    assert all(span["duration_ms"] >= 0 for span in spans)


def test_tracer_marks_exceptions_as_error() -> None:
    tracer = Tracer()
    with pytest.raises(RuntimeError):
        with tracer.span("explode", controller="test"):
            raise RuntimeError("boom")

    span = tracer.spans(limit=1)[0]
    assert span["name"] == "explode"
    assert span["status"] == "ERROR"
    assert span["attributes"]["status.description"] == "RuntimeError: boom"
    assert span["events"][0]["name"] == "exception"
    assert tracer.stats()["errors"] == 1


def test_tracer_is_bounded_and_can_be_cleared() -> None:
    tracer = Tracer(max_spans=5)
    for index in range(20):
        with tracer.span("work", index=index):
            pass
    assert len(tracer.spans(limit=100)) == 5
    tracer.clear()
    assert tracer.spans() == []
    assert tracer.stats()["spans"] == 0


def test_trace_api_and_cli(harness: Harness) -> None:
    server = ApiServer(
        harness.store,
        runtime=harness.runtime,
        metrics=harness.operator.metrics.registry,
        tracer=harness.operator.tracer,
        port=0,
    )
    url = server.start()
    try:
        harness.apply(C.KIND_RAYCLUSTER, "api-traced", cluster_spec(workers=1, min_replicas=1))
        harness.settle(20)
        client = HTTPClient(url)

        payload = client._request("GET", "/api/traces?limit=20")
        assert payload["enabled"] is True
        assert payload["stats"]["spans"] > 0
        assert payload["traces"]
        trace = payload["traces"][-1]
        assert trace["trace_id"]
        assert trace["span_count"] >= 1

        one = client._request("GET", f"/api/traces/{trace['trace_id']}")
        assert one["trace_id"] == trace["trace_id"]
        assert one["spans"]

        assert main(["traces", "--server", url, "--limit", "5", "--tree"]) == 0
    finally:
        server.stop()


def test_span_tree_renders_parent_child_order() -> None:
    spans = [
        {
            "name": "operator.round",
            "span_id": "root",
            "parent_span_id": "",
            "start_time": "2026-01-01T00:00:00Z",
            "duration_ms": 3.0,
            "status": "OK",
            "attributes": {"controller": ""},
        },
        {
            "name": "reconcile",
            "span_id": "child",
            "parent_span_id": "root",
            "start_time": "2026-01-01T00:00:00Z",
            "duration_ms": 1.0,
            "status": "ERROR",
            "attributes": {"controller": "raycluster"},
        },
    ]
    lines = span_tree(spans)
    assert lines[0].startswith("  operator.round")
    assert "! reconcile" in lines[1]
    assert "raycluster" in lines[1]
