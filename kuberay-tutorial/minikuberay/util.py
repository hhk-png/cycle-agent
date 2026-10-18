"""Small, dependency-free helpers shared by models, controllers and the CLI."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import string
from datetime import datetime, timezone
from typing import Any, Iterable

from .clock import iso, parse_iso

DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# RFC 1035 labels (used by KubeRay for CR names) must start with a letter.
DNS1035_LABEL = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
DNS1123_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
SEMVER = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?$")

_MEMORY_SUFFIXES = {
    "": 1,
    "k": 10**3,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}


def deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(value: Any, length: int = 10) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def random_suffix(length: int = 5) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def make_name(prefix: str, suffix: str | None = None, length: int = 5) -> str:
    suffix = suffix if suffix is not None else random_suffix(length)
    prefix = prefix[: 63 - len(suffix) - 1].rstrip("-")
    return f"{prefix}-{suffix}"


def qualified_name(prefix: str, name: str) -> str:
    return f"{prefix}-{name}"


def is_dns1123_label(value: str) -> bool:
    return bool(value) and len(value) <= 63 and bool(DNS1123_LABEL.match(value))


def is_dns1035_label(value: str) -> bool:
    return bool(value) and len(value) <= 63 and bool(DNS1035_LABEL.match(value))


def is_dns1123_subdomain(value: str) -> bool:
    return bool(value) and len(value) <= 253 and bool(DNS1123_SUBDOMAIN.match(value))


def semver_tuple(value: str) -> tuple[int, int, int] | None:
    match = SEMVER.match(value or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def ray_supports_autoscaler_v2(ray_version: str) -> bool:
    parsed = semver_tuple(ray_version)
    return bool(parsed and parsed >= (2, 47, 0))


def parse_cpu(value: Any) -> int:
    """Return CPU in millicores."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(round(float(value) * 1000))
    text = str(value).strip()
    if text.endswith("m"):
        return int(float(text[:-1]))
    return int(round(float(text) * 1000))


def parse_memory(value: Any) -> int:
    """Return memory/storage in bytes."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    match = re.match(r"^([0-9.]+)\s*([A-Za-z]*)$", text)
    if not match:
        raise ValueError(f"invalid quantity: {value!r}")
    number, suffix = match.groups()
    if suffix not in _MEMORY_SUFFIXES:
        raise ValueError(f"unknown quantity suffix: {suffix!r}")
    return int(float(number) * _MEMORY_SUFFIXES[suffix])


def parse_resource(value: Any, resource: str) -> float:
    if resource == "cpu":
        return parse_cpu(value)
    if resource in ("memory", "ephemeral-storage") or resource.endswith("storage"):
        return parse_memory(value)
    if value is None:
        return 0.0
    return float(value)


def format_cpu(millicores: int) -> str:
    if millicores % 1000 == 0:
        return str(millicores // 1000)
    return f"{millicores}m"


def format_memory(num_bytes: int) -> str:
    for suffix, factor in (("Gi", 2**30), ("Mi", 2**20), ("Ki", 2**10)):
        if num_bytes and num_bytes % factor == 0:
            return f"{num_bytes // factor}{suffix}"
    return str(num_bytes)


def resource_quantity(resources: dict | None, resource: str) -> float:
    resources = resources or {}
    return parse_resource(resources.get(resource), resource)


def sum_resources(pods: Iterable[dict], resource: str) -> float:
    total = 0.0
    for pod in pods:
        spec = pod.get("spec", {})
        for container in spec.get("containers", []):
            total += resource_quantity(container.get("resources", {}).get("requests"), resource)
    return total


def status_quantity(value: float, resource: str) -> str:
    if resource == "cpu":
        return format_cpu(int(round(value)))
    if resource == "memory":
        return format_memory(int(round(value)))
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def labels_match(selector: dict | None, labels: dict | None) -> bool:
    """Minimal LabelSelector implementation.

    Accepts both the object form (``matchLabels``/``matchExpressions``) and the
    flat ``key=value`` form used by Kubernetes query strings and by callers of
    ``ApiStore.list``.  A flat key prefixed with ``!`` means "not equal".
    """
    if not selector:
        return True
    labels = labels or {}
    match_labels = dict(selector.get("matchLabels") or {})
    for key, value in selector.items():
        if key in ("matchLabels", "matchExpressions"):
            continue
        if key.startswith("__exists__:"):
            if key.split(":", 1)[1] not in labels:
                return False
            continue
        if key.startswith("!"):
            if labels.get(key[1:]) == value:
                return False
        elif key in labels and labels.get(key) != value:
            return False
        elif key not in labels:
            return False
        else:
            match_labels[key] = value
    for key, value in match_labels.items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key = expr.get("key", "")
        operator = expr.get("operator", "")
        values = expr.get("values") or []
        if operator == "In":
            if key not in labels or labels[key] not in values:
                return False
        elif operator == "NotIn":
            if key in labels and labels[key] in values:
                return False
        elif operator == "Exists":
            if key not in labels:
                return False
        elif operator == "DoesNotExist":
            if key in labels:
                return False
    return True


def parse_duration_seconds(value: str | int | float | None, default: float = 0.0) -> float:
    """Parse Go-style durations used by KubeRay annotations (30m, 1h, 45s)."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    units = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", text)
    if not match:
        raise ValueError(f"invalid duration: {value!r}")
    return float(match.group(1)) * units[match.group(2)]


def age_seconds(created: str | None, now: datetime) -> float:
    if not created:
        return 0.0
    return max(0.0, (now - parse_iso(created)).total_seconds())


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def ensure_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def env_value(container: dict, name: str) -> str | None:
    for item in container.get("env") or []:
        if item.get("name") == name:
            return item.get("value")
    return None


def set_env(container: dict, name: str, value: str) -> None:
    env = container.setdefault("env", [])
    for item in env:
        if item.get("name") == name:
            item["value"] = value
            return
    env.append({"name": name, "value": value})


def get_ray_container(pod_spec: dict) -> dict | None:
    containers = pod_spec.get("containers") or []
    for container in containers:
        if container.get("name") == "ray":
            return container
    return containers[0] if containers else None


def utc_timestamp(value: datetime | None = None) -> str:
    return iso(value or datetime.now(timezone.utc))


def clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def ceil_div(a: float, b: float) -> int:
    if b <= 0:
        return 0
    return int(math.ceil(a / b))
