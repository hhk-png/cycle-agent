"""轻量指标系统，对应 Flink MetricGroup/Counter/Gauge/Meter 的核心概念。"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable


class Metric:
    def snapshot(self) -> Any:
        raise NotImplementedError


@dataclass
class Counter(Metric):
    name: str
    value: int = 0

    def inc(self, amount: int = 1) -> None:
        self.value += amount

    def dec(self, amount: int = 1) -> None:
        self.value -= amount

    def snapshot(self) -> int:
        return self.value


@dataclass
class Gauge(Metric):
    name: str
    supplier: Callable[[], Any] = lambda: None

    def snapshot(self) -> Any:
        return self.supplier()


@dataclass
class Meter(Metric):
    name: str
    count: int = 0
    started_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def mark(self, amount: int = 1) -> None:
        self.count += amount

    def rate_per_second(self) -> float:
        elapsed = max(1, int(time.time() * 1000) - self.started_ms)
        return self.count * 1000.0 / elapsed

    def snapshot(self) -> dict[str, Any]:
        return {"count": self.count, "rate_per_second": self.rate_per_second()}


class MetricGroup:
    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self._metrics: dict[str, Metric] = {}
        self._groups: dict[str, "MetricGroup"] = {}

    def counter(self, name: str) -> Counter:
        key = self._key(name)
        metric = self._metrics.get(key)
        if metric is None:
            metric = Counter(key)
            self._metrics[key] = metric
        if not isinstance(metric, Counter):
            raise TypeError(f"{key} 不是 Counter")
        return metric

    def gauge(self, name: str, supplier: Callable[[], Any]) -> Gauge:
        key = self._key(name)
        metric = Gauge(key, supplier)
        self._metrics[key] = metric
        return metric

    def meter(self, name: str) -> Meter:
        key = self._key(name)
        metric = self._metrics.get(key)
        if metric is None:
            metric = Meter(key)
            self._metrics[key] = metric
        if not isinstance(metric, Meter):
            raise TypeError(f"{key} 不是 Meter")
        return metric

    def add_group(self, name: str) -> "MetricGroup":
        group = self._groups.get(name)
        if group is None:
            group = MetricGroup(self._key(name))
            self._groups[name] = group
        return group

    def snapshot(self) -> dict[str, Any]:
        result = {name: metric.snapshot() for name, metric in self._metrics.items()}
        for name, group in self._groups.items():
            result[name] = group.snapshot()
        return result

    def _key(self, name: str) -> str:
        return f"{self.prefix}.{name}" if self.prefix else name


class MetricReporter:
    def report(self, metrics: dict[str, Any]) -> None:
        raise NotImplementedError


class LoggingReporter(MetricReporter):
    def __init__(self) -> None:
        self.history: list[dict[str, Any]] = []

    def report(self, metrics: dict[str, Any]) -> None:
        self.history.append(metrics)
