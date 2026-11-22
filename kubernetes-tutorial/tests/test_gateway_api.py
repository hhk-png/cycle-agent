"""Gateway API status, attachment and routing semantics."""

from __future__ import annotations

def gateway_class(name: str = "mini", controller: str = "mini-k8s.io/gateway-controller") -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "GatewayClass",
        "metadata": {"name": name},
        "spec": {"controllerName": controller},
    }


def gateway(name: str = "edge", *, namespace: str = "default", hostname: str = "*.example.com",
            allowed_from: str = "Same", selector: dict | None = None) -> dict:
    namespaces: dict = {"from": allowed_from}
    if selector:
        namespaces["selector"] = selector
    return {
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "gatewayClassName": "mini",
            "listeners": [{
                "name": "http", "port": 80, "protocol": "HTTP",
                "hostname": hostname,
                "allowedRoutes": {"namespaces": namespaces},
            }],
        },
    }


def service(name: str, *, namespace: str = "default") -> dict:
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"ports": [{"name": "http", "port": 80, "targetPort": 8080}]},
    }


def route(name: str = "web", *, namespace: str = "default", gateway_name: str = "edge",
          gateway_namespace: str | None = None, hostnames: list[str] | None = None,
          rules: list[dict] | None = None) -> dict:
    parent: dict = {"name": gateway_name}
    if gateway_namespace:
        parent["namespace"] = gateway_namespace
    return {
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "parentRefs": [parent],
            "hostnames": hostnames if hostnames is not None else ["shop.example.com"],
            "rules": rules or [{"backendRefs": [{"name": "web", "port": 80}]}],
        },
    }


def condition(obj: dict, condition_type: str, key: str = "conditions") -> dict | None:
    items = obj.get(key) or obj.get("status", {}).get(key, []) or []
    for item in items:
        if item.get("type") == condition_type:
            return item
    return None


def wait_condition(cluster, kind: str, name: str, condition_type: str, *,
                   expected: str = "True", namespace: str = "default", timeout: float = 8) -> dict:
    def matches(obj: dict | None) -> bool:
        if not obj:
            return False
        found = condition(obj, condition_type)
        if found is not None and found.get("status") == expected:
            return True
        return any(
            (condition(parent, condition_type) or {}).get("status") == expected
            for parent in obj.get("status", {}).get("parents", [])
        )

    cluster.wait_until(lambda: matches(cluster.get(kind, name, namespace)), timeout,
                       description=f"{kind}/{name} {condition_type}={expected}")
    return cluster.get(kind, name, namespace)


def parent_status(cluster, route_name: str, gateway_name: str = "edge",
                  namespace: str = "default") -> dict:
    for parent in (cluster.get("httproutes", route_name, namespace) or {}).get(
            "status", {}).get("parents", []):
        if parent.get("parentRef", {}).get("name") == gateway_name:
            return parent
    return {}


def test_gateway_class_acceptance(cluster):
    cluster.apply(gateway_class("mini"))
    cluster.apply(gateway_class("other", controller="example.com/other"))

    accepted = wait_condition(cluster, "gatewayclasses", "mini", "Accepted")
    assert accepted["status"]["conditions"][0]["reason"] == "Accepted"

    rejected = wait_condition(cluster, "gatewayclasses", "other", "Accepted", expected="False")
    rejected_condition = condition(rejected, "Accepted")
    assert rejected_condition["reason"] == "Unsupported"
    assert "mini-k8s.io/gateway-controller" in rejected_condition["message"]


def test_gateway_status_and_attached_routes(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(service("web"))
    cluster.apply(route("r1"))
    cluster.apply(route("r2"))

    gateway_obj = wait_condition(cluster, "gateways", "edge", "Programmed")
    assert gateway_obj["status"]["addresses"] == [{"type": "IPAddress", "value": "192.0.2.101"}]
    listener = gateway_obj["status"]["listeners"][0]
    cluster.wait_until(
        lambda: (cluster.get("gateways", "edge")["status"]["listeners"][0]["attachedRoutes"]) == 2,
        timeout=8, description="attachedRoutes")
    assert listener["supportedKinds"] == [
        {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute"}]
    assert condition(listener, "Accepted")["status"] == "True"


def test_httproute_resolution_and_router(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(service("web"))
    cluster.apply(route(rules=[{
        "matches": [{"path": {"type": "PathPrefix", "value": "/api"}}],
        "backendRefs": [{"name": "web", "port": 80}],
    }]))

    wait_condition(cluster, "httproutes", "web", "ResolvedRefs")
    parent = parent_status(cluster, "web")
    assert condition(parent, "Accepted")["status"] == "True"
    resolved = cluster.gateway.route("shop.example.com", "/api/users")
    assert resolved["service"] == "web"
    assert resolved["namespace"] == "default"
    assert resolved["port"] == 80
    assert resolved["route"] == "web"
    assert cluster.gateway.route("shop.example.com", "/other") is None
    assert cluster.gateway.route("other.example.com", "/api/users") is None


def test_path_precedence_exact_then_longest_prefix(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(service("api"))
    cluster.apply(service("users"))
    cluster.apply(route("api", rules=[{
        "matches": [{"path": {"type": "PathPrefix", "value": "/api"}}],
        "backendRefs": [{"name": "api", "port": 80}]}]))
    cluster.apply(route("users", rules=[{
        "matches": [{"path": {"type": "Exact", "value": "/api/users"}}],
        "backendRefs": [{"name": "users", "port": 80}]}]))

    wait_condition(cluster, "httproutes", "api", "ResolvedRefs")
    wait_condition(cluster, "httproutes", "users", "ResolvedRefs")
    assert cluster.gateway.route("shop.example.com", "/api/users")["service"] == "users"
    assert cluster.gateway.route("shop.example.com", "/api/orders")["service"] == "api"
    assert cluster.gateway.route("shop.example.com", "/api/users/1")["service"] == "api"


def test_missing_backend_is_not_routable(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(route(rules=[{"backendRefs": [{"name": "missing", "port": 80}]}]))

    cluster.wait_until(
        lambda: condition(parent_status(cluster, "web"), "ResolvedRefs") is not None
        and condition(parent_status(cluster, "web"), "ResolvedRefs")["status"] == "False",
        timeout=8, description="ResolvedRefs false")
    resolved = condition(parent_status(cluster, "web"), "ResolvedRefs")
    assert resolved["reason"] == "BackendNotFound"
    assert cluster.gateway.route("shop.example.com", "/") is None


def test_cross_namespace_backend_requires_reference_grant(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "backend"}})
    cluster.apply(service("api", namespace="backend"))
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(route(rules=[{
        "backendRefs": [{"name": "api", "namespace": "backend", "port": 80}]}]))

    cluster.wait_until(
        lambda: condition(parent_status(cluster, "web"), "ResolvedRefs") is not None
        and condition(parent_status(cluster, "web"), "ResolvedRefs")["status"] == "False",
        timeout=8, description="cross-namespace denied")
    assert condition(parent_status(cluster, "web"), "ResolvedRefs")["reason"] == "RefNotPermitted"

    cluster.apply({
        "apiVersion": "gateway.networking.k8s.io/v1beta1", "kind": "ReferenceGrant",
        "metadata": {"name": "allow-default", "namespace": "backend"},
        "spec": {
            "from": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute",
                      "namespace": "default"}],
            "to": [{"group": "", "kind": "Service", "name": "api"}],
        },
    })
    wait_condition(cluster, "httproutes", "web", "ResolvedRefs")
    resolved = cluster.gateway.route("shop.example.com", "/")
    assert resolved["service"] == "api"
    assert resolved["namespace"] == "backend"


def test_listener_allowed_routes_namespace_policy(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "team", "labels": {"team": "blue"}}})
    cluster.apply(gateway_class())
    cluster.apply(gateway("same-gw"))
    cluster.apply(gateway("all-gw", allowed_from="All"))
    cluster.apply(gateway("selector-gw", allowed_from="Selector",
                          selector={"matchLabels": {"team": "blue"}}))
    cluster.apply(service("web", namespace="team"))

    cluster.apply(route("denied", namespace="team", gateway_name="same-gw",
                        gateway_namespace="default",
                        rules=[{"backendRefs": [{"name": "web", "port": 80}]}]))
    cluster.apply(route("allowed", namespace="team", gateway_name="all-gw",
                        gateway_namespace="default",
                        rules=[{"backendRefs": [{"name": "web", "port": 80}]}]))
    cluster.apply(route("selected", namespace="team", gateway_name="selector-gw",
                        gateway_namespace="default",
                        rules=[{"backendRefs": [{"name": "web", "port": 80}]}]))

    cluster.wait_until(
        lambda: condition(parent_status(cluster, "denied", "same-gw", "team"), "Accepted") is not None
        and condition(parent_status(cluster, "denied", "same-gw", "team"), "Accepted")["status"] == "False",
        timeout=8, description="same namespace denied")
    assert condition(parent_status(cluster, "denied", "same-gw", "team"), "Accepted")["reason"] == "NotAllowed"
    cluster.wait_until(
        lambda: (cluster.get("gateways", "same-gw") or {}).get("status", {}).get(
            "listeners", [{}])[0].get("attachedRoutes") == 0,
        timeout=8, description="denied route is not counted as attached")
    cluster.wait_until(
        lambda: condition(parent_status(cluster, "allowed", "all-gw", "team"), "Accepted") is not None
        and condition(parent_status(cluster, "allowed", "all-gw", "team"), "Accepted")["status"] == "True",
        timeout=8, description="all namespaces allowed")
    cluster.wait_until(
        lambda: condition(parent_status(cluster, "selected", "selector-gw", "team"), "Accepted") is not None
        and condition(parent_status(cluster, "selected", "selector-gw", "team"), "Accepted")["status"] == "True",
        timeout=8, description="selector namespace allowed")
    # The same route is not routable through the denying Gateway.
    assert cluster.gateway.route("shop.example.com", "/")["route"] in ("allowed", "selected")


def test_header_method_and_weighted_backends(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(service("stable"))
    cluster.apply(service("canary"))
    cluster.apply(route("method", rules=[
        {
            "matches": [{
                "method": "POST",
                "headers": [{"name": "x-version", "value": "v2"}],
            }],
            "backendRefs": [{"name": "stable", "port": 80}],
        },
        {"backendRefs": [{"name": "canary", "port": 80}]},
    ]))
    cluster.apply(route("weighted", hostnames=["weighted.example.com"], rules=[{
        "backendRefs": [
            {"name": "stable", "port": 80, "weight": 3},
            {"name": "canary", "port": 80, "weight": 1},
        ],
    }]))
    wait_condition(cluster, "httproutes", "method", "ResolvedRefs")
    wait_condition(cluster, "httproutes", "weighted", "ResolvedRefs")

    assert cluster.gateway.route("shop.example.com", "/", method="POST",
                                 headers={"X-Version": "v2"})["service"] == "stable"
    assert cluster.gateway.route("shop.example.com", "/", method="GET")["service"] == "canary"
    assert cluster.gateway.route("shop.example.com", "/", method="POST")["service"] == "canary"

    counts: dict[str, int] = {}
    for _ in range(8):
        selected = cluster.gateway.route("weighted.example.com", "/")["service"]
        counts[selected] = counts.get(selected, 0) + 1
    assert counts == {"stable": 6, "canary": 2}


def test_non_http_listener_does_not_attach_httproute(cluster):
    cluster.apply(gateway_class())
    cluster.apply({
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway",
        "metadata": {"name": "tcp", "namespace": "default"},
        "spec": {
            "gatewayClassName": "mini",
            "listeners": [{
                "name": "tcp", "port": 9000, "protocol": "TCP",
                "allowedRoutes": {"namespaces": {"from": "Same"}},
            }],
        },
    })
    cluster.apply(service("web"))
    cluster.apply(route("tcp-route", gateway_name="tcp"))

    cluster.wait_until(
        lambda: condition(parent_status(cluster, "tcp-route", "tcp"), "Accepted") is not None
        and condition(parent_status(cluster, "tcp-route", "tcp"), "Accepted")["status"] == "False",
        timeout=8, description="non-HTTP listener rejects HTTPRoute")
    parent = parent_status(cluster, "tcp-route", "tcp")
    assert condition(parent, "Accepted")["reason"] == "UnsupportedProtocol"
    gateway_obj = cluster.get("gateways", "tcp")
    assert condition(gateway_obj, "Programmed")["status"] == "False"
    listener_conditions = gateway_obj["status"]["listeners"][0]["conditions"]
    assert condition({"conditions": listener_conditions}, "Accepted")["status"] == "False"
    assert condition({"conditions": listener_conditions}, "Accepted")["reason"] == "UnsupportedProtocol"
    assert gateway_obj["status"]["listeners"][0]["attachedRoutes"] == 0
    assert gateway_obj["status"]["addresses"] == []
    assert cluster.gateway.route("shop.example.com", "/") is None


def test_gateway_allowed_routes_null_is_defaulted(cluster):
    cluster.apply(gateway_class())
    cluster.apply({
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway",
        "metadata": {"name": "null-allowed", "namespace": "default"},
        "spec": {
            "gatewayClassName": "mini",
            "listeners": [{"name": "http", "port": 80, "protocol": "HTTP",
                           "allowedRoutes": None}],
        },
    })
    cluster.apply(service("web"))
    cluster.apply(route("null-route", gateway_name="null-allowed"))
    gateway_obj = wait_condition(cluster, "gateways", "null-allowed", "Programmed")
    assert gateway_obj["spec"]["listeners"][0]["allowedRoutes"]["namespaces"]["from"] == "Same"
    wait_condition(cluster, "httproutes", "null-route", "Accepted")


def test_invalid_backend_weight_is_rejected_by_status(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(service("web"))
    cluster.apply(route("bad-weight", hostnames=["weight.example.com"], rules=[{
        "backendRefs": [{"name": "web", "port": 80, "weight": "abc"}]}]))
    cluster.wait_until(
        lambda: condition(parent_status(cluster, "bad-weight"), "ResolvedRefs") is not None
        and condition(parent_status(cluster, "bad-weight"), "ResolvedRefs")["status"] == "False",
        timeout=8, description="invalid weight rejected")
    assert condition(parent_status(cluster, "bad-weight"), "ResolvedRefs")["reason"] == "InvalidParameters"
    assert cluster.gateway.route("weight.example.com", "/") is None


def test_multi_listener_route_isolation(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "team"}})
    cluster.apply(gateway_class())
    cluster.apply({
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway",
        "metadata": {"name": "split", "namespace": "default"},
        "spec": {
            "gatewayClassName": "mini",
            "listeners": [
                {"name": "public", "port": 80, "protocol": "HTTP",
                 "hostname": "a.example.com",
                 "allowedRoutes": {"namespaces": {"from": "All"}}},
                {"name": "private", "port": 80, "protocol": "HTTP",
                 "hostname": "b.example.com",
                 "allowedRoutes": {"namespaces": {"from": "Same"}}},
            ],
        },
    })
    cluster.apply(service("web", namespace="team"))
    cluster.apply({
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
        "metadata": {"name": "team-route", "namespace": "team"},
        "spec": {
            "parentRefs": [{"name": "split", "namespace": "default"}],
            "rules": [{"backendRefs": [{"name": "web", "port": 80}]}],
        },
    })
    wait_condition(cluster, "httproutes", "team-route", "Accepted", namespace="team")
    gateway_obj = cluster.get("gateways", "split")
    assert gateway_obj["status"]["listeners"][0]["attachedRoutes"] == 1
    assert gateway_obj["status"]["listeners"][1]["attachedRoutes"] == 0
    selected = cluster.gateway.route("a.example.com", "/")
    assert selected["service"] == "web"
    assert selected["listener"] == "public"
    # b.example.com is served by the private listener, which denied this Route.
    assert cluster.gateway.route("b.example.com", "/") is None


def test_missing_service_port_is_resolved_refs_false(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(service("web"))
    cluster.apply(route("wrong-port", rules=[{
        "backendRefs": [{"name": "web", "port": 8080}]}]))
    cluster.wait_until(
        lambda: condition(parent_status(cluster, "wrong-port"), "ResolvedRefs") is not None
        and condition(parent_status(cluster, "wrong-port"), "ResolvedRefs")["status"] == "False",
        timeout=8, description="missing Service port")
    assert condition(parent_status(cluster, "wrong-port"), "ResolvedRefs")["reason"] == "BackendNotFound"
    assert cluster.gateway.route("shop.example.com", "/") is None


def test_empty_label_value_in_allowed_routes_selector(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "emptyval", "labels": {"team": ""}}})
    cluster.apply(gateway_class())
    cluster.apply(gateway("sel-gw", allowed_from="Selector",
                          selector={"matchLabels": {"team": ""}}))
    cluster.apply(service("web", namespace="emptyval"))
    cluster.apply(route("empty-route", namespace="emptyval", gateway_name="sel-gw",
                        gateway_namespace="default",
                        rules=[{"backendRefs": [{"name": "web", "port": 80}]}]))
    wait_condition(cluster, "httproutes", "empty-route", "Accepted", namespace="emptyval")
    assert cluster.gateway.route("shop.example.com", "/")["route"] == "empty-route"


def test_matching_rule_without_backends_is_terminal(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(service("web"))
    cluster.apply(route("fallback", rules=[{
        "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
        "backendRefs": [{"name": "web", "port": 80}],
    }]))
    cluster.apply(route("empty", rules=[{
        "matches": [{"path": {"type": "Exact", "value": "/foo"}}],
        "backendRefs": [],
    }]))
    wait_condition(cluster, "httproutes", "empty", "ResolvedRefs")
    selected = cluster.gateway.route("shop.example.com", "/foo")
    assert selected["error"] == "NoBackends"
    assert selected["service"] is None
    assert cluster.gateway.route("shop.example.com", "/bar")["route"] == "fallback"


def test_zero_weight_backend_is_terminal(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(service("web"))
    cluster.apply(route("zero", hostnames=["zero.example.com"], rules=[{
        "backendRefs": [{"name": "web", "port": 80, "weight": 0}]}]))
    wait_condition(cluster, "httproutes", "zero", "ResolvedRefs")
    assert cluster.gateway.route("zero.example.com", "/")["error"] == "NoBackends"


def test_gateway_without_valid_listeners_is_not_accepted(cluster):
    cluster.apply(gateway_class())
    cluster.apply({
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway",
        "metadata": {"name": "broken", "namespace": "default"},
        "spec": {
            "gatewayClassName": "mini",
            "listeners": [
                {"name": "zero", "port": 0, "protocol": "HTTP"},
                {"name": "huge", "port": 70000, "protocol": "HTTP"},
            ],
        },
    })
    cluster.wait_until(
        lambda: condition(cluster.get("gateways", "broken"), "Accepted") is not None
        and condition(cluster.get("gateways", "broken"), "Accepted")["status"] == "False",
        timeout=8, description="invalid listeners rejected")
    gateway_obj = cluster.get("gateways", "broken")
    assert condition(gateway_obj, "Accepted")["reason"] == "ListenersNotValid"
    assert condition(gateway_obj, "Programmed")["status"] == "False"
    assert gateway_obj["status"]["addresses"] == []


def test_listener_protocol_change_converges(cluster):
    cluster.apply(gateway_class())
    tcp_gateway = {
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway",
        "metadata": {"name": "changing", "namespace": "default"},
        "spec": {
            "gatewayClassName": "mini",
            "listeners": [{"name": "l", "port": 8080, "protocol": "TCP",
                           "allowedRoutes": {"namespaces": {"from": "Same"}}}],
        },
    }
    cluster.apply(tcp_gateway)
    cluster.wait_until(
        lambda: any(item.get("type") == "Programmed"
                    for item in (cluster.get("gateways", "changing") or {}).get(
                        "status", {}).get("listeners", [{}])[0].get("conditions", [])),
        timeout=8, description="TCP listener condition")
    tcp_gateway["spec"]["listeners"][0]["protocol"] = "HTTP"
    tcp_gateway["spec"]["listeners"][0]["hostname"] = "*.example.com"
    cluster.apply(tcp_gateway)
    cluster.wait_until(
        lambda: condition(cluster.get("gateways", "changing"), "Programmed") is not None
        and condition(cluster.get("gateways", "changing"), "Programmed")["status"] == "True",
        timeout=8, description="HTTP listener programmed")
    listener_conditions = cluster.get("gateways", "changing")["status"]["listeners"][0]["conditions"]
    assert {item["type"] for item in listener_conditions} == {"Accepted"}


def test_malformed_reference_grant_does_not_authorize(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "backend"}})
    cluster.apply(service("api", namespace="backend"))
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(route("cross", rules=[{
        "backendRefs": [{"name": "api", "namespace": "backend", "port": 80}]}]))
    cluster.apply({
        "apiVersion": "gateway.networking.k8s.io/v1beta1", "kind": "ReferenceGrant",
        "metadata": {"name": "malformed", "namespace": "backend"},
        "spec": {
            "from": [{"kind": "HTTPRoute", "namespace": "default"}],
            "to": [{"kind": "Service", "name": "api"}],
        },
    })
    cluster.wait_until(
        lambda: condition(parent_status(cluster, "cross"), "ResolvedRefs") is not None
        and condition(parent_status(cluster, "cross"), "ResolvedRefs")["status"] == "False",
        timeout=8, description="malformed grant denied")
    assert condition(parent_status(cluster, "cross"), "ResolvedRefs")["reason"] == "RefNotPermitted"
    assert cluster.gateway.route("shop.example.com", "/") is None


def test_reference_grant_revocation_is_enforced_by_data_plane(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "backend"}})
    cluster.apply(service("api", namespace="backend"))
    cluster.apply(gateway_class())
    cluster.apply(gateway())
    cluster.apply(route("cross", rules=[{
        "backendRefs": [{"name": "api", "namespace": "backend", "port": 80}]}]))
    cluster.apply({
        "apiVersion": "gateway.networking.k8s.io/v1beta1", "kind": "ReferenceGrant",
        "metadata": {"name": "allow", "namespace": "backend"},
        "spec": {
            "from": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute",
                      "namespace": "default"}],
            "to": [{"group": "", "kind": "Service", "name": "api"}],
        },
    })
    wait_condition(cluster, "httproutes", "cross", "ResolvedRefs")
    assert cluster.gateway.route("shop.example.com", "/")["namespace"] == "backend"
    cluster.store.delete(("gateway.networking.k8s.io", "v1beta1", "referencegrants"),
                         "backend", "allow", kind="ReferenceGrant")
    denied = cluster.gateway.route("shop.example.com", "/")
    assert denied["error"] == "NoBackends"
    assert denied["service"] is None


def test_multi_parent_route_reports_serving_gateway(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway("gw-a", hostname="a.example.com"))
    cluster.apply(gateway("gw-b", hostname="b.example.com"))
    cluster.apply(service("web"))
    cluster.apply({
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
        "metadata": {"name": "multi", "namespace": "default"},
        "spec": {
            "parentRefs": [{"name": "gw-a"}, {"name": "gw-b"}],
            "rules": [{"backendRefs": [{"name": "web", "port": 80}]}],
        },
    })
    wait_condition(cluster, "httproutes", "multi", "Accepted")
    selected = cluster.gateway.route("b.example.com", "/")
    assert selected["gateway"] == "gw-b"
    assert selected["listener"] == "http"


def test_listener_hostname_intersection(cluster):
    cluster.apply(gateway_class())
    cluster.apply(gateway(hostname="shop.example.com"))
    cluster.apply(service("web"))
    cluster.apply(route("matching", hostnames=["shop.example.com"]))
    cluster.apply(route("wildcard", hostnames=["*.example.com"]))
    cluster.apply(route("wrong", hostnames=["api.example.org"]))

    wait_condition(cluster, "httproutes", "matching", "Accepted")
    wait_condition(cluster, "httproutes", "wildcard", "Accepted")
    cluster.wait_until(
        lambda: condition(parent_status(cluster, "wrong"), "Accepted") is not None
        and condition(parent_status(cluster, "wrong"), "Accepted")["status"] == "False",
        timeout=8, description="hostname mismatch")
    assert condition(parent_status(cluster, "wrong"), "Accepted")["reason"] == "NoMatchingListenerHostname"
    assert cluster.gateway.route("shop.example.com", "/")["route"] in ("matching", "wildcard")
    assert cluster.gateway.route("api.example.com", "/") is None
