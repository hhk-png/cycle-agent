from __future__ import annotations

import copy

from minikuberay import constants as C
from minikuberay.models.meta import new_object
from tests.conftest import Harness, cluster_spec


def serve_config(version="v1"):
    return f"""applications:
  - name: app
    import_path: app:{version}
    route_prefix: /
    deployments:
      - name: D
        num_replicas: 1
"""


def service_spec(**overrides):
    spec = {
        "serveConfigV2": serve_config(),
        "rayClusterConfig": cluster_spec(workers=1, min_replicas=1),
    }
    spec.update(overrides)
    return spec


def apply_service(harness: Harness, spec=None, name="svc", annotations=None):
    obj = new_object(C.RAY_API_VERSION, C.KIND_RAYSERVICE, name, "default", spec=copy.deepcopy(spec or service_spec()), annotations=annotations)
    return harness.store.create(C.KIND_RAYSERVICE, obj, "default")


def run(harness: Harness, rounds=30, advance=0.2):
    harness.step(rounds, advance=advance)


def bump_cluster_image(service: dict, version: str = "2.60.0") -> dict:
    """Change a field that requires a new RayCluster (not serveConfigV2)."""
    cluster = service["spec"]["rayClusterConfig"]
    containers = cluster["headGroupSpec"]["template"]["spec"]["containers"]
    for container in containers:
        container["image"] = f"rayproject/ray:{version}"
    for group in cluster.get("workerGroupSpecs") or []:
        for container in group["template"]["spec"]["containers"]:
            container["image"] = f"rayproject/ray:{version}"
    return service


def test_service_becomes_ready(harness):
    apply_service(harness)
    run(harness)
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    assert service["status"]["serviceStatus"] == "Running"
    assert service["status"]["numServeEndpoints"] == 2
    active = service["status"]["activeServiceStatus"]
    assert active["rayClusterName"]
    assert active["applicationStatuses"]["app"]["status"] == "RUNNING"
    assert active["trafficRoutedPercent"] == 100
    assert service["status"]["observedGeneration"] == service["metadata"]["generation"]


def test_service_services_and_selectors(harness):
    apply_service(harness)
    run(harness)
    active = harness.get(C.KIND_RAYSERVICE, "svc")["status"]["activeServiceStatus"]["rayClusterName"]
    head = harness.get(C.KIND_SERVICE, "svc-head-svc")
    serve = harness.get(C.KIND_SERVICE, "svc-serve-svc")
    assert head["spec"]["selector"] == {C.LABEL_CLUSTER: active, C.LABEL_NODE_TYPE: C.NODE_TYPE_HEAD}
    assert serve["spec"]["selector"] == {C.LABEL_CLUSTER: active, C.LABEL_SERVE: "true"}


def test_service_exclude_head(harness):
    apply_service(harness, service_spec(excludeHeadPodFromServeSvc=True))
    run(harness)
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    assert service["status"]["serviceStatus"] == "Running"
    assert service["status"]["numServeEndpoints"] == 1
    active = service["status"]["activeServiceStatus"]["rayClusterName"]
    head = harness.get(C.KIND_POD, f"{active}-head")
    assert head["metadata"]["labels"][C.LABEL_SERVE] == "false"


def test_service_exclude_head_without_workers_is_not_ready(harness):
    apply_service(
        harness,
        service_spec(
            excludeHeadPodFromServeSvc=True,
            rayClusterConfig=cluster_spec(workers=0),
        ),
    )
    run(harness)
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    assert service["status"]["numServeEndpoints"] == 0
    assert service["status"]["serviceStatus"] == ""


def test_new_cluster_upgrade(harness):
    apply_service(harness)
    run(harness)
    old_active = harness.get(C.KIND_RAYSERVICE, "svc")["status"]["activeServiceStatus"]["rayClusterName"]
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    bump_cluster_image(service, "2.60.0")
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 30)
    current = harness.get(C.KIND_RAYSERVICE, "svc")
    new_active = current["status"]["activeServiceStatus"]["rayClusterName"]
    assert new_active != old_active
    assert current["status"]["serviceStatus"] == "Running"
    old_cluster = harness.get(C.KIND_RAYCLUSTER, old_active)
    assert old_cluster["metadata"]["annotations"]["minikuberay.io/delete-after"]
    harness.clock.advance(61)
    run(harness, 10)
    assert not harness.store.exists(C.KIND_RAYCLUSTER, old_active, "default")


def test_incremental_upgrade(harness):
    config = cluster_spec(workers=1, min_replicas=1, autoscaling=True)
    spec = service_spec(
        rayClusterConfig=config,
        upgradeStrategy={
            "type": "NewClusterWithIncrementalUpgrade",
            "clusterUpgradeOptions": {
                "maxSurgePercent": 100,
                "stepSizePercent": 50,
                "intervalSeconds": 5,
                "gatewayClassName": "gateway",
            },
        },
    )
    apply_service(harness, spec)
    run(harness)
    old_active = harness.get(C.KIND_RAYSERVICE, "svc")["status"]["activeServiceStatus"]["rayClusterName"]
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    bump_cluster_image(service, "2.60.0")
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 12, 0.5)
    status = harness.get(C.KIND_RAYSERVICE, "svc")["status"]
    assert status["pendingServiceStatus"].get("trafficRoutedPercent", 0) >= 50
    run(harness, 30, 0.5)
    current = harness.get(C.KIND_RAYSERVICE, "svc")
    assert current["status"]["activeServiceStatus"]["rayClusterName"] != old_active
    assert current["status"]["serviceStatus"] == "Running"


def test_rollback_pending_cluster(harness):
    apply_service(harness)
    run(harness)
    old_active = harness.get(C.KIND_RAYSERVICE, "svc")["status"]["activeServiceStatus"]["rayClusterName"]
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    bump_cluster_image(service, "2.60.0")
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 4)
    assert harness.get(C.KIND_RAYSERVICE, "svc")["status"].get("pendingServiceStatus")
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    bump_cluster_image(service, "2.59.0")
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 10)
    status = harness.get(C.KIND_RAYSERVICE, "svc")["status"]
    assert status["activeServiceStatus"]["rayClusterName"] == old_active
    assert not status.get("pendingServiceStatus")


def test_suspend_and_resume(harness):
    apply_service(harness)
    run(harness)
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    service["spec"]["suspend"] = True
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 30)
    suspended = harness.get(C.KIND_RAYSERVICE, "svc")
    assert suspended["status"]["serviceStatus"] == ""
    assert suspended["status"]["numServeEndpoints"] == 0
    assert harness.list(C.KIND_RAYCLUSTER) == []
    suspended["spec"]["suspend"] = False
    harness.store.update(C.KIND_RAYSERVICE, suspended)
    run(harness, 30)
    assert harness.get(C.KIND_RAYSERVICE, "svc")["status"]["serviceStatus"] == "Running"


def test_delete_service_cleans_clusters(harness):
    apply_service(harness)
    run(harness)
    harness.store.delete(C.KIND_RAYSERVICE, "svc", "default")
    run(harness, 30)
    assert not harness.store.exists(C.KIND_RAYSERVICE, "svc", "default")
    assert harness.list(C.KIND_RAYCLUSTER) == []
    assert harness.list(C.KIND_SERVICE) == []


def test_invalid_serve_config(harness):
    apply_service(harness, service_spec(serveConfigV2="applications: [not: valid"))
    run(harness, 10)
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    assert service["status"]["serviceStatus"] == ""
    condition = {item["type"]: item for item in service["status"]["conditions"]}[C.COND_READY]
    assert condition["reason"] == "ValidationFailed"


def test_custom_serve_service_ports(harness):
    custom = {"metadata": {"annotations": {"example.com/own": "true"}}, "spec": {"ports": [{"name": "http", "port": 9000, "targetPort": 8000}]}}
    apply_service(harness, service_spec(serveService=custom))
    run(harness)
    serve = harness.get(C.KIND_SERVICE, "svc-serve-svc")
    assert serve["spec"]["ports"][0]["port"] == 9000
    assert serve["metadata"]["annotations"]["example.com/own"] == "true"


def test_upgrade_strategy_none_keeps_cluster(harness):
    spec = service_spec(upgradeStrategy={"type": "None"})
    apply_service(harness, spec)
    run(harness)
    active = harness.get(C.KIND_RAYSERVICE, "svc")["status"]["activeServiceStatus"]["rayClusterName"]
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    service["spec"]["serveConfigV2"] = serve_config("v2")
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 20)
    current = harness.get(C.KIND_RAYSERVICE, "svc")
    assert current["status"]["activeServiceStatus"]["rayClusterName"] == active
    assert len(harness.list(C.KIND_RAYCLUSTER)) == 1


def test_incremental_upgrade_creates_gateway_and_weighted_route(harness):
    config = cluster_spec(workers=1, min_replicas=1, autoscaling=True)
    spec = service_spec(
        rayClusterConfig=config,
        upgradeStrategy={
            "type": "NewClusterWithIncrementalUpgrade",
            "clusterUpgradeOptions": {
                "maxSurgePercent": 100,
                "stepSizePercent": 50,
                "intervalSeconds": 5,
                "gatewayClassName": "gateway",
            },
        },
    )
    apply_service(harness, spec)
    run(harness)
    active = harness.get(C.KIND_RAYSERVICE, "svc")["status"]["activeServiceStatus"]["rayClusterName"]
    assert harness.store.exists(C.KIND_GATEWAY, "svc-gateway", "default")
    route = harness.get(C.KIND_HTTPROUTE, "svc-httproute", "default")
    refs = route["spec"]["rules"][0]["backendRefs"]
    assert [(ref["name"], ref["weight"]) for ref in refs] == [(f"{active}-serve-svc", 100)]
    assert harness.store.exists(C.KIND_SERVICE, f"{active}-serve-svc", "default")

    service = harness.get(C.KIND_RAYSERVICE, "svc")
    bump_cluster_image(service, "2.60.0")
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 12, 0.5)
    route = harness.get(C.KIND_HTTPROUTE, "svc-httproute", "default")
    refs = route["spec"]["rules"][0]["backendRefs"]
    assert len(refs) == 2
    assert sum(ref["weight"] for ref in refs) == 100
    assert min(ref["weight"] for ref in refs) > 0


def test_adopted_httproute_only_patches_backend_refs(harness):
    route = new_object(
        "gateway.networking.k8s.io/v1",
        C.KIND_HTTPROUTE,
        "shared-route",
        "default",
        spec={
            "parentRefs": [{"name": "shared-gateway", "namespace": "default"}],
            "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}], "backendRefs": []}],
        },
        labels={"owner": "platform"},
    )
    harness.store.create(C.KIND_HTTPROUTE, route)
    config = cluster_spec(workers=1, min_replicas=1, autoscaling=True)
    spec = service_spec(
        rayClusterConfig=config,
        upgradeStrategy={
            "type": "NewClusterWithIncrementalUpgrade",
            "clusterUpgradeOptions": {
                "maxSurgePercent": 50,
                "stepSizePercent": 10,
                "intervalSeconds": 5,
                "httpRouteName": "shared-route",
            },
        },
    )
    apply_service(harness, spec)
    run(harness)
    patched = harness.get(C.KIND_HTTPROUTE, "shared-route", "default")
    assert patched["metadata"]["labels"]["owner"] == "platform"
    assert patched["spec"]["parentRefs"][0]["name"] == "shared-gateway"
    refs = patched["spec"]["rules"][0]["backendRefs"]
    assert len(refs) == 1 and refs[0]["weight"] == 100
    assert not harness.store.exists(C.KIND_GATEWAY, "svc-gateway", "default")


def test_serve_config_change_updates_active_cluster_in_place(harness):
    apply_service(harness)
    run(harness)
    old_active = harness.get(C.KIND_RAYSERVICE, "svc")["status"]["activeServiceStatus"]["rayClusterName"]
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    service["spec"]["serveConfigV2"] = serve_config("v2")
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 20)
    current = harness.get(C.KIND_RAYSERVICE, "svc")
    assert current["status"]["activeServiceStatus"]["rayClusterName"] == old_active
    assert not current["status"].get("pendingServiceStatus")
    # No second RayCluster was created.
    assert len(harness.list(C.KIND_RAYCLUSTER)) == 1
    assert current["status"]["serviceStatus"] == "Running"


def test_adopted_httproute_converges(harness):
    route = new_object(
        "gateway.networking.k8s.io/v1",
        C.KIND_HTTPROUTE,
        "shared-route",
        "default",
        spec={"parentRefs": [{"name": "shared-gateway"}], "rules": [{"backendRefs": []}]},
    )
    harness.store.create(C.KIND_HTTPROUTE, route)
    spec = service_spec(
        rayClusterConfig=cluster_spec(workers=1, min_replicas=1, autoscaling=True),
        upgradeStrategy={
            "type": "NewClusterWithIncrementalUpgrade",
            "clusterUpgradeOptions": {
                "maxSurgePercent": 50,
                "stepSizePercent": 10,
                "intervalSeconds": 5,
                "httpRouteName": "shared-route",
            },
        },
    )
    apply_service(harness, spec)
    harness.settle(60)
    assert harness.operator.run_once() is False
    assert harness.operator.run_once() is False


def test_condition_observed_generation_tracks_object_generation(harness):
    apply_service(harness)
    run(harness, 20)
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    service = bump_cluster_image(service)
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 50)
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    generation = service["metadata"]["generation"]
    assert generation > 1
    assert service["status"]["observedGeneration"] == generation
    for condition in service["status"].get("conditions") or []:
        assert condition.get("observedGeneration") == generation, condition
