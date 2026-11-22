"""Controller runtime: one reconcile loop per controller, plus shared helpers."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from mini_k8s.apimachinery.meta import deep_copy, now_iso, owner_reference


class Controller:
    name = "controller"

    def __init__(self, store, *, interval: float = 0.05, enabled: bool = True):
        self.store = store
        self.interval = interval
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors = 0

    def start(self) -> "Controller":
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"mini-k8s-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception as exc:
                self.errors += 1
                self.event("Warning", f"{self.name}Error", str(exc)[:500], namespace="kube-system")
            self._stop.wait(self.interval)

    def reconcile(self) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ helpers
    def event(self, event_type: str, reason: str, message: str, obj: dict | None = None,
              *, namespace: str | None = None) -> None:
        ns = namespace or (obj.get("metadata", {}).get("namespace") if obj else "kube-system") or "default"
        name = f"{self.name}.{uuid.uuid4().hex[:12]}"
        body = {
            "apiVersion": "v1", "kind": "Event",
            "metadata": {"name": name, "namespace": ns},
            "type": event_type, "reason": reason, "message": message,
            "source": {"component": self.name},
            "firstTimestamp": now_iso(), "lastTimestamp": now_iso(), "count": 1,
        }
        if obj:
            body["involvedObject"] = {
                "apiVersion": obj.get("apiVersion"), "kind": obj.get("kind"),
                "name": obj["metadata"].get("name"), "namespace": obj["metadata"].get("namespace", ""),
                "uid": obj["metadata"].get("uid", ""),
            }
        try:
            self.store.create(("", "v1", "events"), body, kind="Event", namespace=ns)
        except Exception:
            pass

    def create_owned(self, gvr, obj: dict, kind: str, owner: dict, namespace: str | None = None,
                     *, controller: bool = True) -> dict | None:
        obj = deep_copy(obj)
        obj.setdefault("metadata", {}).setdefault("ownerReferences", []).append(owner_reference(owner, controller=controller))
        try:
            return self.store.create(gvr, obj, kind=kind, namespace=namespace)
        except Exception:
            return None

    def update_status(self, gvr, obj: dict, status: dict) -> None:
        if obj.get("status") != status:
            self.store.update_status(gvr, obj["metadata"].get("namespace", ""), obj["metadata"]["name"], status, kind=obj.get("kind", ""))

    def list_namespaced(self, gvr, namespace: str) -> list[dict]:
        return self.store.list(gvr, namespace)[0]

    def get(self, gvr, namespace: str, name: str) -> dict | None:
        return self.store.get(gvr, namespace, name)


def condition(cond_type: str, status: bool, reason: str, message: str, *, observed_generation: int | None = None,
              last_transition: str | None = None) -> dict:
    result = {
        "type": cond_type,
        "status": "True" if status else "False",
        "reason": reason,
        "message": message,
        "lastTransitionTime": last_transition or now_iso(),
    }
    if observed_generation is not None:
        result["observedGeneration"] = observed_generation
    return result


def set_condition(conditions: list[dict], new_condition: dict) -> list[dict]:
    """Replace a condition, preserving lastTransitionTime if status is unchanged."""
    result = [c for c in conditions if c.get("type") != new_condition["type"]]
    for existing in conditions:
        if existing.get("type") == new_condition["type"] and existing.get("status") == new_condition.get("status"):
            new_condition = {**new_condition, "lastTransitionTime": existing.get("lastTransitionTime")}
            break
    result.append(new_condition)
    return result


def pod_template_hash(template: dict) -> str:
    from mini_k8s.apimachinery.meta import stable_hash
    return stable_hash(template, 10)


def resolve_int_or_percent(value: Any, total: int, *, round_up: bool = True) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value)
    if text.endswith("%"):
        try:
            pct = float(text[:-1])
        except ValueError:
            return 0
        raw = total * pct / 100.0
        return int(raw + 0.9999) if round_up else int(raw)
    try:
        return int(text)
    except ValueError:
        return 0
