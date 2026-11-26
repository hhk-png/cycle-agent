"""PromQL function registry and implementations.

Each function declares a type signature (checked by the parser) and a handler
that receives evaluated arguments plus an ``EvalContext``.  Range functions
implement Prometheus' extrapolation rules, including counter reset handling,
because those details decide whether `rate()` matches dashboards and alerts.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Callable

from ..labels import Labels
from ..model import ErrorType, PrometheusError, format_value
from ..util.timeutil import is_stale
from .value import Matrix, Scalar, Series, StringValue, Vector, VectorItem

INSTANT = "vector"
RANGE = "matrix"
SCALAR = "scalar"
STRING = "string"


@dataclass
class EvalContext:
    timestamp: int
    start: int
    end: int
    warnings: list[str] = field(default_factory=list)
    range_hint: int = 0

    @property
    def timestamp_seconds(self) -> float:
        return self.timestamp / 1000.0


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    arg_types: tuple[str, ...]
    return_type: str
    min_args: int
    max_args: int
    handler: Callable
    variadic: bool = False
    experimental: bool = False

    def expected_type(self, index: int) -> str:
        if index < len(self.arg_types):
            return self.arg_types[index]
        return self.arg_types[-1]


FUNCTIONS: dict[str, FunctionSpec] = {}


def _register(
    name: str,
    arg_types: tuple[str, ...],
    return_type: str,
    min_args: int | None = None,
    max_args: int | None = None,
    *,
    variadic: bool = False,
    experimental: bool = False,
):
    def decorator(handler):
        FUNCTIONS[name] = FunctionSpec(
            name,
            arg_types,
            return_type,
            len(arg_types) if min_args is None else min_args,
            len(arg_types) if max_args is None else max_args,
            handler,
            variadic,
            experimental,
        )
        return handler

    return decorator


# ---------------------------------------------------------------------------
# helpers


def _drop_name(labels: Labels) -> Labels:
    return labels.without("__name__")


def _map_vector(value: Vector, fn, *, drop_name: bool = False) -> Vector:
    out = []
    for item in value.items:
        result = fn(item.value)
        if result is None:
            continue
        labels = item.labels
        if drop_name:
            labels = _drop_name(labels)
        out.append(
            VectorItem(labels, result, item.timestamp, item.source_timestamp)
        )
    return Vector(out)


def _single_series(matrix: Matrix) -> list[Series]:
    return [series for series in matrix.series if len(series.samples) > 0]


def _range_start_end(matrix: Matrix, ctx: EvalContext, range_ms: int):
    # The matrix selector already selected exactly the requested window; the
    # function needs the *effective* end (after offset/@), not the query time.
    if matrix.range_ms:
        # end_time can legitimately be 0 (``@ 0`` or a negative offset); a
        # truthiness check would silently fall back to the evaluation time and
        # integrate the wrong window.
        return matrix.end_time - matrix.range_ms, matrix.end_time
    return ctx.timestamp - range_ms, ctx.timestamp


def _result_labels(labels: Labels) -> Labels:
    return _drop_name(labels)


def _quantile(q: float, values: list[float]) -> float:
    if not values or math.isnan(q):
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf
    # Prometheus' vectorByValueHeap sorts NaN before every finite value.
    ordered = sorted(
        values, key=lambda value: (0, 0.0) if math.isnan(value) else (1, value)
    )
    if len(ordered) == 1:
        return ordered[0]
    rank = q * (len(ordered) - 1)
    lower_index = int(math.floor(rank))
    upper_index = int(math.ceil(rank))
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = rank - lower_index
    return ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight


def _stddev(values: list[float], *, sample: bool = False) -> float:
    if not values:
        return math.nan
    mean = sum(values) / len(values)
    divisor = len(values) - 1 if sample else len(values)
    if divisor <= 0:
        return 0.0
    return math.sqrt(sum((value - mean) ** 2 for value in values) / divisor)


def _median(values: list[float]) -> float:
    return _quantile(0.5, values)


def _linear_regression(samples: list[tuple[int, float]], intercept_time: int):
    """Return (slope per second, intercept at intercept_time)."""
    n = len(samples)
    if n < 2:
        return None
    sum_x = sum_y = sum_xy = sum_x2 = 0.0
    for timestamp, value in samples:
        x = (timestamp - intercept_time) / 1000.0
        sum_x += x
        sum_y += value
        sum_xy += x * value
        sum_x2 += x * x
    denominator = n * sum_x2 - sum_x * sum_x
    if denominator == 0:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _extrapolated(
    samples: list[tuple[int, float]],
    range_start: int,
    range_end: int,
    *,
    is_counter: bool,
    is_rate: bool,
    range_ms: int,
) -> float | None:
    """Prometheus' extrapolatedRate(), including reset compensation."""
    if len(samples) < 2:
        return None
    first_ts, first_value = samples[0]
    last_ts, last_value = samples[-1]
    result = last_value - first_value
    if is_counter:
        previous = first_value
        for _, value in samples[1:]:
            if value < previous:
                result += previous
            previous = value
    duration_to_start = (first_ts - range_start) / 1000.0
    duration_to_end = (range_end - last_ts) / 1000.0
    sampled_interval = (last_ts - first_ts) / 1000.0
    if sampled_interval <= 0:
        return None
    average_duration = sampled_interval / (len(samples) - 1)
    if is_counter and result > 0 and first_value >= 0:
        duration_to_zero = sampled_interval * (first_value / result)
        if duration_to_zero < duration_to_start:
            duration_to_start = duration_to_zero
    extrapolation_threshold = average_duration * 1.1
    if duration_to_start >= extrapolation_threshold:
        duration_to_start = average_duration / 2
    if duration_to_end >= extrapolation_threshold:
        duration_to_end = average_duration / 2
    factor = (sampled_interval + duration_to_start + duration_to_end) / sampled_interval
    if is_rate:
        factor /= range_ms / 1000.0
    return result * factor


def _matrix_to_samples(
    matrix: Matrix,
) -> list[tuple[Labels, list[tuple[int, float]]]]:
    return [(series.labels, series.samples) for series in matrix.series if series.samples]


# ---------------------------------------------------------------------------
# range-vector functions


def _rate_factory(is_counter: bool, is_rate: bool):
    def handler(args, ctx: EvalContext):
        matrix: Matrix = args[0]
        range_ms = ctx.range_hint
        range_start, range_end = _range_start_end(matrix, ctx, range_ms)
        out = []
        for labels, samples in _matrix_to_samples(matrix):
            cleaned = [(ts, value) for ts, value in samples if not is_stale(value)]
            value = _extrapolated(
                cleaned,
                range_start,
                range_end,
                is_counter=is_counter,
                is_rate=is_rate,
                range_ms=range_ms,
            )
            if value is not None:
                out.append(VectorItem(_drop_name(labels), value, ctx.timestamp))
        return Vector(out)

    return handler


FUNCTIONS["rate"] = FunctionSpec("rate", (RANGE,), INSTANT, 1, 1, _rate_factory(True, True))
FUNCTIONS["increase"] = FunctionSpec("increase", (RANGE,), INSTANT, 1, 1, _rate_factory(True, False))
FUNCTIONS["delta"] = FunctionSpec("delta", (RANGE,), INSTANT, 1, 1, _rate_factory(False, False))


def _irate_handler(args, ctx: EvalContext):
    matrix: Matrix = args[0]
    out = []
    for labels, samples in _matrix_to_samples(matrix):
        cleaned = [(ts, value) for ts, value in samples if not is_stale(value)]
        if len(cleaned) < 2:
            continue
        (t0, v0), (t1, v1) = cleaned[-2], cleaned[-1]
        if t1 == t0:
            continue
        result = v1 - v0
        if result < 0:
            result = v1
        out.append(
            VectorItem(_drop_name(labels), result / ((t1 - t0) / 1000.0), ctx.timestamp)
        )
    return Vector(out)


def _idelta_handler(args, ctx: EvalContext):
    matrix: Matrix = args[0]
    out = []
    for labels, samples in _matrix_to_samples(matrix):
        cleaned = [(ts, value) for ts, value in samples if not is_stale(value)]
        if len(cleaned) < 2:
            continue
        result = cleaned[-1][1] - cleaned[-2][1]
        out.append(VectorItem(_drop_name(labels), result, ctx.timestamp))
    return Vector(out)


FUNCTIONS["irate"] = FunctionSpec("irate", (RANGE,), INSTANT, 1, 1, _irate_handler)
FUNCTIONS["idelta"] = FunctionSpec("idelta", (RANGE,), INSTANT, 1, 1, _idelta_handler)


def _deriv_handler(args, ctx: EvalContext):
    matrix: Matrix = args[0]
    out = []
    for labels, samples in _matrix_to_samples(matrix):
        cleaned = [(ts, value) for ts, value in samples if not is_stale(value)]
        regression = _linear_regression(cleaned, ctx.timestamp)
        if regression is None:
            continue
        out.append(VectorItem(_drop_name(labels), regression[0], ctx.timestamp))
    return Vector(out)


@_register("deriv", (RANGE,), INSTANT)
def _deriv(args, ctx):
    return _deriv_handler(args, ctx)


@_register("predict_linear", (RANGE, SCALAR), INSTANT)
def _predict_linear(args, ctx):
    matrix: Matrix = args[0]
    seconds: float = args[1].value
    out = []
    for labels, samples in _matrix_to_samples(matrix):
        cleaned = [(ts, value) for ts, value in samples if not is_stale(value)]
        regression = _linear_regression(cleaned, ctx.timestamp)
        if regression is None:
            continue
        slope, intercept = regression
        out.append(
            VectorItem(_drop_name(labels), slope * seconds + intercept, ctx.timestamp)
        )
    return Vector(out)


def _resets_handler(args, ctx: EvalContext):
    matrix: Matrix = args[0]
    out = []
    for labels, samples in _matrix_to_samples(matrix):
        count = 0
        previous = None
        for _, value in samples:
            if is_stale(value):
                continue
            if previous is not None and value < previous:
                count += 1
            previous = value
        out.append(VectorItem(_drop_name(labels), float(count), ctx.timestamp))
    return Vector(out)


def _changes_handler(args, ctx: EvalContext):
    matrix: Matrix = args[0]
    out = []
    for labels, samples in _matrix_to_samples(matrix):
        count = 0
        previous = None
        for _, value in samples:
            if is_stale(value):
                continue
            # NaN -> NaN is not a change (NaN != NaN in Python, so this needs
            # an explicit exception, matching Prometheus).
            if previous is not None and not (
                math.isnan(previous) and math.isnan(value)
            ) and value != previous:
                count += 1
            previous = value
        out.append(VectorItem(_drop_name(labels), float(count), ctx.timestamp))
    return Vector(out)


FUNCTIONS["resets"] = FunctionSpec("resets", (RANGE,), INSTANT, 1, 1, _resets_handler)
FUNCTIONS["changes"] = FunctionSpec("changes", (RANGE,), INSTANT, 1, 1, _changes_handler)
# absent()/absent_over_time() are evaluated specially (they need the AST to
# synthesize labels from equality matchers); the handler is only a fallback.
FUNCTIONS["absent"] = FunctionSpec("absent", (INSTANT,), INSTANT, 1, 1, lambda args, ctx: Vector([]))
FUNCTIONS["absent_over_time"] = FunctionSpec(
    "absent_over_time", (RANGE,), INSTANT, 1, 1, lambda args, ctx: Vector([])
)


def _over_time_factory(mode: str):
    def handler(args, ctx: EvalContext):
        matrix: Matrix = args[0]
        out = []
        for labels, samples in _matrix_to_samples(matrix):
            values = [value for _, value in samples if not is_stale(value)]
            if not values:
                continue
            if mode == "sum":
                result = sum(values)
            elif mode == "avg":
                result = sum(values) / len(values)
            elif mode == "count":
                result = float(len(values))
            elif mode == "min":
                finite = [value for value in values if not math.isnan(value)]
                result = min(finite) if finite else math.nan
            elif mode == "max":
                finite = [value for value in values if not math.isnan(value)]
                result = max(finite) if finite else math.nan
            elif mode == "last":
                result = values[-1]
            elif mode == "first":
                result = values[0]
            elif mode == "present":
                result = 1.0
            elif mode == "stddev":
                result = _stddev(values)
            elif mode == "stdvar":
                result = _stddev(values) ** 2
            elif mode == "mad":
                median = _median(values)
                result = _median([abs(value - median) for value in values])
            else:
                raise ValueError(mode)
            # first_over_time and last_over_time are the two range functions
            # that preserve the metric name (like offset); all other
            # *_over_time drop it.
            result_labels = labels if mode in ("first", "last") else _drop_name(labels)
            out.append(VectorItem(result_labels, result, ctx.timestamp))
        return Vector(out)

    return handler


for _mode in ("sum", "avg", "count", "min", "max", "last", "first", "present",
              "stddev", "stdvar", "mad"):
    _name = {
        "sum": "sum_over_time",
        "avg": "avg_over_time",
        "count": "count_over_time",
        "min": "min_over_time",
        "max": "max_over_time",
        "last": "last_over_time",
        "first": "first_over_time",
        "present": "present_over_time",
        "stddev": "stddev_over_time",
        "stdvar": "stdvar_over_time",
        "mad": "mad_over_time",
    }[_mode]
    FUNCTIONS[_name] = FunctionSpec(_name, (RANGE,), INSTANT, 1, 1, _over_time_factory(_mode))


def _ts_over_time_factory(which: str):
    """Return the timestamp (seconds) of the first/last sample in the window.

    Like the other timestamp helpers, these functions drop ``__name__``.  The
    timestamp is the *sample* timestamp, not the evaluation time.
    """

    def handler(args, ctx: EvalContext):
        matrix: Matrix = args[0]
        out = []
        for labels, samples in _matrix_to_samples(matrix):
            cleaned = [(ts, value) for ts, value in samples if not is_stale(value)]
            if not cleaned:
                continue
            timestamp = cleaned[0][0] if which == "first" else cleaned[-1][0]
            out.append(
                VectorItem(_drop_name(labels), timestamp / 1000.0, ctx.timestamp)
            )
        return Vector(out)

    return handler


FUNCTIONS["ts_of_first_over_time"] = FunctionSpec(
    "ts_of_first_over_time", (RANGE,), INSTANT, 1, 1,
    _ts_over_time_factory("first"), experimental=True,
)
FUNCTIONS["ts_of_last_over_time"] = FunctionSpec(
    "ts_of_last_over_time", (RANGE,), INSTANT, 1, 1,
    _ts_over_time_factory("last"), experimental=True,
)


@_register("integral", (RANGE,), INSTANT, experimental=True)
def _integral(args, ctx: EvalContext):
    """Trapezoidal integral over the samples in a range vector.

    Prometheus maps NaN samples to zero for this function and returns 0 when
    the window has a single sample (there is no interval to integrate).
    """
    matrix: Matrix = args[0]
    out = []
    for labels, samples in _matrix_to_samples(matrix):
        cleaned = [
            (ts, 0.0 if math.isnan(value) else value)
            for ts, value in samples
            if not is_stale(value)
        ]
        if not cleaned:
            continue
        total = 0.0
        for (prev_ts, prev_value), (curr_ts, curr_value) in zip(cleaned, cleaned[1:]):
            total += (prev_value + curr_value) / 2.0 * (curr_ts - prev_ts) / 1000.0
        out.append(VectorItem(_drop_name(labels), total, ctx.timestamp))
    return Vector(out)


@_register("quantile_over_time", (SCALAR, RANGE), INSTANT)
def _quantile_over_time(args, ctx):
    q: float = args[0].value
    matrix: Matrix = args[1]
    out = []
    for labels, samples in _matrix_to_samples(matrix):
        values = [value for _, value in samples if not is_stale(value)]
        if not values:
            continue
        out.append(VectorItem(_drop_name(labels), _quantile(q, values), ctx.timestamp))
    return Vector(out)


@_register("holt_winters", (RANGE, SCALAR, SCALAR), INSTANT)
@_register("double_exponential_smoothing", (RANGE, SCALAR, SCALAR), INSTANT)
def _holt_winters(args, ctx):
    matrix: Matrix = args[0]
    sf: float = args[1].value
    tf: float = args[2].value
    if not (0 < sf < 1) or not (0 < tf < 1):
        raise PrometheusError(
            "invalid smoothing factor: expected 0 < sf < 1 and 0 < tf < 1",
            error_type=ErrorType.EXECUTION,
        )
    out = []
    for labels, samples in _matrix_to_samples(matrix):
        values = [value for _, value in samples if not is_stale(value)]
        # Upstream skips series with fewer than two samples.
        if len(values) < 2:
            continue
        # Prometheus' initialization and trend update:
        #   s0 = 0, s1 = v[0], b = v[1] - v[0]
        #   for i >= 2: b = tf*(s1-s0) + (1-tf)*b
        #   s0 = s1; s1 = sf*v[i] + (1-sf)*(s1+b)
        s0 = 0.0
        s1 = values[0]
        b = values[1] - values[0]
        for index in range(1, len(values)):
            value = values[index]
            if index > 1:
                b = tf * (s1 - s0) + (1 - tf) * b
            s0 = s1
            s1 = sf * value + (1 - sf) * (s1 + b)
        out.append(VectorItem(_drop_name(labels), s1, ctx.timestamp))
    return Vector(out)


# ---------------------------------------------------------------------------
# histogram


@_register("histogram_quantile", (SCALAR, INSTANT), INSTANT)
def _histogram_quantile(args, ctx):
    q: float = args[0].value
    vector: Vector = args[1]
    groups: dict[Labels, list[tuple[float, float]]] = {}
    for item in vector.items:
        if "le" not in item.labels:
            ctx.warnings.append(
                "histogram_quantile: series missing 'le' label was ignored"
            )
            continue
        try:
            upper = float(item.labels.get("le"))
        except ValueError:
            continue
        base = item.labels.without("le", "__name__")
        groups.setdefault(base, []).append((upper, item.value))
    out = []
    for labels, buckets in groups.items():
        value = _bucket_quantile(q, buckets)
        out.append(VectorItem(labels, value, ctx.timestamp))
    return Vector(out)


def _bucket_quantile(q: float, buckets: list[tuple[float, float]]) -> float:
    """Prometheus' classic-histogram quantile, including its +Inf fallback.

    The highest bucket must be ``+Inf``.  A quantile that lands in that bucket
    cannot be interpolated (there is no finite upper edge), so Prometheus
    returns the upper bound of the *second highest* bucket.  Returning ``+Inf``
    here would be a visible semantic difference from every real Prometheus.
    """
    # Prometheus checks the quantile bounds before looking at the buckets.
    if math.isnan(q):
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf
    if not buckets:
        return math.nan
    buckets = sorted(buckets, key=lambda item: item[0])
    if not math.isinf(buckets[-1][0]) or buckets[-1][0] < 0:
        return math.nan
    # Enforce monotonicity, as Prometheus does, then find the rank.
    cumulative = []
    previous = 0.0
    for upper, count in buckets:
        if math.isnan(count) or count < 0:
            count = 0.0
        if count < previous:
            count = previous
        cumulative.append((upper, count))
        previous = count
    # Upstream also returns NaN when the +Inf bucket is the only bucket.
    if len(cumulative) < 2:
        return math.nan
    total = cumulative[-1][1]
    if total == 0:
        return math.nan
    rank = q * total
    # Search only the finite buckets.  If none has enough observations, the
    # quantile is in the open-ended +Inf bucket, and the last finite bound is
    # the most precise answer available.
    index = 0
    while index < len(cumulative) - 1 and cumulative[index][1] < rank:
        index += 1
    if index == len(cumulative) - 1:
        return cumulative[-2][0]
    upper, count = cumulative[index]
    if index == 0 and upper <= 0:
        return upper
    if index == 0:
        lower_upper, lower_count = 0.0, 0.0
    else:
        lower_upper, lower_count = cumulative[index - 1]
    if count == lower_count:
        # Matches upstream: interpolation is undefined when the bucket adds
        # no observations.
        return math.nan
    return lower_upper + (upper - lower_upper) * (rank - lower_count) / (
        count - lower_count
    )


# ---------------------------------------------------------------------------
# scalar/vector conversions and time


@_register("time", (), SCALAR)
def _time(args, ctx):
    return Scalar(ctx.timestamp_seconds)


@_register("pi", (), SCALAR)
def _pi(args, ctx):
    return Scalar(math.pi)


@_register("vector", (SCALAR,), INSTANT)
def _vector(args, ctx):
    return Vector([VectorItem(Labels.empty(), args[0].value, ctx.timestamp)])


@_register("scalar", (INSTANT,), SCALAR)
def _scalar(args, ctx):
    vector: Vector = args[0]
    if len(vector.items) != 1:
        return Scalar(math.nan)
    return Scalar(vector.items[0].value)


@_register("timestamp", (INSTANT,), INSTANT)
def _timestamp(args, ctx):
    vector: Vector = args[0]
    out = []
    for item in vector.items:
        out.append(
            VectorItem(
                _drop_name(item.labels),
                (
                    item.source_timestamp
                    if item.source_timestamp is not None
                    else item.timestamp
                )
                / 1000.0,
                ctx.timestamp,
            )
        )
    return Vector(out)


# ---------------------------------------------------------------------------
# labels


@_register("label_replace", (INSTANT, STRING, STRING, STRING, STRING), INSTANT)
def _label_replace(args, ctx):
    vector: Vector = args[0]
    dst = args[1].value
    replacement = args[2].value
    src = args[3].value
    pattern = args[4].value
    try:
        # Upstream compiles the regex as ``^(?s:re)$``: the whole label value
        # must match, and ``.`` also matches newlines.
        regex = re.compile(pattern, re.DOTALL)
    except re.error as exc:
        raise ValueError(f"label_replace: invalid regex {pattern!r}: {exc}") from exc
    out = []
    for item in vector.items:
        source = item.labels.get(src)
        match = regex.fullmatch(source)
        if match is None:
            # No match leaves the series unchanged (including dst).
            out.append(item)
            continue
        new_value = _go_expand_match(match, replacement)
        # An empty expansion deletes dst, matching Prometheus' labels builder.
        labels = item.labels.set(dst, new_value)
        out.append(VectorItem(labels, item.value, item.timestamp, item.source_timestamp))
    return Vector(out)


@_register("label_join", (INSTANT, STRING, STRING, STRING), INSTANT, min_args=4, max_args=32)
def _label_join(args, ctx):
    vector: Vector = args[0]
    dst = args[1].value
    separator = args[2].value
    sources = [arg.value for arg in args[3:]]
    out = []
    for item in vector.items:
        joined = separator.join(item.labels.get(name) for name in sources)
        labels = item.labels.set(dst, joined)
        out.append(VectorItem(labels, item.value, item.timestamp, item.source_timestamp))
    return Vector(out)


# ---------------------------------------------------------------------------
# sorting / selection


def _sort_handler(descending: bool):
    def handler(args, ctx):
        vector: Vector = args[0]
        # Prometheus always places NaN at the end, for both sort() and
        # sort_desc().  Reversing the whole list would move NaN to the front
        # in the descending case.
        finite = [item for item in vector.items if not math.isnan(item.value)]
        nans = [item for item in vector.items if math.isnan(item.value)]
        finite.sort(key=lambda item: item.value, reverse=descending)
        return Vector(finite + nans)

    return handler


FUNCTIONS["sort"] = FunctionSpec("sort", (INSTANT,), INSTANT, 1, 1, _sort_handler(False))
FUNCTIONS["sort_desc"] = FunctionSpec("sort_desc", (INSTANT,), INSTANT, 1, 1, _sort_handler(True))


def _natural_sort_key(value: str) -> tuple:
    """Natural sort key, so ``a2`` sorts before ``a10``.

    Prometheus' sort_by_label uses natural sort; plain lexicographic order
    puts ``a10`` before ``a2`` and surprises dashboard readers.
    """
    parts = re.split(r"(\d+)", value)
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in parts
    )


@_register("sort_by_label", (INSTANT, STRING), INSTANT, max_args=32)
def _sort_by_label(args, ctx):
    vector: Vector = args[0]
    names = [arg.value for arg in args[1:]]
    return Vector(
        sorted(
            vector.items,
            key=lambda item: tuple(
                _natural_sort_key(item.labels.get(n)) for n in names
            ),
        )
    )


@_register("sort_by_label_desc", (INSTANT, STRING), INSTANT, max_args=32)
def _sort_by_label_desc(args, ctx):
    vector: Vector = args[0]
    names = [arg.value for arg in args[1:]]
    return Vector(
        sorted(
            vector.items,
            key=lambda item: tuple(
                _natural_sort_key(item.labels.get(n)) for n in names
            ),
            reverse=True,
        )
    )


# ---------------------------------------------------------------------------
# math


def _unary_math(name: str, fn):
    def handler(args, ctx):
        if args[0].type == SCALAR:
            return Scalar(_go_math(fn, args[0].value))
        # Prometheus drops __name__ for transforming math functions.
        return _map_vector(
            args[0], lambda value: _go_math(fn, value), drop_name=True
        )

    FUNCTIONS[name] = FunctionSpec(name, (INSTANT,), INSTANT, 1, 1, handler)


def _go_math(fn, value: float) -> float:
    """Apply a Go ``math`` function with Go's edge-case semantics.

    Python raises for several inputs where Go returns Inf/NaN (log(0),
    exp overflow, ceil(Inf), atanh(±1)), so the exceptions cannot be mapped
    to NaN unconditionally.
    """
    try:
        return fn(value)
    except ValueError:
        if math.isnan(value):
            return math.nan
        if value == 0 and fn in (math.log, math.log2, math.log10):
            return -math.inf
        if fn is math.atanh:
            if value == 1:
                return math.inf
            if value == -1:
                return -math.inf
        return math.nan
    except OverflowError:
        if fn in (math.ceil, math.floor) and math.isinf(value):
            return value
        if fn in (math.exp, math.cosh):
            return math.inf
        if fn is math.sinh:
            return math.inf if value > 0 else -math.inf
        return math.nan


for _name, _fn in {
    "abs": abs,
    "ceil": math.ceil,
    "floor": math.floor,
    "exp": math.exp,
    "ln": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "asinh": math.asinh,
    "acosh": math.acosh,
    "atanh": math.atanh,
    "deg": math.degrees,
    "rad": math.radians,
    "sgn": lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else x),
}.items():
    _unary_math(_name, _fn)


def _go_max(a: float, b: float) -> float:
    """Go math.Max: NaN propagates, unlike Python's max()."""
    if math.isnan(a) or math.isnan(b):
        return math.nan
    return a if a > b else b


def _go_min(a: float, b: float) -> float:
    """Go math.Min: NaN propagates, unlike Python's min()."""
    if math.isnan(a) or math.isnan(b):
        return math.nan
    return a if a < b else b


@_register("clamp", (INSTANT, SCALAR, SCALAR), INSTANT)
def _clamp(args, ctx):
    vector: Vector = args[0]
    minimum: float = args[1].value
    maximum: float = args[2].value
    if maximum < minimum:
        return Vector([])
    return _map_vector(
        vector,
        lambda value: _go_max(minimum, _go_min(maximum, value)),
        drop_name=True,
    )


@_register("clamp_min", (INSTANT, SCALAR), INSTANT)
def _clamp_min(args, ctx):
    minimum = args[1].value
    return _map_vector(
        args[0], lambda value: _go_max(minimum, value), drop_name=True
    )


@_register("clamp_max", (INSTANT, SCALAR), INSTANT)
def _clamp_max(args, ctx):
    maximum = args[1].value
    return _map_vector(
        args[0], lambda value: _go_min(maximum, value), drop_name=True
    )


@_register("round", (INSTANT, SCALAR), INSTANT, min_args=1, max_args=2)
def _round(args, ctx):
    nearest = args[1].value if len(args) > 1 else 1.0
    if nearest == 0 or not math.isfinite(nearest):
        # Prometheus computes 1/toNearest and then 0/0 or Inf/Inf; each
        # sample becomes NaN, but the series is kept.
        return _map_vector(args[0], lambda value: math.nan, drop_name=True)

    def round_half_up(value: float) -> float:
        if math.isnan(value) or math.isinf(value):
            return value
        # Prometheus: floor(v/toNearest + 0.5) * toNearest.  Negative ties
        # therefore round toward +Inf (-1.5 -> -1), not away from zero.
        return math.floor(value / nearest + 0.5) * nearest

    return _map_vector(args[0], round_half_up, drop_name=True)


# ---------------------------------------------------------------------------
# date and time


def _date_part(name: str, fn):
    def handler(args, ctx):
        if args:
            vector: Vector = args[0]
            out = []
            for item in vector.items:
                stamp = (
                    item.source_timestamp
                    if item.source_timestamp is not None
                    else ctx.timestamp
                )
                out.append(
                    VectorItem(
                        _drop_name(item.labels),
                        float(fn(_to_datetime(stamp))),
                        ctx.timestamp,
                    )
                )
            return Vector(out)
        return Vector([VectorItem(Labels.empty(), float(fn(_to_datetime(ctx.timestamp))), ctx.timestamp)])

    FUNCTIONS[name] = FunctionSpec(name, (INSTANT,), INSTANT, 0, 1, handler)


def _to_datetime(timestamp_ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(timestamp_ms / 1000.0, tz=_dt.timezone.utc)


def _days_in_month(dt: _dt.datetime) -> int:
    if dt.month == 12:
        following = dt.replace(year=dt.year + 1, month=1, day=1)
    else:
        following = dt.replace(month=dt.month + 1, day=1)
    return (following - _dt.timedelta(days=1)).day


for _name, _fn in {
    "day_of_month": lambda dt: dt.day,
    "day_of_week": lambda dt: (dt.weekday() + 1) % 7,
    "day_of_year": lambda dt: dt.timetuple().tm_yday,
    "days_in_month": _days_in_month,
    "hour": lambda dt: dt.hour,
    "minute": lambda dt: dt.minute,
    "month": lambda dt: dt.month,
    "year": lambda dt: dt.year,
}.items():
    _date_part(_name, _fn)


# ---------------------------------------------------------------------------
# set-like helpers exposed as functions


def hash_labels(labels: Labels) -> float:
    digest = hashlib.sha256(
        "\xff".join(f"{n}={v}" for n, v in labels.items()).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def label_value(labels: Labels, name: str) -> str:
    return labels.get(name)


# Go's Expand consumes the longest alphanumeric name after '$': "$1x" is the
# name "1x" (normally undefined), not group 1 followed by "x".
_GO_EXPAND_RE = re.compile(r"\$\$|\$\{([A-Za-z0-9_]+)\}|\$([A-Za-z0-9_]+)")


def _go_expand_match(match: re.Match[str], replacement: str) -> str:
    """Expand Go's ``$1``/``${name}`` syntax, missing groups become empty.

    Python's ``Match.expand`` raises ``IndexError`` for an absent group and
    treats an unknown name differently, while Go's ``Expand`` substitutes an
    empty string.  This matters for ``label_replace`` and relabeling, where a
    missing group silently changes the label value.
    """

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


__all__ = ["FUNCTIONS", "FunctionSpec", "EvalContext", "hash_labels", "label_value", "format_value"]
