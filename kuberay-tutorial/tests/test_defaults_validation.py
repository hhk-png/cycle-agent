from __future__ import annotations

import copy

from minikuberay import constants as C
from minikuberay.models.defaults import apply_defaults
from minikuberay.models.meta import new_object
from minikuberay.models.validation import validate_object, validate_raycluster_spec

from .conftest import cluster_spec


def cluster(spec=None, name="demo", kind=C.KIND_RAYCLUSTER):
    return apply_defaults(new_object(C.RAY_API_VERSION, kind, name, "default", spec=copy.deepcopy(spec or cluster_spec())))


def test_cluster_defaults():
    obj = cluster()
    assert obj["spec"]["rayVersion"] == "2.59.0"
    assert obj["spec"]["workerGroupSpecs"][0]["numOfHosts"] == 1
    assert obj["spec"]["workerGroupSpecs"][0]["priority"] == 0
    assert obj["spec"]["headGroupSpec"]["serviceType"] == "ClusterIP"
    assert obj["spec"]["headGroupSpec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_cluster_autoscaling_defaults():
    spec = cluster_spec(autoscaling=True)
    obj = cluster(spec)
    options = obj["spec"]["autoscalerOptions"]
    assert options["upscalingMode"] == "Default"
    assert options["idleTimeoutSeconds"] == 15
    assert options["version"] == "v2"  # Ray 2.59 supports autoscaler v2


def test_cluster_valid():
    assert validate_object(cluster()) == []


def test_cluster_name_too_long():
    obj = cluster(name="a" * 54)
    assert any("no more than 53" in error for error in validate_object(obj))


def test_cluster_invalid_dns_name():
    obj = cluster(name="Bad_Name")
    assert any("DNS-1035" in error for error in validate_object(obj))


def test_cluster_name_cannot_start_with_digit():
    # KubeRay validates CR names as DNS-1035 (must start with a lowercase
    # letter), unlike worker group names/namespaces which allow digits.
    obj = cluster(name="1-cluster")
    assert any("DNS-1035" in error for error in validate_object(obj))


def test_cluster_missing_ray_container():
    spec = cluster_spec()
    spec["headGroupSpec"]["template"]["spec"]["containers"][0]["name"] = "sidecar"
    errors: list[str] = []
    validate_raycluster_spec(spec, errors)
    assert any("named 'ray'" in error for error in errors)


def test_cluster_ray_container_must_be_first():
    spec = cluster_spec()
    spec["headGroupSpec"]["template"]["spec"]["containers"].insert(0, {"name": "sidecar", "image": "busybox"})
    errors: list[str] = []
    validate_raycluster_spec(spec, errors)
    assert any("first container" in error for error in errors)


def test_cluster_duplicate_group_names():
    spec = cluster_spec()
    spec["workerGroupSpecs"].append(copy.deepcopy(spec["workerGroupSpecs"][0]))
    errors = validate_object(cluster(spec))
    assert any("duplicate worker group" in error for error in errors)


def test_cluster_min_greater_than_max():
    spec = cluster_spec()
    spec["workerGroupSpecs"][0]["minReplicas"] = 10
    spec["workerGroupSpecs"][0]["maxReplicas"] = 2
    errors = validate_object(cluster(spec))
    assert any("minReplicas" in error and "maxReplicas" in error for error in errors)


def test_cluster_invalid_autoscaler_mode():
    spec = cluster_spec(autoscaling=True)
    spec["autoscalerOptions"]["upscalingMode"] = "Turbo"
    errors = validate_object(cluster(spec))
    assert any("upscalingMode" in error for error in errors)


def test_cluster_autoscaler_v2_requires_ray_247():
    spec = cluster_spec(autoscaling=True)
    spec["rayVersion"] = "2.46.0"
    spec["autoscalerOptions"]["version"] = "v2"
    errors = validate_object(cluster(spec))
    assert any("v2 requires Ray" in error for error in errors)


def test_cluster_gcs_ft_redis_requires_address():
    spec = cluster_spec()
    spec["gcsFaultToleranceOptions"] = {"backend": "redis"}
    errors = validate_object(cluster(spec))
    assert any("redisAddress" in error for error in errors)


def test_cluster_rocksdb_rejects_redis_address():
    spec = cluster_spec()
    spec["gcsFaultToleranceOptions"] = {"backend": "rocksdb", "redisAddress": "redis:6379"}
    errors = validate_object(cluster(spec))
    assert any("redisAddress must be empty" in error for error in errors)


def test_cluster_active_passive_lease_order():
    spec = cluster_spec()
    spec["gcsFaultToleranceOptions"] = {
        "backend": "redis",
        "redisAddress": "redis:6379",
        "activePassiveHeadOptions": {"enabled": True, "leaseDurationSeconds": 5, "renewDeadlineSeconds": 10, "retryPeriodSeconds": 2},
    }
    errors = validate_object(cluster(spec))
    assert any("leaseDurationSeconds" in error for error in errors)


def test_cluster_network_policy_mode():
    spec = cluster_spec()
    spec["networkPolicy"] = {"mode": "AllowSome"}
    errors = validate_object(cluster(spec))
    assert any("networkPolicy.mode" in error for error in errors)


def test_cluster_history_collector_requires_image():
    spec = cluster_spec()
    spec["historyServerOptions"] = {"collectorOptions": {}}
    errors = validate_object(cluster(spec))
    assert any("collectorOptions.image" in error for error in errors)


def test_cluster_conflicting_resources_and_ray_start_params():
    spec = cluster_spec()
    spec["workerGroupSpecs"][0]["rayStartParams"] = {"num-cpus": "2"}
    spec["workerGroupSpecs"][0]["resources"] = {"Custom": "1"}
    errors = validate_object(cluster(spec))
    assert any("resource fields" in error for error in errors)


def test_rayjob_valid():
    obj = apply_defaults(
        new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec={"entrypoint": "echo hi", "rayClusterSpec": cluster_spec(workers=0)})
    )
    assert validate_object(obj) == []


def test_rayjob_requires_cluster_or_selector():
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec={"entrypoint": "echo hi"}))
    errors = validate_object(obj)
    assert any("one of rayClusterSpec or clusterSelector" in error for error in errors)


def test_rayjob_selector_requires_cluster_key():
    obj = apply_defaults(
        new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec={"entrypoint": "echo", "clusterSelector": {"team": "ml"}})
    )
    errors = validate_object(obj)
    assert any("ray.io/cluster" in error for error in errors)


def test_rayjob_selector_suspend_incompatible():
    obj = apply_defaults(
        new_object(
            C.RAY_API_VERSION,
            C.KIND_RAYJOB,
            "job",
            "default",
            spec={"entrypoint": "echo", "clusterSelector": {"ray.io/cluster": "c"}, "suspend": True, "shutdownAfterJobFinishes": True},
        )
    )
    errors = validate_object(obj)
    assert any("does not support suspend" in error for error in errors)


def test_rayjob_sidecar_restrictions():
    spec = {
        "entrypoint": "echo",
        "submissionMode": "SidecarMode",
        "submitterPodTemplate": {"spec": {"containers": [{"name": "s", "image": "x"}]}},
        "rayClusterSpec": cluster_spec(workers=0),
    }
    errors = validate_object(apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec=spec)))
    assert any("does not support submitterPodTemplate" in error for error in errors)


def test_rayjob_interactive_backoff_incompatible():
    spec = {"entrypoint": "", "submissionMode": "InteractiveMode", "backoffLimit": 2, "rayClusterSpec": cluster_spec(workers=0)}
    errors = validate_object(apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec=spec)))
    assert any("InteractiveMode" in error for error in errors)


def test_rayjob_ttl_requires_shutdown():
    spec = {"entrypoint": "echo", "ttlSecondsAfterFinished": 10, "rayClusterSpec": cluster_spec(workers=0)}
    errors = validate_object(apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec=spec)))
    assert any("requires shutdownAfterJobFinishes" in error for error in errors)


def test_rayjob_invalid_deletion_strategy():
    spec = {
        "entrypoint": "echo",
        "rayClusterSpec": cluster_spec(workers=0),
        "deletionStrategy": {"deletionRules": [{"policy": "DeleteCluster", "condition": {"jobStatus": "SUCCEEDED", "jobDeploymentStatus": "Failed"}}]},
    }
    errors = validate_object(apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec=spec)))
    assert any("exactly one" in error for error in errors)


def test_rayjob_deadlines_must_be_positive():
    spec = {"entrypoint": "echo", "activeDeadlineSeconds": 0, "preRunningDeadlineSeconds": -1, "rayClusterSpec": cluster_spec(workers=0)}
    errors = validate_object(apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec=spec)))
    assert any("activeDeadlineSeconds" in error for error in errors)
    assert any("preRunningDeadlineSeconds" in error for error in errors)


def test_rayservice_valid():
    spec = {
        "serveConfigV2": "applications:\n  - name: app\n    import_path: app:deployment\n",
        "rayClusterConfig": cluster_spec(workers=1),
    }
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYSERVICE, "svc", "default", spec=spec))
    assert validate_object(obj) == []


def test_rayservice_incremental_requires_autoscaling():
    spec = {
        "serveConfigV2": "applications: []",
        "rayClusterConfig": cluster_spec(workers=1),
        "upgradeStrategy": {
            "type": "NewClusterWithIncrementalUpgrade",
            "clusterUpgradeOptions": {"stepSizePercent": 10, "intervalSeconds": 10, "gatewayClassName": "g"},
        },
    }
    errors = validate_object(apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYSERVICE, "svc", "default", spec=spec)))
    assert any("requires Ray autoscaling" in error for error in errors)


def test_rayservice_incremental_options():
    spec = {
        "serveConfigV2": "applications: []",
        "rayClusterConfig": cluster_spec(workers=1, autoscaling=True),
        "upgradeStrategy": {
            "type": "NewClusterWithIncrementalUpgrade",
            "clusterUpgradeOptions": {"maxSurgePercent": 10, "stepSizePercent": 20, "intervalSeconds": 0, "gatewayClassName": "g"},
        },
    }
    errors = validate_object(apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYSERVICE, "svc", "default", spec=spec)))
    assert any("stepSizePercent" in error for error in errors)
    assert any("intervalSeconds" in error for error in errors)


def test_rayservice_custom_head_service_name_rejected():
    spec = {"serveConfigV2": "applications: []", "rayClusterConfig": cluster_spec(workers=1)}
    spec["rayClusterConfig"]["headGroupSpec"]["headService"] = {"metadata": {"name": "custom"}}
    errors = validate_object(apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYSERVICE, "svc", "default", spec=spec)))
    assert any("headService.metadata.name" in error for error in errors)


def test_raycronjob_valid():
    spec = {
        "schedule": "*/5 * * * *",
        "timeZone": "UTC",
        "jobTemplate": {"entrypoint": "echo hi", "rayClusterSpec": cluster_spec(workers=0)},
    }
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCRONJOB, "cron", "default", spec=spec))
    assert validate_object(obj) == []


def test_raycronjob_invalid_schedule_and_timezone():
    spec = {
        "schedule": "TZ=UTC * * * * *",
        "timeZone": "Mars/Olympus",
        "jobTemplate": {"entrypoint": "echo", "rayClusterSpec": cluster_spec(workers=0)},
    }
    errors = validate_object(apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCRONJOB, "cron", "default", spec=spec)))
    assert any("TZ" in error for error in errors)
    assert any("invalid time zone" in error for error in errors)


def test_raycronjob_requires_template():
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCRONJOB, "cron", "default", spec={"schedule": "* * * * *"}))
    errors = validate_object(obj)
    assert any("jobTemplate" in error for error in errors)


def test_rayjob_submitter_backoff_defaults_to_two():
    spec = {
        "entrypoint": "echo hi",
        "submissionMode": "K8sJobMode",
        "rayClusterSpec": cluster_spec(workers=0),
    }
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec=spec))
    assert obj["spec"]["submitterConfig"]["backoffLimit"] == 2


def test_sidecar_mode_has_no_defaulted_submitter_config():
    spec = {
        "entrypoint": "echo hi",
        "submissionMode": "SidecarMode",
        "rayClusterSpec": cluster_spec(workers=0),
    }
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "job", "default", spec=spec))
    assert "submitterConfig" not in obj["spec"]
    assert validate_object(obj) == []


def test_defaulting_does_not_silently_add_ray_to_sidecar_only_template():
    spec = cluster_spec(workers=0)
    spec["headGroupSpec"]["template"]["spec"]["containers"] = [
        {"name": "sidecar", "image": "busybox", "resources": {"requests": {"cpu": "100m"}}}
    ]
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "demo", "default", spec=spec))
    assert [item["name"] for item in obj["spec"]["headGroupSpec"]["template"]["spec"]["containers"]] == ["sidecar"]
    errors = validate_object(obj)
    assert any("named 'ray'" in error or "must be the first" in error for error in errors)
