"""A small versioned object store with watch semantics.

It plays the role of etcd plus the API server's storage layer: every mutation
receives a monotonically increasing resourceVersion, optimistic concurrency is
enforced with that version, and watchers receive ordered events.

This is deliberately in-memory.  The interface is the interesting part; a real
implementation would put etcd behind it and add compaction/revision semantics.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator

from mini_k8s.apimachinery.meta import (
    deep_copy, default_metadata, json_patch, merge_patch, new_uid, now_iso,
    validate_name, validate_namespace,
)
from mini_k8s.apimachinery.selectors import match_field_selector, match_labels
from mini_k8s.errors import already_exists, bad_request, conflict, not_found

GVR = tuple[str, str, str]


@dataclass(frozen=True)
class WatchEvent:
    type: str  # ADDED, MODIFIED, DELETED, BOOKMARK, ERROR
    object: dict
    resource_version: int
    gvr: GVR | None = None


class Watcher:
    def __init__(self, store: "ResourceStore", gvr: GVR, namespace: str | None, start_rv: int):
        self.store = store
        self.gvr = gvr
        self.namespace = namespace
        self.start_rv = start_rv
        self.queue: queue.Queue[WatchEvent | None] = queue.Queue(maxsize=1000)
        self.closed = False
        self.overflowed = False

    def push(self, event: WatchEvent) -> None:
        if self.closed or self.overflowed:
            return
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            # A slow watcher is disconnected, like the real API server's
            # "too slow" behaviour.  Drop the backlog so the ERROR can be
            # delivered immediately; the client must relist and resume.
            self.overflowed = True
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self.queue.put_nowait(
                    WatchEvent("ERROR", {"code": 410, "message": "watch closed: client too slow"},
                               event.resource_version)
                )
            except queue.Full:  # pragma: no cover - queue was just drained
                pass

    def __iter__(self) -> Iterator[WatchEvent]:
        return self.events()

    def events(self) -> Iterator[WatchEvent]:
        while True:
            if self.closed and self.queue.empty():
                return
            try:
                event = self.queue.get(timeout=1.0)
            except queue.Empty:
                if self.closed:
                    return
                continue
            if event is None:
                return
            yield event
            if event.type == "ERROR":
                return

    def close(self) -> None:
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass


class ResourceStore:
    def __init__(self, *, resource_version: int = 1000, event_history: int = 5000):
        self._lock = threading.RLock()
        self._data: dict[GVR, dict[tuple[str, str], dict]] = {}
        self._resource_version = resource_version
        self._watchers: list[Watcher] = []
        self._events: deque[WatchEvent] = deque(maxlen=event_history)
        self._event_conditions: list[threading.Condition] = []

    # ------------------------------------------------------------------ reads
    @property
    def resource_version(self) -> int:
        return self._resource_version

    def _bucket(self, gvr: GVR) -> dict[tuple[str, str], dict]:
        return self._data.setdefault(gvr, {})

    def get(self, gvr: GVR, namespace: str, name: str) -> dict | None:
        with self._lock:
            obj = self._bucket(gvr).get((namespace, name))
            return deep_copy(obj) if obj is not None else None

    def must_get(self, gvr: GVR, namespace: str, name: str, kind: str = "object") -> dict:
        obj = self.get(gvr, namespace, name)
        if obj is None:
            raise not_found(kind, name)
        return obj

    def list(
        self,
        gvr: GVR,
        namespace: str | None = None,
        *,
        label_selector: str | None = None,
        field_selector: str | None = None,
        limit: int | None = None,
        continue_token: str | None = None,
    ) -> tuple[list[dict], str | None]:
        with self._lock:
            objects = []
            for (ns, name), obj in self._bucket(gvr).items():
                if namespace is not None and ns != namespace:
                    continue
                if label_selector and not match_labels(obj.get("metadata", {}).get("labels"), label_selector):
                    continue
                if field_selector and not match_field_selector(obj, field_selector):
                    continue
                objects.append(deep_copy(obj))
            objects.sort(key=lambda o: (o["metadata"].get("namespace", ""), o["metadata"]["name"]))
            token: str | None = None
            if continue_token:
                try:
                    start = int(continue_token)
                except ValueError as exc:
                    raise bad_request("invalid continue token") from exc
                objects = objects[start:]
            if limit is not None and limit > 0 and len(objects) > limit:
                offset = int(continue_token or 0)
                token = str(offset + limit)
                objects = objects[:limit]
            return objects, token

    def count(self, gvr: GVR, namespace: str | None = None) -> int:
        with self._lock:
            return sum(1 for ns, _ in self._bucket(gvr) if namespace is None or ns == namespace)

    def keys(self, gvr: GVR) -> list[tuple[str, str]]:
        with self._lock:
            return sorted(self._bucket(gvr))

    # --------------------------------------------------------------- mutations
    def _next_rv(self) -> int:
        self._resource_version += 1
        return self._resource_version

    @staticmethod
    def _api_version(gvr: GVR) -> str:
        group, version, _plural = gvr
        return version if not group else f"{group}/{version}"

    def _emit(self, event_type: str, obj: dict, rv: int, gvr: GVR) -> None:
        event = WatchEvent(event_type, deep_copy(obj), rv, gvr)
        self._events.append(event)
        for watcher in list(self._watchers):
            if watcher.gvr != gvr:
                continue
            if watcher.namespace is not None and obj.get("metadata", {}).get("namespace") != watcher.namespace:
                continue
            if rv <= watcher.start_rv:
                continue
            watcher.push(event)
            if watcher.closed:
                self._watchers.remove(watcher)
        for condition in list(self._event_conditions):
            with condition:
                condition.notify_all()

    def create(self, gvr: GVR, obj: dict, *, kind: str, namespace: str | None = None) -> dict:
        if not isinstance(obj, dict):
            raise bad_request("object must be a JSON object")
        obj = deep_copy(obj)
        namespace = namespace if namespace is not None else obj.get("metadata", {}).get("namespace", "default")
        if namespace:
            validate_namespace(namespace)
        meta = default_metadata(obj, namespace=namespace, kind=kind)
        validate_name(meta.get("name", ""), kind)
        with self._lock:
            bucket = self._bucket(gvr)
            key = (namespace, meta["name"])
            if key in bucket:
                raise already_exists(kind, meta["name"])
            rv = self._next_rv()
            # Server-owned metadata must never be accepted from the client:
            # otherwise uid/resourceVersion/generation can be spoofed.
            meta["resourceVersion"] = str(rv)
            meta["uid"] = new_uid()
            meta["creationTimestamp"] = now_iso()
            meta["generation"] = 1
            meta.pop("deletionTimestamp", None)
            meta.pop("deletionGracePeriodSeconds", None)
            expected_api = self._api_version(gvr)
            if obj.get("apiVersion") not in (None, "") and obj["apiVersion"] != expected_api:
                raise bad_request(f"apiVersion {obj['apiVersion']!r} does not match {expected_api!r}")
            if obj.get("kind") not in (None, "") and obj["kind"] != kind:
                raise bad_request(f"kind {obj['kind']!r} does not match {kind!r}")
            obj["apiVersion"] = expected_api
            obj["kind"] = kind
            bucket[key] = obj
            self._emit("ADDED", obj, rv, gvr)
            return deep_copy(obj)

    def update(self, gvr: GVR, obj: dict, *, kind: str, namespace: str | None = None) -> dict:
        if not isinstance(obj, dict):
            raise bad_request("object must be a JSON object")
        obj = deep_copy(obj)
        meta = default_metadata(obj)
        name = meta.get("name", "")
        namespace = namespace if namespace is not None else meta.get("namespace", "default")
        meta["namespace"] = namespace
        with self._lock:
            bucket = self._bucket(gvr)
            key = (namespace, name)
            existing = bucket.get(key)
            if existing is None:
                raise not_found(kind, name)
            supplied_rv = meta.get("resourceVersion")
            if supplied_rv and str(supplied_rv) != existing["metadata"]["resourceVersion"]:
                raise conflict(
                    f'Operation cannot be fulfilled on {kind.lower()}s "{name}": '
                    f'the object has been modified; please apply your changes to the latest version and try again'
                )
            rv = self._next_rv()
            meta["resourceVersion"] = str(rv)
            meta["uid"] = existing["metadata"]["uid"]
            meta["creationTimestamp"] = existing["metadata"]["creationTimestamp"]
            previous_generation = int(existing["metadata"].get("generation", 1))
            if obj.get("spec") != existing.get("spec"):
                meta["generation"] = previous_generation + 1
            else:
                meta["generation"] = previous_generation
            # Deletion state is server-owned.  An update may preserve an
            # existing deletionTimestamp (controllers clear finalizers this
            # way), but a client must not be able to inject one and thereby
            # delete a healthy object without calling DELETE.
            meta.pop("deletionTimestamp", None)
            meta.pop("deletionGracePeriodSeconds", None)
            if existing["metadata"].get("deletionTimestamp"):
                meta["deletionTimestamp"] = existing["metadata"]["deletionTimestamp"]
                if existing["metadata"].get("deletionGracePeriodSeconds") is not None:
                    meta["deletionGracePeriodSeconds"] = existing["metadata"]["deletionGracePeriodSeconds"]
            obj["apiVersion"] = existing.get("apiVersion", obj.get("apiVersion"))
            obj["kind"] = existing.get("kind", obj.get("kind"))
            bucket[key] = obj
            self._emit("MODIFIED", obj, rv, gvr)
            # Clearing the last finalizer on a terminating object completes
            # deletion, exactly as the real API server does.
            if meta.get("deletionTimestamp") and not (meta.get("finalizers") or []):
                removed = bucket.pop(key)
                delete_rv = self._next_rv()
                removed["metadata"]["resourceVersion"] = str(delete_rv)
                self._emit("DELETED", removed, delete_rv, gvr)
                return deep_copy(removed)
            return deep_copy(obj)

    def update_status(self, gvr: GVR, namespace: str, name: str, status: dict, *, kind: str) -> dict:
        with self._lock:
            existing = self.get(gvr, namespace, name)
            if existing is None:
                raise not_found(kind, name)
            existing["status"] = deep_copy(status)
            return self.update(gvr, existing, kind=kind, namespace=namespace)

    def patch(self, gvr: GVR, namespace: str, name: str, patch: Any, *, kind: str,
              patch_type: str = "merge") -> dict:
        with self._lock:
            existing = self.get(gvr, namespace, name)
            if existing is None:
                raise not_found(kind, name)
            if patch_type == "json":
                patched = json_patch(existing, patch)
            else:
                patched = merge_patch(existing, patch)
            return self.update(gvr, patched, kind=kind, namespace=namespace)

    def delete(self, gvr: GVR, namespace: str, name: str, *, kind: str,
               force: bool = False, propagation: str = "background") -> bool:
        """Delete an object.

        Returns True when the object was physically removed.  Objects with
        finalizers receive a deletionTimestamp and remain until finalizers are
        cleared.  ``propagation="foreground"`` is recorded but garbage
        collection is performed asynchronously by the GC controller.
        """
        with self._lock:
            bucket = self._bucket(gvr)
            key = (namespace, name)
            existing = bucket.get(key)
            if existing is None:
                raise not_found(kind, name)
            finalizers = existing["metadata"].get("finalizers") or []
            if finalizers and not force:
                if not existing["metadata"].get("deletionTimestamp"):
                    existing = deep_copy(existing)
                    existing["metadata"]["deletionTimestamp"] = now_iso()
                    existing["metadata"]["deletionGracePeriodSeconds"] = 0
                    existing["metadata"]["resourceVersion"] = str(self._next_rv())
                    bucket[key] = existing
                    self._emit("MODIFIED", existing, int(existing["metadata"]["resourceVersion"]), gvr)
                return False
            removed = bucket.pop(key)
            rv = self._next_rv()
            removed["metadata"]["resourceVersion"] = str(rv)
            removed["metadata"]["deletionTimestamp"] = removed["metadata"].get("deletionTimestamp") or now_iso()
            removed["metadata"]["deletionGracePeriodSeconds"] = 0
            self._emit("DELETED", removed, rv, gvr)
            return True

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._events.clear()

    # ----------------------------------------------------------------- watchers
    def watch(self, gvr: GVR, namespace: str | None = None, *, resource_version: int | str | None = None,
              send_initial: bool = False) -> Watcher:
        try:
            start_rv = int(resource_version) if resource_version not in (None, "") else self._resource_version
        except (TypeError, ValueError) as exc:
            raise bad_request("invalid resourceVersion for watch") from exc
        watcher = Watcher(self, gvr, namespace, start_rv)
        with self._lock:
            oldest = self._events[0].resource_version if self._events else self._resource_version
            initial_snapshot = send_initial and start_rv <= 0
            if start_rv < oldest - 1 and not initial_snapshot:
                # The requested version has fallen out of the retained event
                # history.  Signal 410 so the client knows to re-list instead
                # of silently continuing with a gap.
                watcher.queue.put_nowait(WatchEvent(
                    "ERROR", {"code": 410, "message": f"too old resource version: {start_rv} ({oldest})"}, oldest
                ))
                return watcher
            self._watchers.append(watcher)
            # Replay events retained in the history buffer.  resourceVersion=0
            # is a snapshot request, not a replay from the beginning of time.
            for event in ([] if initial_snapshot else self._events):
                if event.resource_version <= start_rv:
                    continue
                if event.type == "DELETED" and event.object.get("metadata", {}).get("namespace") != namespace and namespace is not None:
                    continue
                # Identify the resource through the event's apiVersion/kind is
                # insufficient for same-kind resources in different groups, so
                # we store the GVR in a side table on emission.
                if event.gvr != gvr:
                    continue
                if namespace is not None and event.object.get("metadata", {}).get("namespace") != namespace:
                    continue
                watcher.push(event)
            if send_initial:
                for (ns, _name), obj in sorted(self._bucket(gvr).items()):
                    if namespace is not None and ns != namespace:
                        continue
                    if initial_snapshot or int(obj["metadata"]["resourceVersion"]) > start_rv:
                        watcher.push(WatchEvent("ADDED", deep_copy(obj), int(obj["metadata"]["resourceVersion"]), gvr))
        return watcher

    def wait_for_event(self, timeout: float = 2.0) -> bool:
        """Block until any mutation occurs.  Controllers use this as a wake-up."""
        condition = threading.Condition()
        with self._lock:
            self._event_conditions.append(condition)
        try:
            with condition:
                return condition.wait(timeout=timeout)
        finally:
            with self._lock:
                if condition in self._event_conditions:
                    self._event_conditions.remove(condition)
