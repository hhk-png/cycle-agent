"""A small, durable History Server for MiniKubeRay.

The upstream KubeRay History Server collects Ray events (and periodically
snapshots state) so that a RayCluster can still be inspected after it has been
deleted.  MiniKubeRay models the same contract with a deliberately small data
plane:

* every controller event is attributed to a ``namespace/cluster`` session;
* periodic snapshots capture spec, status and the simulated Ray runtime;
* sessions survive deletion of the RayCluster;
* the API server exposes list/read/replay endpoints and the CLI can query them.

The store is in-memory by default.  Passing ``path`` makes it JSON-backed, which
is enough to demonstrate retention across process restarts without pretending to
be an object-store-backed production service.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .. import constants as C
from ..clock import Clock, RealClock, iso
from ..util import deep_copy

SESSION_SEPARATOR = "/"


@dataclass(frozen=True)
class HistoryEvent:
    session: str
    namespace: str
    cluster: str
    event_type: str
    reason: str
    message: str
    timestamp: str
    object_kind: str = ""
    object_name: str = ""

    def to_dict(self) -> dict:
        return {
            "session": self.session,
            "namespace": self.namespace,
            "cluster": self.cluster,
            "type": self.event_type,
            "reason": self.reason,
            "message": self.message,
            "timestamp": self.timestamp,
            "objectKind": self.object_kind,
            "objectName": self.object_name,
        }


@dataclass
class _Session:
    namespace: str
    cluster: str
    created_at: str
    deleted_at: str | None = None
    snapshot: dict = field(default_factory=dict)
    events: list[HistoryEvent] = field(default_factory=list)
    last_snapshot_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.namespace}{SESSION_SEPARATOR}{self.cluster}"

    def summary(self) -> dict:
        return {
            "namespace": self.namespace,
            "cluster": self.cluster,
            "session": self.key,
            "createdAt": self.created_at,
            "deletedAt": self.deleted_at,
            "eventCount": len(self.events),
            "lastEventTime": self.events[-1].timestamp if self.events else "",
            "lastSnapshotTime": self.last_snapshot_at,
            "state": (self.snapshot.get("status") or {}).get("state", ""),
            "rayVersion": (self.snapshot.get("spec") or {}).get("rayVersion", ""),
        }


class HistoryStore:
    """Event log + snapshots keyed by ``namespace/cluster``.

    Retention is bounded per session.  A production History Server writes to
    object storage and uses a session index; the semantics that matter for the
    tutorial -- attribution, snapshots, replay after delete -- are identical.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        max_events_per_session: int = 2000,
        path: str | Path | None = None,
    ):
        self.clock = clock or RealClock()
        self.max_events_per_session = max(1, int(max_events_per_session))
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        if self.path and self.path.exists():
            self.load()

    # ------------------------------------------------------------------ helpers
    def _now(self) -> str:
        return iso(self.clock.now())

    @staticmethod
    def _session_for_object(obj: dict) -> tuple[str, str] | None:
        kind = obj.get("kind", "")
        metadata = obj.get("metadata") or {}
        status = obj.get("status") or {}
        namespace = metadata.get("namespace", "default")
        if kind == C.KIND_RAYCLUSTER:
            name = metadata.get("name", "")
            return (namespace, name) if name else None
        if kind == C.KIND_RAYJOB:
            # Before the RayCluster exists there is no meaningful session.
            # Falling back to the RayJob name would create a pseudo-session
            # that never receives the job's real events.
            name = status.get("rayClusterName")
            return (namespace, name) if name else None
        if kind == C.KIND_RAYSERVICE:
            # RayService keeps the cluster name under active/pending status,
            # not at the top level.  Attribute events to the active cluster so
            # they land in the same session as the RayCluster's own events.
            active = (status.get("activeServiceStatus") or {}).get("rayClusterName")
            pending = (status.get("pendingServiceStatus") or {}).get("rayClusterName")
            name = active or pending
            return (namespace, name) if name else None
        if kind == C.KIND_RAYCRONJOB:
            # A RayCronJob owns RayJobs, not a RayCluster.  Keep its own
            # session; child RayJobs are attributed to their clusters.
            name = metadata.get("name", "")
            return (namespace, name) if name else None
        labels = metadata.get("labels") or {}
        cluster = (
            labels.get(C.LABEL_CLUSTER)
            or labels.get(C.LABEL_ORIGINATED_NAME)
            or (obj.get("involvedObject") or {}).get("name")
        )
        if kind == C.KIND_EVENT:
            involved = obj.get("involvedObject") or {}
            if involved.get("kind") == C.KIND_RAYCLUSTER:
                cluster = involved.get("name")
        return (namespace, cluster) if cluster else None

    def _session(self, namespace: str, cluster: str, *, create: bool = True) -> _Session | None:
        key = f"{namespace}{SESSION_SEPARATOR}{cluster}"
        session = self._sessions.get(key)
        if session is None and create:
            session = _Session(namespace=namespace, cluster=cluster, created_at=self._now())
            self._sessions[key] = session
        return session

    # ------------------------------------------------------------------- writes
    def record(
        self,
        obj: dict,
        event_type: str,
        reason: str,
        message: str,
        *,
        timestamp: str | None = None,
    ) -> HistoryEvent | None:
        target = self._session_for_object(obj)
        if target is None:
            return None
        namespace, cluster = target
        metadata = obj.get("metadata") or {}
        event = HistoryEvent(
            session=f"{namespace}{SESSION_SEPARATOR}{cluster}",
            namespace=namespace,
            cluster=cluster,
            event_type=event_type,
            reason=reason,
            message=message,
            timestamp=timestamp or self._now(),
            object_kind=obj.get("kind", ""),
            object_name=metadata.get("name", ""),
        )
        with self._lock:
            session = self._session(namespace, cluster)
            assert session is not None
            session.events.append(event)
            if len(session.events) > self.max_events_per_session:
                session.events = session.events[-self.max_events_per_session :]
        self._persist()
        return event

    def snapshot(self, cluster: dict, *, runtime: dict | None = None) -> dict:
        """Store a compact replayable snapshot for a RayCluster."""
        target = self._session_for_object(cluster)
        if target is None:
            return {}
        namespace, name = target
        metadata = cluster.get("metadata") or {}
        snapshot = {
            "apiVersion": cluster.get("apiVersion"),
            "kind": cluster.get("kind", C.KIND_RAYCLUSTER),
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": metadata.get("uid"),
                "creationTimestamp": metadata.get("creationTimestamp"),
                "deletionTimestamp": metadata.get("deletionTimestamp"),
                "labels": deep_copy(metadata.get("labels") or {}),
                "annotations": deep_copy(metadata.get("annotations") or {}),
            },
            "spec": deep_copy(cluster.get("spec") or {}),
            "status": deep_copy(cluster.get("status") or {}),
            "snapshotAt": self._now(),
        }
        if runtime is not None:
            snapshot["runtime"] = deep_copy(runtime)
        with self._lock:
            session = self._session(namespace, name)
            assert session is not None
            session.snapshot = snapshot
            session.last_snapshot_at = snapshot["snapshotAt"]
            if metadata.get("deletionTimestamp"):
                session.deleted_at = metadata["deletionTimestamp"]
        self._persist()
        return snapshot

    def mark_deleted(self, namespace: str, cluster: str) -> None:
        with self._lock:
            session = self._session(namespace, cluster, create=False)
            if session and not session.deleted_at:
                session.deleted_at = self._now()
        self._persist()

    # -------------------------------------------------------------------- reads
    def sessions(self) -> list[dict]:
        with self._lock:
            return sorted(
                (session.summary() for session in self._sessions.values()),
                key=lambda item: item.get("createdAt", ""),
                reverse=True,
            )

    def get(self, namespace: str, cluster: str) -> dict | None:
        with self._lock:
            session = self._session(namespace, cluster, create=False)
            if not session:
                return None
            return {
                **session.summary(),
                "snapshot": deep_copy(session.snapshot),
                "events": [event.to_dict() for event in session.events],
            }

    def events(
        self,
        namespace: str,
        cluster: str,
        *,
        limit: int | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        with self._lock:
            session = self._session(namespace, cluster, create=False)
            if not session:
                return []
            selected: Iterable[HistoryEvent] = session.events
            if kind:
                selected = [event for event in selected if event.object_kind == kind]
            result = [event.to_dict() for event in selected]
        if limit:
            result = result[-int(limit) :]
        return result

    def replay(self, namespace: str, cluster: str) -> dict | None:
        """Return the last snapshot plus its event timeline."""
        with self._lock:
            session = self._session(namespace, cluster, create=False)
            if not session:
                return None
            return {
                "session": session.key,
                "snapshot": deep_copy(session.snapshot),
                "events": [event.to_dict() for event in session.events],
                "deletedAt": session.deleted_at,
            }

    def search(self, query: str) -> list[dict]:
        query = (query or "").lower()
        if not query:
            return self.sessions()
        result = []
        with self._lock:
            for session in sorted(
                self._sessions.values(), key=lambda item: item.created_at, reverse=True
            ):
                if query in session.key.lower():
                    result.append(session.summary())
                    continue
                if any(
                    query in event.reason.lower() or query in event.message.lower()
                    for event in session.events
                ):
                    result.append(session.summary())
        return result

    # --------------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "sessions": [
                    {
                        **session.summary(),
                        "snapshot": deep_copy(session.snapshot),
                        "events": [event.to_dict() for event in session.events],
                    }
                    for session in self._sessions.values()
                ],
            }

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            self._sessions.clear()
            for item in payload.get("sessions") or []:
                session = _Session(
                    namespace=item.get("namespace", "default"),
                    cluster=item.get("cluster", ""),
                    created_at=item.get("createdAt", ""),
                    deleted_at=item.get("deletedAt"),
                    snapshot=item.get("snapshot") or {},
                    last_snapshot_at=item.get("lastSnapshotTime", ""),
                )
                session.events = [
                    HistoryEvent(
                        session=event.get("session", session.key),
                        namespace=event.get("namespace", session.namespace),
                        cluster=event.get("cluster", session.cluster),
                        event_type=event.get("type", "Normal"),
                        reason=event.get("reason", ""),
                        message=event.get("message", ""),
                        timestamp=event.get("timestamp", ""),
                        object_kind=event.get("objectKind", ""),
                        object_name=event.get("objectName", ""),
                    )
                    for event in item.get("events") or []
                ]
                if session.cluster:
                    self._sessions[session.key] = session

    def save(self) -> None:
        if not self.path:
            return
        payload = self.to_dict()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _persist(self) -> None:
        # Persisting on every event is acceptable for the teaching data volume;
        # the real History Server batches writes to object storage.
        if self.path:
            try:
                self.save()
            except OSError as exc:
                # History collection is best-effort: an unwritable path must
                # not take down the operator loop.
                print(f"history persistence disabled: {exc}")

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
        self._persist()


class HistoryServer:
    """Query facade used by the API server and CLI."""

    def __init__(self, store: HistoryStore | None = None):
        self.store = store or HistoryStore()

    def list_sessions(self) -> list[dict]:
        return self.store.sessions()

    def get_session(self, namespace: str, cluster: str) -> dict | None:
        return self.store.get(namespace, cluster)

    def events(self, namespace: str, cluster: str, *, limit: int | None = None, kind: str | None = None) -> list[dict]:
        return self.store.events(namespace, cluster, limit=limit, kind=kind)

    def replay(self, namespace: str, cluster: str) -> dict | None:
        return self.store.replay(namespace, cluster)

    def search(self, query: str) -> list[dict]:
        return self.store.search(query)
