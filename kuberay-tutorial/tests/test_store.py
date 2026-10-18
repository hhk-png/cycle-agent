from __future__ import annotations

import pytest

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.errors import AlreadyExists, Conflict, NotFound
from minikuberay.models.meta import new_object
from minikuberay.store.store import ApiStore


def make_cluster(name="demo", namespace="default", replicas=1):
    return new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYCLUSTER,
        name,
        namespace,
        spec={
            "rayVersion": "2.59.0",
            "headGroupSpec": {"template": {"spec": {"containers": [{"name": "ray", "image": "x"}]}}},
            "workerGroupSpecs": [
                {"groupName": "w", "replicas": replicas, "minReplicas": 0, "maxReplicas": 3, "template": {"spec": {"containers": [{"name": "ray", "image": "x"}]}}}
            ],
        },
    )


@pytest.fixture
def store():
    return ApiStore(ManualClock())


def test_create_get_and_version(store):
    created = store.create(C.KIND_RAYCLUSTER, make_cluster())
    assert created["metadata"]["resourceVersion"] == "1"
    assert created["metadata"]["generation"] == 1
    fetched = store.get(C.KIND_RAYCLUSTER, "demo", "default")
    assert fetched["metadata"]["uid"] == created["metadata"]["uid"]
    assert fetched["spec"]["rayVersion"] == "2.59.0"


def test_already_exists(store):
    store.create(C.KIND_RAYCLUSTER, make_cluster())
    with pytest.raises(AlreadyExists):
        store.create(C.KIND_RAYCLUSTER, make_cluster())


def test_not_found(store):
    with pytest.raises(NotFound):
        store.get(C.KIND_RAYCLUSTER, "missing", "default")


def test_list_namespace_and_selector(store):
    store.create(C.KIND_RAYCLUSTER, make_cluster("a", "one"))
    store.create(C.KIND_RAYCLUSTER, make_cluster("b", "two"))
    assert len(store.list(C.KIND_RAYCLUSTER, None)) == 2
    assert len(store.list(C.KIND_RAYCLUSTER, "one")) == 1
    obj = make_cluster("c", "one")
    obj["metadata"]["labels"] = {"team": "ml", "ray.io/cluster": "c"}
    store.create(C.KIND_RAYCLUSTER, obj)
    assert len(store.list(C.KIND_RAYCLUSTER, "one", label_selector={"team": "ml"})) == 1
    assert len(store.list(C.KIND_RAYCLUSTER, "one", label_selector={"team": "other"})) == 0
    assert len(store.list(C.KIND_RAYCLUSTER, "one", label_selector={"matchLabels": {"team": "ml"}})) == 1


def test_optimistic_concurrency(store):
    created = store.create(C.KIND_RAYCLUSTER, make_cluster())
    stale = store.get(C.KIND_RAYCLUSTER, "demo", "default")
    fresh = store.get(C.KIND_RAYCLUSTER, "demo", "default")
    fresh["spec"]["rayVersion"] = "2.60.0"
    store.update(C.KIND_RAYCLUSTER, fresh)
    stale["spec"]["rayVersion"] = "2.61.0"
    with pytest.raises(Conflict):
        store.update(C.KIND_RAYCLUSTER, stale)
    assert created["metadata"]["resourceVersion"] == "1"


def test_generation_changes_only_on_spec(store):
    store.create(C.KIND_RAYCLUSTER, make_cluster())
    obj = store.get(C.KIND_RAYCLUSTER, "demo", "default")
    obj["status"] = {"state": "ready"}
    store.update_status(obj)
    after_status = store.get(C.KIND_RAYCLUSTER, "demo", "default")
    assert after_status["metadata"]["generation"] == 1
    assert after_status["status"]["state"] == "ready"
    after_status["spec"]["rayVersion"] = "2.60.0"
    store.update(C.KIND_RAYCLUSTER, after_status)
    assert store.get(C.KIND_RAYCLUSTER, "demo", "default")["metadata"]["generation"] == 2


def test_status_subresource_does_not_accept_spec(store):
    store.create(C.KIND_RAYCLUSTER, make_cluster())
    obj = store.get(C.KIND_RAYCLUSTER, "demo", "default")
    obj["spec"]["rayVersion"] = "9.9.9"
    obj["status"] = {"state": "ready"}
    store.update_status(obj)
    current = store.get(C.KIND_RAYCLUSTER, "demo", "default")
    assert current["spec"]["rayVersion"] == "2.59.0"
    assert current["status"]["state"] == "ready"


def test_finalizer_two_phase_delete(store):
    obj = make_cluster()
    obj["metadata"]["finalizers"] = ["example.com/finalizer"]
    store.create(C.KIND_RAYCLUSTER, obj)
    deleted = store.delete(C.KIND_RAYCLUSTER, "demo", "default")
    assert deleted["metadata"]["deletionTimestamp"]
    assert store.exists(C.KIND_RAYCLUSTER, "demo", "default")
    store.remove_finalizer(C.KIND_RAYCLUSTER, "demo", "default", "example.com/finalizer")
    assert not store.exists(C.KIND_RAYCLUSTER, "demo", "default")


def test_force_delete_ignores_finalizers(store):
    obj = make_cluster()
    obj["metadata"]["finalizers"] = ["example.com/finalizer"]
    store.create(C.KIND_RAYCLUSTER, obj)
    store.delete(C.KIND_RAYCLUSTER, "demo", "default", force=True)
    assert not store.exists(C.KIND_RAYCLUSTER, "demo", "default")


def test_patch_merge(store):
    store.create(C.KIND_RAYCLUSTER, make_cluster())
    patched = store.patch(
        C.KIND_RAYCLUSTER,
        "demo",
        "default",
        {"metadata": {"labels": {"team": "ml"}}, "spec": {"rayVersion": "2.60.0"}},
    )
    assert patched["metadata"]["labels"]["team"] == "ml"
    assert patched["spec"]["rayVersion"] == "2.60.0"
    assert patched["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["name"] == "ray"


def test_watch_current_and_new_events(store):
    store.create(C.KIND_RAYCLUSTER, make_cluster("a"))
    gen = store.watch(C.KIND_RAYCLUSTER, "default")
    first = next(gen)
    assert first.type == "ADDED"
    assert first.object["metadata"]["name"] == "a"
    store.create(C.KIND_RAYCLUSTER, make_cluster("b"))
    second = next(gen)
    assert second.type == "ADDED"
    assert second.object["metadata"]["name"] == "b"
    obj = store.get(C.KIND_RAYCLUSTER, "b", "default")
    obj["spec"]["rayVersion"] = "2.60.0"
    store.update(C.KIND_RAYCLUSTER, obj)
    third = next(gen)
    assert third.type == "MODIFIED"
    store.delete(C.KIND_RAYCLUSTER, "b", "default")
    fourth = next(gen)
    assert fourth.type == "DELETED"
    store.close()


def test_watch_since_resource_version(store):
    store.create(C.KIND_RAYCLUSTER, make_cluster("a"))
    rv = store.resource_version
    store.create(C.KIND_RAYCLUSTER, make_cluster("b"))
    gen = store.watch(C.KIND_RAYCLUSTER, "default", since_resource_version=rv, include_current=False)
    event = next(gen)
    assert event.object["metadata"]["name"] == "b"
    store.close()


def test_dump_and_load_roundtrip(store):
    store.create(C.KIND_RAYCLUSTER, make_cluster("a"))
    snapshot = store.dump()
    other = ApiStore(ManualClock())
    other.load(snapshot)
    assert other.get(C.KIND_RAYCLUSTER, "a", "default")["metadata"]["name"] == "a"
    assert other.resource_version == snapshot["resourceVersion"]


def test_namespaces_are_isolated(store):
    store.create(C.KIND_RAYCLUSTER, make_cluster("a", "one"))
    store.create(C.KIND_RAYCLUSTER, make_cluster("a", "two"))
    assert store.get(C.KIND_RAYCLUSTER, "a", "one")["metadata"]["namespace"] == "one"
    assert store.get(C.KIND_RAYCLUSTER, "a", "two")["metadata"]["namespace"] == "two"


def test_field_selector(store):
    store.create(C.KIND_POD, new_object(C.CORE_API_VERSION, C.KIND_POD, "p", "default", spec={"containers": []}, status={"phase": "Running"}))
    assert len(store.list(C.KIND_POD, "default", field_selector={"status.phase": "Running"})) == 1
    assert len(store.list(C.KIND_POD, "default", field_selector={"status.phase": "Pending"})) == 0


def test_label_selector_match_expressions(store):
    for name, team in (("a", "ml"), ("b", "data"), ("c", None)):
        obj = make_cluster(name)
        if team:
            obj["metadata"]["labels"] = {"team": team}
        store.create(C.KIND_RAYCLUSTER, obj)

    selector = {"matchExpressions": [{"key": "team", "operator": "In", "values": ["ml", "data"]}]}
    assert {obj["metadata"]["name"] for obj in store.list(C.KIND_RAYCLUSTER, "default", label_selector=selector)} == {"a", "b"}

    selector = {"matchExpressions": [{"key": "team", "operator": "NotIn", "values": ["ml"]}]}
    assert {obj["metadata"]["name"] for obj in store.list(C.KIND_RAYCLUSTER, "default", label_selector=selector)} == {"b", "c"}

    selector = {"matchExpressions": [{"key": "team", "operator": "Exists"}]}
    assert {obj["metadata"]["name"] for obj in store.list(C.KIND_RAYCLUSTER, "default", label_selector=selector)} == {"a", "b"}

    selector = {"matchExpressions": [{"key": "team", "operator": "DoesNotExist"}]}
    assert {obj["metadata"]["name"] for obj in store.list(C.KIND_RAYCLUSTER, "default", label_selector=selector)} == {"c"}


def test_strategic_patch_merges_and_json_patch_is_rejected(store):
    from minikuberay.errors import BadRequest

    store.create(C.KIND_RAYCLUSTER, make_cluster())
    patched = store.patch(
        C.KIND_RAYCLUSTER,
        "demo",
        "default",
        {"metadata": {"labels": {"team": "ml"}}, "spec": {"rayVersion": "2.60.0"}},
        patch_type="strategic",
    )
    assert patched["metadata"]["labels"]["team"] == "ml"
    assert patched["spec"]["rayVersion"] == "2.60.0"
    assert patched["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["name"] == "ray"

    with pytest.raises(BadRequest):
        store.patch(C.KIND_RAYCLUSTER, "demo", "default", {}, patch_type="json")
