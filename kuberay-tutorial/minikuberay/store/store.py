"""An in-memory Kubernetes-like object store with resource versions and watches.

It implements the small but important subset of API semantics that an operator
depends on:

* optimistic concurrency via ``metadata.resourceVersion``
* ``metadata.generation`` increments only when the spec changes
* the ``status`` subresource is written separately
* finalizers turn a delete into a two-phase operation
* watch streams carry ADDED / MODIFIED / DELETED events

The implementation is deliberately synchronous and lock-based.  Controllers can
run in threads, while tests call the same methods without any timing games.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from .. import constants as C
from ..clock import Clock, RealClock, iso
from ..errors import AlreadyExists, BadRequest, Conflict, NotFound, ResourceVersionTooOld
from ..util import canonical_json, deep_copy, labels_match

API_VERSIONS = {
    C.KIND_RAYCLUSTER: C.RAY_API_VERSION,
    C.KIND_RAYJOB: C.RAY_API_VERSION,
    C.KIND_RAYSERVICE: C.RAY_API_VERSION,
    C.KIND_RAYCRONJOB: C.RAY_API_VERSION,
    C.KIND_POD: C.CORE_API_VERSION,
    C.KIND_SERVICE: C.CORE_API_VERSION,
    C.KIND_EVENT: C.CORE_API_VERSION,
    C.KIND_SECRET: C.CORE_API_VERSION,
    C.KIND_PVC: C.CORE_API_VERSION,
    C.KIND_SERVICEACCOUNT: C.CORE_API_VERSION,
    C.KIND_JOB: C.BATCH_API_VERSION,
    C.KIND_ENDPOINTSLICE: C.DISCOVERY_API_VERSION,
    C.KIND_NETWORKPOLICY: C.NETWORKING_API_VERSION,
    C.KIND_HTTPROUTE: "gateway.networking.k8s.io/v1",
    C.KIND_GATEWAY: "gateway.networking.k8s.io/v1",
    C.KIND_PODGROUP: "scheduling.volcano.sh/v1beta1",
    C.KIND_INGRESS: "networking.k8s.io/v1",
}


@dataclass(frozen=True)
class WatchEvent:
    type: str
    object: dict
    resource_version: int


@dataclass(frozen=True)
class _LogEntry:
    resource_version: int
    event_type: str
    kind: str
    namespace: str
    name: str
    object: dict


class ApiStore:
    def __init__(self, clock: Clock | None = None, *, event_log_size: int = 20000):
        self.clock = clock or RealClock()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._objects: dict[tuple[str, str, str], dict] = {}
        self._resource_version = 0
        self._event_log: deque[_LogEntry] = deque(maxlen=event_log_size)
        self._closed = False

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _key(kind: str, namespace: str, name: str) -> tuple[str, str, str]:
        return kind, namespace or "default", name

    @staticmethod
    def _meta(obj: dict) -> dict:
        return obj.setdefault("metadata", {})

    def _next_rv(self) -> int:
        self._resource_version += 1
        return self._resource_version

    def _record(self, event_type: str, obj: dict) -> WatchEvent:
        metadata = obj["metadata"]
        entry = _LogEntry(
            resource_version=int(metadata["resourceVersion"]),
            event_type=event_type,
            kind=obj["kind"],
            namespace=metadata.get("namespace", "default"),
            name=metadata["name"],
            object=deep_copy(obj),
        )
        self._event_log.append(entry)
        self._changed.notify_all()
        return WatchEvent(event_type, deep_copy(obj), entry.resource_version)

    def _prepare_create(self, kind: str, obj: dict, namespace: str | None) -> dict:
        obj = deep_copy(obj)
        if not obj.get("kind"):
            obj["kind"] = kind
        if obj.get("kind") != kind:
            raise BadRequest(f"kind mismatch: expected {kind}, got {obj.get('kind')}")
        obj.setdefault("apiVersion", API_VERSIONS.get(kind, C.CORE_API_VERSION))
        metadata = self._meta(obj)
        if namespace:
            metadata["namespace"] = namespace
        metadata.setdefault("namespace", "default")
        if not metadata.get("name"):
            raise BadRequest("metadata.name is required")
        metadata["name"] = str(metadata["name"])
        # The API server owns UID.  Accepting a client-supplied UID would let
        # two objects share an identity, which breaks ownerReference/GC.
        metadata["uid"] = str(uuid.uuid4())
        # The API server, not the client, owns creationTimestamp.
        metadata["creationTimestamp"] = iso(self.clock.now())
        metadata["deletionTimestamp"] = metadata.get("deletionTimestamp")
        if metadata["deletionTimestamp"] is None:
            metadata.pop("deletionTimestamp", None)
        metadata.setdefault("labels", {})
        metadata.setdefault("annotations", {})
        metadata.setdefault("finalizers", [])
        metadata.setdefault("ownerReferences", [])
        metadata["generation"] = 1
        metadata["resourceVersion"] = str(self._next_rv())
        return obj

    # --------------------------------------------------------------------- CRUD
    def create(self, kind: str, obj: dict, namespace: str | None = None) -> dict:
        with self._lock:
            prepared = self._prepare_create(kind, obj, namespace)
            metadata = prepared["metadata"]
            key = self._key(kind, metadata["namespace"], metadata["name"])
            if key in self._objects:
                raise AlreadyExists(f"{kind} {metadata['namespace']}/{metadata['name']} already exists")
            self._objects[key] = prepared
            self._record("ADDED", prepared)
            return deep_copy(prepared)

    def get(self, kind: str, name: str, namespace: str = "default") -> dict:
        with self._lock:
            key = self._key(kind, namespace, name)
            if key not in self._objects:
                raise NotFound(f"{kind} {namespace}/{name} not found")
            return deep_copy(self._objects[key])

    def exists(self, kind: str, name: str, namespace: str = "default") -> bool:
        with self._lock:
            return self._key(kind, namespace, name) in self._objects

    def list(
        self,
        kind: str,
        namespace: str | None = None,
        *,
        label_selector: dict | None = None,
        field_selector: dict | None = None,
    ) -> list[dict]:
        with self._lock:
            result = []
            for (item_kind, item_ns, _), obj in self._objects.items():
                if item_kind != kind:
                    continue
                if namespace is not None and item_ns != namespace:
                    continue
                if label_selector and not labels_match(label_selector, obj.get("metadata", {}).get("labels") or {}):
                    continue
                if field_selector:
                    fields = {
                        "metadata.name": obj.get("metadata", {}).get("name"),
                        "metadata.namespace": item_ns,
                        "status.phase": obj.get("status", {}).get("phase"),
                    }
                    if any(fields.get(key) != value for key, value in field_selector.items()):
                        continue
                result.append(deep_copy(obj))
            result.sort(key=lambda item: (item.get("metadata", {}).get("namespace", ""), item.get("metadata", {}).get("name", "")))
            return result

    def update(
        self,
        kind: str,
        obj: dict,
        *,
        subresource: str | None = None,
        expected_resource_version: str | int | None = None,
    ) -> dict:
        with self._lock:
            obj = deep_copy(obj)
            if obj.get("kind") and obj["kind"] != kind:
                raise BadRequest(f"kind mismatch: expected {kind}, got {obj['kind']}")
            metadata = self._meta(obj)
            key = self._key(kind, metadata.get("namespace", "default"), metadata.get("name", ""))
            if key not in self._objects:
                raise NotFound(f"{kind} {key[1]}/{key[2]} not found")
            current = self._objects[key]
            if obj.get("apiVersion") and obj["apiVersion"] != current.get("apiVersion"):
                raise BadRequest("apiVersion is immutable")
            supplied_rv = expected_resource_version if expected_resource_version is not None else metadata.get("resourceVersion")
            if supplied_rv not in (None, "", "0") and int(supplied_rv) != int(current["metadata"]["resourceVersion"]):
                raise Conflict(
                    f"the object has been modified; please apply your changes to the latest version "
                    f"(current {current['metadata']['resourceVersion']}, supplied {supplied_rv})"
                )

            if subresource == "status":
                merged = deep_copy(current)
                merged["status"] = deep_copy(obj.get("status", {}))
                # Status writers must not smuggle spec changes.
            else:
                merged = obj
                merged["metadata"]["uid"] = current["metadata"]["uid"]
                merged["metadata"]["creationTimestamp"] = current["metadata"].get("creationTimestamp")
                merged["metadata"]["namespace"] = current["metadata"].get("namespace", "default")
                merged["metadata"]["deletionTimestamp"] = current["metadata"].get("deletionTimestamp")
                merged["metadata"].setdefault("finalizers", current["metadata"].get("finalizers", []))
                merged["metadata"].setdefault("ownerReferences", current["metadata"].get("ownerReferences", []))
                # Metadata maps follow PUT/merge-patch semantics: an explicit
                # removal (or omission) stays removed instead of being
                # resurrected from the previous object.
                merged["metadata"]["labels"] = deep_copy(merged["metadata"].get("labels") or {})
                merged["metadata"]["annotations"] = deep_copy(merged["metadata"].get("annotations") or {})
                old_spec = canonical_json(current.get("spec"))
                new_spec = canonical_json(merged.get("spec"))
                generation = int(current["metadata"].get("generation", 1))
                if old_spec != new_spec:
                    generation += 1
                merged["metadata"]["generation"] = generation
                # Internal controllers (e.g. the simulated kubelet) update
                # spec and status together.  The API server strips status from
                # normal PUT/PATCH requests before calling this method, so
                # external clients still cannot smuggle status.
                merged["status"] = (
                    deep_copy(merged.get("status")) if "status" in merged else deep_copy(current.get("status", {}))
                )

            merged["metadata"]["resourceVersion"] = str(self._next_rv())
            self._objects[key] = merged

            # A finalizer-free object that is being deleted is removed after the
            # final update, matching kube-apiserver behaviour.
            if merged["metadata"].get("deletionTimestamp") and not merged["metadata"].get("finalizers"):
                del self._objects[key]
                self._record("DELETED", merged)
                return deep_copy(merged)

            self._record("MODIFIED", merged)
            return deep_copy(merged)

    def update_status(self, obj: dict, *, expected_resource_version: str | int | None = None) -> dict:
        return self.update(obj["kind"], obj, subresource="status", expected_resource_version=expected_resource_version)

    def patch(
        self,
        kind: str,
        name: str,
        namespace: str,
        patch: dict,
        *,
        patch_type: str = "merge",
        subresource: str | None = None,
    ) -> dict:
        with self._lock:
            current = self.get(kind, name, namespace)
            patch = deep_copy(patch)
            metadata_patch = patch.get("metadata") or {}
            if metadata_patch.get("name") and metadata_patch["name"] != name:
                raise BadRequest("metadata.name is immutable")
            if metadata_patch.get("namespace") and metadata_patch["namespace"] != namespace:
                raise BadRequest("metadata.namespace is immutable")
            if subresource == "status":
                # Status writers may only touch status.
                patch = {"status": patch.get("status") or {}}
            else:
                # The main resource must not smuggle status changes; status is
                # a separate subresource.
                patch.pop("status", None)
            if patch_type == "merge":
                from ..models.meta import merge_patch

                patched = merge_patch(current, patch)
            elif patch_type == "json":
                raise BadRequest("JSON patch is not implemented; use merge or strategic")
            elif patch_type in ("strategic", "apply"):
                # A pragmatic approximation: recursive dict merge, lists replace.
                patched = _merge_dicts(current, patch)
            else:
                raise BadRequest(f"unsupported patch type: {patch_type}")
            if subresource == "status":
                return self.update(kind, patched, subresource="status")
            return self.update(kind, patched)

    def delete(self, kind: str, name: str, namespace: str = "default", *, force: bool = False) -> dict:
        with self._lock:
            key = self._key(kind, namespace, name)
            if key not in self._objects:
                raise NotFound(f"{kind} {namespace}/{name} not found")
            current = self._objects[key]
            if force or not current["metadata"].get("finalizers"):
                del self._objects[key]
                tombstone = deep_copy(current)
                tombstone["metadata"]["resourceVersion"] = str(self._next_rv())
                self._record("DELETED", tombstone)
                return deep_copy(tombstone)
            if not current["metadata"].get("deletionTimestamp"):
                current = deep_copy(current)
                current["metadata"]["deletionTimestamp"] = iso(self.clock.now())
                current["metadata"]["resourceVersion"] = str(self._next_rv())
                self._objects[key] = current
                self._record("MODIFIED", current)
            return deep_copy(current)

    def remove_finalizer(self, kind: str, name: str, namespace: str, finalizer: str) -> dict:
        with self._lock:
            current = self.get(kind, name, namespace)
            finalizers = current["metadata"].setdefault("finalizers", [])
            if finalizer in finalizers:
                finalizers.remove(finalizer)
            return self.update(kind, current)

    # -------------------------------------------------------------------- watch
    def check_watch_resource_version(self, since_resource_version: int) -> None:
        """Raise 410 when the requested RV has fallen out of the event log.

        Informers must relist when they see this instead of silently missing
        DELETED events (which would leave stale objects forever).
        """
        with self._lock:
            if since_resource_version <= 0 or not self._event_log:
                return
            oldest = self._event_log[0].resource_version
            if since_resource_version < oldest - 1:
                raise ResourceVersionTooOld(
                    f"too old resource version: {since_resource_version} (oldest retained {oldest})"
                )

    def watch(
        self,
        kind: str,
        namespace: str | None = None,
        *,
        since_resource_version: int = 0,
        include_current: bool = True,
    ) -> Iterator[WatchEvent]:
        """Yield events forever (until the store closes).

        With the default ``since_resource_version=0`` the stream starts with an
        ADDED event for every current object, then follows changes.  Supplying a
        resource version replays matching events newer than it, which is what a
        reconnecting informer does.
        """
        self.check_watch_resource_version(since_resource_version)
        with self._lock:
            if since_resource_version <= 0 and include_current:
                current_objects = [
                    obj
                    for (item_kind, item_ns, _), obj in self._objects.items()
                    if item_kind == kind and (namespace is None or item_ns == namespace)
                ]
                current = [WatchEvent("ADDED", deep_copy(obj), int(obj["metadata"]["resourceVersion"])) for obj in current_objects]
                start_rv = self._resource_version
            else:
                current = []
                start_rv = since_resource_version

        for event in current:
            yield event

        cursor = start_rv
        while True:
            with self._lock:
                matching: list[_LogEntry] = []
                while not self._closed:
                    matching = [entry for entry in self._event_log if entry.resource_version > cursor]
                    if matching:
                        break
                    if not self._changed.wait(timeout=1.0):
                        # Periodic wake-up keeps generators responsive to close().
                        continue
                if self._closed and not matching:
                    return
                pending = [entry for entry in matching if entry.kind == kind and (namespace is None or entry.namespace == namespace)]
                # Advance past all entries (not only matching ones) so unrelated
                # kinds do not cause repeated scans.
                cursor = max((entry.resource_version for entry in matching), default=cursor)
                events = [
                    WatchEvent(entry.event_type, deep_copy(entry.object), entry.resource_version)
                    for entry in pending
                ]
            for event in events:
                yield event

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._changed.notify_all()

    # ----------------------------------------------------------------- utilities
    @property
    def resource_version(self) -> int:
        return self._resource_version

    def dump(self) -> dict:
        with self._lock:
            return {
                "resourceVersion": self._resource_version,
                "objects": [deep_copy(obj) for obj in self._objects.values()],
            }

    def load(self, snapshot: dict) -> None:
        with self._lock:
            self._objects.clear()
            self._event_log.clear()
            self._resource_version = int(snapshot.get("resourceVersion", 0))
            for obj in snapshot.get("objects", []):
                metadata = obj["metadata"]
                key = self._key(obj["kind"], metadata.get("namespace", "default"), metadata["name"])
                self._objects[key] = deep_copy(obj)
            self._changed.notify_all()


def _merge_dicts(base: dict, patch: dict) -> dict:
    result = deep_copy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = deep_copy(value)
    return result
