from __future__ import annotations

import time

from mini_k8s.network.ingress import IngressRouter


def deploy_and_service(cluster, name="web", replicas=2, port=80, target=8080):
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": "default"},
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}},
                              "spec": {"containers": [{"name": "web", "image": "nginx",
                                                       "ports": [{"containerPort": target}]}]}}},
    })
    cluster.apply({
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": "default"},
        "spec": {"selector": {"app": name},
                 "ports": [{"name": "http", "port": port, "targetPort": target}]},
    })


def test_service_allocates_cluster_ip_and_endpoints(cluster):
    deploy_and_service(cluster)
    cluster.wait_for_deployment("web")
    cluster.wait_until(lambda: len((cluster.get("endpoints", "web") or {}).get("subsets", [{}])[0].get("addresses", [])) == 2,
                       timeout=8, description="two endpoints")
    service = cluster.get("services", "web")
    assert service["spec"]["clusterIP"].startswith("10.96.")
    endpoints = cluster.get("endpoints", "web")
    assert endpoints["subsets"][0]["ports"][0]["port"] == 8080
    slice_obj = cluster.list("endpointslices")[0]
    assert slice_obj["metadata"]["labels"]["kubernetes.io/service-name"] == "web"
    assert len(slice_obj["endpoints"]) == 2


def test_dns_short_fqdn_and_headless(cluster):
    deploy_and_service(cluster)
    cluster.wait_for_deployment("web")
    cluster.wait_until(lambda: len(cluster.dns.resolve("web")) == 1, timeout=8, description="dns")
    assert cluster.dns.resolve("web.default.svc.cluster.local") == cluster.dns.resolve("web")
    assert cluster.dns.resolve("missing") == []
    cluster.apply({"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": "headless", "namespace": "default"},
                   "spec": {"clusterIP": "None", "selector": {"app": "web"},
                            "ports": [{"port": 80, "targetPort": 8080}]}})
    cluster.wait_until(lambda: len(cluster.dns.resolve("headless")) == 2,
                       timeout=8, description="headless dns")
    ips = cluster.dns.resolve("headless")
    assert all(ip.startswith("10.244.") for ip in ips)


def test_external_name_service_has_no_endpoints(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": "external", "namespace": "default"},
                   "spec": {"type": "ExternalName", "externalName": "example.com"}})
    time.sleep(0.3)
    service = cluster.get("services", "external")
    assert service["spec"]["clusterIP"] == ""
    assert cluster.get("endpoints", "external") is None
    # ExternalName resolves to the external target (the simulator returns the
    # CNAME target rather than an A record).
    assert cluster.dns.resolve("external") == ["example.com"]


def test_node_port_and_load_balancer(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": "nodeport", "namespace": "default"},
                   "spec": {"type": "NodePort", "selector": {"app": "x"},
                            "ports": [{"port": 80}]}})
    cluster.apply({"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": "lb", "namespace": "default"},
                   "spec": {"type": "LoadBalancer", "selector": {"app": "x"},
                            "ports": [{"port": 443}]}})
    cluster.wait_until(lambda: bool((cluster.get("services", "nodeport") or {}).get("spec", {}).get("ports", [{}])[0].get("nodePort")),
                       timeout=5, description="nodePort allocation")
    node_port = cluster.get("services", "nodeport")["spec"]["ports"][0]["nodePort"]
    assert 30000 <= node_port <= 32767
    cluster.wait_until(lambda: bool((cluster.get("services", "lb") or {}).get("status", {}).get("loadBalancer", {}).get("ingress")),
                       timeout=5, description="load balancer status")
    assert cluster.get("services", "lb")["status"]["loadBalancer"]["ingress"][0]["ip"] == "192.0.2.100"


def test_proxy_enforces_network_policy(cluster):
    deploy_and_service(cluster)
    cluster.wait_for_deployment("web")
    cluster.apply({"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"name": "trusted", "labels": {"role": "trusted"}},
                   "spec": {"containers": [{"name": "c", "image": "x"}]}})
    cluster.apply({"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"name": "untrusted", "labels": {"role": "untrusted"}},
                   "spec": {"containers": [{"name": "c", "image": "x"}]}})
    cluster.wait_for_pod("trusted")
    cluster.wait_for_pod("untrusted")
    cluster.wait_until(lambda: len(cluster.proxy.endpoints("web", "default", 80)) == 2,
                       timeout=8, description="proxy endpoints")
    trusted = cluster.get("pods", "trusted")
    untrusted = cluster.get("pods", "untrusted")
    assert cluster.proxy.connect(trusted, "web", "default", 80)["connected"]
    assert cluster.proxy.connect(untrusted, "web", "default", 80)["connected"]
    cluster.apply({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                   "metadata": {"name": "only-trusted", "namespace": "default"},
                   "spec": {"podSelector": {"matchLabels": {"app": "web"}},
                            "policyTypes": ["Ingress"],
                            "ingress": [{"from": [{"podSelector": {"matchLabels": {"role": "trusted"}}}],
                                         "ports": [{"port": 8080, "protocol": "TCP"}]}]}})
    result = cluster.proxy.connect(untrusted, "web", "default", 80)
    assert not result["connected"]
    assert "NetworkPolicy" in result["reason"]
    assert cluster.proxy.connect(trusted, "web", "default", 80)["connected"]


def test_network_policy_egress_and_namespace_selector(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "other", "labels": {"team": "other"}}})
    cluster.apply({"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"name": "src", "labels": {"app": "src"}},
                   "spec": {"containers": [{"name": "c", "image": "x"}]}})
    cluster.apply({"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"name": "dst", "namespace": "other", "labels": {"app": "dst"}},
                   "spec": {"containers": [{"name": "c", "image": "x"}]}})
    cluster.wait_for_pod("src")
    cluster.wait_for_pod("dst", "other")
    src = cluster.get("pods", "src")
    dst = cluster.get("pods", "dst", "other")
    assert cluster.network.can_connect(src, dst, 80)
    cluster.apply({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                   "metadata": {"name": "egress-deny", "namespace": "default"},
                   "spec": {"podSelector": {"matchLabels": {"app": "src"}},
                            "policyTypes": ["Egress"],
                            "egress": [{"to": [{"namespaceSelector": {"matchLabels": {"team": "allowed"}}}]}]}})
    assert not cluster.network.can_connect(src, dst, 80)


def test_ingress_router_longest_prefix_and_default_backend(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": "api", "namespace": "default"},
                   "spec": {"selector": {"app": "api"}, "ports": [{"port": 80}]}})
    cluster.apply({"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": "site", "namespace": "default"},
                   "spec": {"selector": {"app": "site"}, "ports": [{"port": 80}]}})
    cluster.apply({"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                   "metadata": {"name": "routes", "namespace": "default"},
                   "spec": {"rules": [{"host": "example.test", "http": {"paths": [
                       {"path": "/", "pathType": "Prefix",
                        "backend": {"service": {"name": "site", "port": {"number": 80}}}},
                       {"path": "/api", "pathType": "Prefix",
                        "backend": {"service": {"name": "api", "port": {"number": 80}}}},
                   ]}}], "defaultBackend": {"service": {"name": "site", "port": {"number": 80}}}}})
    time.sleep(0.2)
    assert cluster.ingress.route("example.test", "/api/v1")["service"] == "api"
    assert cluster.ingress.route("example.test", "/other")["service"] == "site"
    assert cluster.ingress.route("unknown.test", "/")["service"] == "site"


def test_ingress_status_published(cluster):
    cluster.apply({"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                   "metadata": {"name": "empty", "namespace": "default"},
                   "spec": {"defaultBackend": {"service": {"name": "nope", "port": {"number": 80}}}}})
    cluster.wait_until(lambda: bool((cluster.get("ingresses", "empty") or {}).get("status", {}).get("loadBalancer", {}).get("ingress")),
                       timeout=5, description="ingress status")
    assert cluster.get("ingresses", "empty")["status"]["loadBalancer"]["ingress"][0]["ip"] == "192.0.2.100"


def test_terminating_pod_leaves_ready_endpoints(cluster):
    deploy_and_service(cluster, replicas=2)
    cluster.wait_for_deployment("web")
    cluster.wait_until(
        lambda: len((cluster.get("endpoints", "web") or {}).get("subsets", [{}])[0].get("addresses", [])) == 2,
        timeout=8, description="two endpoints")
    pods = [p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")]
    victim = pods[0]
    victim["metadata"]["finalizers"] = ["example.com/hold"]
    cluster.apply(victim)
    cluster.delete(f"pod/{victim['metadata']['name']}")
    cluster.wait_until(
        lambda: len((cluster.get("endpoints", "web") or {}).get("subsets", [{}])[0].get("addresses", [])) == 1,
        timeout=8, description="terminating endpoint removed")
    slice_obj = cluster.list("endpointslices")[0]
    terminating = [e for e in slice_obj["endpoints"] if e["conditions"]["terminating"]]
    assert terminating and terminating[0]["conditions"]["ready"] is False


def test_network_policy_named_port_is_resolved(cluster):
    target = {"apiVersion": "v1", "kind": "Pod",
              "metadata": {"name": "named-target", "namespace": "default", "labels": {"app": "api"}},
              "spec": {"containers": [{"name": "api", "image": "x",
                                       "ports": [{"name": "http", "containerPort": 8080}]}]}}
    source = {"apiVersion": "v1", "kind": "Pod",
              "metadata": {"name": "named-source", "namespace": "default", "labels": {"app": "trusted"}},
              "spec": {"containers": [{"name": "c", "image": "x"}]}}
    cluster.apply(target)
    cluster.apply(source)
    cluster.wait_for_pod("named-target")
    cluster.wait_for_pod("named-source")
    cluster.apply({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                   "metadata": {"name": "named-port", "namespace": "default"},
                   "spec": {"podSelector": {"matchLabels": {"app": "api"}},
                            "policyTypes": ["Ingress"],
                            "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "trusted"}}}],
                                         "ports": [{"protocol": "TCP", "port": "http"}]}]}})
    target_pod = cluster.get("pods", "named-target")
    source_pod = cluster.get("pods", "named-source")
    assert cluster.network.can_connect(source_pod, target_pod, 8080) is True
    assert cluster.network.can_connect(source_pod, target_pod, 9090) is False


def test_statefulset_pod_dns_records(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": "db", "namespace": "default"},
                   "spec": {"clusterIP": "None", "selector": {"app": "db"},
                            "ports": [{"port": 5432, "targetPort": 5432}]}})
    cluster.apply({"apiVersion": "apps/v1", "kind": "StatefulSet",
                   "metadata": {"name": "db", "namespace": "default"},
                   "spec": {"serviceName": "db", "replicas": 2,
                            "selector": {"matchLabels": {"app": "db"}},
                            "template": {"metadata": {"labels": {"app": "db"}},
                                         "spec": {"containers": [{"name": "db", "image": "postgres:16",
                                                                  "ports": [{"containerPort": 5432}]}]}}}})
    cluster.wait_until(lambda: (cluster.get("statefulsets", "db") or {}).get("status", {}).get("readyReplicas") == 2,
                       timeout=10, description="statefulset ready")
    pod = cluster.get("pods", "db-0")
    assert pod["spec"]["hostname"] == "db-0"
    assert pod["spec"]["subdomain"] == "db"
    ip = pod["status"]["podIP"]
    assert cluster.dns.resolve("db-0.db") == [ip]
    assert cluster.dns.resolve("db-0.db.default") == [ip]
    assert cluster.dns.resolve("db-0.db.default.svc.cluster.local") == [ip]
    assert cluster.dns.resolve("db-1.db.default.svc.cluster.local") == [cluster.get("pods", "db-1")["status"]["podIP"]]
