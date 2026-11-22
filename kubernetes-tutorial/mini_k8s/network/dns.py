"""A deterministic, in-process stand-in for cluster DNS."""

from __future__ import annotations

from mini_k8s.apimachinery.selectors import match_labels

SERVICE_GVR = ("", "v1", "services")
ENDPOINT_GVR = ("", "v1", "endpoints")
POD_GVR = ("", "v1", "pods")


class ClusterDNS:
    """Resolve Kubernetes service names without a real CoreDNS process.

    Supported names, searched from the pod's namespace:
      ``api``, ``api.default``, ``api.default.svc``,
      ``api.default.svc.cluster.local``.
    """

    def __init__(self, store, *, cluster_domain: str = "cluster.local", service_cidr: str = "10.96.0.0/16"):
        self.store = store
        self.cluster_domain = cluster_domain
        self.service_cidr = service_cidr

    def resolve(self, name: str, namespace: str = "default") -> list[str]:
        name = name.rstrip(".")
        parts = name.split(".")
        # Service names win when ambiguous (e.g. "web.default" is a Service,
        # while "db-0.db" is a StatefulSet Pod record).  Fall back to Pod
        # records only when Service resolution yields nothing.
        service_result = self._resolve_service(parts, namespace)
        if service_result:
            return service_result
        pod_result = self._resolve_pod_record(parts, namespace)
        return pod_result or []

    def _resolve_service(self, parts: list[str], namespace: str) -> list[str]:
        if len(parts) == 1:
            service_name, service_ns = parts[0], namespace
        elif len(parts) == 2:
            service_name, service_ns = parts
        elif len(parts) in (3, 4, 5) and parts[2] == "svc":
            service_name, service_ns = parts[0], parts[1]
            if len(parts) >= 4 and ".".join(parts[3:]) != self.cluster_domain:
                return []
        else:
            return []
        service = self.store.get(SERVICE_GVR, service_ns, service_name)
        if service is None:
            return []
        spec = service.get("spec", {})
        if spec.get("type") == "ExternalName":
            # A real resolver returns a CNAME.  The simulator has no record
            # type, so it returns the target hostname as the "address"; callers
            # can detect it by the presence of dots instead of an IP.
            target = spec.get("externalName", "")
            return [target] if target else []
        if spec.get("clusterIP") and spec["clusterIP"] != "None":
            return [spec["clusterIP"]]
        # Headless: A records are the ready pod IPs behind the service.
        selector = spec.get("selector")
        if not selector:
            endpoints = self.store.get(ENDPOINT_GVR, service_ns, service_name)
            return [address["ip"] for subset in (endpoints or {}).get("subsets", [])
                    for address in subset.get("addresses", [])]
        expression = ",".join(f"{k}={v}" for k, v in selector.items())
        pods = self.store.list(POD_GVR, service_ns)[0]
        publish_not_ready = bool(spec.get("publishNotReadyAddresses"))
        return [pod["status"]["podIP"] for pod in pods
                if match_labels(pod["metadata"].get("labels"), expression)
                and pod.get("status", {}).get("podIP")
                and (publish_not_ready
                     or any(c.get("type") == "Ready" and c.get("status") == "True"
                            for c in pod.get("status", {}).get("conditions", [])))]

    def _resolve_pod_record(self, parts: list[str], namespace: str) -> list[str] | None:
        """Resolve <pod>.<headless-service>[.<ns>[.svc[.cluster.local]]].

        Returns None when the name does not have a Pod-record shape, so the
        caller can fall back to Service resolution.
        """
        pod_name = service_name = None
        service_ns = namespace
        if len(parts) == 2:
            pod_name, service_name = parts
        elif len(parts) == 3:
            pod_name, service_name, service_ns = parts
        elif len(parts) == 4 and parts[3] == "svc":
            pod_name, service_name, service_ns = parts[:3]
        elif len(parts) == 6 and parts[3] == "svc" and ".".join(parts[4:]) == self.cluster_domain:
            pod_name, service_name, service_ns = parts[:3]
        else:
            return None
        service = self.store.get(SERVICE_GVR, service_ns, service_name)
        if service is None or service.get("spec", {}).get("clusterIP") != "None":
            return []
        selector = service.get("spec", {}).get("selector") or {}
        pod = self.store.get(POD_GVR, service_ns, pod_name)
        if pod is None or not pod.get("status", {}).get("podIP"):
            return []
        if selector and not match_labels(pod.get("metadata", {}).get("labels"), ",".join(f"{k}={v}" for k, v in selector.items())):
            return []
        return [pod["status"]["podIP"]]

    def reverse(self, ip: str) -> list[str]:
        """Return service FQDNs that currently resolve to *ip*."""
        result = []
        for service in self.store.list(SERVICE_GVR)[0]:
            spec = service.get("spec", {})
            if spec.get("clusterIP") == ip:
                name = service["metadata"]["name"]
                ns = service["metadata"]["namespace"]
                result.append(f"{name}.{ns}.svc.{self.cluster_domain}")
        return result

    def search_domains(self, namespace: str) -> list[str]:
        return [
            f"{namespace}.svc.{self.cluster_domain}",
            f"svc.{self.cluster_domain}",
            self.cluster_domain,
        ]
