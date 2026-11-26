"""Parser and writer for the Prometheus text exposition format.

Two dialects are handled:

* the classic text format (``metric{label="value"} 1 1700000000000``), and
* OpenMetrics 1.0 (``# EOF``, ``# UNIT``, exemplars, ``_total`` counters).

The parser is intentionally strict about the rules that matter for a scrape:
one sample per series per timestamp, TYPE/HELP before samples, metric-name
syntax, and label escaping.  It returns plain dataclasses so the scrape layer
can apply metric relabelling and the TSDB can append without re-parsing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

from .labels import (
    LABEL_NAME_RE,
    METRIC_NAME_LABEL,
    LabelError,
    Labels,
    format_labels as _canonical_labels,
    quote_metric_name,
)
from .model import Exemplar, MetricMetadata, MetricType
from .util.timeutil import now_ms

_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_VALUE_RE = re.compile(
    r"^[+-]?(?:Inf|inf|INF|Infinity|infinity|NaN|nan|NAN|"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$"
)
_TIMESTAMP_RE = re.compile(r"^-?\d+$")
_TYPE_NAMES = {member.value: member for member in MetricType}


class ExpositionError(ValueError):
    pass


@dataclass(slots=True)
class ParsedSample:
    labels: Labels
    value: float
    timestamp: int | None = None
    exemplars: list[Exemplar] = field(default_factory=list)


@dataclass(slots=True)
class ParsedFamily:
    name: str
    type: MetricType = MetricType.UNKNOWN
    help: str = ""
    unit: str = ""
    samples: list[ParsedSample] = field(default_factory=list)


@dataclass(slots=True)
class ScrapePayload:
    families: dict[str, ParsedFamily] = field(default_factory=dict)
    samples: list[ParsedSample] = field(default_factory=list)
    metadata: dict[str, MetricMetadata] = field(default_factory=dict)
    exemplars: list[tuple[Labels, Exemplar]] = field(default_factory=list)

    def num_samples(self) -> int:
        return len(self.samples)


def _parse_value(text: str) -> float:
    lowered = text.lower()
    if lowered in ("nan",):
        return math.nan
    if lowered in ("+inf", "inf", "infinity", "+infinity"):
        return math.inf
    if lowered in ("-inf", "-infinity"):
        return -math.inf
    try:
        return float(text)
    except ValueError as exc:
        raise ExpositionError(f"invalid sample value {text!r}") from exc


def _unescape_label(value: str) -> str:
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
            raise ExpositionError("unterminated escape in label value")
        esc = value[index]
        if esc == "n":
            out.append("\n")
        elif esc == "\\":
            out.append("\\")
        elif esc == '"':
            out.append('"')
        else:
            # Prometheus rejects unknown escapes; being strict catches exporter
            # bugs early instead of silently corrupting label values.
            raise ExpositionError(f"invalid escape sequence \\{esc}")
        index += 1
    return "".join(out)


def _scan_quoted(text: str, index: int) -> tuple[str, int]:
    if index >= len(text) or text[index] != '"':
        raise ExpositionError("expected quoted string")
    index += 1
    raw: list[str] = []
    while index < len(text) and text[index] != '"':
        if text[index] == "\\":
            if index + 1 >= len(text):
                raise ExpositionError("unterminated escape in quoted string")
            raw.append(text[index])
            raw.append(text[index + 1])
            index += 2
            continue
        raw.append(text[index])
        index += 1
    if index >= len(text):
        raise ExpositionError("unterminated quoted string")
    return _unescape_label("".join(raw)), index + 1


def _quoted_followed_by_equals(text: str, index: int) -> bool:
    """Return True when the quoted string at ``index`` is a label name.

    A leading quoted token can be either the metric name or a UTF-8 label
    name.  The disambiguator is the following ``=``; metric names are never
    followed by an operator inside the braces.
    """
    _, end = _scan_quoted(text, index)
    while end < len(text) and text[end] in " \t":
        end += 1
    return end < len(text) and text[end] == "="


def _scan_label_set(
    text: str, start: int, *, allow_metric_name: bool = False
) -> tuple[list[tuple[str, str]], int, str]:
    """Parse ``{a="b",...}`` starting at ``start``.

    With ``allow_metric_name`` a leading quoted string is accepted as the
    metric name (``{"utf8.name",job="api"}``), matching Prometheus 3.
    Returns ``(labels, end_index, metric_name)``.
    """
    assert text[start] == "{"
    index = start + 1
    items: list[tuple[str, str]] = []
    metric_name = ""
    first = True
    while True:
        while index < len(text) and text[index] in " \t":
            index += 1
        if index >= len(text):
            raise ExpositionError("unterminated label set")
        if text[index] == "}":
            return items, index + 1, metric_name
        if text[index] == '"':
            # A quoted string is the metric name only when it appears first
            # and is not followed by an operator.  Otherwise it is a UTF-8
            # label name: {"http.server.duration", "service.name"="api"}.
            if (
                allow_metric_name
                and first
                and not metric_name
                and not _quoted_followed_by_equals(text, index)
            ):
                metric_name, index = _scan_quoted(text, index)
                if not metric_name:
                    raise ExpositionError("metric name must not be empty")
            else:
                name, index = _scan_quoted(text, index)
                if not name:
                    raise ExpositionError("label name must not be empty")
                while index < len(text) and text[index] in " \t":
                    index += 1
                if index >= len(text) or text[index] != "=":
                    raise ExpositionError(f"expected '=' after label {name!r}")
                index += 1
                while index < len(text) and text[index] in " \t":
                    index += 1
                value, index = _scan_quoted(text, index)
                items.append((name, value))
        else:
            name_start = index
            while index < len(text) and (text[index].isalnum() or text[index] == "_"):
                index += 1
            name = text[name_start:index]
            if not name or not LABEL_NAME_RE.match(name):
                raise ExpositionError(f"invalid label name {name!r}")
            while index < len(text) and text[index] in " \t":
                index += 1
            if index >= len(text) or text[index] != "=":
                raise ExpositionError(f"expected '=' after label {name!r}")
            index += 1
            while index < len(text) and text[index] in " \t":
                index += 1
            value, index = _scan_quoted(text, index)
            items.append((name, value))
        first = False
        while index < len(text) and text[index] in " \t":
            index += 1
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "}":
            return items, index + 1, metric_name
        raise ExpositionError("expected ',' or '}' in label set")


def _scan_metric_name(text: str, start: int) -> tuple[str, int, bool]:
    """Return (name, next index, quoted). Accepts ``{"utf8.name",a="b"}``."""
    if text[start] == '"':
        try:
            name, index = _scan_quoted(text, start)
        except ExpositionError as exc:
            raise ExpositionError(f"invalid quoted metric name: {exc}") from exc
        return name, index, True
    index = start
    while index < len(text) and (text[index].isalnum() or text[index] in "_:"):
        index += 1
    name = text[start:index]
    return name, index, False


def _parse_timestamp(text: str, *, openmetrics: bool, context: str) -> int:
    """Parse a sample/exemplar timestamp into milliseconds.

    The two dialects disagree on units: Prometheus text 0.0.4 uses integer
    milliseconds, while OpenMetrics uses (possibly fractional) seconds.  A
    bare 1700000000 therefore means two very different instants depending on
    the negotiated format.
    """
    if openmetrics:
        try:
            seconds = float(text)
        except ValueError as exc:
            raise ExpositionError(f"invalid {context} timestamp {text!r}") from exc
        if not math.isfinite(seconds):
            raise ExpositionError(f"invalid {context} timestamp {text!r}")
        return int(seconds * 1000)
    if not _TIMESTAMP_RE.match(text):
        raise ExpositionError(f"invalid {context} timestamp {text!r}")
    return int(text)


def _parse_exemplar(
    text: str, default_timestamp: int, *, openmetrics: bool = False
) -> Exemplar:
    # text starts with '{'.  Format: {labels} value [timestamp]
    labels, index, _name = _scan_label_set(text, 0)
    rest = text[index:].strip()
    if not rest:
        raise ExpositionError("exemplar is missing a value")
    parts = rest.split()
    if len(parts) > 2:
        raise ExpositionError("exemplar has too many fields")
    value = _parse_value(parts[0])
    timestamp = default_timestamp
    if len(parts) == 2:
        timestamp = _parse_timestamp(
            parts[1], openmetrics=openmetrics, context="exemplar"
        )
    return Exemplar(Labels(labels), value, timestamp)


def parse_exposition(
    text: str,
    *,
    default_timestamp: int | None = None,
    openmetrics: bool = False,
    max_samples: int | None = None,
) -> ScrapePayload:
    """Parse a text/OpenMetrics payload.

    ``default_timestamp`` is used when a line omits its timestamp.  The scrape
    manager passes the scrape start time, mirroring Prometheus' "timestamp is
    assigned at ingestion" rule.
    """
    if default_timestamp is None:
        default_timestamp = now_ms()
    payload = ScrapePayload()
    seen: dict[tuple[Labels, int], float] = {}
    current_family: ParsedFamily | None = None
    saw_eof = False
    # The text format is newline-delimited; split on "\n" only.  str.splitlines
    # also breaks on \r, \v, \f, U+2028..., which can appear inside a quoted
    # label value and would corrupt an otherwise valid payload.
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line_number = index + 1
        raw_line = lines[index]
        index += 1
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line[1:].strip()
            if body in ("EOF", "EOF "):
                saw_eof = True
                break
            if body.startswith("HELP "):
                rest = body[5:].strip()
                name, help_text = _split_metric_and_text(rest, line_number)
                family = _family(payload, name)
                if family.help:
                    raise ExpositionError(
                        f"line {line_number}: second HELP for {name!r}"
                    )
                family.help = _unescape_help(help_text)
                current_family = family
                continue
            if body.startswith("TYPE "):
                rest = body[5:].strip()
                name, type_text = _split_metric_and_text(rest, line_number)
                type_name = type_text.strip().lower()
                if type_name not in _TYPE_NAMES:
                    raise ExpositionError(
                        f"line {line_number}: unknown metric type {type_text!r}"
                    )
                family = _family(payload, name)
                if family.samples:
                    raise ExpositionError(
                        f"line {line_number}: TYPE for {name!r} after its samples"
                    )
                if family.type not in (MetricType.UNKNOWN, _TYPE_NAMES[type_name]):
                    raise ExpositionError(
                        f"line {line_number}: conflicting TYPE for {name!r}"
                    )
                family.type = _TYPE_NAMES[type_name]
                current_family = family
                continue
            if body.startswith("UNIT "):
                rest = body[5:].strip()
                name, unit = _split_metric_and_text(rest, line_number)
                family = _family(payload, name)
                family.unit = unit.strip()
                current_family = family
                continue
            # Unknown comments are ignored, as required by the spec.
            continue

        # Data line: name [labels] value [timestamp] [# exemplar]
        label_items: list[tuple[str, str]] = []
        if line[0] == "{":
            label_items, position, name = _scan_label_set(
                line, 0, allow_metric_name=True
            )
            quoted = True
            if not name:
                raise ExpositionError(f"line {line_number}: missing metric name")
        else:
            name, position, quoted = _scan_metric_name(line, 0)
            if not name:
                raise ExpositionError(f"line {line_number}: missing metric name")
            if not _METRIC_NAME_RE.match(name):
                raise ExpositionError(f"line {line_number}: invalid metric name {name!r}")
            if position < len(line) and line[position] == "{":
                label_items, position, _unused_name = _scan_label_set(line, position)
        rest = line[position:].strip()
        exemplar_text = ""
        if " # " in rest:
            rest, exemplar_text = rest.split(" # ", 1)
            rest = rest.rstrip()
        fields = rest.split()
        if len(fields) not in (1, 2):
            raise ExpositionError(
                f"line {line_number}: expected 'value [timestamp]', got {rest!r}"
            )
        value = _parse_value(fields[0])
        timestamp: int | None = None
        if len(fields) == 2:
            timestamp = _parse_timestamp(
                fields[1],
                openmetrics=openmetrics,
                context=f"line {line_number}",
            )
        effective_timestamp = timestamp if timestamp is not None else default_timestamp
        if not quoted and name == METRIC_NAME_LABEL:
            raise ExpositionError(
                f"line {line_number}: __name__ cannot be set from the exposition format"
            )
        try:
            labels = Labels(label_items + [(METRIC_NAME_LABEL, name)])
        except LabelError as exc:
            raise ExpositionError(f"line {line_number}: {exc}") from exc
        key = (labels, effective_timestamp)
        if key in seen:
            previous = seen[key]
            if previous == value or (math.isnan(previous) and math.isnan(value)):
                # Identical duplicate: harmless, keep one copy.
                continue
            raise ExpositionError(
                f"line {line_number}: duplicate sample for series {name}{{...}} "
                f"at timestamp {effective_timestamp}"
            )
        seen[key] = value
        sample = ParsedSample(labels, value, timestamp)
        payload.samples.append(sample)
        family = payload.families.get(name)
        if family is None:
            # Untyped metric: infer nothing. Prometheus reports it as unknown.
            family = _family(payload, name)
        family.samples.append(sample)
        if exemplar_text:
            try:
                exemplar = _parse_exemplar(
                    exemplar_text.strip(),
                    effective_timestamp,
                    openmetrics=openmetrics,
                )
            except ExpositionError as exc:
                raise ExpositionError(f"line {line_number}: {exc}") from exc
            sample.exemplars.append(exemplar)
            payload.exemplars.append((labels, exemplar))
        if max_samples is not None and len(payload.samples) > max_samples:
            raise ExpositionError(
                f"sample limit exceeded ({max_samples}); scrape aborted"
            )

    if openmetrics and not saw_eof:
        raise ExpositionError("OpenMetrics payload is missing '# EOF'")
    if openmetrics:
        for family in payload.families.values():
            payload.metadata[family.name] = MetricMetadata(
                family.name, family.type, family.help, family.unit
            )
    else:
        for family in payload.families.values():
            payload.metadata[family.name] = MetricMetadata(
                family.name, family.type, family.help, family.unit
            )
    return payload


def _family(payload: ScrapePayload, name: str) -> ParsedFamily:
    family = payload.families.get(name)
    if family is None:
        family = ParsedFamily(name)
        payload.families[name] = family
    return family


def _split_metric_and_text(text: str, line_number: int) -> tuple[str, str]:
    text = text.lstrip()
    if not text:
        raise ExpositionError(f"line {line_number}: missing metric name in directive")
    if text[0] == '"':
        name, index, _ = _scan_metric_name(text, 0)
        return name, text[index:].strip()
    parts = text.split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _unescape_help(value: str) -> str:
    return value.replace("\\\\", "\\").replace("\\n", "\n")


def _metadata_family(name: str, metadata: dict[str, MetricMetadata]) -> str:
    """Find the family metadata for a sample name.

    Histogram/summary samples carry suffixes (``_bucket``/``_sum``/``_count``)
    while HELP/TYPE are recorded for the base family.  Without this lookup a
    re-export loses the type information and a subsequent parse treats the
    samples as untyped.
    """
    if name in metadata:
        return name
    for suffix in ("_bucket", "_sum", "_count", "_created"):
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            if base in metadata:
                return base
    return name


def format_exposition(payload: ScrapePayload | Iterable[ParsedSample]) -> str:
    """Serialize samples back to the text format (used by /metrics, /federate)."""
    samples = payload.samples if isinstance(payload, ScrapePayload) else list(payload)
    metadata: dict[str, MetricMetadata] = {}
    if isinstance(payload, ScrapePayload):
        metadata = payload.metadata
    lines: list[str] = []
    emitted: set[str] = set()
    for sample in samples:
        name = sample.labels.get(METRIC_NAME_LABEL)
        family = _metadata_family(name, metadata)
        if family not in emitted:
            rendered = quote_metric_name(family)
            meta = metadata.get(family)
            if meta and meta.help:
                lines.append(f"# HELP {rendered} {_escape_help(meta.help)}")
            if meta and meta.type != MetricType.UNKNOWN:
                lines.append(f"# TYPE {rendered} {meta.type.value}")
            emitted.add(family)
        label_text = _canonical_labels(sample.labels)
        value = _format_float(sample.value)
        suffix = f" {sample.timestamp}" if sample.timestamp is not None else ""
        lines.append(f"{label_text} {value}{suffix}")
    return "\n".join(lines) + ("\n" if lines else "")


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _format_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(value)
