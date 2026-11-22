"""Run every example against an isolated MiniK8s cluster and assert outcomes.

Usage:
    python examples/run_all.py
    python examples/run_all.py --only 03,12
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini_k8s.cluster import MiniCluster
from mini_k8s.errors import ApiError

EXAMPLES = ROOT / "examples"


def _load(name: str) -> list[dict]:
    import yaml
    return [doc for doc in yaml.safe_load_all((EXAMPLES / name).read_text(encoding="utf-8")) if doc]


def cluster_for(options: dict | None = None) -> MiniCluster:
    instance = MiniCluster(**(options or {}))
    instance.start()
    return instance


def wait_ready(instance: MiniCluster, deployment: str, timeout: float = 15):
    instance.wait_for_deployment(deployment, timeout=timeout)


def scenario_01(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "01-hello.yaml")
    wait_ready(instance, "web")
    instance.wait_until(lambda: len((instance.get("endpoints", "web") or {}).get("subsets", [{}])[0].get("addresses", [])) == 2,
                        timeout=8, description="hello endpoints")
    assert instance.dns.resolve("web") == [instance.get("services", "web")["spec"]["clusterIP"]]
    instance.api.apply({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "hello-client", "namespace": "default"},
        "spec": {"containers": [{"name": "c", "image": "curl"}]},
    })
    instance.wait_for_pod("hello-client")
    result = instance.proxy.connect(instance.get("pods", "hello-client"), "web", "default", 80)
    assert result["connected"], result


def scenario_02(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "02-rollout.yaml")
    wait_ready(instance, "rollout-demo")
    manifest = _load("02-rollout.yaml")[0]
    manifest["spec"]["template"]["spec"]["containers"][0]["image"] = "demo:v2"
    instance.api.apply(manifest)
    instance.wait_until(
        lambda: (instance.get("deployments", "rollout-demo") or {}).get("status", {}).get("updatedReplicas") == 4
        and (instance.get("deployments", "rollout-demo") or {}).get("status", {}).get("availableReplicas") == 4
        and (instance.get("deployments", "rollout-demo") or {}).get("status", {}).get("observedGeneration")
            == (instance.get("deployments", "rollout-demo") or {}).get("metadata", {}).get("generation")
        and all(p["spec"]["containers"][0]["image"] == "demo:v2"
                for p in instance.list("pods") if p["metadata"]["name"].startswith("rollout-demo-")),
        timeout=15, description="rollout v2")
    replicasets = [r for r in instance.list("replicasets") if r["metadata"]["name"].startswith("rollout-demo-")]
    assert len(replicasets) == 2
    old = min(replicasets, key=lambda r: int(r["metadata"]["annotations"]["deployment.kubernetes.io/revision"]))
    assert old["spec"]["replicas"] == 0


def scenario_03(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "03-statefulset.yaml")
    instance.wait_until(lambda: (instance.get("statefulsets", "db") or {}).get("status", {}).get("readyReplicas") == 2,
                        timeout=15, description="statefulset ready")
    pods = sorted(p["metadata"]["name"] for p in instance.list("pods") if p["metadata"]["name"].startswith("db-"))
    assert pods == ["db-0", "db-1"]
    pvcs = instance.list("persistentvolumeclaims")
    assert {p["metadata"]["name"] for p in pvcs} == {"data-db-0", "data-db-1"}
    assert all(p["status"]["phase"] == "Bound" for p in pvcs)
    assert len(instance.dns.resolve("db")) == 2


def scenario_04(instance: MiniCluster) -> None:
    instance.add_node("worker-b", index=1)
    instance.wait_until(lambda: len(instance.get("nodes", "worker-b").get("status", {}).get("conditions", [])) > 0,
                        timeout=5, description="second node ready")
    instance.api.apply_file(EXAMPLES / "04-daemonset.yaml")
    instance.wait_until(lambda: (instance.get("daemonsets", "node-agent", "kube-system") or {}).get("status", {}).get("numberReady") == 2,
                        timeout=15, description="daemonset on both nodes")
    pods = [p for p in instance.list("pods", "kube-system") if p["metadata"]["name"].startswith("node-agent-")]
    assert {p["spec"]["nodeName"] for p in pods} == {"mini-node-1", "worker-b"}


def scenario_05(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "05-jobs.yaml")
    instance.wait_until(lambda: (instance.get("jobs", "batch-etl") or {}).get("status", {}).get("succeeded") == 3,
                        timeout=15, description="job completion")
    assert instance.get("cronjobs", "nightly-report")["spec"]["schedule"] == "*/5 * * * *"


def scenario_06(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "06-config-secret.yaml")
    instance.wait_for_pod("configured-app")
    assert instance.get("configmaps", "app-config")["data"]["APP_MODE"] == "production"
    assert instance.get("secrets", "app-secret")["stringData"]["token"] == "s3cr3t-demo-token"
    assert instance.get("pods", "configured-app")["status"]["phase"] == "Running"
    # valueFrom is resolved by the simulated kubelet, so `exec env` proves the
    # ConfigMap/Secret references actually reached the container contract.
    env = instance.exec("default", "configured-app", None, ["env"])
    assert env["exitCode"] == 0
    assert "APP_MODE=production" in env["stdout"]
    assert "TOKEN=s3cr3t-demo-token" in env["stdout"]


def scenario_07(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "07-storage.yaml")
    instance.wait_for_pod("storage-consumer")
    claim = instance.get("persistentvolumeclaims", "workspace")
    assert claim["status"]["phase"] == "Bound"
    volume = instance.get("persistentvolumes", claim["spec"]["volumeName"])
    assert volume["spec"]["capacity"]["storage"] == "2Gi"


def scenario_08(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "08-ingress.yaml")
    wait_ready(instance, "site")
    instance.wait_until(lambda: bool((instance.get("ingresses", "site") or {}).get("status", {}).get("loadBalancer", {}).get("ingress")),
                        timeout=5, description="ingress address")
    assert instance.ingress.route("demo.local", "/api/items")["service"] == "site"
    assert instance.ingress.route("demo.local", "/")["service"] == "site"


def scenario_09(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "09-networkpolicy.yaml")
    wait_ready(instance, "api")
    instance.wait_for_pod("trusted-client")
    instance.wait_for_pod("untrusted-client")
    instance.wait_until(lambda: len(instance.proxy.endpoints("api", "default", 80)) == 2,
                        timeout=8, description="api endpoints")
    trusted = instance.get("pods", "trusted-client")
    untrusted = instance.get("pods", "untrusted-client")
    assert instance.proxy.connect(trusted, "api", "default", 80)["connected"]
    assert not instance.proxy.connect(untrusted, "api", "default", 80)["connected"]


def scenario_10(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "10-rbac.yaml")
    time.sleep(0.2)
    token = instance.api.request(
        "POST", "/api/v1/namespaces/team-dev/serviceaccounts/deployer/token",
        {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
         "spec": {"audiences": ["mini"], "expirationSeconds": 600}})["status"]["token"]
    from mini_k8s.client import ApiClient
    client = ApiClient(instance.url, token=token)
    allowed, _ = client.can_i("list", "pods", namespace="team-dev")
    denied, _ = client.can_i("delete", "pods", namespace="team-dev")
    assert allowed and not denied


def scenario_11(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "11-quota-limitrange.yaml")
    instance.wait_for_pod("demo", "constrained")
    pod = instance.get("pods", "demo", "constrained")
    requests = pod["spec"]["containers"][0]["resources"]["requests"]
    limits = pod["spec"]["containers"][0]["resources"]["limits"]
    assert requests["cpu"] == "100m" and limits["cpu"] == "500m"
    instance.wait_until(lambda: (instance.get("resourcequotas", "compute", "constrained") or {}).get("status", {}).get("used", {}).get("pods") == "1",
                        timeout=5, description="quota usage")
    for index in range(2, 5):
        body = {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": f"p{index}", "namespace": "constrained"},
                "spec": {"containers": [{"name": "c", "image": "x"}]}}
        try:
            instance.api.apply(body)
        except ApiError as exc:
            assert "exceeded quota" in exc.message
            break
    else:
        raise AssertionError("quota never rejected a fourth pod")


def scenario_12(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "12-hpa.yaml")
    instance.wait_until(lambda: (instance.get("deployments", "cpu-app") or {}).get("spec", {}).get("replicas") == 4,
                        timeout=10, description="HPA scale")
    hpa = instance.get("horizontalpodautoscalers", "cpu-app")
    assert hpa["status"]["currentMetrics"][0]["resource"]["current"]["averageUtilization"] == 100


def scenario_13(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "13-pdb.yaml")
    wait_ready(instance, "quorum")
    instance.wait_until(lambda: (instance.get("poddisruptionbudgets", "quorum") or {}).get("status", {}).get("disruptionsAllowed") == 0,
                        timeout=5, description="pdb")
    pod = [p for p in instance.list("pods") if p["metadata"]["name"].startswith("quorum-")][0]
    try:
        instance.api.evict(pod["metadata"]["name"])
    except ApiError as exc:
        assert exc.status == 429
    else:
        raise AssertionError("eviction should have been blocked by the PDB")


def scenario_14(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "14-crd-operator.yaml")
    time.sleep(0.3)
    spec = importlib.util.spec_from_file_location("website_operator", EXAMPLES / "website_operator.py")
    operator = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(operator)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                operator.reconcile(instance.api)
            except Exception:
                pass
            time.sleep(0.2)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        instance.wait_until(lambda: (instance.get("deployments", "blog") or {}).get("status", {}).get("readyReplicas") == 2,
                            timeout=15, description="operator deployment")
        instance.wait_until(lambda: (instance.get("websites", "blog") or {}).get("status", {}).get("readyReplicas") == 2,
                            timeout=10, description="operator status")
        assert instance.get("services", "blog") is not None
    finally:
        stop.set()
        thread.join(timeout=3)


def scenario_15(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "15-probes.yaml")
    wait_ready(instance, "probed")
    instance.wait_until(lambda: len((instance.get("endpoints", "probed") or {}).get("subsets", [{}])[0].get("addresses", [])) == 2,
                        timeout=8, description="probed endpoints")
    pods = [p for p in instance.list("pods") if p["metadata"]["name"].startswith("probed-")]
    assert len(pods) == 2
    assert all(p["status"]["containerStatuses"][0]["restartCount"] == 0 for p in pods)
    assert all(any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"]["conditions"]) for p in pods)


def scenario_16(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "16-gpu-extended-resource.yaml")
    instance.wait_for_pod("gpu-trainer-a")
    instance.wait_for_pod("gpu-trainer-b")
    time.sleep(0.5)
    nodes = {instance.get("pods", name)["spec"]["nodeName"] for name in ("gpu-trainer-a", "gpu-trainer-b")}
    assert nodes == {"mini-node-1", "mini-node-2"}, nodes
    pending = instance.get("pods", "gpu-trainer-c")
    assert pending["status"]["phase"] == "Pending"
    assert pending["spec"].get("nodeName") is None


def scenario_17(instance: MiniCluster) -> None:
    instance.add_node("worker-b", index=1)
    instance.wait_until(
        lambda: any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in instance.get("nodes", "worker-b").get("status", {}).get("conditions", [])),
        timeout=5, description="second node ready")
    instance.api.apply_file(EXAMPLES / "17-topology-spread.yaml")
    instance.wait_for_deployment("spread")
    pods = [p for p in instance.list("pods") if p["metadata"]["name"].startswith("spread-")]
    assert len(pods) == 4, pods
    counts: dict[str, int] = {}
    for pod in pods:
        node = pod["spec"].get("nodeName")
        assert node, pod
        counts[node] = counts.get(node, 0) + 1
    assert set(counts) == {"mini-node-1", "worker-b"}, counts
    assert max(counts.values()) - min(counts.values()) <= 1, counts


def scenario_18(instance: MiniCluster) -> None:
    instance.api.apply_file(EXAMPLES / "18-gateway-api.yaml")
    wait_ready(instance, "shop")

    def route_resolved() -> bool:
        route = instance.get("httproutes", "shop")
        for parent in (route or {}).get("status", {}).get("parents", []):
            conditions = {item.get("type"): item.get("status")
                          for item in parent.get("conditions", [])}
            if conditions.get("Accepted") == "True" and conditions.get("ResolvedRefs") == "True":
                return True
        return False

    instance.wait_until(route_resolved, timeout=10, description="HTTPRoute accepted")
    gateway = instance.get("gateways", "edge")
    assert gateway["status"]["addresses"] == [{"type": "IPAddress", "value": "192.0.2.101"}]
    listener = gateway["status"]["listeners"][0]
    assert listener["attachedRoutes"] == 1
    assert listener["supportedKinds"] == [
        {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute"}]

    selected = instance.gateway.route("shop.example.com", "/api/items")
    assert selected["service"] == "shop", selected
    assert selected["namespace"] == "default"
    assert selected["port"] == 80
    assert instance.gateway.route("other.example.com", "/api/items") is None

    instance.wait_until(
        lambda: len((instance.get("endpoints", "shop") or {}).get("subsets", [{}])[0].get("addresses", [])) == 2,
        timeout=8, description="shop endpoints")
    instance.api.apply({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "gateway-client", "namespace": "default"},
        "spec": {"containers": [{"name": "c", "image": "curl"}]},
    })
    instance.wait_for_pod("gateway-client")
    result = instance.proxy.connect(
        instance.get("pods", "gateway-client"),
        selected["service"], selected["namespace"], selected["port"], path="/api/items")
    assert result["connected"], result


SCENARIOS = [
    ("01", "hello deployment + service + DNS", scenario_01, {}),
    ("02", "rolling update", scenario_02, {}),
    ("03", "statefulset + headless dns + PVC", scenario_03, {}),
    ("04", "daemonset on two nodes", scenario_04, {}),
    ("05", "job + cronjob", scenario_05, {}),
    ("06", "configmap + secret", scenario_06, {}),
    ("07", "dynamic storage", scenario_07, {}),
    ("08", "ingress routing", scenario_08, {}),
    ("09", "network policy", scenario_09, {}),
    ("10", "rbac service account", scenario_10, {}),
    ("11", "quota + limitrange", scenario_11, {}),
    ("12", "horizontal pod autoscaler", scenario_12, {}),
    ("13", "pod disruption budget", scenario_13, {}),
    ("14", "CRD + custom operator", scenario_14, {}),
    ("15", "probes and readiness", scenario_15, {}),
    ("16", "gpu extended resource", scenario_16, {"node_count": 2, "node_gpus": 1}),
    ("17", "topology spread across nodes", scenario_17, {}),
    ("18", "gateway api routing", scenario_18, {}),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="comma-separated scenario numbers, e.g. 01,12")
    args = parser.parse_args()
    wanted = set(args.only.split(",")) if args.only else None
    passed = 0
    failed = 0
    for number, description, function, options in SCENARIOS:
        if wanted and number not in wanted:
            continue
        started = time.time()
        instance = cluster_for(options)
        try:
            function(instance)
            elapsed = time.time() - started
            print(f"PASS  {number}  {description}  ({elapsed:.2f}s)")
            passed += 1
        except Exception:
            failed += 1
            print(f"FAIL  {number}  {description}")
            traceback.print_exc()
        finally:
            instance.stop()
    print(f"\n{passed} passed, {failed} failed, {passed + failed} scenarios")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
