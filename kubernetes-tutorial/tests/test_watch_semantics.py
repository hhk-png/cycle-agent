"""Watch semantics: initial state (resourceVersion=0) and bookmarks."""

from __future__ import annotations


def configmap(name: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": "default"},
        "data": {"k": name},
    }


def test_watch_resource_version_zero_sends_initial_state(api_cluster):
    for name in ("c0", "c1"):
        api_cluster.api.create(configmap(name))
    events = list(api_cluster.api.watch(
        "v1", "ConfigMap", "default", resource_version="0", timeout_seconds=1
    ))
    added = [event for event in events if event["type"] == "ADDED"]
    assert {event["object"]["metadata"]["name"] for event in added} == {"c0", "c1"}
    assert all(event["type"] != "ERROR" for event in events)


def test_watch_bookmark_has_current_resource_version(api_cluster):
    api_cluster.api.create(configmap("c0"))
    events = list(api_cluster.api.watch(
        "v1", "ConfigMap", "default", resource_version="0",
        timeout_seconds=1, allow_bookmarks=True,
    ))
    assert events[0]["type"] == "BOOKMARK"
    bookmark = events[0]["object"]
    assert bookmark["kind"] == "ConfigMap"
    assert int(bookmark["metadata"]["resourceVersion"]) >= 1000
    assert any(event["type"] == "ADDED" for event in events[1:])


def test_watch_resource_version_zero_honours_label_selector(api_cluster):
    first = configmap("c0")
    first["metadata"]["labels"] = {"team": "a"}
    second = configmap("c1")
    second["metadata"]["labels"] = {"team": "b"}
    api_cluster.api.create(first)
    api_cluster.api.create(second)
    events = list(api_cluster.api.watch(
        "v1", "ConfigMap", "default", resource_version="0",
        timeout_seconds=1, label_selector="team=a",
    ))
    added = [event for event in events if event["type"] == "ADDED"]
    assert [event["object"]["metadata"]["name"] for event in added] == ["c0"]


def test_watch_times_out_without_changes(api_cluster):
    events = list(api_cluster.api.watch(
        "v1", "ConfigMap", "default", resource_version="0", timeout_seconds=1
    ))
    assert events == []
