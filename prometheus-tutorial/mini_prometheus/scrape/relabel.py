"""Relabeling, including the exact anchoring and expansion rules.

Prometheus relabel regexes are fully anchored (``^(?:...)$``) even though the
YAML does not show anchors, and replacements use Go's ``$1``/``${name}``
syntax.  Getting either detail wrong silently produces wrong target labels, so
both are centralised here.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

from ..config import RelabelConfig
from ..labels import Labels


class RelabelError(ValueError):
    pass


def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RelabelError(f"invalid relabel regex {pattern!r}: {exc}") from exc


def _matches(regex: re.Pattern[str], value: str) -> bool:
    return regex.fullmatch(value) is not None


# Go's Expand consumes the longest alphanumeric name after '$': "$1x" is the
# name "1x" (normally undefined), not group 1 followed by "x".
_GO_EXPAND_RE = re.compile(r"\$\$|\$\{([A-Za-z0-9_]+)\}|\$([A-Za-z0-9_]+)")


def _expand(match: re.Match[str], replacement: str) -> str:
    """Expand Go's ``$1``/``${name}`` syntax; missing groups become empty."""

    def expand(part: re.Match[str]) -> str:
        if part.group(0) == "$$":
            return "$"
        name = part.group(1) or part.group(2)
        try:
            if name.isdigit():
                value = match.group(int(name))
            else:
                value = match.group(name)
        except (IndexError, re.error, KeyError):
            value = ""
        return value or ""

    return _GO_EXPAND_RE.sub(expand, replacement)


def _source_value(labels: dict[str, str], rule: RelabelConfig) -> str:
    return rule.separator.join(labels.get(name, "") for name in rule.source_labels)


def relabel_target(labels: dict[str, str], rules: Iterable[RelabelConfig]) -> dict[str, str] | None:
    """Apply target relabeling; return ``None`` when the target is dropped."""
    current = dict(labels)
    for rule in rules:
        regex = _compile(rule.regex)
        action = rule.action
        if action == "replace":
            value = _source_value(current, rule)
            match = regex.fullmatch(value)
            if match is None:
                continue
            new_value = _expand(match, rule.replacement)
            _set(current, rule.target_label, new_value)
        elif action == "keep":
            if not _matches(regex, _source_value(current, rule)):
                return None
        elif action == "drop":
            if _matches(regex, _source_value(current, rule)):
                return None
        elif action == "hashmod":
            value = _source_value(current, rule)
            # Prometheus reads the first 8 bytes of the MD5 digest as a
            # big-endian uint64. Using the full 128-bit digest would assign
            # the same target to a different shard than upstream.
            digest = hashlib.md5(value.encode("utf-8")).digest()
            number = int.from_bytes(digest[:8], "big")
            _set(current, rule.target_label, str(number % rule.modulus))
        elif action == "labelmap":
            for name, value in list(current.items()):
                match = regex.fullmatch(name)
                if match:
                    new_name = _expand(match, rule.replacement)
                    if new_name:
                        current[new_name] = value
        elif action == "labeldrop":
            for name in list(current):
                if _matches(regex, name):
                    del current[name]
        elif action == "labelkeep":
            for name in list(current):
                if not _matches(regex, name):
                    del current[name]
        elif action == "lowercase":
            _set(current, rule.target_label, _source_value(current, rule).lower())
        elif action == "uppercase":
            _set(current, rule.target_label, _source_value(current, rule).upper())
        elif action == "keepequal":
            if current.get(rule.target_label, "") != _source_value(current, rule):
                return None
        elif action == "dropequal":
            if current.get(rule.target_label, "") == _source_value(current, rule):
                return None
        else:  # pragma: no cover - config validation rejects this
            raise RelabelError(f"unknown relabel action {action!r}")
    return current


def _set(labels: dict[str, str], name: str, value: str) -> None:
    if not name:
        raise RelabelError("relabel action produced an empty target label name")
    if value == "":
        labels.pop(name, None)
    else:
        labels[name] = value


def relabel_samples(labels: Labels, rules: Iterable[RelabelConfig]) -> Labels | None:
    """Apply metric_relabel_configs to one sample's labels."""
    current = labels.to_dict()
    for rule in rules:
        regex = _compile(rule.regex)
        action = rule.action
        if action == "replace":
            value = _source_value(current, rule)
            match = regex.fullmatch(value)
            if match is None:
                continue
            _set(current, rule.target_label, _expand(match, rule.replacement))
        elif action == "keep":
            if not _matches(regex, _source_value(current, rule)):
                return None
        elif action == "drop":
            if _matches(regex, _source_value(current, rule)):
                return None
        elif action == "hashmod":
            digest = hashlib.md5(
                _source_value(current, rule).encode("utf-8")
            ).digest()
            _set(
                current,
                rule.target_label,
                str(int.from_bytes(digest[:8], "big") % rule.modulus),
            )
        elif action == "labelmap":
            for name, value in list(current.items()):
                match = regex.fullmatch(name)
                if match:
                    new_name = _expand(match, rule.replacement)
                    if new_name:
                        current[new_name] = value
        elif action == "labeldrop":
            for name in list(current):
                if _matches(regex, name):
                    del current[name]
        elif action == "labelkeep":
            for name in list(current):
                if not _matches(regex, name):
                    del current[name]
        elif action == "lowercase":
            _set(current, rule.target_label, _source_value(current, rule).lower())
        elif action == "uppercase":
            _set(current, rule.target_label, _source_value(current, rule).upper())
        elif action == "keepequal":
            if current.get(rule.target_label, "") != _source_value(current, rule):
                return None
        elif action == "dropequal":
            if current.get(rule.target_label, "") == _source_value(current, rule):
                return None
    if not current.get("__name__"):
        return None
    return Labels(current)
