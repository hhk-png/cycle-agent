"""Gateway API controller: GatewayClass admission, Gateway listeners, HTTPRoute status.

The controller implements the status half of the Gateway API contract:

* a GatewayClass is accepted when it names MiniK8s' controller;
* a Gateway is accepted when its class is handled and at least one listener is
  structurally valid and supported; it is programmed when a usable HTTP/HTTPS
  listener exists;
* an HTTPRoute gets one ``status.parents`` entry per parentRef, with
  ``Accepted`` and ``ResolvedRefs`` conditions computed from listener
  attachment, hostname intersection and backend references.

ReferenceGrant is enforced for cross-namespace Service backends.  The actual
routing table lives in :mod:`mini_k8s.network.gateway`; keeping status and data
plane separate mirrors how a real implementation programs proxies from watched
objects.  Both sides import the pure rules in
:mod:`mini_k8s.apimachinery.gateway` so they cannot drift.
"""

from __future__ import annotations

from mini_k8s.apimachinery.gateway import (
    GATEWAY_GROUP, hostnames_intersect, listener_supported, listener_valid,
    route_allowed,
)
from mini_k8s.apimachinery.meta import deep_copy
from mini_k8s.controllers.base import Controller, condition, set_condition

GATEWAYCLASS_GVR = ("gateway.networking.k8s.io", "v1", "gatewayclasses")
GATEWAY_GVR = ("gateway.networking.k8s.io", "v1", "gateways")
HTTPROUTE_GVR = ("gateway.networking.k8s.io", "v1", "httproutes")
REFERENCEGRANT_GVR = ("gateway.networking.k8s.io", "v1beta1", "referencegrants")
SERVICE_GVR = ("", "v1", "services")
NAMESPACE_GVR = ("", "v1", "namespaces")

CONTROLLER_NAME = "mini-k8s.io/gateway-controller"


class GatewayController(Controller):
    name = "gateway-controller"

    def __init__(self, store, *, address: str = "192.0.2.101", interval: float = 0.1):
        super().__init__(store, interval=interval)
        self.address = address

    def reconcile(self) -> None:
        classes = {item["metadata"]["name"]: item
                   for item in self.store.list(GATEWAYCLASS_GVR)[0]
                   if not item["metadata"].get("deletionTimestamp")}
        gateways = [item for item in self.store.list(GATEWAY_GVR)[0]
                    if not item["metadata"].get("deletionTimestamp")]
        routes = [item for item in self.store.list(HTTPROUTE_GVR)[0]
                  if not item["metadata"].get("deletionTimestamp")]
        self._reconcile_classes(classes)
        self._reconcile_gateways(classes, gateways, routes)
        self._reconcile_routes(classes, gateways, routes)

    # ---------------------------------------------------------------- classes
    def _reconcile_classes(self, classes: dict[str, dict]) -> None:
        for gateway_class in classes.values():
            controller_name = gateway_class.get("spec", {}).get("controllerName", "")
            accepted = controller_name == CONTROLLER_NAME
            if not controller_name:
                reason, message = "InvalidParameters", "spec.controllerName is required"
            elif accepted:
                reason, message = "Accepted", f"Handled by {CONTROLLER_NAME}"
            else:
                reason, message = "Unsupported", (
                    f"{controller_name!r} is not the MiniK8s Gateway API controller "
                    f"({CONTROLLER_NAME})")
            existing = gateway_class.get("status", {}).get("conditions", [])
            conditions = set_condition(existing, condition(
                "Accepted", accepted, reason, message,
                observed_generation=gateway_class["metadata"].get("generation")))
            status = deep_copy(gateway_class.get("status", {}))
            status["conditions"] = conditions
            self.update_status(GATEWAYCLASS_GVR, gateway_class, status)

    # --------------------------------------------------------------- gateways
    def _reconcile_gateways(self, classes: dict[str, dict], gateways: list[dict],
                            routes: list[dict]) -> None:
        for gateway in gateways:
            if not isinstance(gateway, dict):
                continue
            spec = gateway.get("spec")
            if not isinstance(spec, dict):
                spec = {}
            class_name = spec.get("gatewayClassName", "")
            gateway_class = classes.get(class_name)
            class_ok = bool(
                gateway_class
                and gateway_class.get("spec", {}).get("controllerName") == CONTROLLER_NAME)
            previous_status = gateway.get("status", {})
            previous_listeners = {item.get("name"): item
                                  for item in previous_status.get("listeners", [])}
            listeners = spec.get("listeners") or []
            if not isinstance(listeners, list):
                listeners = []
            listener_status = []
            any_usable = False
            for listener in listeners:
                if not isinstance(listener, dict):
                    continue
                listener_name = listener.get("name", "")
                protocol = listener.get("protocol", "")
                structurally_valid = listener_valid(listener)
                supported = structurally_valid and listener_supported(listener)
                if supported:
                    any_usable = True
                supported_kinds = ([{"group": GATEWAY_GROUP, "kind": "HTTPRoute"}]
                                   if supported else [])
                attached = self._attached_routes(gateway, listener, routes, class_ok)
                if not structurally_valid:
                    listener_conditions = [condition(
                        "Accepted", False, "Invalid",
                        "name/port/protocol is invalid",
                        observed_generation=gateway["metadata"].get("generation"))]
                elif not supported:
                    listener_conditions = [
                        condition("Accepted", False, "UnsupportedProtocol",
                                  f"{protocol} listeners are not supported by MiniK8s",
                                  observed_generation=gateway["metadata"].get("generation")),
                        condition("Programmed", False, "UnsupportedProtocol",
                                  f"{protocol} listeners are accepted but not programmed",
                                  observed_generation=gateway["metadata"].get("generation")),
                    ]
                else:
                    listener_conditions = [condition(
                        "Accepted", True, "Accepted", "Listener is valid",
                        observed_generation=gateway["metadata"].get("generation"))]
                previous = previous_listeners.get(listener_name)
                if previous:
                    # Drop condition types the new listener no longer reports;
                    # otherwise a TCP Programmed=False condition survives a
                    # change to HTTP and the object never converges.
                    new_types = {item["type"] for item in listener_conditions}
                    preserved = [item for item in previous.get("conditions", [])
                                 if item.get("type") in new_types]
                    for new_condition in listener_conditions:
                        preserved = set_condition(preserved, new_condition)
                    listener_conditions = preserved
                listener_status.append({
                    "name": listener_name,
                    "supportedKinds": supported_kinds,
                    "attachedRoutes": attached,
                    "conditions": listener_conditions,
                })

            conditions: list[dict] = list(previous_status.get("conditions", []))
            if not gateway_class:
                accept_reason = "InvalidParameters"
                accept_message = f"GatewayClass {class_name!r} not found"
                accepted = False
            elif not class_ok:
                accept_reason = "InvalidParameters"
                accept_message = f"GatewayClass {class_name!r} is not handled by {CONTROLLER_NAME}"
                accepted = False
            elif not listeners:
                accept_reason = "ListenersNotValid"
                accept_message = "Gateway has no listeners"
                accepted = False
            elif not any_usable:
                accept_reason = "ListenersNotValid"
                accept_message = "No listener is structurally valid and supported"
                accepted = False
            else:
                accept_reason = "Accepted"
                accept_message = f"Accepted by {CONTROLLER_NAME}"
                accepted = True
            conditions = set_condition(conditions, condition(
                "Accepted", accepted, accept_reason, accept_message,
                observed_generation=gateway["metadata"].get("generation")))

            programmed = bool(accepted and any_usable)
            if programmed:
                conditions = set_condition(conditions, condition(
                    "Programmed", True, "Programmed",
                    f"Address {self.address} is ready",
                    observed_generation=gateway["metadata"].get("generation")))
            else:
                conditions = set_condition(conditions, condition(
                    "Programmed", False, "NotProgrammed",
                    "Gateway is not accepted or has no supported listener",
                    observed_generation=gateway["metadata"].get("generation")))

            status = deep_copy(gateway.get("status", {}))
            status["conditions"] = conditions
            status["listeners"] = listener_status
            status["addresses"] = ([{"type": "IPAddress", "value": self.address}]
                                   if programmed else [])
            self.update_status(GATEWAY_GVR, gateway, status)

    def _attached_routes(self, gateway: dict, listener: dict, routes: list[dict],
                         class_accepted: bool) -> int:
        if (not class_accepted or not listener_valid(listener)
                or not listener_supported(listener)):
            return 0
        count = 0
        for route in routes:
            for parent_ref in route.get("spec", {}).get("parentRefs", []) or []:
                if not _ref_targets_gateway(parent_ref, route, gateway):
                    continue
                if parent_ref.get("sectionName") and parent_ref["sectionName"] != listener.get("name"):
                    continue
                if parent_ref.get("port") and parent_ref["port"] != listener.get("port"):
                    continue
                allowed, _, _ = route_allowed(
                    route, listener, gateway, self._namespace_labels(
                        route["metadata"].get("namespace", "default")))
                if not allowed:
                    continue
                if not hostnames_intersect(route.get("spec", {}).get("hostnames"),
                                           listener.get("hostname")):
                    continue
                count += 1
                break
        return count

    def _namespace_labels(self, namespace: str) -> dict[str, str]:
        obj = self.store.get(NAMESPACE_GVR, "", namespace) or {}
        return obj.get("metadata", {}).get("labels", {})

    # ----------------------------------------------------------------- routes
    def _reconcile_routes(self, classes: dict[str, dict], gateways: list[dict],
                          routes: list[dict]) -> None:
        gateway_index = {(item["metadata"].get("namespace", "default"), item["metadata"]["name"]): item
                         for item in gateways}
        for route in routes:
            try:
                self._reconcile_route(route, gateway_index, classes)
            except Exception as exc:  # keep one malformed Route from wedging all others
                self.event("Warning", "GatewayRouteError", str(exc)[:500], obj=route)

    def _reconcile_route(self, route: dict, gateway_index: dict[tuple[str, str], dict],
                         classes: dict[str, dict]) -> None:
        existing = route.get("status", {}).get("parents", [])
        parents = []
        for parent_ref in route.get("spec", {}).get("parentRefs", []) or []:
            parent = self._parent_status(route, parent_ref, gateway_index, classes)
            previous = next((item for item in existing
                             if _same_parent_ref(item.get("parentRef", {}),
                                                 parent.get("parentRef", {}))), None)
            if previous:
                conditions = previous.get("conditions", [])
                for new_condition in parent["conditions"]:
                    conditions = set_condition(conditions, new_condition)
                parent["conditions"] = conditions
            parents.append(parent)
        status = deep_copy(route.get("status", {}))
        status["parents"] = parents
        self.update_status(HTTPROUTE_GVR, route, status)

    def _parent_status(self, route: dict, parent_ref: dict,
                       gateway_index: dict[tuple[str, str], dict],
                       classes: dict[str, dict]) -> dict:
        namespace = route["metadata"].get("namespace", "default")
        group = parent_ref.get("group", GATEWAY_GROUP)
        kind = parent_ref.get("kind", "Gateway")
        gateway_namespace = parent_ref.get("namespace") or namespace
        gateway_name = parent_ref.get("name", "")
        normalized_ref = {
            "group": group, "kind": kind, "name": gateway_name,
            "namespace": gateway_namespace,
        }
        if parent_ref.get("sectionName"):
            normalized_ref["sectionName"] = parent_ref["sectionName"]
        if parent_ref.get("port"):
            normalized_ref["port"] = parent_ref["port"]

        result = {
            "parentRef": normalized_ref,
            "controllerName": CONTROLLER_NAME,
            "conditions": [],
        }
        if group != GATEWAY_GROUP or kind != "Gateway":
            result["conditions"] = [condition(
                "Accepted", False, "InvalidKind",
                f"MiniK8s only supports parentRef kind Gateway in group {GATEWAY_GROUP}",
                observed_generation=route["metadata"].get("generation"))]
            return result

        gateway = gateway_index.get((gateway_namespace, gateway_name))
        if gateway is None:
            result["conditions"] = [condition(
                "Accepted", False, "NoMatchingParent",
                f"Gateway {gateway_namespace}/{gateway_name} not found",
                observed_generation=route["metadata"].get("generation"))]
            return result

        listeners = self._matching_listeners(gateway, parent_ref)
        if not listeners:
            result["conditions"] = [condition(
                "Accepted", False, "NoMatchingParent",
                "No listener matches sectionName/port",
                observed_generation=route["metadata"].get("generation"))]
            return result

        gateway_class = classes.get(gateway.get("spec", {}).get("gatewayClassName", ""))
        class_ready = bool(gateway_class
                           and gateway_class.get("spec", {}).get("controllerName") == CONTROLLER_NAME)
        namespace_labels = self._namespace_labels(namespace)

        accepted = False
        accept_reason = "NotAllowed"
        accept_message = "Route is not allowed by any matching listener"
        for listener in listeners:
            if not listener_valid(listener):
                accept_reason, accept_message = (
                    "InvalidParameters", f"Listener {listener.get('name')} is invalid")
                continue
            if not listener_supported(listener):
                accept_reason, accept_message = (
                    "UnsupportedProtocol",
                    f"Listener {listener.get('name')} does not serve HTTPRoute")
                continue
            allowed, reason, message = route_allowed(
                route, listener, gateway, namespace_labels)
            if not allowed:
                accept_reason, accept_message = reason, message
                continue
            if not hostnames_intersect(route.get("spec", {}).get("hostnames"),
                                       listener.get("hostname")):
                accept_reason, accept_message = (
                    "NoMatchingListenerHostname",
                    "Route hostnames do not intersect the listener hostname")
                continue
            if not class_ready:
                accept_reason, accept_message = "InvalidParameters", "GatewayClass is not accepted"
                continue
            accepted = True
            accept_reason, accept_message = "Accepted", f"Attached to listener {listener.get('name')}"
            break

        resolved, resolve_reason, resolve_message = self._resolve_backends(route, namespace)
        result["conditions"] = [
            condition("Accepted", accepted, accept_reason, accept_message,
                      observed_generation=route["metadata"].get("generation")),
            condition("ResolvedRefs", resolved and accepted,
                      resolve_reason if accepted else "ParentNotAccepted",
                      resolve_message if accepted else "Parent is not accepted",
                      observed_generation=route["metadata"].get("generation")),
        ]
        return result

    @staticmethod
    def _matching_listeners(gateway: dict, parent_ref: dict) -> list[dict]:
        listeners = gateway.get("spec", {}).get("listeners") or []
        selected = []
        for listener in listeners:
            if parent_ref.get("sectionName") and listener.get("name") != parent_ref["sectionName"]:
                continue
            if parent_ref.get("port") and listener.get("port") != parent_ref["port"]:
                continue
            selected.append(listener)
        return selected

    def _resolve_backends(self, route: dict, namespace: str) -> tuple[bool, str, str]:
        rules = route.get("spec", {}).get("rules") or []
        if not rules:
            return False, "NoBackends", "HTTPRoute has no rules"
        checked = 0
        for rule in rules:
            for backend_ref in rule.get("backendRefs") or []:
                checked += 1
                group = backend_ref.get("group", "")
                kind = backend_ref.get("kind", "Service")
                if group not in ("", "core") or kind != "Service":
                    return False, "InvalidKind", (
                        f"MiniK8s supports Service backends only, got {group}/{kind}")
                if backend_ref.get("weight") is not None:
                    try:
                        if int(backend_ref["weight"]) < 0:
                            raise ValueError
                    except (TypeError, ValueError):
                        return False, "InvalidParameters", "backendRef weight must be a non-negative integer"
                service_namespace = backend_ref.get("namespace") or namespace
                if service_namespace != namespace and not reference_granted(
                        self.store, route, namespace, service_namespace,
                        backend_ref.get("name", "")):
                    return False, "RefNotPermitted", (
                        f"ReferenceGrant in {service_namespace} does not allow this backend")
                service = self.store.get(SERVICE_GVR, service_namespace, backend_ref.get("name", ""))
                if service is None:
                    return False, "BackendNotFound", (
                        f"Service {service_namespace}/{backend_ref.get('name')} not found")
                if backend_ref.get("port") is None:
                    return False, "InvalidParameters", "Service backendRef requires a port"
                if not _service_has_port(service, backend_ref["port"]):
                    return False, "BackendNotFound", (
                        f"Service {service_namespace}/{backend_ref.get('name')} "
                        f"does not expose port {backend_ref['port']}")
        if checked == 0:
            # No references means there is nothing to resolve.  A rule with no
            # backendRefs is still terminal; the data plane will return a
            # NoBackends error instead of falling through to a broader Route.
            return True, "ResolvedRefs", "No backendRefs to resolve"
        return True, "ResolvedRefs", "All backend references resolved"


def _service_has_port(service: dict, port: int | str) -> bool:
    for candidate in service.get("spec", {}).get("ports") or []:
        if isinstance(port, int):
            if int(candidate.get("port", 0)) == port:
                return True
        elif candidate.get("name") == port:
            return True
        elif str(candidate.get("port", "")) == str(port):
            return True
    return False


def reference_granted(store, route: dict, route_namespace: str, target_namespace: str,
                      target_name: str) -> bool:
    """Return True when a ReferenceGrant permits route -> Service in target namespace.

    Gateway API marks ``from.group``/``from.kind``/``from.namespace`` and
    ``to.group``/``to.kind`` as required.  Missing fields are not treated as
    defaults: a malformed grant must not silently authorize cross-namespace
    access.
    """
    if target_namespace == route_namespace:
        return True
    for grant in store.list(REFERENCEGRANT_GVR, target_namespace)[0]:
        if grant["metadata"].get("deletionTimestamp"):
            continue
        from_ok = False
        for source in grant.get("spec", {}).get("from", []) or []:
            if (source.get("group") == GATEWAY_GROUP
                    and source.get("kind") == "HTTPRoute"
                    and source.get("namespace") == route_namespace):
                from_ok = True
                break
        if not from_ok:
            continue
        for target in grant.get("spec", {}).get("to", []) or []:
            if target.get("group") != "" or target.get("kind") != "Service":
                continue
            if target.get("name") and target["name"] != target_name:
                continue
            return True
    return False


def _same_parent_ref(left: dict, right: dict) -> bool:
    keys = ("group", "kind", "name", "namespace", "sectionName", "port")
    return all(left.get(key) == right.get(key) for key in keys)


def _ref_targets_gateway(parent_ref: dict, route: dict, gateway: dict) -> bool:
    if parent_ref.get("group", GATEWAY_GROUP) != GATEWAY_GROUP:
        return False
    if parent_ref.get("kind", "Gateway") != "Gateway":
        return False
    if parent_ref.get("name") != gateway["metadata"]["name"]:
        return False
    namespace = parent_ref.get("namespace") or route["metadata"].get("namespace", "default")
    return namespace == gateway["metadata"].get("namespace", "default")
