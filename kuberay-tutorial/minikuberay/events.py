"""Kubernetes-style event recording used by controllers and ``describe``."""

from __future__ import annotations

import uuid

from . import constants as C
from .clock import Clock, RealClock, iso
from .models.core import new_event
from .store.store import ApiStore


class EventRecorder:
    def __init__(
        self,
        store: ApiStore,
        clock: Clock | None = None,
        source: str = "minikuberay-operator",
        history=None,
    ):
        self.store = store
        self.clock = clock or RealClock()
        self.source = source
        # Optional HistoryStore sink.  The real History Server collects Ray
        # events from sidecars; the teaching model records the operator's own
        # event stream, which is enough to replay what happened to a cluster.
        self.history = history

    def emit(
        self,
        obj: dict,
        event_type: str,
        reason: str,
        message: str,
        *,
        action: str = "Reconcile",
    ) -> dict | None:
        metadata = obj.get("metadata", {})
        name = f"{metadata.get('name', 'object')}.{uuid.uuid4().hex[:10]}"
        event = new_event(
            name,
            metadata.get("namespace", "default"),
            obj,
            reason,
            message,
            event_type=event_type,
            action=action,
            source=self.source,
        )
        timestamp = iso(self.clock.now())
        event["firstTimestamp"] = timestamp
        event["lastTimestamp"] = timestamp
        event["eventTime"] = timestamp
        if self.history is not None:
            try:
                self.history.record(obj, event_type, reason, message, timestamp=timestamp)
            except Exception:
                # History collection is best-effort and must never break
                # reconciliation.
                pass
        try:
            return self.store.create(C.KIND_EVENT, event)
        except Exception:
            # Observability must never break reconciliation.  The real recorder
            # also drops events rather than failing the controller.
            return None

    def normal(self, obj: dict, reason: str, message: str, **kwargs) -> dict | None:
        return self.emit(obj, "Normal", reason, message, **kwargs)

    def warning(self, obj: dict, reason: str, message: str, **kwargs) -> dict | None:
        return self.emit(obj, "Warning", reason, message, **kwargs)

    def list_for(self, obj: dict, namespace: str | None = None) -> list[dict]:
        ref_uid = obj.get("metadata", {}).get("uid")
        ref_name = obj.get("metadata", {}).get("name")
        events = self.store.list(C.KIND_EVENT, namespace or obj.get("metadata", {}).get("namespace"))
        matched = [
            event
            for event in events
            if (event.get("involvedObject") or {}).get("uid") == ref_uid
            or (
                (event.get("involvedObject") or {}).get("name") == ref_name
                and (event.get("involvedObject") or {}).get("kind") == obj.get("kind")
            )
        ]
        return sorted(matched, key=lambda event: event.get("lastTimestamp", ""))
