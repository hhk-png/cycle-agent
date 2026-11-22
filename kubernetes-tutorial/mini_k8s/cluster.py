"""Compose the control plane and node agents into a runnable MiniK8s cluster."""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from mini_k8s.admission.admission import AdmissionChain
from mini_k8s.api.registry import ResourceRegistry
from mini_k8s.api.server import APIServer
from mini_k8s.auth.authn import AuthenticatorChain, ServiceAccountTokenIssuer, StaticTokenAuthenticator, User
from mini_k8s.auth.authz import RBACAuthorizer, bootstrap_rbac
from mini_k8s.controllers.autoscaling import HPAController, MetricsController
from mini_k8s.controllers.lifecycle import (
    CRDController, GarbageCollector, NamespaceController, NodeLifecycleController,
    ServiceAccountController,
)
from mini_k8s.controllers.gateway import GatewayController
from mini_k8s.controllers.networking import EndpointsController, IngressController, ServiceController
from mini_k8s.controllers.policy import PDBController, ResourceQuotaController
from mini_k8s.controllers.storage import PVController
from mini_k8s.controllers.workloads import (
    CronJobController, DaemonSetController, DeploymentController, JobController,
    ReplicaSetController, StatefulSetController,
)
from mini_k8s.kubelet.kubelet import Kubelet
from mini_k8s.network.dns import ClusterDNS
from mini_k8s.network.gateway import GatewayRouter
from mini_k8s.network.ingress import IngressRouter
from mini_k8s.network.policy import NetworkSimulator
from mini_k8s.network.proxy import ServiceProxy
from mini_k8s.scheduler.scheduler import Scheduler
from mini_k8s.store.store import ResourceStore

ADMIN_TOKEN = "mini-admin-token"
DEV_TOKEN = "mini-dev-token"


class MiniCluster:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, node_count: int = 1,
                 start_api: bool = True, verbose: bool = False, node_cpu: str = "4",
                 node_memory: str = "8Gi", node_pods: int = 110, node_gpus: int = 0,
                 tick_interval: float = 0.1, auto_start: bool = False,
                 storage_provision_root: str = "/tmp/mini-k8s-volumes"):
        self.store = ResourceStore()
        self.registry = ResourceRegistry()
        self.admission = AdmissionChain(self.store, self.registry)
        self.tokens = {
            ADMIN_TOKEN: User("system:mini-admin", ["system:masters", "system:authenticated"]),
            DEV_TOKEN: User("developer", ["developers", "system:authenticated"]),
        }
        self.token_issuer = ServiceAccountTokenIssuer(b"mini-k8s-development-signing-key")
        self.authenticator = AuthenticatorChain(StaticTokenAuthenticator(self.tokens), self.token_issuer)
        self.authorizer = RBACAuthorizer(self.store)
        self.api_server = APIServer(
            self.store, self.registry, self.admission,
            authenticator=self.authenticator, authorizer=self.authorizer,
            token_issuer=self.token_issuer, host=host, port=port, verbose=verbose,
            log_provider=self.logs, exec_provider=self.exec,
        )
        self.host = host
        self.port = port
        self.start_api_enabled = start_api
        self.verbose = verbose
        self.node_cpu = node_cpu
        self.node_memory = node_memory
        self.node_pods = node_pods
        self.node_gpus = node_gpus
        self.tick_interval = tick_interval
        self.storage_provision_root = storage_provision_root
        self.kubelets: dict[str, Kubelet] = {}
        self.node_index = 0
        self.started = False
        self.api: Any = None
        self._bootstrap()
        self.scheduler = Scheduler(self.store)
        self.controllers = [
            NamespaceController(self.store, self.registry, admission=self.admission),
            ServiceAccountController(self.store, admission=self.admission),
            ServiceController(self.store),
            EndpointsController(self.store),
            DeploymentController(self.store, admission=self.admission),
            ReplicaSetController(self.store, admission=self.admission),
            StatefulSetController(self.store, admission=self.admission),
            DaemonSetController(self.store, admission=self.admission),
            JobController(self.store, admission=self.admission),
            CronJobController(self.store, admission=self.admission),
            PVController(self.store, provision_root=storage_provision_root),
            HPAController(self.store),
            PDBController(self.store),
            ResourceQuotaController(self.store),
            IngressController(self.store),
            GatewayController(self.store),
            NodeLifecycleController(self.store),
            GarbageCollector(self.store, self.registry),
            CRDController(self.store, self.registry),
            MetricsController(self.store, interval=1.0),
        ]
        self.dns = ClusterDNS(self.store)
        self.network = NetworkSimulator(self.store)
        self.ingress = IngressRouter(self.store)
        self.gateway = GatewayRouter(self.store)
        self.proxy = ServiceProxy(self.store)
        for index in range(node_count):
            self.add_node(f"mini-node-{index + 1}", index=index)
        if auto_start:
            self.start()

    # --------------------------------------------------------------- bootstrap
    def _bootstrap(self) -> None:
        for namespace in ("default", "kube-system", "kube-public"):
            self.store.create(("", "v1", "namespaces"), {
                "apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": namespace, "labels": {"kubernetes.io/metadata.name": namespace}},
            }, kind="Namespace", namespace="")
        bootstrap_rbac(self.store, self.registry)
        self.store.create(("storage.k8s.io", "v1", "storageclasses"), {
            "apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": "standard", "annotations": {
                "storageclass.kubernetes.io/is-default-class": "true"}},
            "provisioner": "mini-k8s.io/hostpath",
            "reclaimPolicy": "Delete",
            "volumeBindingMode": "Immediate",
            "allowVolumeExpansion": True,
        }, kind="StorageClass", namespace="")

    def add_node(self, name: str, *, index: int | None = None, labels: dict[str, str] | None = None,
                 taints: list[dict] | None = None, gpus: int | None = None) -> dict:
        index = self.node_index if index is None else index
        self.node_index = max(self.node_index, index + 1)
        if self.store.get(("", "v1", "nodes"), "", name):
            return self.store.get(("", "v1", "nodes"), "", name)
        node_labels = {
            "kubernetes.io/hostname": name,
            "kubernetes.io/os": "linux",
            "kubernetes.io/arch": "amd64",
            "node.kubernetes.io/instance-type": "mini.small",
            "topology.kubernetes.io/zone": "mini-a",
            "topology.kubernetes.io/region": "mini",
        }
        node_labels.update(labels or {})
        body = {
            "apiVersion": "v1", "kind": "Node",
            "metadata": {"name": name, "labels": node_labels},
            "spec": {"taints": taints or [], "podCIDR": f"10.244.{index + 1}.0/24",
                     "podCIDRs": [f"10.244.{index + 1}.0/24"]},
            "status": {"phase": "Running"},
        }
        node = self.store.create(("", "v1", "nodes"), body, kind="Node", namespace="")
        gpu_count = self.node_gpus if gpus is None else gpus
        kubelet = Kubelet(self.store, name, pod_cidr=f"10.244.{index + 1}.0/24",
                          cpu=self.node_cpu, memory=self.node_memory, pods=self.node_pods,
                          tick_interval=self.tick_interval,
                          extended_resources={"nvidia.com/gpu": str(gpu_count)} if gpu_count else None)
        self.kubelets[name] = kubelet
        if self.started:
            kubelet.start()
        return node

    # --------------------------------------------------------------- lifecycle
    def start(self) -> "MiniCluster":
        if self.started:
            return self
        if self.start_api_enabled:
            self.api_server.start()
            self.port = self.api_server.port
            from mini_k8s.client import ApiClient
            self.api = ApiClient(self.api_server.url, token=ADMIN_TOKEN)
        for kubelet in self.kubelets.values():
            kubelet.start()
        self.scheduler.start()
        for controller in self.controllers:
            controller.start()
        self.started = True
        return self

    def stop(self) -> None:
        for controller in getattr(self, "controllers", []):
            controller.stop()
        if hasattr(self, "scheduler"):
            self.scheduler.stop()
        for kubelet in self.kubelets.values():
            kubelet.stop()
        self.api_server.stop()
        self.started = False

    def __enter__(self) -> "MiniCluster":
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()

    @property
    def url(self) -> str:
        return self.api_server.url

    # --------------------------------------------------------------- manifests
    def load_yaml(self, source: str | Path) -> list[dict]:
        if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source and Path(source).exists()):
            text = Path(source).read_text(encoding="utf-8")
        else:
            text = source
        documents = []
        for document in yaml.safe_load_all(text):
            if document is None:
                continue
            if document.get("kind", "").endswith("List") and "items" in document:
                documents.extend(document["items"])
            else:
                documents.append(document)
        return documents

    def apply(self, source: Any) -> list[dict]:
        if isinstance(source, (str, Path)):
            documents = self.load_yaml(source)
        elif isinstance(source, dict):
            if source.get("kind", "").endswith("List") and "items" in source:
                documents = source["items"]
            else:
                documents = [source]
        else:
            documents = list(source)
        return [self.apply_one(document) for document in documents]

    def apply_one(self, obj: dict) -> dict:
        obj = copy.deepcopy(obj)
        api_version = obj.get("apiVersion")
        kind = obj.get("kind")
        if not api_version or not kind:
            raise ValueError("every object needs apiVersion and kind")
        self.api_server.sync_crds()
        definition = self.registry.by_kind(api_version, kind)
        namespace = obj.get("metadata", {}).get("namespace", "default") if definition.namespaced else ""
        meta = obj.setdefault("metadata", {})
        meta.setdefault("namespace", namespace)
        existing = self.store.get(definition.gvr, namespace, meta.get("name", ""))
        from mini_k8s.admission.admission import AdmissionRequest
        request = AdmissionRequest(
            "UPDATE" if existing else "CREATE", definition.gvr, kind, obj, old_obj=existing,
            namespace=namespace if definition.namespaced else None, name=meta.get("name"),
            username="system:mini-admin", groups=["system:masters"],
        )
        admitted = self.admission.admit(request)
        if existing:
            admitted["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
            if definition.has_status:
                admitted["status"] = existing.get("status", {})
            return self.store.update(definition.gvr, admitted, kind=kind,
                                     namespace=namespace if definition.namespaced else "")
        return self.store.create(definition.gvr, admitted, kind=kind,
                                 namespace=namespace if definition.namespaced else "")

    def delete(self, source: str | dict, namespace: str | None = None) -> None:
        if isinstance(source, dict):
            objects = [source]
        elif "\n" in source or source.endswith((".yaml", ".yml")):
            objects = self.load_yaml(source)
        else:
            # Delete by kind/name, e.g. "deployment/nginx" or "pod/nginx-a".
            kind_token, _, name = source.partition("/")
            definition = self.registry.resolve_cli_name(kind_token)
            objects = [{"apiVersion": definition.api_version, "kind": definition.kind,
                        "metadata": {"name": name, "namespace": namespace or "default"}}]
        for obj in objects:
            definition = self.registry.by_kind(obj["apiVersion"], obj["kind"])
            ns = (obj.get("metadata", {}).get("namespace") or namespace or "default") if definition.namespaced else ""
            self.store.delete(definition.gvr, ns, obj["metadata"]["name"], kind=definition.kind)

    # ------------------------------------------------------------------- access
    def get(self, kind: str, name: str, namespace: str = "default") -> dict | None:
        definition = self.registry.resolve_cli_name(kind)
        return self.store.get(definition.gvr, namespace if definition.namespaced else "", name)

    def list(self, kind: str, namespace: str | None = None) -> list[dict]:
        definition = self.registry.resolve_cli_name(kind)
        return self.store.list(definition.gvr, namespace if definition.namespaced else None)[0]

    def create(self, obj: dict) -> dict:
        return self.apply_one(obj)

    def wait_until(self, predicate: Callable[[], bool] | Callable[["MiniCluster"], bool],
                   timeout: float = 10.0, *, description: str = "condition", interval: float = 0.05) -> None:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                value = predicate(self) if getattr(predicate, "__code__", None) and predicate.__code__.co_argcount else predicate()
                if value:
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(interval)
        suffix = f" (last error: {last_error})" if last_error else ""
        raise TimeoutError(f"timed out waiting for {description}{suffix}")

    def wait_for_pod(self, name: str, namespace: str = "default", *, phase: str = "Running",
                     ready: bool = True, timeout: float = 10.0) -> dict:
        def check() -> bool:
            pod = self.get("pods", name, namespace)
            return pod is not None and pod.get("status", {}).get("phase") == phase and (
                not ready or any(c.get("type") == "Ready" and c.get("status") == "True"
                                 for c in pod.get("status", {}).get("conditions", [])))
        try:
            self.wait_until(check, timeout, description=f"pod {namespace}/{name} {phase}")
        except TimeoutError:
            pod = self.get("pods", name, namespace)
            raise TimeoutError(f"pod {namespace}/{name} not {phase}/ready: {pod.get('status') if pod else 'missing'}")
        return self.get("pods", name, namespace) or {}

    def wait_for_deployment(self, name: str, namespace: str = "default", timeout: float = 15.0) -> dict:
        def check() -> bool:
            deployment = self.get("deployments", name, namespace)
            if not deployment:
                return False
            desired = deployment.get("spec", {}).get("replicas", 1)
            status = deployment.get("status", {})
            return (status.get("updatedReplicas", 0) == desired
                    and status.get("availableReplicas", 0) == desired
                    and status.get("replicas", 0) == desired)
        self.wait_until(check, timeout, description=f"deployment {namespace}/{name} available")
        return self.get("deployments", name, namespace) or {}

    # ------------------------------------------------------------------ runtime
    def logs(self, namespace: str, pod_name: str, container: str | None = None, tail: int = 200,
             previous: bool = False) -> str:
        for kubelet in self.kubelets.values():
            text = kubelet.logs(namespace, pod_name, container, tail, previous)
            if text:
                return text
        return ""

    def exec(self, namespace: str, pod_name: str, container: str | None, command: list[str]) -> dict:
        for kubelet in self.kubelets.values():
            if any(r.namespace == namespace and r.name == pod_name for r in kubelet.runtimes.values()):
                return kubelet.exec(namespace, pod_name, container, command)
        return {"stdout": "", "stderr": f"pod {namespace}/{pod_name} not found on any node\n", "exitCode": 1}

    def diagnostics(self) -> dict[str, Any]:
        return {
            "nodes": [n["metadata"]["name"] for n in self.store.list(("", "v1", "nodes"))[0]],
            "pods": [(p["metadata"]["namespace"], p["metadata"]["name"], p.get("status", {}).get("phase"))
                     for p in self.store.list(("", "v1", "pods"))[0]],
            "deployments": [(d["metadata"]["namespace"], d["metadata"]["name"], d.get("status", {}))
                            for d in self.store.list(("apps", "v1", "deployments"))[0]],
        }
