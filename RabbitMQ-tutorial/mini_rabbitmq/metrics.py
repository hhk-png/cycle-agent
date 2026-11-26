"""线程安全的计数器与 Prometheus 文本导出。"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Iterable


_SANITIZE = str.maketrans({"-": "_", ".": "_", "/": "_", ":": "_"})


def _name(value: str) -> str:
    return value.translate(_SANITIZE)


class Metrics:
    """极简指标注册表。

    采用扁平 key，例如 ``messages_published_total``。标签由调用方拼进 key
    （如 ``messages_published_total{exchange=amq.direct}``），以保证零依赖。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        name = _name(name)
        with self._lock:
            self._counters[name] += value

    def set(self, name: str, value: float) -> None:
        name = _name(name)
        with self._lock:
            self._gauges[name] = float(value)

    def get(self, name: str, default: float = 0.0) -> float:
        name = _name(name)
        with self._lock:
            return self._counters.get(name, self._gauges.get(name, default))

    def counters(self) -> dict[str, float]:
        with self._lock:
            return dict(self._counters)

    def gauges(self) -> dict[str, float]:
        with self._lock:
            return dict(self._gauges)

    def render_prometheus(
        self,
        prefix: str = "mini_rabbitmq",
        extra_lines: Iterable[str] = (),
    ) -> str:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
        lines: list[str] = []
        for key in sorted(counters):
            if "{" in key:
                metric, labels = key.split("{", 1)
                lines.append(f"# TYPE {prefix}_{metric} counter")
                lines.append(f"{prefix}_{key} {counters[key]:g}")
            else:
                lines.append(f"# TYPE {prefix}_{key} counter")
                lines.append(f"{prefix}_{key} {counters[key]:g}")
        for key in sorted(gauges):
            if "{" in key:
                metric, _ = key.split("{", 1)
                lines.append(f"# TYPE {prefix}_{metric} gauge")
                lines.append(f"{prefix}_{key} {gauges[key]:g}")
            else:
                lines.append(f"# TYPE {prefix}_{key} gauge")
                lines.append(f"{prefix}_{key} {gauges[key]:g}")
        lines.extend(extra_lines)
        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {"counters": dict(self._counters), "gauges": dict(self._gauges)}
