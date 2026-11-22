"""A teaching implementation of Server-Side Apply (SSA).

The observable contract implemented here is:

* a field manager owns the fields it applies;
* another manager that applies a different value to an owned field gets 409;
* ``force=true`` takes ownership away from the other manager;
* fields that the manager owned in its previous apply but omitted now are
  removed when no other manager owns them;
* ``metadata.managedFields`` is server-owned and never accepted from clients.

Deliberate simplifications (documented in chapters 4 and 35):

* lists are owned atomically instead of using associative list keys;
* schema defaults are not tracked as separate managers;
* status is a separate subresource and is ignored on the main resource;
* only Apply managers participate in conflict detection; plain PUT/PATCH keep
  the existing managedFields untouched.
"""

from __future__ import annotations

from typing import Any, Iterable

from mini_k8s.apimachinery.meta import deep_copy, now_iso
from mini_k8s.errors import bad_request, conflict

APPLY_CONTENT_TYPE = "application/apply-patch+yaml"

# Fields the API server (or the store) owns.  A client may send them, but they
# must not become part of the apply patch or of field ownership.
SERVER_METADATA = {
    "managedFields",
    "resourceVersion",
    "uid",
    "creationTimestamp",
    "generation",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
    "selfLink",
}

_IDENTITY_PATHS = {
    ("apiVersion",),
    ("kind",),
    ("metadata", "name"),
    ("metadata", "namespace"),
}

_MISSING = object()


def _flatten(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Flatten a JSON object into leaf paths.

    Lists are leaves: owning ``spec.containers`` owns the whole list.  Empty
    maps are ignored because they own no concrete field.
    """
    if isinstance(value, dict):
        if not value:
            return {}
        result: dict[tuple[str, ...], Any] = {}
        for key, child in value.items():
            result.update(_flatten(child, prefix + (str(key),)))
        return result
    if not prefix:
        return {}
    return {prefix: value}


def _get(obj: Any, path: tuple[str, ...]) -> Any:
    current = obj
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _set(obj: dict, path: tuple[str, ...], value: Any) -> None:
    current = obj
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[path[-1]] = deep_copy(value)


def _delete(obj: dict, path: tuple[str, ...]) -> None:
    current: Any = obj
    for part in path[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(path[-1], None)


def _merge(live: Any, patch: Any) -> Any:
    """JSON merge semantics used by apply: maps merge, lists replace."""
    if not isinstance(patch, dict):
        return deep_copy(patch)
    result = deep_copy(live) if isinstance(live, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge(result.get(key), value)
    return result


def _related(left: Iterable[str], right: tuple[str, ...]) -> bool:
    """Whether an owned path and an applied path overlap (equal or parent)."""
    right_list = tuple(right)
    for path in left:
        path = tuple(path)
        if path == right_list:
            return True
        shorter = min(len(path), len(right_list))
        if path[:shorter] == right_list[:shorter]:
            return True
    return False


def _fields_tree(paths: Iterable[tuple[str, ...]]) -> dict:
    tree: dict[str, Any] = {}
    for path in sorted(paths):
        node = tree
        for part in path:
            node = node.setdefault(f"f:{part}", {})
    return tree


def _parse_fields_tree(tree: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if not isinstance(tree, dict) or not tree:
        return {prefix} if prefix else set()
    paths: set[tuple[str, ...]] = set()
    for key, child in tree.items():
        if key.startswith("f:"):
            name = key[2:]
        elif key.startswith("k:"):
            # Associative list keys are not produced by this implementation.
            # Treat the parent field as atomic if an external object contains
            # them, so we never silently drop ownership.
            paths.add(prefix)
            continue
        elif key.startswith("v:"):
            continue
        else:
            name = key
        paths |= _parse_fields_tree(child, prefix + (name,))
    return paths


def _ownership(obj: dict) -> dict[str, set[tuple[str, ...]]]:
    owners: dict[str, set[tuple[str, ...]]] = {}
    for entry in obj.get("metadata", {}).get("managedFields", []) or []:
        manager = entry.get("manager")
        if not manager:
            continue
        owners.setdefault(manager, set()).update(_parse_fields_tree(entry.get("fieldsV1", {})))
    return owners


def _format_path(path: tuple[str, ...]) -> str:
    return "." + ".".join(path)


def manager_of(obj: dict, manager: str) -> dict | None:
    for entry in obj.get("metadata", {}).get("managedFields", []) or []:
        if entry.get("manager") == manager:
            return entry
    return None


def apply_object(
    live: dict | None,
    patch: dict,
    *,
    manager: str,
    force: bool = False,
    api_version: str,
    kind: str,
    namespace: str | None = None,
) -> dict:
    """Return the merged object with updated ``metadata.managedFields``.

    Raises a 409 ``Conflict`` when a field owned by another manager is being
    changed without ``force``.  The caller is responsible for persistence and
    for admission (the API server does both).
    """
    if not manager:
        raise bad_request("fieldManager is required for server-side apply")
    if not isinstance(patch, dict):
        raise bad_request("apply patch must be a JSON object")

    patch = deep_copy(patch)
    meta = patch.setdefault("metadata", {})
    if not isinstance(meta, dict):
        raise bad_request("metadata must be an object")
    for field in list(meta):
        if field in SERVER_METADATA:
            meta.pop(field)
    # Status has its own subresource; a normal apply must not write it.
    patch.pop("status", None)
    patch.setdefault("apiVersion", api_version)
    patch.setdefault("kind", kind)
    if namespace:
        meta.setdefault("namespace", namespace)

    if live is None:
        live_obj: dict = {
            "apiVersion": api_version,
            "kind": kind,
            "metadata": {"name": meta.get("name")},
        }
        if namespace:
            live_obj["metadata"]["namespace"] = namespace
    else:
        live_obj = deep_copy(live)

    applied = {
        path: value
        for path, value in _flatten(patch).items()
        if path not in _IDENTITY_PATHS
    }
    owners = _ownership(live_obj)

    # Conflict detection compares the applied value with the live value.  Two
    # managers may converge on the same value without conflict.
    conflicts: list[tuple[str, tuple[str, ...]]] = []
    for path, value in applied.items():
        for other, paths in owners.items():
            if other == manager:
                continue
            if not _related(paths, path):
                continue
            current = _get(live_obj, path)
            if current is _MISSING or current != value:
                conflicts.append((other, path))
                break
    if conflicts and not force:
        rendered = ", ".join(f'"{owner}": {_format_path(path)}' for owner, path in conflicts[:5])
        more = f" (and {len(conflicts) - 5} more)" if len(conflicts) > 5 else ""
        raise conflict(
            f"Apply failed with {len(conflicts)} conflict(s): conflict with {rendered}{more}"
        )

    merged = _merge(live_obj, patch)

    # Fields this manager owned before but omitted now are removed if no other
    # manager owns them.  This is the part that makes apply declarative rather
    # than a plain merge patch.
    previous = owners.get(manager, set())
    others = {path for other, paths in owners.items() if other != manager for path in paths}
    for path in sorted(previous - set(applied), key=len, reverse=True):
        if not _related(others, path):
            _delete(merged, path)

    # Recompute ownership: other managers keep their fields unless force steals
    # an overlapping one; this manager owns every field it successfully applied.
    new_owners: dict[str, set[tuple[str, ...]]] = {
        other: set(paths) for other, paths in owners.items() if other != manager
    }
    # A null value deletes the field; the applier does not keep ownership of a
    # field that no longer exists.
    held = {path for path, value in applied.items() if value is not None}
    for path in held:
        overlapping = [other for other, paths in new_owners.items() if _related(paths, path)]
        if overlapping and not force:
            continue
        for other in overlapping:
            new_owners[other] = {
                owned for owned in new_owners[other] if not _related({owned}, path)
            }
            if not new_owners[other]:
                del new_owners[other]
        new_owners.setdefault(manager, set()).add(path)

    old_entries = {
        entry.get("manager"): entry
        for entry in live_obj.get("metadata", {}).get("managedFields", []) or []
        if entry.get("manager")
    }
    entries = []
    for owner in sorted(new_owners):
        paths = new_owners[owner]
        if not paths:
            continue
        if owner == manager:
            entries.append({
                "manager": owner,
                "operation": "Apply",
                "apiVersion": api_version,
                "time": now_iso(),
                "fieldsType": "FieldsV1",
                "fieldsV1": _fields_tree(paths),
            })
        else:
            base = deep_copy(old_entries.get(owner, {}))
            base.update({"manager": owner, "fieldsType": "FieldsV1",
                         "fieldsV1": _fields_tree(paths)})
            base.setdefault("operation", "Apply")
            entries.append(base)
    merged.setdefault("metadata", {})["managedFields"] = entries
    return merged
