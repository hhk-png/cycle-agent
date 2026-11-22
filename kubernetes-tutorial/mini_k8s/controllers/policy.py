"""PodDisruptionBudget status and ResourceQuota status controllers."""

from __future__ import annotations

from mini_k8s.admission.admission import quota_usage
from mini_k8s.apimachinery.meta import deep_copy
from mini_k8s.apimachinery.selectors import match_expression
from mini_k8s.controllers.base import Controller, condition, set_condition

PDB_GVR = ("policy", "v1", "poddisruptionbudgets")
POD_GVR = ("", "v1", "pods")
QUOTA_GVR = ("", "v1", "resourcequotas")


def selector_matches(selector: dict, labels: dict[str, str]) -> bool:
    if not selector:
        return False
    match_labels_map = selector.get("matchLabels", {})
    if match_labels_map and not all(labels.get(k) == v for k, v in match_labels_map.items()):
        return False
    for expression in selector.get("matchExpressions", []):
        if not match_expression(expression, labels):
            return False
    return bool(match_labels_map or selector.get("matchExpressions"))


class PDBController(Controller):
    name = "disruption-controller"

    def reconcile(self) -> None:
        for pdb in self.store.list(PDB_GVR)[0]:
            if pdb["metadata"].get("deletionTimestamp"):
                continue
            namespace = pdb["metadata"]["namespace"]
            selector = pdb.get("spec", {}).get("selector", {})
            pods = [p for p in self.store.list(POD_GVR, namespace)[0]
                    if selector_matches(selector, p["metadata"].get("labels", {}))]
            expected = len(pods)
            healthy = len([p for p in pods if _healthy(p)])
            spec = pdb.get("spec", {})
            desired = expected
            if "minAvailable" in spec:
                desired = _resolve_amount(spec["minAvailable"], expected)
            elif "maxUnavailable" in spec:
                desired = max(0, expected - _resolve_amount(spec["maxUnavailable"], expected))
            disruptions = max(0, healthy - desired)
            status = {
                "observedGeneration": pdb["metadata"].get("generation", 1),
                "disruptedPods": pdb.get("status", {}).get("disruptedPods", {}),
                "currentHealthy": healthy,
                "desiredHealthy": desired,
                "expectedPods": expected,
                "disruptionsAllowed": disruptions,
                "conditions": [condition("DisruptionAllowed", disruptions > 0,
                                         "SufficientPods" if disruptions > 0 else "InsufficientPods",
                                         f"{healthy} healthy pods, {desired} required")],
            }
            self.update_status(PDB_GVR, pdb, status)


def _resolve_amount(value, total: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.endswith("%"):
        try:
            return int(total * float(value[:-1]) / 100.0 + 0.9999)
        except ValueError:
            return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _healthy(pod: dict) -> bool:
    if pod.get("metadata", {}).get("deletionTimestamp"):
        return False
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in pod.get("status", {}).get("conditions", []))


class ResourceQuotaController(Controller):
    name = "resourcequota-controller"

    def reconcile(self) -> None:
        from mini_k8s.apimachinery.quantity import format_quantity
        for quota in self.store.list(QUOTA_GVR)[0]:
            if quota["metadata"].get("deletionTimestamp"):
                continue
            namespace = quota["metadata"]["namespace"]
            usage = quota_usage(self.store, namespace)
            hard = quota.get("spec", {}).get("hard", {})
            used = {}
            for key in hard:
                value = usage.get(key, 0.0)
                resource = "cpu" if key in ("cpu", "requests.cpu", "limits.cpu") else ""
                used[key] = format_quantity(value, resource=resource) if value else "0"
            status = {
                "hard": deep_copy(hard),
                "used": used,
            }
            self.update_status(QUOTA_GVR, quota, status)
