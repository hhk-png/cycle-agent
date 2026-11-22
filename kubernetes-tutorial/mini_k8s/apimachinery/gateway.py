"""Pure Gateway API helpers shared by the controller and the data plane.

Keeping these rules in one place matters: the controller decides what to
publish in status, and the in-process router must make the *same* decision.
If the two implementations drift, a Route can be reported Accepted but never
served (or worse, served on a listener that denied it).
"""

from __future__ import annotations

from mini_k8s.apimachinery.selectors import match_expression

GATEWAY_GROUP = "gateway.networking.k8s.io"
SUPPORTED_PROTOCOLS = ("HTTP", "HTTPS")


def listener_valid(listener: dict) -> bool:
    """Structural validity only; protocol support is a separate question."""
    name = listener.get("name", "")
    port = listener.get("port")
    protocol = listener.get("protocol", "")
    return bool(name and isinstance(port, int) and 1 <= port <= 65535
                and protocol in ("HTTP", "HTTPS", "TLS", "TCP", "UDP"))


def listener_supported(listener: dict) -> bool:
    return listener.get("protocol") in SUPPORTED_PROTOCOLS


def route_allowed(route: dict, listener: dict, gateway: dict,
                  namespace_labels: dict[str, str] | None) -> tuple[bool, str, str]:
    """Apply allowedRoutes (kinds + namespaces) for one listener."""
    allowed = listener.get("allowedRoutes")
    if not isinstance(allowed, dict):
        allowed = {}
    kinds = allowed.get("kinds")
    if kinds is not None:
        if not isinstance(kinds, list) or not any(
                isinstance(item, dict)
                and item.get("group", GATEWAY_GROUP) == GATEWAY_GROUP
                and item.get("kind", "HTTPRoute") == "HTTPRoute" for item in kinds):
            return False, "NotAllowed", "Listener does not allow HTTPRoute"
    namespaces = allowed.get("namespaces")
    if not isinstance(namespaces, dict):
        namespaces = {}
    mode = namespaces.get("from", "Same")
    route_namespace = route["metadata"].get("namespace", "default")
    gateway_namespace = gateway["metadata"].get("namespace", "default")
    if mode == "Same":
        return (route_namespace == gateway_namespace, "NotAllowed",
                "Listener only accepts routes from the same namespace")
    if mode == "All":
        return True, "Accepted", "All namespaces are allowed"
    if mode == "Selector":
        selector = namespaces.get("selector")
        if not isinstance(selector, dict):
            selector = {}
        labels = namespace_labels or {}
        match_labels = selector.get("matchLabels")
        if not isinstance(match_labels, dict):
            match_labels = {}
        for key, value in match_labels.items():
            if labels.get(key) != value:
                return False, "NotAllowed", (
                    "Route namespace is not selected by listener allowedRoutes")
        expressions = selector.get("matchExpressions")
        if not isinstance(expressions, list):
            expressions = []
        for expression in expressions:
            if not isinstance(expression, dict) or not match_expression(expression, labels):
                return False, "NotAllowed", (
                    "Route namespace is not selected by listener allowedRoutes")
        return True, "Accepted", "Route namespace is selected by allowedRoutes"
    return False, "NotAllowed", f"Unknown allowedRoutes.namespaces.from {mode!r}"


def hostname_score(pattern: str, host: str) -> tuple[int, int] | None:
    """Return (specificity, length) when pattern matches host, else None."""
    if pattern == host:
        return (3, len(host))
    if pattern == "*":
        return (1, 0)
    if pattern.startswith("*."):
        suffix = pattern[1:]
        if host.endswith(suffix) and host.count(".") == pattern.count("."):
            return (2, len(suffix))
    return None


def listener_covers_host(listener: dict, host: str) -> bool:
    hostname = listener.get("hostname")
    return not hostname or hostname_score(hostname, host) is not None


def hostnames_intersect(route_hostnames: list[str] | None,
                        listener_hostname: str | None) -> bool:
    if not listener_hostname:
        return True
    patterns = route_hostnames or [listener_hostname]
    return any(hostname_patterns_intersect(pattern, listener_hostname) for pattern in patterns)


def hostname_patterns_intersect(left: str, right: str) -> bool:
    if left == right or left == "*" or right == "*":
        return True
    left_wild = left.startswith("*.")
    right_wild = right.startswith("*.")
    if left_wild and right_wild:
        return left[1:] == right[1:]
    if left_wild:
        return right.endswith(left[1:]) and right.count(".") == left.count(".")
    if right_wild:
        return left.endswith(right[1:]) and left.count(".") == right.count(".")
    return False
