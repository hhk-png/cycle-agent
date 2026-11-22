from __future__ import annotations

import queue
import threading
import time

import pytest

from mini_k8s.errors import ApiError
from mini_k8s.store.store import ResourceStore, WatchEvent

GVR = ("example.com", "v1", "widgets")


def make(name: str, namespace: str = "default", **spec) -> dict:
    return {"apiVersion": "example.com/v1", "kind": "Widget",
            "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}},
            "spec": spec or {"size": 1}}


def test_create_assigns_server_fields():
    store = ResourceStore()
    obj = store.create(GVR, make("one"), kind="Widget")
    assert obj["metadata"]["uid"]
    assert obj["metadata"]["resourceVersion"]
    assert obj["metadata"]["generation"] == 1
    assert obj["metadata"]["creationTimestamp"]


def test_create_overwrites_spoofed_server_fields():
    store = ResourceStore()
    body = make("one")
    body["metadata"].update({
        "uid": "spoofed-uid",
        "resourceVersion": "999",
        "creationTimestamp": "2000-01-01T00:00:00Z",
        "generation": 42,
    })
    created = store.create(GVR, body, kind="Widget")
    assert created["metadata"]["uid"] != "spoofed-uid"
    assert created["metadata"]["resourceVersion"] != "999"
    assert created["metadata"]["creationTimestamp"] != "2000-01-01T00:00:00Z"
    assert created["metadata"]["generation"] == 1


def test_create_rejects_mismatched_api_version_and_kind():
    store = ResourceStore()
    with pytest.raises(ApiError) as exc:
        store.create(GVR, {**make("bad-api"), "apiVersion": "other.example.com/v1"}, kind="Widget")
    assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        store.create(GVR, {**make("bad-kind"), "kind": "Gadget"}, kind="Widget")
    assert exc.value.status == 400


def test_create_duplicate():
    store = ResourceStore()
    store.create(GVR, make("one"), kind="Widget")
    with pytest.raises(ApiError) as exc:
        store.create(GVR, make("one"), kind="Widget")
    assert exc.value.status == 409


def test_update_increments_generation_only_for_spec():
    store = ResourceStore()
    obj = store.create(GVR, make("one"), kind="Widget")
    obj["metadata"]["labels"]["x"] = "y"
    updated = store.update(GVR, obj, kind="Widget")
    assert updated["metadata"]["generation"] == 1
    updated["spec"]["size"] = 2
    updated = store.update(GVR, updated, kind="Widget")
    assert updated["metadata"]["generation"] == 2


def test_optimistic_concurrency_conflict():
    store = ResourceStore()
    obj = store.create(GVR, make("one"), kind="Widget")
    other = store.get(GVR, "default", "one")
    obj["spec"]["size"] = 2
    store.update(GVR, obj, kind="Widget")
    other["spec"]["size"] = 3
    with pytest.raises(ApiError) as exc:
        store.update(GVR, other, kind="Widget")
    assert exc.value.status == 409


def test_list_namespace_and_selectors():
    store = ResourceStore()
    store.create(GVR, make("one"), kind="Widget")
    store.create(GVR, make("two", "other"), kind="Widget")
    store.create(GVR, make("three"), kind="Widget")
    assert len(store.list(GVR)[0]) == 3
    assert len(store.list(GVR, "default")[0]) == 2
    assert [o["metadata"]["name"] for o in store.list(GVR, label_selector="app=two")[0]] == ["two"]


def test_pagination_continue_token():
    store = ResourceStore()
    for index in range(5):
        store.create(GVR, make(f"w{index}"), kind="Widget")
    first, token = store.list(GVR, limit=2)
    assert len(first) == 2 and token
    second, token2 = store.list(GVR, limit=2, continue_token=token)
    assert len(second) == 2 and token2
    third, token3 = store.list(GVR, limit=2, continue_token=token2)
    assert len(third) == 1 and token3 is None


def test_finalizer_blocks_deletion_until_cleared():
    store = ResourceStore()
    obj = store.create(GVR, make("one"), kind="Widget")
    obj["metadata"]["finalizers"] = ["example.com/cleanup"]
    store.update(GVR, obj, kind="Widget")
    assert store.delete(GVR, "default", "one", kind="Widget") is False
    pending = store.get(GVR, "default", "one")
    assert pending["metadata"]["deletionTimestamp"]
    pending["metadata"]["finalizers"] = []
    # Clearing the last finalizer through a normal update completes deletion.
    store.update(GVR, pending, kind="Widget")
    assert store.get(GVR, "default", "one") is None
    with pytest.raises(ApiError) as exc:
        store.delete(GVR, "default", "one", kind="Widget")
    assert exc.value.status == 404


def test_json_merge_patch():
    store = ResourceStore()
    store.create(GVR, make("one", size=1), kind="Widget")
    patched = store.patch(GVR, "default", "one", {"spec": {"size": 5}}, kind="Widget")
    assert patched["spec"]["size"] == 5


def test_watch_receives_events():
    store = ResourceStore()
    watcher = store.watch(GVR, "default")
    obj = store.create(GVR, make("one"), kind="Widget")
    event = next(watcher.events())
    assert event.type == "ADDED"
    assert event.object["metadata"]["name"] == "one"
    obj["spec"]["size"] = 9
    store.update(GVR, obj, kind="Widget")
    event = next(watcher.events())
    assert event.type == "MODIFIED"
    watcher.close()


def test_watch_replays_after_resource_version():
    store = ResourceStore()
    first = store.create(GVR, make("one"), kind="Widget")
    watcher = store.watch(GVR, "default", resource_version=first["metadata"]["resourceVersion"])
    store.create(GVR, make("two"), kind="Widget")
    event = next(watcher.events())
    assert event.type == "ADDED" and event.object["metadata"]["name"] == "two"


def test_watch_overflow_delivers_error_instead_of_raising():
    store = ResourceStore()
    watcher = store.watch(GVR, "default")
    watcher.queue = queue.Queue(maxsize=1)
    watcher.push(WatchEvent("ADDED", make("one"), 1, GVR))
    watcher.push(WatchEvent("ADDED", make("two"), 2, GVR))
    events = list(watcher.events())
    assert events and events[-1].type == "ERROR"
    assert events[-1].object["code"] == 410


def test_watch_too_old_resource_version_returns_410():
    store = ResourceStore(event_history=2)
    for index in range(4):
        store.create(GVR, make(f"w{index}"), kind="Widget")
    watcher = store.watch(GVR, "default", resource_version=0)
    event = next(watcher.events())
    assert event.type == "ERROR"
    assert event.object["code"] == 410
    assert "too old resource version" in event.object["message"]


def test_watch_is_namespace_scoped():
    store = ResourceStore()
    watcher = store.watch(GVR, "other")
    store.create(GVR, make("one"), kind="Widget")
    assert watcher.queue.empty()
    store.create(GVR, make("two", "other"), kind="Widget")
    event = next(watcher.events())
    assert event.object["metadata"]["name"] == "two"


def test_concurrent_creates_are_serialised():
    store = ResourceStore()
    errors = []

    def worker(index: int) -> None:
        try:
            store.create(GVR, make(f"w{index}"), kind="Widget")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(store.list(GVR)[0]) == 20
    versions = [int(o["metadata"]["resourceVersion"]) for o in store.list(GVR)[0]]
    assert len(set(versions)) == 20
