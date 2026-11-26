"""Immutable label sets and matchers.

A label set is the identity of a time series.  Prometheus treats a label with
an empty value as absent, sorts labels by name for canonical hashing, and
requires ``__name__`` to hold the metric name.  This module enforces all three
rules in one place so every layer above can rely on them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping

METRIC_NAME_LABEL = "__name__"

# The "legacy" character sets.  Prometheus 3.0 accepts any valid UTF-8 metric
# or label name, but names matching these regexes need no quoting and remain
# the portable subset understood by older servers, clients and tooling.
LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
RESERVED_PREFIX = "__"


class LabelError(ValueError):
    pass


def is_legacy_label_name(name: str) -> bool:
    return LABEL_NAME_RE.match(name) is not None


def is_legacy_metric_name(name: str) -> bool:
    return METRIC_NAME_RE.match(name) is not None


def _validate_utf8_name(name: str, kind: str) -> None:
    if not isinstance(name, str) or not name:
        raise LabelError(f"{kind} name must not be empty")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:  # lone surrogates cannot be encoded
        raise LabelError(f"{kind} name is not valid UTF-8: {name!r}") from exc


def validate_label_name(name: str, *, legacy_only: bool = True) -> None:
    """Validate a label name.

    ``legacy_only`` is the default because configuration written for
    Prometheus 2.x should keep working.  Parsers that explicitly implement
    Prometheus 3 quoted names pass ``legacy_only=False``; validation still
    rejects empty strings and invalid Unicode.  Label names beginning with
    ``__`` are reserved for internal use but are legal in relabeling, so they
    are not rejected here.
    """
    if legacy_only:
        if not LABEL_NAME_RE.match(name):
            raise LabelError(f"invalid label name {name!r}")
        return
    _validate_utf8_name(name, "label")


def validate_metric_name(name: str, *, allow_utf8: bool = True) -> None:
    if not name:
        raise LabelError("metric name must not be empty")
    if allow_utf8:
        _validate_utf8_name(name, "metric")
        return
    if not METRIC_NAME_RE.match(name):
        raise LabelError(f"invalid metric name {name!r}")


def escape_name(name: str) -> str:
    """Escape a name for use inside double quotes (PromQL/exposition)."""
    return name.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def quote_label_name(name: str) -> str:
    return name if is_legacy_label_name(name) else f'"{escape_name(name)}"'


def quote_metric_name(name: str) -> str:
    return name if is_legacy_metric_name(name) else f'"{escape_name(name)}"'


def format_labels(labels: "Labels") -> str:
    """Canonical ``metric{a="b"}`` form, quoting non-legacy UTF-8 names.

    Prometheus 3.0 requires a quoted metric name inside braces:
    ``{"http.server.duration", "service.name"="api"}``.  Legacy names keep
    the familiar unquoted form so logs, tests and federation output remain
    readable.
    """
    name = labels.get(METRIC_NAME_LABEL)
    rest = labels.without(METRIC_NAME_LABEL)
    if not rest:
        if not name:
            return ""
        if is_legacy_metric_name(name):
            return name
        # A bare quoted name is not valid exposition input for our parser or
        # for Prometheus' text format; the braces make it unambiguous.
        return f'{{"{escape_name(name)}"}}'
    body = ",".join(
        f"{quote_label_name(key)}=\"{_escape_label_value(value)}\""
        for key, value in rest.items()
    )
    if not name:
        return f"{{{body}}}"
    if is_legacy_metric_name(name):
        return f"{name}{{{body}}}"
    # Prometheus 3 exposition form: the quoted metric name is the first
    # element inside the braces, e.g. {"http.server.duration",job="api"}.
    return f'{{"{escape_name(name)}",{body}}}'


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Labels:
    """An immutable, canonically ordered label set."""

    __slots__ = ("_items", "_dict", "_hash")

    def __init__(self, items: Iterable[tuple[str, str]] | Mapping[str, str] | "Labels" = ()):
        if isinstance(items, Labels):
            self._items = items._items
            self._dict = items._dict
            self._hash = items._hash
            return
        if isinstance(items, Mapping):
            items = items.items()
        seen: dict[str, str] = {}
        for name, value in items:
            if not isinstance(name, str) or not isinstance(value, str):
                raise LabelError("label names and values must be strings")
            if value == "":
                continue
            if name in seen and seen[name] != value:
                raise LabelError(f"duplicate label {name!r}")
            seen[name] = value
        self._items = tuple(sorted(seen.items()))
        self._dict = seen
        self._hash = hash(self._items)

    @classmethod
    def from_pairs(cls, *pairs: tuple[str, str]) -> "Labels":
        return cls(pairs)

    @classmethod
    def empty(cls) -> "Labels":
        return cls(())

    def items(self) -> tuple[tuple[str, str], ...]:
        return self._items

    def keys(self) -> Iterable[str]:
        return self._dict.keys()

    def values(self) -> Iterable[str]:
        return self._dict.values()

    def get(self, name: str, default: str = "") -> str:
        return self._dict.get(name, default)

    def __getitem__(self, name: str) -> str:
        return self._dict[name]

    def __contains__(self, name: str) -> bool:
        return name in self._dict

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Labels) and self._items == other._items

    def __hash__(self) -> int:
        return self._hash

    def __lt__(self, other: "Labels") -> bool:
        if not isinstance(other, Labels):
            return NotImplemented
        return self._items < other._items

    def __repr__(self) -> str:
        body = ", ".join(f"{k}={v!r}" for k, v in self._items)
        return f"Labels({{{body}}})"

    def to_dict(self) -> dict[str, str]:
        return dict(self._items)

    def get_name(self) -> str:
        return self._dict.get(METRIC_NAME_LABEL, "")

    def without(self, *names: str) -> "Labels":
        names_set = set(names)
        return Labels(item for item in self._items if item[0] not in names_set)

    def keep(self, *names: str) -> "Labels":
        names_set = set(names)
        return Labels(item for item in self._items if item[0] in names_set)

    def merge(self, other: "Labels") -> "Labels":
        """Return ``self`` overridden by ``other`` (other wins)."""
        merged = dict(self._items)
        for name, value in other.items():
            if value == "":
                merged.pop(name, None)
            else:
                merged[name] = value
        return Labels(merged)

    def set(self, name: str, value: str) -> "Labels":
        """Set a label; an empty value deletes it (Prometheus semantics)."""
        if value == "":
            return self.without(name)
        return self.merge(Labels([(name, value)]))

    def drop(self, name: str) -> "Labels":
        return self.without(name)

    def map(self, fn) -> "Labels":
        out = []
        for name, value in self._items:
            new_name, new_value = fn(name, value)
            if new_value != "":
                out.append((new_name, new_value))
        return Labels(out)

    def is_empty(self) -> bool:
        return not self._items

    def bytes_size(self) -> int:
        return sum(len(name) + len(value) for name, value in self._items)


def metric_name(labels: Labels) -> str:
    return labels.get(METRIC_NAME_LABEL)


@dataclass(frozen=True)
class Matcher:
    name: str
    op: str
    value: str
    _regex: re.Pattern[str] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.op not in ("=", "!=", "=~", "!~"):
            raise LabelError(f"invalid matcher operator {self.op!r}")
        if self.op in ("=~", "!~"):
            try:
                pattern = re.compile(self.value)
            except re.error as exc:
                raise LabelError(f"invalid regex {self.value!r}: {exc}") from exc
            object.__setattr__(self, "_regex", pattern)

    def matches(self, value: str) -> bool:
        if self.op == "=":
            return value == self.value
        if self.op == "!=":
            return value != self.value
        assert self._regex is not None
        found = self._regex.fullmatch(value) is not None
        return found if self.op == "=~" else not found

    def matches_labels(self, labels: Labels) -> bool:
        return self.matches(labels.get(self.name))

    def __str__(self) -> str:
        return f"{self.name}{self.op}{self.value!r}"

    @property
    def is_equality(self) -> bool:
        return self.op == "="

    @property
    def is_regex(self) -> bool:
        return self.op in ("=~", "!~")


def match_all(matchers: Iterable[Matcher], labels: Labels) -> bool:
    return all(matcher.matches_labels(labels) for matcher in matchers)


def labels_from_string(text: str) -> Labels:
    """Parse ``{a="b",c="d"}`` (also accepts a bare metric name)."""
    text = text.strip()
    if not text:
        return Labels.empty()
    if not text.startswith("{"):
        return Labels([(METRIC_NAME_LABEL, text)])
    if not text.endswith("}"):
        raise LabelError(f"unterminated label set {text!r}")
    body = text[1:-1]
    items: list[tuple[str, str]] = []
    for part in _split_label_parts(body):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise LabelError(f"invalid label {part!r}")
        name, raw = part.split("=", 1)
        name = name.strip()
        raw = raw.strip()
        if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
            raise LabelError(f"label value must be quoted: {part!r}")
        items.append((name, _unescape(raw[1:-1])))
    return Labels(items)


def _split_label_parts(body: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quote = False
    escaped = False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and in_quote:
            current.append(char)
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            current.append(char)
            continue
        if char == "," and not in_quote:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(value):
            raise LabelError("trailing backslash in label value")
        esc = value[index]
        if esc == "n":
            out.append("\n")
        elif esc == "t":
            out.append("\t")
        elif esc == "\\":
            out.append("\\")
        elif esc == '"':
            out.append('"')
        else:
            out.append("\\" + esc)
        index += 1
    return "".join(out)
