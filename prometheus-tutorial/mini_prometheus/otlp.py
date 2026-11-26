"""OTLP/HTTP JSON receiver.

Real Prometheus accepts OTLP/protobuf (and, in recent versions, OTLP/JSON)
on ``POST /api/v1/otlp/v1/metrics``.  This module implements the translation
layer for OTLP/JSON:

* resource attributes -> labels (``service.name``/``service.instance.id``
  become ``job``/``instance``);
* instrumentation scope -> ``otel_scope_*`` labels;
* metric names -> Prometheus names, with unit suffixes and ``_total``;
* gauges, monotonic/non-monotonic sums, explicit-bucket histograms and
  summaries;
* optional delta -> cumulative conversion with per-series state.

Deliberate Mini boundaries (documented in appendix AG):

* OTLP/protobuf is not decoded; use the JSON encoding.
* Exponential histograms need native-histogram storage and are rejected
  rather than silently dropped.
* Exemplars are counted but not stored (Mini has no exemplar TSDB).
* Explicit-bucket histograms are expanded to classic ``_bucket`` series;
  Prometheus 3 normally keeps them as custom-bucket native histograms.
"""

from __future__ import annotations

import base64
import json
import math
import re
import threading
from dataclasses import dataclass, field, replace

from .labels import (
    Labels,
    METRIC_NAME_LABEL,
    LabelError,
    validate_label_name,
    validate_metric_name,
)
from .model import ErrorType, PrometheusError
from .util.timeutil import now_ms

DELTA = 1
CUMULATIVE = 2

STRATEGY_NO_UTF8 = "NoUTF8EscapingWithSuffixes"
STRATEGY_UNDERSCORE = "UnderscoreEscapingWithSuffixes"
TRANSLATION_STRATEGIES = (STRATEGY_NO_UTF8, STRATEGY_UNDERSCORE)

# UCUM unit -> Prometheus suffix.  Unknown units are sanitized generically.
_UNIT_SUFFIXES = {
    "s": "_seconds",
    "ms": "_milliseconds",
    "us": "_microseconds",
    "ns": "_nanoseconds",
    "min": "_minutes",
    "h": "_hours",
    "d": "_days",
    "By": "_bytes",
    "KiBy": "_kibibytes",
    "MiBy": "_mebibytes",
    "GiBy": "_gibibytes",
    "TiBy": "_tebibytes",
    "By/s": "_bytes_per_second",
    "m": "_meters",
    "m/s": "_meters_per_second",
    "Cel": "_celsius",
    "%": "_percent",
    "1": "",
}

_LEGACY_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

class OTLPError(PrometheusError):
    """An OTLP payload that cannot be ingested faithfully."""

    def __init__(self, message: str):
        super().__init__(message, error_type=ErrorType.BAD_DATA)


@dataclass
class OTLPOptions:
    translation_strategy: str = STRATEGY_NO_UTF8
    delta_to_cumulative: bool = False
    max_delta_series: int = 100_000


@dataclass
class OTLPResult:
    samples: list[tuple[Labels, int, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _DeltaState:
    start_ns: int
    last_ns: int
    value: float = 0.0
    count: float = 0.0
    total: float = 0.0
    buckets: list[float] = field(default_factory=list)
    bounds: list[float] = field(default_factory=list)


def _as_int(value, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError) as exc:
            raise OTLPError(f"invalid integer value {value!r}") from exc


def _as_float(value, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("nan",):
            return math.nan
        if lowered in ("inf", "+inf", "infinity", "+infinity"):
            return math.inf
        if lowered in ("-inf", "-infinity"):
            return -math.inf
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise OTLPError(f"invalid numeric value {value!r}") from exc


def _timestamp_ms(value, default: int | None = None) -> int:
    if value in (None, "", 0, "0"):
        return now_ms() if default is None else default
    return _as_int(value) // 1_000_000


def _number(point: dict, *keys: str) -> float:
    for key in keys:
        if key in point:
            return _as_float(point[key])
    return 0.0


def _attribute_value(value: dict) -> str:
    """Flatten an OTLP AnyValue into a label value.

    Scalars keep their natural representation.  Arrays and maps have no
    Prometheus equivalent; they are rendered as compact JSON so the data is
    still inspectable (and the cardinality risk is visible to the operator).
    """
    if not isinstance(value, dict):
        return ""
    if "stringValue" in value:
        return str(value["stringValue"])
    if "boolValue" in value:
        return "true" if value["boolValue"] else "false"
    if "intValue" in value:
        return str(_as_int(value["intValue"]))
    if "doubleValue" in value:
        number = _as_float(value["doubleValue"])
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "+Inf" if number > 0 else "-Inf"
        return repr(number)
    if "bytesValue" in value:
        try:
            return base64.b64decode(value["bytesValue"]).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return str(value["bytesValue"])
    if "arrayValue" in value:
        items = value["arrayValue"].get("values", []) if value["arrayValue"] else []
        return json.dumps([_attribute_value(item) for item in items], ensure_ascii=False)
    if "kvlistValue" in value:
        pairs = value["kvlistValue"].get("values", []) if value["kvlistValue"] else []
        rendered = {
            str(item.get("key", "")): _attribute_value(item.get("value", {}))
            for item in pairs
        }
        return json.dumps(rendered, ensure_ascii=False, sort_keys=True)
    return ""


def _attributes(attributes) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in attributes or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", ""))
        value = _attribute_value(item.get("value", {}))
        if key and value:
            out.append((key, value))
    return out


def _sanitize_label_name(name: str, strategy: str) -> str:
    """Map an OTLP attribute key to a Prometheus label name.

    ``NoUTF8EscapingWithSuffixes`` keeps UTF-8 label names (Prometheus 3 and
    Mini both support them).  ``UnderscoreEscapingWithSuffixes`` rewrites
    non-legacy characters to ``_`` so the result is portable to 2.x-era
    tooling.  Reserved names are dropped in both modes.
    """
    if not name or name == METRIC_NAME_LABEL or name.startswith("__"):
        return ""
    if strategy == STRATEGY_NO_UTF8:
        try:
            validate_label_name(name, legacy_only=False)
        except LabelError as exc:
            raise OTLPError(str(exc)) from exc
        return name
    if _LEGACY_LABEL_RE.match(name):
        return name
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if not sanitized:
        return ""
    if sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized


def _labels_with_collisions(
    pairs: list[tuple[str, str]], warnings: list[str], strategy: str
) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_name, value in pairs:
        name = _sanitize_label_name(raw_name, strategy)
        if not name:
            continue
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise OTLPError(
                f"label {raw_name!r} has a value that is not valid UTF-8"
            ) from exc
        if name in out and out[name] != value:
            warnings.append(
                f"collision: attribute {raw_name!r} maps to label {name!r} "
                f"which already has a different value"
            )
            continue
        out[name] = value
    return out


def _unit_suffix(unit: str) -> str:
    unit = (unit or "").strip()
    if not unit or unit.startswith("{"):
        return ""
    if unit in _UNIT_SUFFIXES:
        return _UNIT_SUFFIXES[unit]
    rendered = unit.replace("/", "_per_")
    rendered = re.sub(r"[^a-zA-Z0-9]+", "_", rendered).strip("_").lower()
    return f"_{rendered}" if rendered else ""


def _escape_metric_name(name: str, strategy: str) -> str:
    if not name:
        raise OTLPError("metric name must not be empty")
    try:
        validate_metric_name(name, allow_utf8=True)
    except LabelError as exc:
        raise OTLPError(str(exc)) from exc
    if strategy == STRATEGY_NO_UTF8:
        return name
    escaped = re.sub(r"[^a-zA-Z0-9_:]", "_", name)
    escaped = re.sub(r"_+", "_", escaped).strip("_")
    if not escaped:
        raise OTLPError(f"metric name {name!r} has no usable characters")
    if escaped[0].isdigit():
        escaped = "_" + escaped
    return escaped


def _family_name(metric: dict, suffix: str = "", counter: bool = False) -> str:
    base = _escape_metric_name(
        str(metric.get("name", "")), str(metric.get("_strategy", STRATEGY_NO_UTF8))
    )
    unit_suffix = _unit_suffix(str(metric.get("unit", "") or ""))
    if unit_suffix and not base.endswith(unit_suffix):
        base += unit_suffix
    name = base + suffix
    if counter and not name.endswith("_total"):
        name += "_total"
    return name


def _temporality(value) -> int:
    if isinstance(value, str):
        upper = value.upper()
        if "DELTA" in upper:
            return DELTA
        if "CUMULATIVE" in upper:
            return CUMULATIVE
        return 0
    number = _as_int(value, 0)
    return number if number in (DELTA, CUMULATIVE) else 0


def _point_labels(
    resource: dict[str, str],
    scope: dict[str, str],
    attributes,
    warnings: list[str],
    strategy: str,
) -> Labels:
    pairs = list(resource.items()) + list(scope.items()) + _attributes(attributes)
    merged = _labels_with_collisions(pairs, warnings, strategy)
    return Labels(merged)


class OTLPReceiver:
    """Translate OTLP/JSON metric payloads into Prometheus samples."""

    def __init__(self, options: OTLPOptions | None = None) -> None:
        self.options = options or OTLPOptions()
        if self.options.translation_strategy not in TRANSLATION_STRATEGIES:
            raise ValueError(
                f"unknown OTLP translation strategy {self.options.translation_strategy!r}"
            )
        self._delta: dict[Labels, _DeltaState] = {}
        self._lock = threading.RLock()
        self._txn: dict[Labels, _DeltaState | None] | None = None
        self.stats = {
            "requests": 0,
            "metricPoints": 0,
            "samples": 0,
            "rejectedDeltaPoints": 0,
            "deltaResets": 0,
            "droppedOutOfOrderDeltas": 0,
            "droppedExponentialHistograms": 0,
            "droppedExemplars": 0,
            "droppedCollisions": 0,
        }

    # ---- public --------------------------------------------------------
    def translate(self, payload: dict) -> OTLPResult:
        """Translate one request atomically.

        The HTTP handler is threaded, so a request either commits all of its
        delta-state mutations or none.  Serializing translation also keeps
        timestamp comparison deterministic across concurrent requests.
        """
        with self._lock:
            previous = self._txn
            self._txn = {}
            try:
                return self._translate(payload)
            except Exception:
                self._rollback_delta()
                raise
            finally:
                self._txn = previous

    def _translate(self, payload: dict) -> OTLPResult:
        if not isinstance(payload, dict):
            raise OTLPError("OTLP payload must be a JSON object")
        resource_metrics = payload.get("resourceMetrics")
        if resource_metrics is None:
            # The JSON proto wraps the same field in resource_metrics in some
            # SDKs; accept both spellings to make the receiver usable.
            resource_metrics = payload.get("resource_metrics")
        if resource_metrics is None:
            resource_metrics = []
        if not isinstance(resource_metrics, list):
            raise OTLPError("resourceMetrics must be an array")

        result = OTLPResult()
        for resource_metric in resource_metrics:
            if not isinstance(resource_metric, dict):
                raise OTLPError("resourceMetrics entries must be objects")
            resource = self._resource_labels(resource_metric.get("resource"), result.warnings)
            scope_metrics = resource_metric.get("scopeMetrics") or resource_metric.get(
                "scope_metrics"
            ) or []
            if not isinstance(scope_metrics, list):
                raise OTLPError("scopeMetrics must be an array")
            for scope_metric in scope_metrics:
                if not isinstance(scope_metric, dict):
                    raise OTLPError("scopeMetrics entries must be objects")
                scope = self._scope_labels(scope_metric.get("scope"))
                metrics = scope_metric.get("metrics") or []
                if not isinstance(metrics, list):
                    raise OTLPError("metrics must be an array")
                for metric in metrics:
                    if not isinstance(metric, dict):
                        raise OTLPError("metrics entries must be objects")
                    self._translate_metric(metric, resource, scope, result)
        self.stats["samples"] += len(result.samples)
        return result

    # ---- transaction helpers ------------------------------------------
    def _remember(self, key: Labels) -> None:
        """Snapshot a delta state before the first mutation in this request."""
        if self._txn is None or key in self._txn:
            return
        state = self._delta.get(key)
        self._txn[key] = (
            None
            if state is None
            else replace(
                state,
                buckets=list(state.buckets),
                bounds=list(state.bounds),
            )
        )

    def _set_delta(self, key: Labels, state: _DeltaState) -> None:
        self._remember(key)
        self._delta[key] = state

    def _rollback_delta(self) -> None:
        if not self._txn:
            return
        for key, previous in self._txn.items():
            if previous is None:
                self._delta.pop(key, None)
            else:
                self._delta[key] = previous

    # ---- context -------------------------------------------------------
    def _resource_labels(self, resource, warnings: list[str]) -> dict[str, str]:
        attributes = []
        if isinstance(resource, dict):
            attributes = resource.get("attributes") or []
        pairs = _attributes(attributes)
        service_name = ""
        service_namespace = ""
        instance = ""
        rest: list[tuple[str, str]] = []
        for key, value in pairs:
            if key == "service.name":
                service_name = value
            elif key == "service.namespace":
                service_namespace = value
            elif key == "service.instance.id":
                instance = value
            else:
                rest.append((key, value))
        if service_namespace and not service_name:
            # A namespace without a service name is not an identity we can
            # turn into job; keep it as an ordinary attribute instead of
            # silently dropping it.
            rest.append(("service.namespace", service_namespace))
        mapped = _labels_with_collisions(
            rest, warnings, self.options.translation_strategy
        )
        if service_name:
            job = (
                f"{service_namespace}/{service_name}"
                if service_namespace
                else service_name
            )
            try:
                job.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise OTLPError("service.name is not valid UTF-8") from exc
            mapped["job"] = job
        if instance:
            try:
                instance.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise OTLPError(
                    "service.instance.id is not valid UTF-8"
                ) from exc
            mapped["instance"] = instance
        return mapped

    def _scope_labels(self, scope) -> dict[str, str]:
        if not isinstance(scope, dict):
            return {}
        labels: dict[str, str] = {}
        for key, label in (
            ("name", "otel_scope_name"),
            ("version", "otel_scope_version"),
            ("schemaUrl", "otel_scope_schema_url"),
            ("schema_url", "otel_scope_schema_url"),
        ):
            value = scope.get(key)
            if value:
                labels[label] = str(value)
        return labels

    # ---- metric dispatch ----------------------------------------------
    def _translate_metric(
        self,
        metric: dict,
        resource: dict[str, str],
        scope: dict[str, str],
        result: OTLPResult,
    ) -> None:
        metric = dict(metric)
        metric["_strategy"] = self.options.translation_strategy
        if "gauge" in metric:
            self._translate_gauge(metric, resource, scope, result)
        elif "sum" in metric:
            self._translate_sum(metric, resource, scope, result)
        elif "histogram" in metric:
            self._translate_histogram(metric, resource, scope, result)
        elif "exponentialHistogram" in metric or "exponential_histogram" in metric:
            exponential = (
                metric.get("exponentialHistogram")
                or metric.get("exponential_histogram")
                or {}
            )
            self.stats["droppedExponentialHistograms"] += len(
                exponential.get("dataPoints") or []
            )
            raise OTLPError(
                f"metric {metric.get('name')!r} is an exponential histogram; "
                "Mini-Prometheus has no native-histogram storage"
            )
        elif "summary" in metric:
            self._translate_summary(metric, resource, scope, result)
        else:
            raise OTLPError(f"metric {metric.get('name')!r} has no supported data type")

    def _translate_gauge(self, metric, resource, scope, result) -> None:
        name = _family_name(metric)
        points = (metric.get("gauge") or {}).get("dataPoints") or []
        for point in points:
            self.stats["metricPoints"] += 1
            labels = _point_labels(
                resource,
                scope,
                point.get("attributes"),
                result.warnings,
                self.options.translation_strategy,
            ).set(METRIC_NAME_LABEL, name)
            timestamp = _timestamp_ms(point.get("timeUnixNano"))
            value = _number(point, "asDouble", "asInt", "value")
            result.samples.append((labels, timestamp, value))
            self._count_exemplars(point)

    def _translate_sum(self, metric, resource, scope, result) -> None:
        body = metric.get("sum") or {}
        monotonic = bool(body.get("isMonotonic", body.get("is_monotonic", False)))
        name = _family_name(metric, counter=monotonic)
        temporality = _temporality(
            body.get("aggregationTemporality", body.get("aggregation_temporality"))
        )
        for point in body.get("dataPoints") or []:
            self.stats["metricPoints"] += 1
            labels = _point_labels(
                resource,
                scope,
                point.get("attributes"),
                result.warnings,
                self.options.translation_strategy,
            ).set(METRIC_NAME_LABEL, name)
            timestamp = _timestamp_ms(point.get("timeUnixNano"))
            value = _number(point, "asDouble", "asInt", "value")
            if temporality == DELTA:
                value = self._delta_value(labels, point, value, result)
                if value is None:
                    continue
            result.samples.append((labels, timestamp, value))
            self._count_exemplars(point)

    def _translate_histogram(self, metric, resource, scope, result) -> None:
        body = metric.get("histogram") or {}
        name = _family_name(metric)
        temporality = _temporality(
            body.get("aggregationTemporality", body.get("aggregation_temporality"))
        )
        for point in body.get("dataPoints") or []:
            self.stats["metricPoints"] += 1
            base_labels = _point_labels(
                resource,
                scope,
                point.get("attributes"),
                result.warnings,
                self.options.translation_strategy,
            )
            timestamp = _timestamp_ms(point.get("timeUnixNano"))
            count = _as_float(point.get("count", point.get("countValue", 0)))
            total = _as_float(point.get("sum", 0))
            bounds = [_as_float(item) for item in point.get("explicitBounds") or []]
            bucket_counts = [
                _as_float(item) for item in point.get("bucketCounts") or []
            ]
            if bucket_counts:
                if len(bucket_counts) != len(bounds) + 1:
                    raise OTLPError(
                        f"histogram {metric.get('name')!r} has {len(bucket_counts)} "
                        f"bucketCounts for {len(bounds)} explicitBounds "
                        "(expected exactly one more)"
                    )
            elif bounds:
                raise OTLPError(
                    f"histogram {metric.get('name')!r} has explicitBounds but no bucketCounts"
                )
            else:
                # No bucket information at all: represent the distribution as
                # one +Inf bucket carrying the total count.
                bucket_counts = [count]
            if count == 0 and bucket_counts:
                # count is required by the proto, but exporters in the wild
                # occasionally omit it.  Deriving it from the buckets is
                # safer than publishing a _count of 0 next to non-zero
                # buckets (which makes histogram_quantile/count disagree).
                count = sum(bucket_counts)
            if temporality == DELTA:
                converted = self._delta_histogram(
                    base_labels.set(METRIC_NAME_LABEL, name),
                    point,
                    count,
                    total,
                    bucket_counts,
                    bounds,
                    result,
                )
                if converted is None:
                    continue
                count, total, bucket_counts = converted
            cumulative: list[float] = []
            running = 0.0
            for item in bucket_counts:
                running += item
                cumulative.append(running)
            if count and cumulative and cumulative[-1] > count:
                raise OTLPError(
                    f"histogram {metric.get('name')!r} has buckets containing "
                    f"{cumulative[-1]} observations but count is {count}"
                )
            for index, bound in enumerate(bounds):
                le = _format_bound(bound)
                labels = base_labels.set(METRIC_NAME_LABEL, f"{name}_bucket").set("le", le)
                result.samples.append((labels, timestamp, cumulative[index]))
            labels = base_labels.set(METRIC_NAME_LABEL, f"{name}_bucket").set("le", "+Inf")
            result.samples.append((labels, timestamp, count if count else running))
            result.samples.append(
                (base_labels.set(METRIC_NAME_LABEL, f"{name}_sum"), timestamp, total)
            )
            result.samples.append(
                (base_labels.set(METRIC_NAME_LABEL, f"{name}_count"), timestamp, count)
            )
            self._count_exemplars(point)

    def _translate_summary(self, metric, resource, scope, result) -> None:
        body = metric.get("summary") or {}
        name = _family_name(metric)
        for point in body.get("dataPoints") or []:
            self.stats["metricPoints"] += 1
            base_labels = _point_labels(
                resource,
                scope,
                point.get("attributes"),
                result.warnings,
                self.options.translation_strategy,
            )
            timestamp = _timestamp_ms(point.get("timeUnixNano"))
            count = _as_float(point.get("count", 0))
            total = _as_float(point.get("sum", 0))
            result.samples.append(
                (base_labels.set(METRIC_NAME_LABEL, f"{name}_count"), timestamp, count)
            )
            result.samples.append(
                (base_labels.set(METRIC_NAME_LABEL, f"{name}_sum"), timestamp, total)
            )
            for quantile in point.get("quantileValues") or []:
                q = _as_float(quantile.get("quantile", 0))
                value = _as_float(quantile.get("value", 0))
                labels = base_labels.set(METRIC_NAME_LABEL, name).set(
                    "quantile", _format_bound(q)
                )
                result.samples.append((labels, timestamp, value))
            self._count_exemplars(point)

    # ---- delta conversion ---------------------------------------------
    def _delta_key(self, labels: Labels, point: dict) -> Labels | None:
        start = point.get("startTimeUnixNano")
        if start in (None, "", 0, "0"):
            # Without a start time a reset cannot be distinguished from a
            # continuation.  Treat every point as a fresh baseline instead of
            # inventing a cumulative value that may be wrong forever.
            return None
        return labels

    def _delta_value(
        self, labels: Labels, point: dict, delta: float, result: OTLPResult
    ) -> float | None:
        if not self.options.delta_to_cumulative:
            self.stats["rejectedDeltaPoints"] += 1
            raise OTLPError(
                "delta temporality requires --enable-feature=otlp-deltatocumulative"
            )
        key = self._delta_key(labels, point)
        if key is None:
            result.warnings.append(
                "delta point without startTimeUnixNano treated as a fresh baseline"
            )
            return delta
        start_ns = _as_int(point.get("startTimeUnixNano"))
        timestamp = _timestamp_ms(point.get("timeUnixNano"))
        state = self._delta.get(key)
        if state is None:
            state = _DeltaState(start_ns=start_ns, last_ns=timestamp, value=delta)
            self._store_delta(key, state)
            return delta
        if start_ns != state.start_ns:
            # A new start time means a new stream: the previous cumulative
            # value must not leak into it.
            self.stats["deltaResets"] += 1
            state = _DeltaState(start_ns=start_ns, last_ns=timestamp, value=delta)
            self._set_delta(key, state)
            return delta
        if timestamp <= state.last_ns:
            # A retry resends the same point with the same timestamp; a late
            # point arrives with an older one.  Neither may be added again.
            self.stats["droppedOutOfOrderDeltas"] += 1
            result.warnings.append(
                "duplicate/out-of-order delta point ignored; cumulative value unchanged"
            )
            return None
        self._remember(key)
        state.value += delta
        state.last_ns = timestamp
        return state.value

    def _delta_histogram(
        self,
        labels: Labels,
        point: dict,
        count: float,
        total: float,
        buckets: list[float],
        bounds: list[float],
        result: OTLPResult,
    ) -> tuple[float, float, list[float]] | None:
        if not self.options.delta_to_cumulative:
            self.stats["rejectedDeltaPoints"] += 1
            raise OTLPError(
                "delta temporality requires --enable-feature=otlp-deltatocumulative"
            )
        key = self._delta_key(labels, point)
        if key is None:
            result.warnings.append(
                "delta histogram without startTimeUnixNano treated as a fresh baseline"
            )
            return count, total, buckets
        start_ns = _as_int(point.get("startTimeUnixNano"))
        timestamp = _timestamp_ms(point.get("timeUnixNano"))
        state = self._delta.get(key)
        if state is None:
            state = _DeltaState(
                start_ns=start_ns,
                last_ns=timestamp,
                count=count,
                total=total,
                buckets=list(buckets),
                bounds=list(bounds),
            )
            self._store_delta(key, state)
            return state.count, state.total, state.buckets
        if start_ns != state.start_ns:
            self.stats["deltaResets"] += 1
            state = _DeltaState(
                start_ns=start_ns,
                last_ns=timestamp,
                count=count,
                total=total,
                buckets=list(buckets),
                bounds=list(bounds),
            )
            self._set_delta(key, state)
            return state.count, state.total, state.buckets
        if timestamp <= state.last_ns:
            self.stats["droppedOutOfOrderDeltas"] += 1
            result.warnings.append(
                "duplicate/out-of-order delta histogram point ignored; "
                "cumulative value unchanged"
            )
            return None
        if state.bounds != bounds or len(state.buckets) != len(buckets):
            # Same bucket count but different boundaries would still corrupt
            # the cumulative buckets (an old le=1 observation would appear in
            # the new le=10 bucket), so compare the full layout.
            raise OTLPError(
                "delta histogram bucket layout changed within one stream; "
                "this would silently corrupt the cumulative buckets"
            )
        self._remember(key)
        state.count += count
        state.total += total
        state.buckets = [a + b for a, b in zip(state.buckets, buckets)]
        state.last_ns = timestamp
        return state.count, state.total, state.buckets

    def _store_delta(self, key: Labels, state: _DeltaState) -> None:
        if len(self._delta) >= self.options.max_delta_series and key not in self._delta:
            # Bounded state: fail loudly instead of growing without limit.
            raise OTLPError(
                f"delta conversion state exceeds {self.options.max_delta_series} series"
            )
        self._set_delta(key, state)

    # ---- bookkeeping ---------------------------------------------------
    def _count_exemplars(self, point: dict) -> None:
        self.stats["droppedExemplars"] += len(point.get("exemplars", []) or [])


def _format_bound(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(value)
