"""Object metadata, JSON merge-patch, and deterministic serialisation helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from mini_k8s.errors import bad_request

DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
DNS1123_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def new_uid() -> str:
    return str(uuid.uuid4())


def deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(value: Any, length: int = 10) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()[:length]


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON Merge Patch."""
    if not isinstance(patch, dict):
        return deep_copy(patch)
    result = deep_copy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def json_patch(target: Any, operations: list[dict]) -> Any:
    """A deliberately small JSON Patch (RFC 6902) implementation."""
    result = deep_copy(target)
    for operation in operations:
        op = operation.get("op")
        path = [part.replace("~1", "/").replace("~0", "~") for part in operation.get("path", "").split("/")[1:]]
        if not path:
            raise bad_request("JSON Patch root replacement is not supported")
        parent = result
        for part in path[:-1]:
            if isinstance(parent, list):
                parent = parent[int(part)]
            else:
                parent = parent.setdefault(part, {})
        key = path[-1]
        if op in ("add", "replace", "test"):
            if op == "test":
                current = parent[key] if isinstance(parent, dict) else parent[int(key)]
                if current != operation.get("value"):
                    raise bad_request("JSON Patch test operation failed")
                continue
            if isinstance(parent, list):
                if op == "add":
                    parent.insert(int(key), deep_copy(operation.get("value")))
                else:
                    parent[int(key)] = deep_copy(operation.get("value"))
            else:
                parent[key] = deep_copy(operation.get("value"))
        elif op == "remove":
            if isinstance(parent, list):
                parent.pop(int(key))
            else:
                if key not in parent:
                    raise bad_request(f"JSON Patch path not found: {operation.get('path')}")
                parent.pop(key)
        else:
            raise bad_request(f"unsupported JSON Patch op: {op}")
    return result


RBAC_KINDS = {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}
LABEL_NAME_KINDS = {"Namespace", "Service"}


def validate_name(name: str, kind: str = "object", *, dns_subdomain: bool | None = None) -> None:
    if not name:
        raise bad_request(f"{kind} name must not be empty", field="metadata.name")
    if dns_subdomain is None:
        dns_subdomain = kind not in LABEL_NAME_KINDS
    if kind in RBAC_KINDS and ":" in name:
        # RBAC names are path segments, not DNS names (system:discovery).
        if len(name) > 253 or not re.match(r"^[A-Za-z0-9]([-A-Za-z0-9:._]*[A-Za-z0-9])?$", name):
            raise bad_request(f"{kind} name {name!r} is not a valid path segment name", field="metadata.name")
        return
    pattern = DNS1123_SUBDOMAIN if dns_subdomain else DNS1123_LABEL
    max_length = 253 if dns_subdomain else 63
    if len(name) > max_length or not pattern.match(name):
        raise bad_request(
            f"{kind} name {name!r} is not a valid DNS-1123 {'subdomain' if dns_subdomain else 'label'}",
            field="metadata.name",
        )


def validate_namespace(namespace: str) -> None:
    validate_name(namespace, "namespace")


def validate_labels(labels: dict[str, str] | None) -> None:
    for key, value in (labels or {}).items():
        if not isinstance(value, str):
            raise bad_request(f"label {key!r} must have a string value", field="metadata.labels")
        name = key.split("/")[-1]
        if len(name) > 63 or not LABEL_VALUE.match(name):
            raise bad_request(f"invalid label value for {key!r}", field="metadata.labels")


def default_metadata(obj: dict, *, namespace: str | None = None, kind: str = "") -> dict:
    meta = obj.setdefault("metadata", {})
    if namespace is not None:
        meta.setdefault("namespace", namespace)
    meta.setdefault("name", "")
    meta.setdefault("labels", {})
    meta.setdefault("annotations", {})
    meta.setdefault("finalizers", [])
    meta.setdefault("ownerReferences", [])
    validate_labels(meta.get("labels"))
    return meta


def owner_reference(obj: dict, *, controller: bool = True) -> dict:
    meta = obj["metadata"]
    return {
        "apiVersion": obj.get("apiVersion", "v1"),
        "kind": obj.get("kind", ""),
        "name": meta["name"],
        "uid": meta["uid"],
        "controller": controller,
        "blockOwnerDeletion": controller,
    }


def controlled_by(obj: dict, owner_uid: str) -> bool:
    return any(ref.get("uid") == owner_uid and ref.get("controller") for ref in obj.get("metadata", {}).get("ownerReferences", []))


def is_being_deleted(obj: dict) -> bool:
    return bool(obj.get("metadata", {}).get("deletionTimestamp"))


def add_finalizer(obj: dict, finalizer: str) -> None:
    finalizers = obj["metadata"].setdefault("finalizers", [])
    if finalizer not in finalizers:
        finalizers.append(finalizer)


def remove_finalizer(obj: dict, finalizer: str) -> None:
    finalizers = obj["metadata"].setdefault("finalizers", [])
    if finalizer in finalizers:
        finalizers.remove(finalizer)


def get_path(obj: Any, path: str, default: Any = None) -> Any:
    current = obj
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def set_path(obj: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    current = obj
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value
