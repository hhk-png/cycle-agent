"""An in-process Gateway API data plane.

The router walks accepted HTTPRoutes and applies the Gateway API precedence
rules that matter in practice:

1. an older Route wins only after match specificity ties;
2. longer hostnames, exact hostnames, longer paths, exact paths, more header
   matches and more query matches are more specific;
3. backendRefs are weighted and selected round-robin.

It is deliberately evaluated in process: MiniK8s has no real L7 proxy.  The
returned backend can still be fed to :class:`ServiceProxy`, so the full path
"HTTPRoute -> Service -> Endpoints -> Pod" is exercised end to end.

The data plane re-checks the listener attachment, allowedRoutes, hostname
intersection and ReferenceGrant rules even though the controller already wrote
status.  That is deliberate defence in depth: a stale status (controller down,
or a spec update not yet reconciled) must not make an unauthorized backend
reachable.
"""

from __future__ import annotations

import itertools
import re

from mini_k8s.apimachinery.gateway import (
    hostname_score, hostnames_intersect, listener_covers_host, listener_supported,
    listener_valid, route_allowed,
)
from mini_k8s.controllers.gateway import (
    GATEWAY_GVR, HTTPROUTE_GVR, NAMESPACE_GVR, SERVICE_GVR, reference_granted,
)

PATH_RANK = {"Exact": 3, "RegularExpression": 2, "PathPrefix": 1, "ImplementationSpecific": 1}


class GatewayRouter:
    def __init__(self, store):
        self.store = store
        self._counter = itertools.count()

    def route(self, host: str, path: str = "/", *, method: str | None = None,
              headers: dict[str, str] | None = None, query: dict[str, str] | None = None,
              namespace: str | None = None) -> dict | None:
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        query = query or {}
        candidates: list[tuple[tuple, dict, dict, list[dict], dict, dict]] = []
        for route in self.store.list(HTTPROUTE_GVR, namespace)[0]:
            if route["metadata"].get("deletionTimestamp") or not _route_accepted(route):
                continue
            route_namespace = route["metadata"].get("namespace", "default")
            listeners = self._accepted_listeners(route)
            applicable = [(gateway, listener) for gateway, listener in listeners
                          if listener_covers_host(listener, host)]
            if not applicable:
                continue
            # A Route can attach to several listeners/Gateways.  The request
            # host decides which one serves it; more specific hostnames win.
            gateway, listener = max(
                applicable,
                key=lambda item: (
                    hostname_score(item[1].get("hostname", ""), host) or (0, 0),
                    item[1].get("name", ""),
                ),
            )
            host_names = route.get("spec", {}).get("hostnames") or ["*"]
            if not isinstance(host_names, list):
                host_names = ["*"]
            for host_name in host_names:
                host_score = hostname_score(host_name, host)
                if host_score is None:
                    continue
                rules = route.get("spec", {}).get("rules") or []
                if not isinstance(rules, list):
                    rules = []
                for rule_index, rule in enumerate(rules):
                    if not isinstance(rule, dict):
                        continue
                    matches = rule.get("matches") or [{}]
                    if not isinstance(matches, list):
                        matches = [{}]
                    for match in matches:
                        if not isinstance(match, dict):
                            continue
                        if match.get("method"):
                            if method is None or match["method"] != method:
                                continue
                        header_matches = match.get("headers") or []
                        if not isinstance(header_matches, list):
                            header_matches = []
                        if not all(isinstance(item, dict) and _header_matches(item, headers)
                                   for item in header_matches):
                            continue
                        query_matches = match.get("queryParams") or []
                        if not isinstance(query_matches, list):
                            query_matches = []
                        if not all(isinstance(item, dict) and _query_matches(item, query)
                                   for item in query_matches):
                            continue
                        path_spec = match.get("path")
                        if not isinstance(path_spec, dict):
                            path_spec = {}
                        path_value = path_spec.get("value", "/")
                        path_type = path_spec.get("type", "PathPrefix")
                        if not _path_matches(path_value, path, path_type):
                            continue
                        backends = self._backends(route, route_namespace, rule,
                                                  gateway, listener)
                        # Keep the candidate even when it has no usable
                        # backends: a matching rule is terminal, and must not
                        # fall through to a broader Route.
                        key = (
                            -host_score[0], -host_score[1],
                            -PATH_RANK.get(path_type, 1), -len(path_value),
                            -len(header_matches), -len(query_matches),
                            route["metadata"].get("creationTimestamp", ""),
                            route_namespace, route["metadata"]["name"], -rule_index,
                        )
                        candidates.append((key, route, rule, backends, gateway, listener))
        if not candidates:
            return None
        _, route, rule, backends, gateway, listener = min(candidates, key=lambda item: item[0])
        if not backends:
            return {
                "error": "NoBackends",
                "gateway": gateway["metadata"]["name"],
                "gatewayNamespace": gateway["metadata"].get("namespace", "default"),
                "listener": listener.get("name", ""),
                "route": route["metadata"]["name"],
                "routeNamespace": route["metadata"].get("namespace", "default"),
                "host": host,
                "path": path,
                "service": None,
                "namespace": None,
                "port": None,
                "targetPort": None,
                "weight": 0,
                "backends": [],
                "ingress": route["metadata"]["name"],
                "clusterIP": None,
            }
        backend = _pick_backend(backends, next(self._counter))
        return {
            "gateway": backend.pop("gateway"),
            "gatewayNamespace": backend.pop("gatewayNamespace"),
            "listener": backend.pop("listener"),
            "route": route["metadata"]["name"],
            "routeNamespace": route["metadata"].get("namespace", "default"),
            "host": host,
            "path": path,
            "service": backend["service"],
            "namespace": backend["namespace"],
            "port": backend["port"],
            "targetPort": backend["targetPort"],
            "weight": backend["weight"],
            "backends": [
                {key: value for key, value in item.items()
                 if key not in ("gateway", "gatewayNamespace", "listener")}
                for item in backends
            ],
            # Compatibility aliases used by ServiceProxy callers.
            "ingress": route["metadata"]["name"],
            "clusterIP": None,
        }

    def _backends(self, route: dict, route_namespace: str, rule: dict,
                  gateway: dict, listener: dict) -> list[dict]:
        result = []
        backend_refs = rule.get("backendRefs") or []
        if not isinstance(backend_refs, list):
            return result
        for backend_ref in backend_refs:
            if not isinstance(backend_ref, dict):
                continue
            if backend_ref.get("group", "") not in ("", "core"):
                continue
            if backend_ref.get("kind", "Service") != "Service":
                continue
            service_namespace = backend_ref.get("namespace") or route_namespace
            if service_namespace != route_namespace and not reference_granted(
                    self.store, route, route_namespace, service_namespace,
                    backend_ref.get("name", "")):
                continue
            service = self.store.get(SERVICE_GVR, service_namespace, backend_ref.get("name", ""))
            if service is None:
                continue
            port = _resolve_service_port(service, backend_ref.get("port"))
            if port is None:
                continue
            try:
                weight = int(backend_ref.get("weight", 1))
            except (TypeError, ValueError):
                continue
            if weight <= 0:
                continue
            result.append({
                "gateway": gateway["metadata"]["name"],
                "gatewayNamespace": gateway["metadata"].get("namespace", "default"),
                "listener": listener.get("name", ""),
                "service": service["metadata"]["name"],
                "namespace": service_namespace,
                "port": port,
                "targetPort": port,
                "weight": weight,
            })
        return result

    def _accepted_listeners(self, route: dict) -> list[tuple[dict, dict]]:
        result = []
        route_namespace = route["metadata"].get("namespace", "default")
        namespace_labels = (self.store.get(NAMESPACE_GVR, "", route_namespace) or {}).get(
            "metadata", {}).get("labels", {})
        for parent in route.get("status", {}).get("parents", []):
            conditions = {item.get("type"): item.get("status")
                          for item in parent.get("conditions", [])}
            if conditions.get("Accepted") != "True" or conditions.get("ResolvedRefs") != "True":
                continue
            ref = parent.get("parentRef", {})
            gateway = self.store.get(GATEWAY_GVR, ref.get("namespace", "default"), ref.get("name", ""))
            if gateway is None:
                continue
            for listener in gateway.get("spec", {}).get("listeners") or []:
                if ref.get("sectionName") and listener.get("name") != ref["sectionName"]:
                    continue
                if ref.get("port") and listener.get("port") != ref["port"]:
                    continue
                if not listener_valid(listener) or not listener_supported(listener):
                    continue
                allowed, _, _ = route_allowed(route, listener, gateway, namespace_labels)
                if not allowed:
                    continue
                if not hostnames_intersect(route.get("spec", {}).get("hostnames"),
                                           listener.get("hostname")):
                    continue
                result.append((gateway, listener))
        return result


def _route_accepted(route: dict) -> bool:
    for parent in route.get("status", {}).get("parents", []):
        conditions = {item.get("type"): item.get("status") for item in parent.get("conditions", [])}
        if conditions.get("Accepted") == "True" and conditions.get("ResolvedRefs") == "True":
            return True
    return False


def _header_matches(match: dict, headers: dict[str, str]) -> bool:
    name = (match.get("name") or "").lower()
    value = headers.get(name)
    if value is None:
        return False
    expected = match.get("value", "")
    if match.get("type", "Exact") == "RegularExpression":
        try:
            return re.search(expected, value) is not None
        except re.error:
            return False
    return value == expected


def _query_matches(match: dict, query: dict[str, str]) -> bool:
    name = match.get("name", "")
    value = query.get(name)
    if value is None:
        return False
    expected = match.get("value", "")
    if match.get("type", "Exact") == "RegularExpression":
        try:
            return re.search(expected, value) is not None
        except re.error:
            return False
    return value == expected


def _path_matches(rule_path: str, path: str, path_type: str) -> bool:
    if path_type == "Exact":
        return path == rule_path
    if path_type == "RegularExpression":
        try:
            return re.search(rule_path, path) is not None
        except re.error:
            return False
    if rule_path == "/":
        return True
    return path == rule_path or path.startswith(rule_path.rstrip("/") + "/")


def _resolve_service_port(service: dict, port: int | str | None) -> int | None:
    if port is None:
        return None
    for candidate in service.get("spec", {}).get("ports") or []:
        if isinstance(port, str) and candidate.get("name") == port:
            return int(candidate.get("port"))
        try:
            if int(candidate.get("port", 0)) == int(port):
                return int(candidate.get("port"))
        except (TypeError, ValueError):
            continue
    return None


def _pick_backend(backends: list[dict], counter: int) -> dict:
    total = sum(item["weight"] for item in backends)
    slot = counter % total
    for backend in backends:
        if slot < backend["weight"]:
            return dict(backend)
        slot -= backend["weight"]
    return dict(backends[-1])
