"""In-memory head block.

The head owns the write path: it maps label sets to series, appends samples to
XOR chunks, enforces in-order ingestion, and records tombstones.  On a
successful block flush it gives up whole chunks that lie entirely before the
cut time, which keeps block and head time ranges disjoint.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..labels import Labels, Matcher, match_all
from ..model import (
    DuplicateSampleError,
    Interval,
    OutOfOrderError,
    Sample,
    merge_intervals,
)
from .chunkenc import DEFAULT_SAMPLES_PER_CHUNK, XORChunk

# A single chunk never spans more than two hours, mirroring the upstream
# `chunkRange` default.  Together with the sample count cap this bounds the
# cost of decoding one chunk.
MAX_CHUNK_RANGE_MS = 2 * 60 * 60 * 1000


@dataclass(slots=True)
class HeadSeries:
    ref: int
    labels: Labels
    chunks: list[XORChunk] = field(default_factory=list)
    min_time: int = 0
    max_time: int = 0
    last_ts: int | None = None
    last_value: float = 0.0
    num_samples: int = 0

    def append(self, timestamp: int, value: float) -> bool:
        """Append a sample; return False when an identical duplicate is ignored."""
        if self.last_ts is not None and timestamp < self.last_ts:
            raise OutOfOrderError(
                f"out of order sample: series {self.labels.get('__name__')} got "
                f"timestamp {timestamp} after {self.last_ts}"
            )
        if self.last_ts is not None and timestamp == self.last_ts:
            if value == self.last_value or (
                value != value and self.last_value != self.last_value
            ):
                return False
            raise DuplicateSampleError(
                f"duplicate sample for timestamp {timestamp} with a different value"
            )
        chunk = self.chunks[-1] if self.chunks else None
        if (
            chunk is None
            or chunk.num_samples >= DEFAULT_SAMPLES_PER_CHUNK
            or timestamp - chunk.min_time > MAX_CHUNK_RANGE_MS
        ):
            chunk = XORChunk()
            self.chunks.append(chunk)
        chunk.append(timestamp, value)
        if self.num_samples == 0:
            self.min_time = timestamp
        self.max_time = timestamp
        self.last_ts = timestamp
        self.last_value = value
        self.num_samples += 1
        return True

    def samples(self, mint: int, maxt: int) -> list[Sample]:
        """Samples with ``mint <= t <= maxt``, in time order."""
        out: list[Sample] = []
        for chunk in self.chunks:
            if chunk.max_time < mint or chunk.min_time > maxt:
                continue
            for timestamp, value in chunk.samples():
                if mint <= timestamp <= maxt:
                    out.append(Sample(timestamp, value))
        return out

    def appendable(self, timestamp: int) -> bool:
        return self.last_ts is None or timestamp >= self.last_ts

    def chunk_span(self) -> int:
        return self.max_time - self.min_time


class Head:
    def __init__(self, *, chunk_samples: int = DEFAULT_SAMPLES_PER_CHUNK) -> None:
        self._lock = threading.RLock()
        self._series_by_ref: dict[int, HeadSeries] = {}
        self._ref_by_labels: dict[Labels, int] = {}
        self._next_ref = 1
        self._tombstones: dict[int, list[Interval]] = {}
        # None means "no samples yet"; a real sample can be at t=0 or negative.
        self._min_time: int | None = None
        self._max_time: int | None = None
        self._num_samples = 0
        self._chunk_samples = chunk_samples

    # ---- lifecycle -----------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self._series_by_ref.clear()
            self._ref_by_labels.clear()
            self._tombstones.clear()
            self._next_ref = 1
            self._min_time = None
            self._max_time = None
            self._num_samples = 0

    # ---- writes --------------------------------------------------------
    def get_or_create(self, labels: Labels) -> int:
        with self._lock:
            ref = self._ref_by_labels.get(labels)
            if ref is not None:
                return ref
            ref = self._next_ref
            self._next_ref += 1
            self._ref_by_labels[labels] = ref
            self._series_by_ref[ref] = HeadSeries(ref, labels)
            return ref

    def append(self, labels: Labels, timestamp: int, value: float) -> tuple[int, bool]:
        with self._lock:
            ref = self.get_or_create(labels)
            series = self._series_by_ref[ref]
            appended = series.append(timestamp, value)
            if appended:
                if self._min_time is None or timestamp < self._min_time:
                    self._min_time = timestamp
                if self._max_time is None or timestamp > self._max_time:
                    self._max_time = timestamp
                self._num_samples += 1
            return ref, appended

    def append_by_ref(self, ref: int, timestamp: int, value: float) -> bool:
        with self._lock:
            series = self._series_by_ref.get(ref)
            if series is None:
                raise KeyError(f"unknown series ref {ref}")
            appended = series.append(timestamp, value)
            if appended:
                if self._min_time is None or timestamp < self._min_time:
                    self._min_time = timestamp
                if self._max_time is None or timestamp > self._max_time:
                    self._max_time = timestamp
                self._num_samples += 1
            return appended

    # ---- metadata ------------------------------------------------------
    @property
    def min_time(self) -> int:
        return 0 if self._min_time is None else self._min_time

    @property
    def max_time(self) -> int:
        return 0 if self._max_time is None else self._max_time

    @property
    def num_samples(self) -> int:
        return self._num_samples

    @property
    def num_series(self) -> int:
        return len(self._series_by_ref)

    def series(self) -> list[HeadSeries]:
        with self._lock:
            return list(self._series_by_ref.values())

    def labels_for_ref(self, ref: int) -> Labels | None:
        series = self._series_by_ref.get(ref)
        return series.labels if series else None

    def ref_for_labels(self, labels: Labels) -> int | None:
        return self._ref_by_labels.get(labels)

    # ---- reads ---------------------------------------------------------
    def select(
        self,
        matchers: list[Matcher],
        mint: int,
        maxt: int,
        *,
        apply_tombstones: bool = True,
    ) -> list[tuple[Labels, list[Sample]]]:
        with self._lock:
            results: list[tuple[Labels, list[Sample]]] = []
            for series in self._series_by_ref.values():
                if series.max_time < mint or series.min_time > maxt:
                    continue
                if not match_all(matchers, series.labels):
                    continue
                samples = series.samples(mint, maxt)
                if apply_tombstones:
                    samples = [
                        sample
                        for sample in samples
                        if not self._is_deleted(series.ref, sample.timestamp)
                    ]
                if samples:
                    results.append((series.labels, samples))
            return results

    def all_label_sets(self) -> list[Labels]:
        with self._lock:
            return [series.labels for series in self._series_by_ref.values()]

    # ---- tombstones ----------------------------------------------------
    def delete(self, matchers: list[Matcher], mint: int, maxt: int) -> int:
        deleted = 0
        with self._lock:
            for series in self._series_by_ref.values():
                if match_all(matchers, series.labels):
                    self._tombstones.setdefault(series.ref, []).append(
                        Interval(mint, maxt)
                    )
                    deleted += 1
        return deleted

    def _is_deleted(self, ref: int, timestamp: int) -> bool:
        for interval in self._tombstones.get(ref, ()):
            if interval.contains(timestamp):
                return True
        return False

    def tombstones(self) -> dict[int, list[Interval]]:
        with self._lock:
            return {ref: list(items) for ref, items in self._tombstones.items()}

    def add_tombstones(self, labels: Labels, intervals: list[Interval]) -> None:
        with self._lock:
            ref = self._ref_by_labels.get(labels)
            if ref is None:
                return
            self._tombstones.setdefault(ref, []).extend(intervals)
            self._tombstones[ref] = merge_intervals(self._tombstones[ref])

    # ---- flushing ------------------------------------------------------
    def chunks_before(self, cut_time: int) -> list[tuple[Labels, XORChunk]]:
        """Whole chunks with ``max_time <= cut_time`` (used by block flush)."""
        out: list[tuple[Labels, XORChunk]] = []
        with self._lock:
            for series in self._series_by_ref.values():
                for chunk in series.chunks:
                    if chunk.max_time <= cut_time:
                        out.append((series.labels, chunk))
        return out

    def remove_chunks_before(self, cut_time: int) -> None:
        """Drop flushed chunks and series that became empty."""
        with self._lock:
            for ref, series in list(self._series_by_ref.items()):
                remaining = [c for c in series.chunks if c.max_time > cut_time]
                if remaining:
                    series.chunks = remaining
                    series.min_time = remaining[0].min_time
                    continue
                del self._series_by_ref[ref]
                del self._ref_by_labels[series.labels]
                self._tombstones.pop(ref, None)

    def data(self) -> list[tuple[Labels, list[Sample]]]:
        """Full head contents, for WAL checkpoints and snapshots."""
        with self._lock:
            return [
                (series.labels, series.samples(series.min_time, series.max_time))
                for series in self._series_by_ref.values()
                if series.chunks
            ]

    def clear(self) -> None:
        self.reset()

    def __len__(self) -> int:
        return self.num_series
