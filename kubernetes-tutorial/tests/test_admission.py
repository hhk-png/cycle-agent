from __future__ import annotations

import pytest

from mini_k8s.admission.admission import validate_openapi
from mini_k8s.errors import ApiError


def pod(name="p", **spec):
    body = {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "default"},
            "spec": {"containers": [{"name": "c", "image": "demo:1"}]}}
    body["spec"].update(spec)
    return body


def test_pod_defaults(cluster):
    created = cluster.apply(pod())[0]
    spec = created["spec"]
    assert spec["restartPolicy"] == "Always"
    assert spec["dnsPolicy"] == "ClusterFirst"
    assert spec["serviceAccountName"] == "default"
    assert spec["containers"][0]["imagePullPolicy"] == "IfNotPresent"
    assert spec["containers"][0]["resources"] == {}
    assert spec["priority"] == 0


def test_pod_requires_containers(cluster):
    body = pod()
    body["spec"]["containers"] = []
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "containers must not be empty" in exc.value.message


def test_pod_duplicate_container_names(cluster):
    body = pod()
    body["spec"]["containers"].append({"name": "c", "image": "demo:2"})
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "duplicate container name" in exc.value.message


def test_request_cannot_exceed_limit(cluster):
    body = pod()
    body["spec"]["containers"][0]["resources"] = {
        "requests": {"cpu": "1"}, "limits": {"cpu": "500m"}}
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "exceeds limit" in exc.value.message


def test_invalid_quantity_rejected(cluster):
    body = pod()
    body["spec"]["containers"][0]["resources"] = {"requests": {"memory": "12XB"}}
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "invalid quantity" in exc.value.message


def test_deployment_selector_must_match(cluster):
    from conftest import deployment
    body = deployment("bad")
    body["spec"]["selector"] = {"matchLabels": {"app": "different"}}
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "does not match" in exc.value.message


def test_workload_pod_restart_policy_always(cluster):
    from conftest import deployment
    body = deployment("bad-restart")
    body["spec"]["template"]["spec"]["restartPolicy"] = "Never"
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "restartPolicy must be Always" in exc.value.message


def test_service_requires_ports(cluster):
    body = {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "svc", "namespace": "default"},
            "spec": {"selector": {"app": "x"}, "ports": []}}
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "ports" in exc.value.message


def test_service_duplicate_ports(cluster):
    body = {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "svc", "namespace": "default"},
            "spec": {"ports": [{"port": 80}, {"port": 80}]}}
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "duplicate port" in exc.value.message


def test_cronjob_invalid_schedule(cluster):
    body = {"apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": "bad", "namespace": "default"},
            "spec": {"schedule": "not a cron",
                     "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}}}}}
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "cron" in exc.value.message


def test_namespace_must_exist(cluster):
    body = pod()
    body["metadata"]["namespace"] = "missing"
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "not found" in exc.value.message


def test_limit_range_defaults(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "limited"}})
    cluster.apply({"apiVersion": "v1", "kind": "LimitRange",
                   "metadata": {"name": "defaults", "namespace": "limited"},
                   "spec": {"limits": [{"type": "Container",
                                        "default": {"cpu": "500m", "memory": "256Mi"},
                                        "defaultRequest": {"cpu": "100m", "memory": "64Mi"}}]}})
    body = pod()
    body["metadata"]["namespace"] = "limited"
    created = cluster.apply(body)[0]
    resources = created["spec"]["containers"][0]["resources"]
    assert resources["requests"]["cpu"] == "100m"
    assert resources["limits"]["memory"] == "256Mi"


def test_resource_quota_blocks_create(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "quota-ns"}})
    cluster.apply({"apiVersion": "v1", "kind": "ResourceQuota",
                   "metadata": {"name": "q", "namespace": "quota-ns"},
                   "spec": {"hard": {"pods": "1"}}})
    first = pod("q1")
    first["metadata"]["namespace"] = "quota-ns"
    cluster.apply(first)
    second = pod("q2")
    second["metadata"]["namespace"] = "quota-ns"
    with pytest.raises(ApiError) as exc:
        cluster.apply(second)
    assert "exceeded quota" in exc.value.message


def test_quota_counts_updated_object_once(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "quota-update"}})
    cluster.apply({"apiVersion": "v1", "kind": "ResourceQuota",
                   "metadata": {"name": "q", "namespace": "quota-update"},
                   "spec": {"hard": {"pods": "2"}}})
    body = pod("one")
    body["metadata"]["namespace"] = "quota-update"
    created = cluster.apply(body)[0]
    created["metadata"]["labels"] = {"updated": "true"}
    cluster.apply(created)
    assert cluster.get("pods", "one", "quota-update")["metadata"]["labels"]["updated"] == "true"


def test_pod_security_baseline_rejects_privileged(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "secure", "labels": {"pod-security.kubernetes.io/enforce": "baseline"}}})
    body = pod()
    body["metadata"]["namespace"] = "secure"
    body["spec"]["containers"][0]["securityContext"] = {"privileged": True}
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "baseline" in exc.value.message


def test_pod_security_restricted_requires_non_root(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "restricted", "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
    body = pod()
    body["metadata"]["namespace"] = "restricted"
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "runAsNonRoot" in exc.value.message


def test_restricted_pod_can_be_admitted(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "restricted-ok", "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
    body = pod()
    body["metadata"]["namespace"] = "restricted-ok"
    body["spec"]["securityContext"] = {"runAsNonRoot": True}
    body["spec"]["containers"][0]["securityContext"] = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    created = cluster.apply(body)[0]
    assert created["metadata"]["name"] == "p"


def test_openapi_schema_validation():
    schema = {
        "type": "object",
        "required": ["spec"],
        "properties": {
            "spec": {
                "type": "object",
                "required": ["image"],
                "properties": {
                    "image": {"type": "string"},
                    "replicas": {"type": "integer", "minimum": 1},
                    "mode": {"type": "string", "enum": ["a", "b"]},
                },
            }
        },
    }
    assert validate_openapi({"spec": {"image": "x", "replicas": 2, "mode": "a"}}, schema) == []
    errors = validate_openapi({"spec": {"replicas": "two", "mode": "c"}}, schema)
    assert any("image" in error for error in errors)
    assert any("expected number" in error for error in errors)
    assert any("must be one of" in error for error in errors)
    assert any(">=" in error for error in validate_openapi({"spec": {"image": "x", "replicas": 0}}, schema))


def test_limit_range_min_max_and_ratio_are_enforced(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "bounded"}})
    cluster.apply({"apiVersion": "v1", "kind": "LimitRange",
                   "metadata": {"name": "bounds", "namespace": "bounded"},
                   "spec": {"limits": [{"type": "Container",
                                        "min": {"cpu": "50m"},
                                        "max": {"cpu": "200m"},
                                        "maxLimitRequestRatio": {"cpu": "2"}}]}})
    too_big = pod("too-big")
    too_big["metadata"]["namespace"] = "bounded"
    too_big["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "1"}}
    with pytest.raises(ApiError) as exc:
        cluster.apply(too_big)
    assert "exceeds LimitRange maximum" in exc.value.message

    too_small = pod("too-small")
    too_small["metadata"]["namespace"] = "bounded"
    too_small["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "10m"}}
    with pytest.raises(ApiError) as exc:
        cluster.apply(too_small)
    assert "below LimitRange minimum" in exc.value.message

    bad_ratio = pod("bad-ratio")
    bad_ratio["metadata"]["namespace"] = "bounded"
    bad_ratio["spec"]["containers"][0]["resources"] = {
        "requests": {"cpu": "50m"}, "limits": {"cpu": "200m"}}
    with pytest.raises(ApiError) as exc:
        cluster.apply(bad_ratio)
    assert "ratio" in exc.value.message

    ok = pod("ok")
    ok["metadata"]["namespace"] = "bounded"
    ok["spec"]["containers"][0]["resources"] = {
        "requests": {"cpu": "100m"}, "limits": {"cpu": "200m"}}
    cluster.apply(ok)


def test_baseline_rejects_hostpath_volume(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "baseline-hostpath",
                                "labels": {"pod-security.kubernetes.io/enforce": "baseline"}}})
    body = pod()
    body["metadata"]["namespace"] = "baseline-hostpath"
    body["spec"]["volumes"] = [{"name": "host", "hostPath": {"path": "/etc"}}]
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "hostPath" in exc.value.message


def test_restricted_rejects_container_level_run_as_non_root_false(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "restricted-bypass",
                                "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
    body = pod()
    body["metadata"]["namespace"] = "restricted-bypass"
    body["spec"]["securityContext"] = {"runAsNonRoot": True}
    body["spec"]["containers"][0]["securityContext"] = {
        "runAsNonRoot": False,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    with pytest.raises(ApiError) as exc:
        cluster.apply(body)
    assert "runAsNonRoot" in exc.value.message
