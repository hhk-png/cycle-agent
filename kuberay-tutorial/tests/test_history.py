from __future__ import annotations

import json
import urllib.request

import pytest

from minikuberay import constants as C
from minikuberay.apiserver.server import ApiServer
from minikuberay.clock import ManualClock
from minikuberay.history import HistoryStore
from tests.conftest import Harness, cluster_spec


def test_history_records_events_and_snapshot(harness):
    harness.apply(C.KIND_RAYCLUSTER, "observed", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    assert harness.operator.history is not None
    sessions = harness.operator.history.list_sessions()
    session = next(item for item in sessions if item["cluster"] == "observed")
    assert session["state"] == "ready"
    assert session["eventCount"] >= 2  # head + worker creation
    events = harness.operator.history.events("default", "observed")
    reasons = {event["reason"] for event in events}
    assert "CreatedHeadPod" in reasons
    assert "CreatedWorkerPod" in reasons
    replay = harness.operator.history.replay("default", "observed")
    assert replay["snapshot"]["status"]["state"] == "ready"
    assert replay["snapshot"]["runtime"]["nodes"]


def test_history_survives_cluster_deletion(harness):
    harness.apply(C.KIND_RAYCLUSTER, "gone", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    harness.store.delete(C.KIND_RAYCLUSTER, "gone", "default")
    harness.settle(30)
    assert not harness.store.exists(C.KIND_RAYCLUSTER, "gone", "default")
    session = harness.operator.history.get_session("default", "gone")
    assert session is not None
    assert session["deletedAt"]
    assert any(event["reason"] == "DeletedRayCluster" for event in session["events"])


def test_history_attributes_rayservice_events_to_active_cluster():
    store = HistoryStore(ManualClock())
    service = {
        "apiVersion": C.RAY_API_VERSION,
        "kind": C.KIND_RAYSERVICE,
        "metadata": {"name": "history-svc", "namespace": "default", "uid": "u-svc"},
        "status": {"activeServiceStatus": {"rayClusterName": "history-svc-abc12"}},
    }
    event = store.record(service, "Normal", "ServeReady", "ready")
    assert event is not None
    assert event.cluster == "history-svc-abc12"
    assert store.get("default", "history-svc-abc12") is not None
    # The service's own name must not become a second, misleading session.
    assert store.get("default", "history-svc") is None


def test_history_attributes_raycronjob_events_to_its_own_session():
    from tests.test_raycronjob import apply_cron, cron_harness

    harness = cron_harness()
    apply_cron(harness)
    harness.step(6, advance=60)
    session = harness.operator.history.get_session("default", "cron")
    assert session is not None
    assert any(event["objectKind"] == C.KIND_RAYCRONJOB for event in session["events"])


def test_history_store_persists_to_json(tmp_path):
    clock = ManualClock()
    path = tmp_path / "history.json"
    store = HistoryStore(clock, path=path)
    obj = {
        "apiVersion": C.RAY_API_VERSION,
        "kind": C.KIND_RAYCLUSTER,
        "metadata": {"name": "persisted", "namespace": "default", "uid": "u1"},
        "spec": {"rayVersion": "2.59.0"},
        "status": {"state": "ready"},
    }
    store.snapshot(obj, runtime={"nodes": []})
    store.record(obj, "Normal", "CreatedHeadPod", "created")
    store.save()
    assert path.exists()

    reloaded = HistoryStore(clock, path=path)
    session = reloaded.get("default", "persisted")
    assert session is not None
    assert session["eventCount"] == 1
    assert session["snapshot"]["status"]["state"] == "ready"


def test_history_api_endpoints(harness):
    harness.apply(C.KIND_RAYCLUSTER, "api-history", cluster_spec(workers=0))
    harness.settle(20)
    server = ApiServer(
        harness.store,
        runtime=harness.runtime,
        history=harness.operator.history,
        port=0,
    )
    url = server.start()
    try:
        def fetch(path):
            with urllib.request.urlopen(url + path, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        sessions = fetch("/api/history/sessions")["sessions"]
        assert any(item["cluster"] == "api-history" for item in sessions)
        session = fetch("/api/history/sessions/default/api-history")
        assert session["cluster"] == "api-history"
        events = fetch("/api/history/sessions/default/api-history/events?limit=5")["events"]
        assert events
        replay = fetch("/api/history/sessions/default/api-history/replay")
        assert replay["snapshot"]["metadata"]["name"] == "api-history"
        with pytest.raises(Exception):
            fetch("/api/history/sessions/default/missing")
    finally:
        server.stop()
