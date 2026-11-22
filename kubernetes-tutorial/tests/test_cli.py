from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(api_cluster, *args: str, stdin: str | None = None, timeout: float = 20) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "MINIK8S_SERVER": api_cluster.url,
        "MINIK8S_TOKEN": "mini-admin-token",
        "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1]),
    })
    return subprocess.run([sys.executable, "-m", "mini_k8s.cli", *args], capture_output=True,
                          text=True, env=env, input=stdin, timeout=timeout, cwd=ROOT)


def test_cli_version_and_api_resources(api_cluster):
    result = run_cli(api_cluster, "version")
    assert result.returncode == 0
    assert "Server Version" in result.stdout
    result = run_cli(api_cluster, "api-resources")
    assert result.returncode == 0
    assert "deployments" in result.stdout
    assert "NAMESPACED" in result.stdout


def test_cli_apply_get_scale(api_cluster):
    result = run_cli(api_cluster, "apply", "-f", "examples/01-hello.yaml")
    assert result.returncode == 0, result.stderr
    assert "deployment/web created" in result.stdout
    assert "service/web created" in result.stdout
    api_cluster.wait_for_deployment("web")
    result = run_cli(api_cluster, "get", "deployments", "-o", "wide")
    assert result.returncode == 0
    assert "web" in result.stdout and "2/2" in result.stdout
    result = run_cli(api_cluster, "scale", "deployment", "web", "--replicas", "3")
    assert result.returncode == 0, result.stderr
    api_cluster.wait_until(lambda: len([p for p in api_cluster.list("pods") if p["metadata"]["name"].startswith("web-")]) == 3,
                           timeout=8, description="CLI scale")
    result = run_cli(api_cluster, "get", "pods", "-o", "name")
    assert result.stdout.count("pod/") == 3


def test_cli_logs_exec_describe(api_cluster):
    run_cli(api_cluster, "run", "shell", "--image", "busybox", "--port", "8080")
    api_cluster.wait_for_pod("shell")
    result = run_cli(api_cluster, "logs", "shell")
    assert result.returncode == 0 and "Starting container shell" in result.stdout
    result = run_cli(api_cluster, "exec", "shell", "--", "echo", "from-cli")
    assert result.returncode == 0 and result.stdout.strip() == "from-cli"
    result = run_cli(api_cluster, "describe", "pod", "shell")
    assert result.returncode == 0
    assert "Node:" in result.stdout and "Events:" in result.stdout


def test_cli_rollout_status_and_history(api_cluster, deploy):
    api_cluster.apply(deploy("roll", replicas=2))
    api_cluster.wait_for_deployment("roll")
    result = run_cli(api_cluster, "rollout", "status", "deployment", "roll")
    assert result.returncode == 0, result.stderr
    assert "successfully rolled out" in result.stdout
    result = run_cli(api_cluster, "rollout", "history", "deployment", "roll")
    assert result.returncode == 0
    assert "roll-" in result.stdout


def test_cli_create_from_stdin(api_cluster):
    manifest = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: stdin-config
  namespace: default
data:
  hello: world
"""
    result = run_cli(api_cluster, "apply", "-f", "-", stdin=manifest)
    assert result.returncode == 0, result.stderr
    assert api_cluster.get("configmaps", "stdin-config")["data"]["hello"] == "world"


def test_cli_can_i_and_taint(api_cluster):
    result = run_cli(api_cluster, "auth", "can-i", "get", "pods")
    assert result.returncode == 0 and result.stdout.strip() == "yes"
    result = run_cli(api_cluster, "taint", "mini-node-1", "dedicated=db:NoSchedule")
    assert result.returncode == 0, result.stderr
    node = api_cluster.get("nodes", "mini-node-1")
    assert any(t["key"] == "dedicated" for t in node["spec"]["taints"])
    result = run_cli(api_cluster, "taint", "mini-node-1", "dedicated-")
    assert result.returncode == 0, result.stderr
    node = api_cluster.get("nodes", "mini-node-1")
    assert not any(t["key"] == "dedicated" for t in node["spec"]["taints"])


def test_cli_cordon_uncordon(api_cluster):
    assert run_cli(api_cluster, "cordon", "mini-node-1").returncode == 0
    assert api_cluster.get("nodes", "mini-node-1")["spec"]["unschedulable"] is True
    assert run_cli(api_cluster, "uncordon", "mini-node-1").returncode == 0
    assert api_cluster.get("nodes", "mini-node-1")["spec"]["unschedulable"] is False


def test_cli_top_after_metrics(api_cluster):
    run_cli(api_cluster, "run", "metrics-pod", "--image", "x")
    api_cluster.wait_for_pod("metrics-pod")
    time.sleep(1.2)
    result = run_cli(api_cluster, "top", "pods")
    assert result.returncode == 0, result.stderr
    assert "metrics-pod" in result.stdout
    result = run_cli(api_cluster, "top", "nodes")
    assert result.returncode == 0 and "mini-node-1" in result.stdout


def test_cli_delete_file(api_cluster):
    api_cluster.apply({"apiVersion": "v1", "kind": "ConfigMap",
                       "metadata": {"name": "to-delete", "namespace": "default"}, "data": {"a": "b"}})
    result = run_cli(api_cluster, "delete", "configmap", "to-delete")
    assert result.returncode == 0, result.stderr
    assert api_cluster.get("configmaps", "to-delete") is None


def test_cli_forbidden_user(api_cluster):
    env = dict(os.environ)
    env.update({"MINIK8S_SERVER": api_cluster.url, "MINIK8S_TOKEN": "mini-dev-token",
                "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1])})
    result = subprocess.run([sys.executable, "-m", "mini_k8s.cli", "get", "pods"], capture_output=True,
                            text=True, env=env, timeout=20, cwd=ROOT)
    assert result.returncode == 1
    assert "forbidden" in result.stderr


def test_cli_set_image_and_rollback(api_cluster):
    assert run_cli(api_cluster, "apply", "-f", "examples/01-hello.yaml").returncode == 0
    api_cluster.wait_for_deployment("web")
    result = run_cli(api_cluster, "set", "image", "deployment/web", "web=nginx:1.28")
    assert result.returncode == 0, result.stderr
    api_cluster.wait_until(
        lambda: (api_cluster.get("deployments", "web") or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image") == "nginx:1.28",
        timeout=5, description="set image")
    api_cluster.wait_for_deployment("web")
    result = run_cli(api_cluster, "rollout", "undo", "deployment", "web", "--to-revision=1")
    assert result.returncode == 0, result.stderr
    api_cluster.wait_until(
        lambda: (api_cluster.get("deployments", "web") or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image") == "nginx:1.27",
        timeout=5, description="rollback image")


def test_cli_drain_evicts_pods(api_cluster):
    api_cluster.api.apply({"apiVersion": "v1", "kind": "Pod",
                           "metadata": {"name": "drain-me", "namespace": "default"},
                           "spec": {"containers": [{"name": "c", "image": "x"}]}})
    api_cluster.wait_for_pod("drain-me")
    result = run_cli(api_cluster, "drain", "mini-node-1", "--force")
    assert result.returncode == 0, result.stderr
    api_cluster.wait_until(lambda: api_cluster.get("pods", "drain-me") is None,
                           timeout=5, description="drained pod")


def test_cli_wait_for_condition(api_cluster, deploy):
    api_cluster.apply(deploy("waitable", replicas=1))
    result = run_cli(api_cluster, "wait", "--for=condition=Available", "deployment", "waitable",
                     "--timeout", "10")
    assert result.returncode == 0, result.stderr
    assert "condition met" in result.stdout


def test_cli_run_deployment_with_replicas(api_cluster):
    result = run_cli(api_cluster, "run", "multi", "--image", "x", "--replicas", "2")
    assert result.returncode == 0, result.stderr
    api_cluster.wait_until(lambda: len([p for p in api_cluster.list("pods") if p["metadata"]["name"].startswith("multi-")]) == 2,
                           timeout=8, description="run replicas")


def test_cli_json_output(api_cluster):
    api_cluster.apply({"apiVersion": "v1", "kind": "ConfigMap",
                       "metadata": {"name": "json-cm", "namespace": "default"}, "data": {"a": "b"}})
    result = run_cli(api_cluster, "get", "configmap", "json-cm", "-o", "json")
    assert result.returncode == 0, result.stderr
    import json
    parsed = json.loads(result.stdout)
    assert parsed["data"]["a"] == "b"
