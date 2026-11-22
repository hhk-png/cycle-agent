"""Label and field selector parsing with Kubernetes-compatible semantics."""

from __future__ import annotations

import re

from mini_k8s.errors import bad_request

_LABEL_KEY = re.compile(r"^([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9]$")


def _split_commas(expression: str) -> list[str]:
    """Split on commas that are not inside parentheses."""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in expression:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def _validate_key(key: str) -> None:
    if not key:
        raise bad_request("label selector key must not be empty", field="labelSelector")
    # A qualified key has at most one slash; the prefix is a DNS subdomain.
    if "/" in key:
        prefix, name = key.split("/", 1)
        if not prefix or not name:
            raise bad_request(f"invalid label key: {key}", field="labelSelector")
    if not _LABEL_KEY.match(key.split("/")[-1]):
        raise bad_request(f"invalid label key: {key}", field="labelSelector")


def parse_label_selector(expression: str | None) -> list[tuple[str, str, tuple[str, ...]]]:
    """Return requirements as ``(key, operator, values)``.

    Operators: ``=``, ``!=``, ``in``, ``notin``, ``exists``, ``!``.
    """
    if not expression:
        return []
    requirements: list[tuple[str, str, tuple[str, ...]]] = []
    for term in _split_commas(expression):
        negated = False
        if term.startswith("!"):
            negated = True
            term = term[1:].strip()
            _validate_key(term)
            requirements.append((term, "!", ()))
            continue
        match = re.match(r"^([^!=]+?)\s+(in|notin)\s*\((.*)\)$", term)
        if match:
            key, operator, raw_values = match.groups()
            key = key.strip()
            _validate_key(key)
            values = tuple(value.strip() for value in raw_values.split(",") if value.strip())
            if not values:
                raise bad_request(f"selector {term!r} has no values", field="labelSelector")
            requirements.append((key, operator, values))
            continue
        match = re.match(r"^([^=!]+?)\s*(!=|=|==)\s*(.*)$", term)
        if match:
            key, operator, value = match.groups()
            key = key.strip()
            value = value.strip()
            _validate_key(key)
            if not value:
                raise bad_request(f"selector {term!r} has an empty value", field="labelSelector")
            requirements.append((key, "!=" if operator == "!=" else "=", (value,)))
            continue
        # A bare key is an existence requirement.
        _validate_key(term)
        requirements.append((term, "exists", ()))
    return requirements


def match_label_requirements(labels: dict[str, str] | None, requirements: list[tuple[str, str, tuple[str, ...]]]) -> bool:
    labels = labels or {}
    for key, operator, values in requirements:
        present = key in labels
        if operator == "exists" and not present:
            return False
        if operator == "!" and present:
            return False
        if operator == "=" and (not present or labels[key] not in values):
            return False
        if operator == "!=" and present and labels[key] in values:
            return False
        if operator == "in" and (not present or labels[key] not in values):
            return False
        if operator == "notin" and present and labels[key] in values:
            return False
    return True


def match_labels(labels: dict[str, str] | None, expression: str | None) -> bool:
    return match_label_requirements(labels, parse_label_selector(expression))


def parse_field_selector(expression: str | None) -> list[tuple[str, str]]:
    if not expression:
        return []
    result: list[tuple[str, str]] = []
    for term in _split_commas(expression):
        if "!=" in term:
            key, value = term.split("!=", 1)
            operator = "!="
        elif "==" in term:
            key, value = term.split("==", 1)
            operator = "="
        elif "=" in term:
            key, value = term.split("=", 1)
            operator = "="
        else:
            raise bad_request(f"invalid field selector term: {term!r}", field="fieldSelector")
        result.append((key.strip(), f"{operator}{value.strip()}"))
    return result


def _field_value(obj: dict, path: str):
    current = obj
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def match_field_selector(obj: dict, expression: str | None) -> bool:
    for key, requirement in parse_field_selector(expression):
        actual = _field_value(obj, key)
        if requirement.startswith("="):
            if str(actual) != requirement[1:]:
                return False
        else:
            if str(actual) == requirement[2:]:
                return False
    return True


def match_expression(expression: dict, labels: dict[str, str]) -> bool:
    """Match one node/pod affinity ``matchExpressions`` entry."""
    key = expression.get("key", "")
    operator = expression.get("operator", "In")
    values = [str(v) for v in expression.get("values", [])]
    present = key in labels
    value = labels.get(key)
    if operator == "In":
        return present and value in values
    if operator == "NotIn":
        return not present or value not in values
    if operator == "Exists":
        return present
    if operator == "DoesNotExist":
        return not present
    if operator in ("Gt", "Lt"):
        try:
            left, right = int(value or 0), int(values[0])
        except (ValueError, IndexError):
            return False
        return left > right if operator == "Gt" else left < right
    return False
