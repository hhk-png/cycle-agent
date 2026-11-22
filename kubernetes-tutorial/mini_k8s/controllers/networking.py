"""Service IP allocation, Endpoints/EndpointSlice publication, Ingress status."""

from __future__ import annotations

import ipaddress
import uuid

from mini_k8s.apimachinery.meta import deep_copy, owner_reference
from mini_k8s.apimachinery.selectors import match_labels
from mini_k8s.controllers.base import Controller, condition, set_condition

SERVICE_GVR = ("", "v1", "services")
ENDPOINTS_GVR = ("", "v1", "endpoints")
SLICE_GVR = ("discovery.k8s.io", "v1", "endpointslices")
POD_GVR = ("", "v1", "pods")
NODE_GVR = ("", "v1", "nodes")
INGRESS_GVR = ("networking.k8s.io", "v1", "ingresses")


class ServiceController(Controller):
    name = "service-controller"

    def __init__(self, store, *, service_cidr: str = "10.96.0.0/16", node_port_range: tuple[int, int] = (30000, 32767),
                 interval: float = 0.05):
        super().__init__(store, interval=interval)
        self.network = ipaddress.ip_network(service_cidr)
        self.node_port_range = node_port_range

    def reconcile(self) -> None:
        services = self.store.list(SERVICE_GVR)[0]
        used_ips = {s.get("spec", {}).get("clusterIP") for s in services if s.get("spec", {}).get("clusterIP") not in (None, "", "None")}
        used_node_ports = set()
        for service in services:
            for port in service.get("spec", {}).get("ports", []):
                if port.get("nodePort"):
                    used_node_ports.add(int(port["nodePort"]))
        for service in services:
            if service["metadata"].get("deletionTimestamp"):
                continue
            spec = service.get("spec", {})
            changed = False
            updated = deep_copy(service)
            if spec.get("type") == "ExternalName":
                if spec.get("clusterIP"):
                    updated["spec"]["clusterIP"] = ""
                    changed = True
                if changed:
                    try:
                        self.store.update(SERVICE_GVR, updated, kind="Service", namespace=service["metadata"]["namespace"])
                    except Exception:
                        pass
                continue
            if not spec.get("clusterIP"):
                allocated = self._allocate_ip(used_ips)
                if allocated:
                    updated["spec"]["clusterIP"] = allocated
                    used_ips.add(allocated)
                    changed = True
            if spec.get("type") in ("NodePort", "LoadBalancer"):
                for index, port in enumerate(updated["spec"].get("ports", [])):
                    if not port.get("nodePort"):
                        node_port = self._allocate_node_port(used_node_ports)
                        if node_port:
                            port["nodePort"] = node_port
                            used_node_ports.add(node_port)
                            changed = True
            if spec.get("type") == "LoadBalancer":
                status = deep_copy(service.get("status", {}))
                if not status.get("loadBalancer", {}).get("ingress"):
                    status["loadBalancer"] = {"ingress": [{"ip": "192.0.2.100", "hostname": "lb.mini-k8s.local"}]}
                    updated["status"] = status
                    changed = True
            if changed:
                try:
                    self.store.update(SERVICE_GVR, updated, kind="Service", namespace=service["metadata"]["namespace"])
                except Exception:
                    pass

    def _allocate_ip(self, used: set[str]) -> str | None:
        # .0 is the network address and .1 is conventionally the API service.
        start = int(self.network.network_address) + 10
        end = int(self.network.broadcast_address) - 1
        for value in range(start, end):
            candidate = str(ipaddress.ip_address(value))
            if candidate not in used:
                return candidate
        return None

    def _allocate_node_port(self, used: set[int]) -> int | None:
        low, high = self.node_port_range
        for port in range(low, high + 1):
            if port not in used:
                return port
        return None


class EndpointsController(Controller):
    name = "endpoints-controller"

    def reconcile(self) -> None:
        for service in self.store.list(SERVICE_GVR)[0]:
            if service["metadata"].get("deletionTimestamp"):
                continue
            if service.get("spec", {}).get("type") == "ExternalName":
                continue
            selector = service.get("spec", {}).get("selector")
            if not selector:
                continue
            namespace = service["metadata"]["namespace"]
            expression = ",".join(f"{k}={v}" for k, v in selector.items())
            matching = [p for p in self.store.list(POD_GVR, namespace)[0]
                        if match_labels(p["metadata"].get("labels"), expression)
                        and p.get("status", {}).get("podIP")]
            ready_pods = [p for p in matching
                          if _ready(p) and not p["metadata"].get("deletionTimestamp")]
            not_ready_pods = [p for p in matching if p not in ready_pods]
            publish_not_ready = bool(service.get("spec", {}).get("publishNotReadyAddresses"))
            address_pods = matching if publish_not_ready else ready_pods
            subsets = []
            if address_pods or not_ready_pods:
                ports = []
                for port in service.get("spec", {}).get("ports", []):
                    target = _target_port(port, matching)
                    ports.append({"name": port.get("name", ""), "port": target,
                                  "protocol": port.get("protocol", "TCP")})

                def address(pod: dict) -> dict:
                    return {"ip": pod["status"]["podIP"], "nodeName": pod.get("spec", {}).get("nodeName"),
                            "targetRef": {"kind": "Pod", "name": pod["metadata"]["name"],
                                          "namespace": namespace, "uid": pod["metadata"].get("uid", "")}}

                subset = {"addresses": [address(p) for p in address_pods if p.get("status", {}).get("podIP")],
                          "ports": ports}
                if not publish_not_ready:
                    subset["notReadyAddresses"] = [address(p) for p in not_ready_pods
                                                   if p.get("status", {}).get("podIP")]
                subsets.append(subset)
            body = {
                "apiVersion": "v1", "kind": "Endpoints",
                "metadata": {
                    "name": service["metadata"]["name"], "namespace": namespace,
                    "labels": deep_copy(service["metadata"].get("labels", {})),
                    "ownerReferences": [owner_reference(service, controller=False)],
                },
                "subsets": subsets,
            }
            self._upsert(ENDPOINTS_GVR, namespace, service["metadata"]["name"], body, "Endpoints")
            self._reconcile_slice(service, matching)

    def _reconcile_slice(self, service: dict, pods: list[dict]) -> None:
        namespace = service["metadata"]["namespace"]
        ports = [{"name": p.get("name", ""), "port": _target_port(p, pods), "protocol": p.get("protocol", "TCP")}
                 for p in service.get("spec", {}).get("ports", [])]
        from mini_k8s.apimachinery.meta import stable_hash
        slice_name = f"{service['metadata']['name']}-{stable_hash(ports, 6)}"
        endpoints = []
        zones: set[str] = set()
        missing_zone = False
        for pod in pods:
            terminating = bool(pod["metadata"].get("deletionTimestamp"))
            ready = _ready(pod) and not terminating
            node_name = pod.get("spec", {}).get("nodeName")
            zone = ""
            if node_name:
                node = self.store.get(NODE_GVR, "", node_name)
                if node:
                    zone = node.get("metadata", {}).get("labels", {}).get("topology.kubernetes.io/zone", "")
            if zone:
                zones.add(zone)
            else:
                missing_zone = True
            endpoint = {
                "addresses": [pod["status"]["podIP"]],
                "conditions": {"ready": ready, "serving": ready, "terminating": terminating},
                "nodeName": node_name,
                "targetRef": {"kind": "Pod", "name": pod["metadata"]["name"], "namespace": namespace,
                              "uid": pod["metadata"].get("uid", "")},
            }
            if zone:
                endpoint["zone"] = zone
            endpoints.append(endpoint)
        body = {
            "apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice",
            "metadata": {
                "name": slice_name, "namespace": namespace,
                "labels": {"kubernetes.io/service-name": service["metadata"]["name"],
                           "endpointslice.kubernetes.io/managed-by": "mini-k8s"},
                "ownerReferences": [owner_reference(service, controller=False)],
            },
            "addressType": "IPv4",
            "endpoints": endpoints,
            "ports": ports,
        }
        if endpoints and len(zones) == 1 and not missing_zone:
            zone = next(iter(zones))
            body["hints"] = {"forZones": [{"name": zone}]}
            body["metadata"]["labels"]["topology.kubernetes.io/zone"] = zone
        self._upsert(SLICE_GVR, namespace, slice_name, body, "EndpointSlice")
        # Remove stale slices for this service.
        for existing in self.store.list(SLICE_GVR, namespace)[0]:
            if existing["metadata"].get("labels", {}).get("kubernetes.io/service-name") == service["metadata"]["name"]:
                if existing["metadata"]["name"] != slice_name:
                    try:
                        self.store.delete(SLICE_GVR, namespace, existing["metadata"]["name"], kind="EndpointSlice")
                    except Exception:
                        pass

    def _upsert(self, gvr, namespace: str, name: str, body: dict, kind: str) -> None:
        existing = self.store.get(gvr, namespace, name)
        try:
            if existing is None:
                self.store.create(gvr, body, kind=kind, namespace=namespace)
            else:
                merged = deep_copy(body)
                merged["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
                if merged.get("subsets") != existing.get("subsets") or merged.get("endpoints") != existing.get("endpoints"):
                    self.store.update(gvr, merged, kind=kind, namespace=namespace)
        except Exception:
            pass


class IngressController(Controller):
    name = "ingress-controller"

    def __init__(self, store, *, address: str = "192.0.2.100", interval: float = 0.1):
        super().__init__(store, interval=interval)
        self.address = address

    def reconcile(self) -> None:
        for ingress in self.store.list(INGRESS_GVR)[0]:
            if ingress["metadata"].get("deletionTimestamp"):
                continue
            status = deep_copy(ingress.get("status", {}))
            desired = {"loadBalancer": {"ingress": [{"ip": self.address, "hostname": "ingress.mini-k8s.local"}]}}
            if status != desired:
                self.update_status(INGRESS_GVR, ingress, desired)


def _ready(pod: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", []))


def _target_port(service_port: dict, pods: list[dict]) -> int:
    target = service_port.get("targetPort", service_port.get("port", 0))
    if isinstance(target, int):
        return target
    try:
        return int(target)
    except (TypeError, ValueError):
        pass
    for pod in pods:
        for container in pod.get("spec", {}).get("containers", []):
            for port in container.get("ports", []):
                if port.get("name") == target:
                    return int(port.get("containerPort", 0))
    return int(service_port.get("port", 0))
