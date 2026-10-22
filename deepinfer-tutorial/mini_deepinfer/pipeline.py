"""Cost models for asynchronous scheduling and shape-specialised replay.

There is no GPU here.  The simulator makes the overlap argument explicit and
the shape cache provides a real (CPU-observable) analogue of CUDA Graph
replay: work is keyed by a static shape and the per-shape preparation is done
once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class StageTimings:
    schedule_ms: float = 1.2
    prepare_input_ms: float = 0.0
    forward_ms: float = 8.0
    sample_ms: float = 0.8
    post_ms: float = 0.6

    @property
    def cpu_ms(self) -> float:
        return self.schedule_ms + self.prepare_input_ms + self.post_ms

    @property
    def device_ms(self) -> float:
        return self.forward_ms + self.sample_ms


@dataclass
class PipelineResult:
    synchronous_ms: float
    overlapped_ms: float
    speedup: float
    bubble_fraction: float


def simulate_zero_overhead(steps: int, timings: StageTimings) -> PipelineResult:
    """Compare a serial step with the ZOS overlap pipeline.

    Serial: schedule -> forward -> sample -> post, with the device idle during
    the CPU phases.  Overlapped: while the device runs forward/sample for step
    N, the CPU schedules N+1 and post-processes N-1.  The steady-state period
    is the slower of the CPU and device pipelines.
    """
    if steps < 1:
        raise ValueError("steps must be positive")
    serial = steps * (timings.cpu_ms + timings.device_ms)
    period = max(timings.cpu_ms, timings.device_ms)
    overlapped = period * steps + min(timings.cpu_ms, timings.device_ms)
    speedup = serial / overlapped if overlapped else 1.0
    bubble = max(0.0, period - timings.device_ms) / period if period else 0.0
    return PipelineResult(serial, overlapped, speedup, bubble)


class ShapeSpecializedRunner:
    """A tiny analogue of CUDA Graph capture/replay.

    ``capture`` stores a prepared callable and its static key.  ``run`` with
    the same key replays it without rebuilding metadata; a new key pays the
    capture cost once.
    """

    def __init__(self) -> None:
        self._graphs: dict[tuple, Callable[[], object]] = {}
        self.hits = 0
        self.misses = 0
        self.captures = 0

    def capture(self, key: tuple, fn: Callable[[], object]) -> None:
        self._graphs[key] = fn
        self.captures += 1

    def run(self, key: tuple, fn: Callable[[], object]):
        graph = self._graphs.get(key)
        if graph is None:
            self.misses += 1
            self.capture(key, fn)
            graph = fn
        else:
            self.hits += 1
        return graph()

    def stats(self) -> dict[str, int]:
        return {
            "graphs": len(self._graphs),
            "hits": self.hits,
            "misses": self.misses,
            "captures": self.captures,
        }


class DecodePerfCurve:
    """Piecewise-linear lookup of measured decode step time by batch size."""

    def __init__(self, points: list[tuple[int, float]]) -> None:
        if not points:
            raise ValueError("at least one point is required")
        self.points = sorted(points)

    def predict_ms(self, batch_size: int) -> float:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if batch_size <= self.points[0][0]:
            return self.points[0][1]
        for (x0, y0), (x1, y1) in zip(self.points, self.points[1:]):
            if batch_size <= x1:
                ratio = (batch_size - x0) / (x1 - x0)
                return y0 + ratio * (y1 - y0)
        return self.points[-1][1]

    def best_batch_size(self, latency_budget_ms: float) -> int:
        candidates = [size for size, time in self.points if time <= latency_budget_ms]
        return max(candidates) if candidates else self.points[0][0]
