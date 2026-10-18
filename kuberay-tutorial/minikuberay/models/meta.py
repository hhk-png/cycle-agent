"""Object metadata, ownership and condition helpers."""

from __future__ import annotations

import uuid
from typing import Any

from ..clock import Clock, iso
from ..util import deep_copy, utc_timestamp


def new_metadata(
    name: str,
    namespace: str = "default",
    *,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    finalizers: list[str] | None = None,
    owner_references: list[dict] | None = None,
) -> dict:
    return {
        "name": name,
        "namespace": namespace,
        "labels": dict(labels or {}),
        "annotations": dict(annotations or {}),
        "finalizers": list(finalizers or []),
        "ownerReferences": list(owner_references or []),
    }


def new_object(
    api_version: str,
    kind: str,
    name: str,
    namespace: str = "default",
    *,
    spec: dict | None = None,
    status: dict | None = None,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    finalizers: list[str] | None = None,
    owner_references: list[dict] | None = None,
) -> dict:
    # The store fills uid/resourceVersion/generation/creationTimestamp.  The
    # placeholders make objects printable before they are persisted.
    metadata = new_metadata(
        name,
        namespace,
        labels=labels,
        annotations=annotations,
        finalizers=finalizers,
        owner_references=owner_references,
    )
    metadata.update(
        {
            "uid": str(uuid.uuid4()),
            "resourceVersion": "0",
            "generation": 1,
            "creationTimestamp": utc_timestamp(),
        }
    )
    obj = {"apiVersion": api_version, "kind": kind, "metadata": metadata}
    if spec is not None:
        obj["spec"] = spec
    if status is not None:
        obj["status"] = status
    return obj


def name(obj: dict) -> str:
    return obj.get("metadata", {}).get("name", "")


def namespace(obj: dict) -> str:
    return obj.get("metadata", {}).get("namespace", "default")


def labels(obj: dict) -> dict:
    return obj.get("metadata", {}).get("labels", {}) or {}


def annotations(obj: dict) -> dict:
    return obj.get("metadata", {}).get("annotations", {}) or {}


def uid(obj: dict) -> str:
    return obj.get("metadata", {}).get("uid", "")


def resource_version(obj: dict) -> int:
    try:
        return int(obj.get("metadata", {}).get("resourceVersion", "0"))
    except (TypeError, ValueError):
        return 0


def generation(obj: dict) -> int:
    return int(obj.get("metadata", {}).get("generation", 1) or 1)


def deletion_timestamp(obj: dict) -> str | None:
    return obj.get("metadata", {}).get("deletionTimestamp")


def is_being_deleted(obj: dict) -> bool:
    return bool(deletion_timestamp(obj))


def add_finalizer(obj: dict, finalizer: str) -> bool:
    finalizers = obj.setdefault("metadata", {}).setdefault("finalizers", [])
    if finalizer in finalizers:
        return False
    finalizers.append(finalizer)
    return True


def remove_finalizer(obj: dict, finalizer: str) -> bool:
    finalizers = obj.setdefault("metadata", {}).setdefault("finalizers", [])
    if finalizer not in finalizers:
        return False
    finalizers.remove(finalizer)
    return True


def has_finalizer(obj: dict, finalizer: str) -> bool:
    return finalizer in (obj.get("metadata", {}).get("finalizers") or [])


def owner_reference(owner: dict, *, controller: bool = True) -> dict:
    return {
        "apiVersion": owner.get("apiVersion", "ray.io/v1"),
        "kind": owner.get("kind", ""),
        "name": name(owner),
        "uid": uid(owner),
        "controller": controller,
        "blockOwnerDeletion": controller,
    }


def owned_by(obj: dict, owner: dict) -> bool:
    owner_uid = uid(owner)
    return any(ref.get("uid") == owner_uid for ref in obj.get("metadata", {}).get("ownerReferences") or [])


def has_controller_owner(obj: dict) -> bool:
    return any(ref.get("controller") for ref in obj.get("metadata", {}).get("ownerReferences") or [])


def condition_index(conditions: list[dict], condition_type: str) -> int:
    for index, condition in enumerate(conditions):
        if condition.get("type") == condition_type:
            return index
    return -1


def get_condition(obj_or_status: dict, condition_type: str) -> dict | None:
    status = obj_or_status.get("status", obj_or_status)
    for condition in status.get("conditions") or []:
        if condition.get("type") == condition_type:
            return condition
    return None


def is_condition_true(obj_or_status: dict, condition_type: str) -> bool:
    condition = get_condition(obj_or_status, condition_type)
    return bool(condition and condition.get("status") == "True")


def set_condition(
    obj: dict,
    condition_type: str,
    condition_status: bool | str,
    reason: str,
    message: str = "",
    *,
    clock: Clock | None = None,
    observed_generation: int | None = None,
) -> bool:
    """Set a metav1.Condition; return True when anything changed.

    ``lastTransitionTime`` only moves when the boolean status changes, matching
    Kubernetes semantics.  This keeps repeated reconciles from producing an
    endless stream of status writes.
    """
    status_value = condition_status if isinstance(condition_status, str) else ("True" if condition_status else "False")
    status = obj.setdefault("status", {})
    conditions = status.setdefault("conditions", [])
    index = condition_index(conditions, condition_type)
    now = iso(clock.now() if clock else None)
    if observed_generation is None:
        observed_generation = generation(obj)
    if index < 0:
        conditions.append(
            {
                "type": condition_type,
                "status": status_value,
                "reason": reason,
                "message": message,
                "observedGeneration": observed_generation,
                "lastTransitionTime": now,
            }
        )
        return True
    condition = conditions[index]
    changed = False
    if condition.get("status") != status_value:
        condition["status"] = status_value
        condition["lastTransitionTime"] = now
        changed = True
    if condition.get("reason") != reason:
        condition["reason"] = reason
        changed = True
    if condition.get("message") != message:
        condition["message"] = message
        changed = True
    if condition.get("observedGeneration") != observed_generation:
        condition["observedGeneration"] = observed_generation
        changed = True
    return changed


def remove_condition(obj: dict, condition_type: str) -> bool:
    status = obj.setdefault("status", {})
    conditions = status.setdefault("conditions", [])
    index = condition_index(conditions, condition_type)
    if index < 0:
        return False
    conditions.pop(index)
    return True


def merge_patch(target: dict, patch: dict) -> dict:
    """RFC 7386 JSON merge patch."""
    result = deep_copy(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = deep_copy(value)
    return result


def status_equal(a: dict, b: dict) -> bool:
    from ..util import canonical_json

    return canonical_json(a or {}) == canonical_json(b or {})


def update_status(obj: dict, new_status: dict) -> bool:
    if status_equal(obj.get("status", {}), new_status):
        return False
    obj["status"] = new_status
    return True


def status_summary(obj: dict) -> dict[str, Any]:
    status = obj.get("status", {})
    return {
        "state": status.get("state"),
        "conditions": {c.get("type"): c.get("status") for c in status.get("conditions") or []},
        "observedGeneration": status.get("observedGeneration"),
    }
