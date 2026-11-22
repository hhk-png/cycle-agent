"""NetworkPolicy evaluation for the simulated pod network.

This module does not install iptables or eBPF rules.  It computes the same
allow/deny decision a CNI dataplane would implement and exposes it through
``NetworkSimulator`` so tests and examples can prove policy intent.
"""

from __future__ import annotations

import ipaddress

from mini_k8s.apimachinery.selectors import match_expression, match_labels

POD_GVR = ("", "v1", "pods")
NS_GVR = ("", "v1", "namespaces")
POLICY_GVR = ("networking.k8s.io", "v1", "networkpolicies")


def _policy_types(policy: dict) -> list[str]:
    spec = policy.get("spec", {})
    if spec.get("policyTypes"):
        return list(spec["policyTypes"])
    types = ["Ingress"]
    if spec.get("egress"):
        types.append("Egress")
    return types


class NetworkSimulator:
    def __init__(self, store):
        self.store = store

    def can_connect(self, source: dict, target: dict, port: int, protocol: str = "TCP") -> bool:
        source_ns = source["metadata"].get("namespace", "default")
        target_ns = target["metadata"].get("namespace", "default")
        pods = self.store.list(POD_GVR)[0]
        policies = self.store.list(POLICY_GVR)[0]
        egress = [p for p in policies if p["metadata"].get("namespace") == source_ns
                  and p.get("spec", {}).get("podSelector") is not None
                  and self._selects(p["spec"].get("podSelector"), source)
                  and "Egress" in _policy_types(p)]
        if egress and not any(self._egress_allows(p, source, target, port, protocol, target_ns) for p in egress):
            return False
        ingress = [p for p in policies if p["metadata"].get("namespace") == target_ns
                   and p.get("spec", {}).get("podSelector") is not None
                   and self._selects(p["spec"].get("podSelector"), target)
                   and "Ingress" in _policy_types(p)]
        if ingress and not any(self._ingress_allows(p, source, target, port, protocol, source_ns) for p in ingress):
            return False
        return True

    @staticmethod
    def _selects(selector: dict, pod: dict) -> bool:
        labels = pod.get("metadata", {}).get("labels", {})
        match_labels_map = (selector or {}).get("matchLabels", {})
        if match_labels_map and not all(labels.get(k) == v for k, v in match_labels_map.items()):
            return False
        for expression in (selector or {}).get("matchExpressions", []):
            if not match_expression(expression, labels):
                return False
        return True

    def _selector_matches_pod(self, selector: dict | None, pod: dict) -> bool:
        if selector is None:
            return True
        if isinstance(selector, str):
            return match_labels(pod.get("metadata", {}).get("labels"), selector)
        return self._selects(selector, pod)

    def _peer_matches(self, peer: dict, pod: dict, pod_namespace: str) -> bool:
        if "ipBlock" in peer:
            ip = pod.get("status", {}).get("podIP")
            if not ip:
                return False
            cidr = peer["ipBlock"].get("cidr", "")
            try:
                if ipaddress.ip_address(ip) not in ipaddress.ip_network(cidr, strict=False):
                    return False
            except ValueError:
                return False
            for excluded in peer["ipBlock"].get("except", []):
                if ipaddress.ip_address(ip) in ipaddress.ip_network(excluded, strict=False):
                    return False
            return True
        selector = peer.get("namespaceSelector")
        if selector is not None:
            namespace = self.store.get(NS_GVR, "", pod_namespace)
            if namespace is None or not self._selector_matches_pod(selector, namespace):
                return False
        pod_selector = peer.get("podSelector")
        if pod_selector is not None:
            return self._selector_matches_pod(pod_selector, pod)
        return True

    def _ports_match(self, rule: dict, port: int, protocol: str, target: dict | None = None) -> bool:
        ports = rule.get("ports")
        if not ports:
            return True
        for allowed in ports:
            if allowed.get("protocol", "TCP") != protocol:
                continue
            if "port" not in allowed:
                return True
            value = allowed["port"]
            resolved: int | None = None
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
                resolved = int(value)
            elif isinstance(value, str) and "-" in value:
                low, high = value.split("-", 1)
                if int(low) <= port <= int(high):
                    return True
                continue
            elif isinstance(value, str) and target is not None:
                # Named port: resolve against the target Pod's declared ports.
                for container in target.get("spec", {}).get("containers", []):
                    for declared in container.get("ports", []):
                        if declared.get("name") == value:
                            resolved = int(declared.get("containerPort", 0))
            if resolved is not None:
                end_port = allowed.get("endPort")
                if end_port is not None:
                    if resolved <= port <= int(end_port):
                        return True
                elif resolved == port:
                    return True
        return False

    def _ingress_allows(self, policy: dict, source: dict, target: dict, port: int, protocol: str,
                        source_ns: str) -> bool:
        for rule in policy.get("spec", {}).get("ingress", []):
            if not self._ports_match(rule, port, protocol, target):
                continue
            peers = rule.get("from", [])
            if not peers or any(self._peer_matches(peer, source, source_ns) for peer in peers):
                return True
        return False

    def _egress_allows(self, policy: dict, source: dict, target: dict, port: int, protocol: str,
                       target_ns: str) -> bool:
        for rule in policy.get("spec", {}).get("egress", []):
            if not self._ports_match(rule, port, protocol, target):
                continue
            peers = rule.get("to", [])
            if not peers or any(self._peer_matches(peer, target, target_ns) for peer in peers):
                return True
        return False
