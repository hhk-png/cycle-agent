"""Ingress host/path routing, evaluated in process."""

from __future__ import annotations

INGRESS_GVR = ("networking.k8s.io", "v1", "ingresses")
SERVICE_GVR = ("", "v1", "services")


class IngressRouter:
    def __init__(self, store):
        self.store = store

    def route(self, host: str, path: str = "/", namespace: str | None = None) -> dict | None:
        candidates: list[tuple[int, dict, dict]] = []
        for ingress in self.store.list(INGRESS_GVR, namespace)[0]:
            if ingress["metadata"].get("deletionTimestamp"):
                continue
            spec = ingress.get("spec", {})
            for rule in spec.get("rules", []):
                rule_host = rule.get("host", "")
                if rule_host and not _host_matches(rule_host, host):
                    continue
                for route_path in rule.get("http", {}).get("paths", []):
                    path_value = route_path.get("path", "/")
                    if not _path_matches(path_value, path, route_path.get("pathType", "Prefix")):
                        continue
                    candidates.append((len(path_value), route_path, ingress))
        if not candidates:
            for ingress in self.store.list(INGRESS_GVR, namespace)[0]:
                backend = ingress.get("spec", {}).get("defaultBackend")
                if backend:
                    candidates.append((0, {"backend": backend}, ingress))
        if not candidates:
            return None
        _, route_path, ingress = max(candidates, key=lambda item: item[0])
        backend = route_path.get("backend", {})
        service_ref = backend.get("service", {})
        service_name = service_ref.get("name") or backend.get("serviceName")
        if not service_name:
            return None
        service_ns = ingress["metadata"].get("namespace", "default")
        service = self.store.get(SERVICE_GVR, service_ns, service_name)
        if service is None:
            return None
        port = service_ref.get("port", {})
        port_number = port.get("number") or port.get("name")
        return {
            "ingress": ingress["metadata"]["name"],
            "namespace": service_ns,
            "host": host,
            "path": path,
            "service": service_name,
            "port": port_number,
            "clusterIP": service.get("spec", {}).get("clusterIP"),
        }


def _host_matches(rule_host: str, host: str) -> bool:
    if rule_host == host:
        return True
    if rule_host.startswith("*."):
        suffix = rule_host[1:]
        return host.endswith(suffix) and host.count(".") == rule_host.count(".")
    return False


def _path_matches(rule_path: str, path: str, path_type: str) -> bool:
    if path_type == "Exact":
        return path == rule_path
    if rule_path == "/":
        return True
    return path == rule_path or path.startswith(rule_path.rstrip("/") + "/")
