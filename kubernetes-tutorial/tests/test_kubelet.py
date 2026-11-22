from __future__ import annotations

import time


def pod(name="p", *, labels=None, annotations=None, probe=None, volumes=None, init=None, resources=None):
    container = {"name": "c", "image": "demo:1", "ports": [{"containerPort": 8080}]}
    if resources:
        container["resources"] = resources
    if probe:
        container["livenessProbe"] = probe
    spec = {"containers": [container]}
    if volumes:
        spec["volumes"] = volumes
    if init:
        spec["initContainers"] = init
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "default",
                         "labels": labels or {}, "annotations": annotations or {}},
            "spec": spec}


def test_pod_runs_and_gets_identity(cluster):
    cluster.apply(pod("simple"))
    created = cluster.wait_for_pod("simple")
    assert created["status"]["podIP"].startswith("10.244.")
    assert created["status"]["hostIP"] == "127.0.0.1"
    assert created["status"]["qosClass"] == "BestEffort"
    assert created["status"]["containerStatuses"][0]["state"].get("running")


def test_readiness_annotation_delays_ready(cluster):
    cluster.apply(pod("slow", annotations={"mini-k8s.io/simulate.ready-after-ticks": "5"}))
    time.sleep(0.15)
    early = cluster.get("pods", "slow")
    assert early["status"]["phase"] == "Running"
    assert not any(c["type"] == "Ready" and c["status"] == "True" for c in early["status"]["conditions"])
    cluster.wait_for_pod("slow")
    assert any(c["type"] == "Ready" and c["status"] == "True"
               for c in cluster.get("pods", "slow")["status"]["conditions"])


def test_readiness_probe_requires_declared_port(cluster):
    body = pod("bad-probe")
    body["spec"]["containers"][0]["readinessProbe"] = {
        "httpGet": {"path": "/", "port": 9999}, "failureThreshold": 2, "periodSeconds": 1}
    cluster.apply(body)
    time.sleep(0.5)
    current = cluster.get("pods", "bad-probe")
    assert not any(c["type"] == "Ready" and c["status"] == "True" for c in current["status"]["conditions"])


def test_liveness_failure_restarts_container(cluster):
    cluster.apply(pod("crashy", probe={
        "exec": {"command": ["false"]}, "periodSeconds": 1, "failureThreshold": 1}))
    cluster.wait_until(
        lambda: (cluster.get("pods", "crashy") or {}).get("status", {}).get("containerStatuses", [{}])[0].get("restartCount", 0) >= 1,
        timeout=8, description="liveness restart")
    status = cluster.get("pods", "crashy")["status"]["containerStatuses"][0]
    assert status["restartCount"] >= 1
    assert status["lastState"]["terminated"]["exitCode"] == 137


def test_startup_probe_failure_restarts_container(cluster):
    body = pod("starter")
    body["spec"]["containers"][0]["startupProbe"] = {
        "exec": {"command": ["false"]}, "periodSeconds": 1, "failureThreshold": 1}
    cluster.apply(body)
    cluster.wait_until(
        lambda: (cluster.get("pods", "starter") or {}).get("status", {}).get("containerStatuses", [{}])[0].get("restartCount", 0) >= 1,
        timeout=8, description="startup probe restart")
    status = cluster.get("pods", "starter")["status"]["containerStatuses"][0]
    assert status["restartCount"] >= 1


def test_job_pod_completes(cluster):
    body = pod("oneshot", annotations={"mini-k8s.io/simulate.run-ticks": "1"})
    body["spec"]["restartPolicy"] = "Never"
    cluster.apply(body)
    finished = cluster.wait_for_pod("oneshot", phase="Succeeded", ready=False)
    assert finished["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] == 0


def test_failed_one_shot_pod(cluster):
    body = pod("failing", annotations={"mini-k8s.io/simulate.run-ticks": "1",
                                       "mini-k8s.io/simulate.exit-code": "3"})
    body["spec"]["restartPolicy"] = "Never"
    cluster.apply(body)
    finished = cluster.wait_for_pod("failing", phase="Failed", ready=False)
    assert finished["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] == 3


def test_terminal_runtime_is_dropped_only_after_pod_deletion(cluster):
    body = pod("done", annotations={"mini-k8s.io/simulate.run-ticks": "1"})
    body["spec"]["restartPolicy"] = "Never"
    cluster.apply(body)
    finished = cluster.wait_for_pod("done", phase="Succeeded", ready=False)
    uid = finished["metadata"]["uid"]
    assert uid in cluster.kubelets["mini-node-1"].runtimes
    assert "Starting container c" in cluster.logs("default", "done")
    cluster.delete("pod/done")
    cluster.wait_until(lambda: uid not in cluster.kubelets["mini-node-1"].runtimes,
                       timeout=5, description="runtime dropped after deletion")


def test_init_containers_complete_before_app(cluster):
    body = pod("with-init", init=[{"name": "init", "image": "busybox", "command": ["true"]}])
    cluster.apply(body)
    cluster.wait_for_pod("with-init")
    assert cluster.get("pods", "with-init")["status"]["initContainerStatuses"][0]["state"]["terminated"]["exitCode"] == 0


def test_missing_configmap_volume_blocks_pod(cluster):
    body = pod("needs-config", volumes=[{"name": "cfg", "configMap": {"name": "not-yet"}}])
    cluster.apply(body)
    time.sleep(0.4)
    pending = cluster.get("pods", "needs-config")
    assert pending["status"]["phase"] == "Pending"
    assert "configmap/not-yet" in pending["status"].get("message", "")
    cluster.apply({"apiVersion": "v1", "kind": "ConfigMap",
                   "metadata": {"name": "not-yet", "namespace": "default"}, "data": {"a": "b"}})
    cluster.wait_for_pod("needs-config")


def test_logs_and_previous_logs(cluster):
    cluster.apply(pod("logger", probe={"exec": {"command": ["false"]}, "periodSeconds": 1, "failureThreshold": 1}))
    cluster.wait_until(lambda: (cluster.get("pods", "logger") or {}).get("status", {}).get("containerStatuses", [{}])[0].get("restartCount", 0) >= 2,
                       timeout=8, description="two restarts")
    logs = cluster.logs("default", "logger")
    previous = cluster.logs("default", "logger", previous=True)
    assert "Starting container c" in logs
    assert "Restarting container c" in previous or "Restarting container c" in logs


def test_exec_echo(cluster):
    cluster.apply(pod("exec-pod"))
    cluster.wait_for_pod("exec-pod")
    result = cluster.exec("default", "exec-pod", "c", ["echo", "hello"])
    assert result["exitCode"] == 0
    assert result["stdout"] == "hello\n"


def test_qos_class_burstable_and_guaranteed(cluster):
    burstable = pod("burstable", resources={"requests": {"cpu": "100m"}})
    guaranteed = pod("guaranteed", resources={
        "requests": {"cpu": "100m", "memory": "64Mi"},
        "limits": {"cpu": "100m", "memory": "64Mi"},
    })
    cluster.apply(burstable)
    cluster.apply(guaranteed)
    cluster.wait_for_pod("burstable")
    cluster.wait_for_pod("guaranteed")
    assert cluster.get("pods", "burstable")["status"]["qosClass"] == "Burstable"
    assert cluster.get("pods", "guaranteed")["status"]["qosClass"] == "Guaranteed"


def test_qos_cpu_only_is_burstable_not_guaranteed(cluster):
    cluster.apply(pod("cpu-only", resources={"requests": {"cpu": "100m"},
                                             "limits": {"cpu": "100m"}}))
    created = cluster.wait_for_pod("cpu-only")
    assert created["status"]["qosClass"] == "Burstable"
