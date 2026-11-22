"""A simulated service proxy/kube-proxy data plane with policy enforcement."""

from __future__ import annotations

import hashlib
import itertools

from mini_k8s.network.policy import NetworkSimulator

SERVICE_GVR = ("", "v1", "services")
ENDPOINT_GVR = ("", "v1", "endpoints")
POD_GVR = ("", "v1", "pods")


class ServiceProxy:
    def __init__(self, store):
        self.store = store
        self.policies = NetworkSimulator(store)
        self._counter = itertools.count()

    def endpoints(self, service_name: str, namespace: str = "default", port: int | None = None) -> list[dict]:
        service = self.store.get(SERVICE_GVR, namespace, service_name)
        if service is None:
            return []
        service_ports = service.get("spec", {}).get("ports", [])
        selected_service_port = None
        if port is not None:
            selected_service_port = next((p for p in service_ports if int(p.get("port", 0)) == port), None)
            if selected_service_port is None:
                return []
        endpoints = self.store.get(ENDPOINT_GVR, namespace, service_name)
        result = []
        for subset in (endpoints or {}).get("subsets", []):
            ports = subset.get("ports", [])
            target_port = None
            if selected_service_port:
                match = next((p for p in ports if p.get("name") == selected_service_port.get("name")), None)
                target_port = (match or (ports[0] if ports else {})).get("port")
            else:
                target_port = ports[0].get("port") if ports else None
            for address in subset.get("addresses", []):
                pod_ref = address.get("targetRef", {})
                pod = self.store.get(POD_GVR, namespace, pod_ref.get("name", ""))
                result.append({"ip": address.get("ip"), "port": target_port, "node": address.get("nodeName"),
                               "pod": pod, "podName": pod_ref.get("name")})
        return result

    def connect(self, source_pod: dict, service_name: str, namespace: str = "default",
                port: int | None = None, path: str = "/") -> dict:
        endpoints = self.endpoints(service_name, namespace, port)
        if not endpoints:
            return {"connected": False, "reason": "no endpoints", "service": service_name,
                    "namespace": namespace}
        service = self.store.get(SERVICE_GVR, namespace, service_name) or {}
        if service.get("spec", {}).get("sessionAffinity") == "ClientIP":
            # Deterministic stand-in for ClientIP affinity: the source Pod IP
            # (or name) selects the same backend on every connection.
            key = (source_pod.get("status", {}).get("podIP")
                   or source_pod["metadata"]["name"]).encode()
            start = int(hashlib.sha256(key).hexdigest(), 16) % len(endpoints)
            order = endpoints[start:] + endpoints[:start]
        else:
            start = next(self._counter) % len(endpoints)
            order = endpoints[start:] + endpoints[:start]
        # Skip endpoints denied by NetworkPolicy, but preserve the order.
        for endpoint in order:
            target = endpoint.get("pod")
            if target is None:
                continue
            target_port = endpoint.get("port") or port or 0
            if not self.policies.can_connect(source_pod, target, int(target_port), "TCP"):
                continue
            return {
                "connected": True,
                "service": service_name,
                "namespace": namespace,
                "source": source_pod["metadata"]["name"],
                "endpoint": endpoint["podName"],
                "endpointIP": endpoint["ip"],
                "port": target_port,
                "path": path,
                "response": f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nhello from {endpoint['podName']}",
            }
        return {"connected": False, "reason": "traffic denied by NetworkPolicy or no reachable endpoint",
                "service": service_name, "namespace": namespace}
