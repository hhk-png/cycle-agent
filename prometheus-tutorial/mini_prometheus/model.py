"""Core value objects shared by storage, query and API layers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from .labels import Labels


class ErrorType(str, Enum):
    BAD_DATA = "bad_data"
    INTERNAL = "internal"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    CANCELED = "canceled"
    EXECUTION = "execution"


class PrometheusError(Exception):
    """An error that maps onto Prometheus' JSON error envelope."""

    def __init__(self, message: str, *, error_type: ErrorType = ErrorType.INTERNAL):
        super().__init__(message)
        self.error_type = error_type


class ParseError(PrometheusError):
    def __init__(self, message: str, position: int | None = None):
        suffix = f" at char {position}" if position is not None else ""
        super().__init__(message + suffix, error_type=ErrorType.BAD_DATA)
        self.position = position


class ConfigError(PrometheusError):
    def __init__(self, message: str):
        super().__init__(message, error_type=ErrorType.BAD_DATA)


class StorageError(PrometheusError):
    def __init__(self, message: str):
        super().__init__(message, error_type=ErrorType.INTERNAL)


class OutOfOrderError(StorageError):
    pass


class DuplicateSampleError(StorageError):
    pass


class MetricType(str, Enum):
    UNKNOWN = "unknown"
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    GAUGEHISTOGRAM = "gaugehistogram"
    SUMMARY = "summary"
    INFO = "info"
    STATESET = "stateset"


@dataclass(frozen=True, slots=True)
class Sample:
    timestamp: int  # milliseconds
    value: float

    def is_stale(self) -> bool:
        from .util.timeutil import is_stale

        return is_stale(self.value)


@dataclass(slots=True)
class TimeSeries:
    labels: Labels
    samples: list[Sample] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.samples.sort(key=lambda sample: sample.timestamp)

    def add(self, timestamp: int, value: float) -> None:
        self.samples.append(Sample(timestamp, value))

    def __len__(self) -> int:
        return len(self.samples)


@dataclass(frozen=True, slots=True)
class Exemplar:
    labels: Labels
    value: float
    timestamp: int

    def to_api(self) -> dict:
        return {
            "labels": self.labels.to_dict(),
            "value": format_value(self.value),
            "timestamp": self.timestamp / 1000.0,
        }


@dataclass(frozen=True, slots=True)
class MetricMetadata:
    metric: str
    type: MetricType = MetricType.UNKNOWN
    help: str = ""
    unit: str = ""


@dataclass(frozen=True, slots=True)
class Interval:
    mint: int
    maxt: int

    def contains(self, ts: int) -> bool:
        return self.mint <= ts < self.maxt

    def overlaps(self, other: "Interval") -> bool:
        return self.mint < other.maxt and other.mint < self.maxt


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    ordered = sorted(intervals, key=lambda item: (item.mint, item.maxt))
    out: list[Interval] = []
    for interval in ordered:
        if not out or interval.mint > out[-1].maxt:
            out.append(interval)
        elif interval.maxt > out[-1].maxt:
            out[-1] = Interval(out[-1].mint, interval.maxt)
    return out


def format_value(value: float) -> str:
    """Format a sample the way Prometheus' JSON API does, as a string."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == 0:
        return "0"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def format_labels(labels: Labels) -> str:
    """Canonical ``metric{a="b"}`` form used in logs and UI.

    Delegates to :func:`mini_prometheus.labels.format_labels` so UTF-8 names
    are quoted and escaped consistently everywhere.
    """
    from .labels import format_labels as _format_labels

    return _format_labels(labels)
