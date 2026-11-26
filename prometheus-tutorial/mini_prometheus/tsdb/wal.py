"""Write-ahead log.

Framing is deliberately simple and self-describing:

    segment header : b"MPWAL001"                       (8 bytes)
    record         : u32 payload_length | u32 crc32 |
                     u8 record_type | JSON payload

``payload_length`` includes the type byte.  A torn record at the end of a
segment (power loss during write) is ignored on replay; a CRC failure in the
middle of a segment is reported as corruption instead of silently skipping
data.  JSON keeps the lab inspectable; upstream uses protobuf-like binary
records and page-level CRCs.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ..labels import Labels
from ..model import Interval

MAGIC = b"MPWAL001"
SERIES = 1
SAMPLES = 2
TOMBSTONES = 3
METADATA = 4
EXEMPLARS = 5
HEADER_SIZE = len(MAGIC)
FRAME_HEADER = struct.Struct(">IIB")
MAX_RECORD_SIZE = 64 * 1024 * 1024


class WALCorruptionError(RuntimeError):
    pass


@dataclass
class WALReplay:
    series: dict[int, Labels] = field(default_factory=dict)
    series_order: list[int] = field(default_factory=list)
    samples: list[tuple[Labels, int, float]] = field(default_factory=list)
    tombstones: list[tuple[Labels, int, int]] = field(default_factory=list)
    metadata: list[dict] = field(default_factory=list)
    exemplars: list[dict] = field(default_factory=list)
    segments: list[int] = field(default_factory=list)
    truncated_bytes: int = 0


class WAL:
    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        segment_size: int = 8 * 1024 * 1024,
        fsync: bool = False,
    ) -> None:
        self.directory = Path(directory)
        self.segment_size = segment_size
        self.fsync = fsync
        self._lock = threading.RLock()
        self._segment_index = 0
        self._file = None
        self._size = 0
        self._closed = False
        self._records = 0

    # ---- opening -------------------------------------------------------
    def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        indexes = self._segment_indexes()
        if indexes:
            self._segment_index = indexes[-1]
            path = self._segment_path(self._segment_index)
            self._file = open(path, "ab")
            self._size = path.stat().st_size
            if self._size == 0:
                self._file.write(MAGIC)
                self._size = HEADER_SIZE
        else:
            self._segment_index = 0
            self._open_new_segment(0)

    def _segment_indexes(self) -> list[int]:
        indexes = []
        for path in self.directory.iterdir():
            if path.is_file() and len(path.name) == 8 and path.name.isdigit():
                indexes.append(int(path.name))
        return sorted(indexes)

    def _segment_path(self, index: int) -> Path:
        return self.directory / f"{index:08d}"

    def _open_new_segment(self, index: int) -> None:
        if self._file is not None:
            self._file.flush()
            if self.fsync:
                os.fsync(self._file.fileno())
            self._file.close()
        path = self._segment_path(index)
        self._file = open(path, "wb")
        self._file.write(MAGIC)
        self._file.flush()
        self._size = HEADER_SIZE
        self._segment_index = index

    @property
    def segment_index(self) -> int:
        return self._segment_index

    @property
    def num_records(self) -> int:
        return self._records

    @property
    def size_bytes(self) -> int:
        return sum(
            self._segment_path(index).stat().st_size
            for index in self._segment_indexes()
        )

    # ---- writing -------------------------------------------------------
    def _write_record(self, record_type: int, payload: dict | list) -> None:
        if self._closed:
            raise RuntimeError("WAL is closed")
        body = json.dumps(
            payload, separators=(",", ":"), allow_nan=True
        ).encode("utf-8")
        if len(body) > MAX_RECORD_SIZE:
            raise ValueError("WAL record too large")
        length = len(body) + 1
        crc = zlib.crc32(bytes([record_type]) + body) & 0xFFFFFFFF
        frame = FRAME_HEADER.pack(length, crc, record_type) + body
        if self._size + len(frame) > self.segment_size and self._size > HEADER_SIZE:
            self._open_new_segment(self._segment_index + 1)
        self._file.write(frame)
        self._size += len(frame)
        self._records += 1
        if self.fsync:
            self._file.flush()
            os.fsync(self._file.fileno())

    def log_series(self, entries: list[tuple[int, Labels]]) -> None:
        if not entries:
            return
        with self._lock:
            self._write_record(
                SERIES,
                [[ref, [[name, value] for name, value in labels.items()]] for ref, labels in entries],
            )

    def log_samples(self, samples: list[tuple[int, int, float]]) -> None:
        if not samples:
            return
        with self._lock:
            self._write_record(
                SAMPLES, [[ref, ts, value] for ref, ts, value in samples]
            )

    def log_tombstones(self, entries: list[tuple[Labels, int, int]]) -> None:
        if not entries:
            return
        with self._lock:
            self._write_record(
                TOMBSTONES,
                [
                    [[[name, value] for name, value in labels.items()], mint, maxt]
                    for labels, mint, maxt in entries
                ],
            )

    def log_metadata(self, entries: list[dict]) -> None:
        if entries:
            with self._lock:
                self._write_record(METADATA, entries)

    def log_exemplars(self, entries: list[dict]) -> None:
        if entries:
            with self._lock:
                self._write_record(EXEMPLARS, entries)

    def roll(self) -> int:
        """Start a new segment and return the index of the previous one."""
        with self._lock:
            previous = self._segment_index
            self._open_new_segment(previous + 1)
            return previous

    def truncate_through(self, index: int) -> int:
        """Delete segments ``<= index``; callers must have checkpointed first."""
        removed = 0
        with self._lock:
            for candidate in self._segment_indexes():
                if candidate <= index:
                    if candidate == self._segment_index:
                        continue
                    self._segment_path(candidate).unlink(missing_ok=True)
                    removed += 1
        return removed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._file is not None:
                self._file.flush()
                if self.fsync:
                    os.fsync(self._file.fileno())
                self._file.close()
            self._closed = True

    # ---- replay --------------------------------------------------------
    def replay(self, *, min_segment: int = 0) -> WALReplay:
        """Read segments ``>= min_segment``, tolerating a torn tail."""
        result = WALReplay()
        for index in self._segment_indexes():
            if index < min_segment:
                continue
            result.segments.append(index)
            path = self._segment_path(index)
            data = path.read_bytes()
            if len(data) < HEADER_SIZE:
                if data and data != MAGIC[: len(data)]:
                    raise WALCorruptionError(f"{path}: bad magic")
                continue
            if data[:HEADER_SIZE] != MAGIC:
                raise WALCorruptionError(f"{path}: bad magic")
            position = HEADER_SIZE
            while position + FRAME_HEADER.size <= len(data):
                length, crc, record_type = FRAME_HEADER.unpack_from(data, position)
                frame_end = position + FRAME_HEADER.size + length - 1
                if length < 1 or length > MAX_RECORD_SIZE or frame_end > len(data):
                    # Leave ``position`` at the torn frame; the tail accounting
                    # below adds the remaining bytes exactly once.
                    break
                payload = data[position + FRAME_HEADER.size : frame_end]
                actual = zlib.crc32(bytes([record_type]) + payload) & 0xFFFFFFFF
                if actual != crc:
                    if frame_end == len(data):
                        break
                    raise WALCorruptionError(
                        f"{path}: CRC mismatch at offset {position}"
                    )
                try:
                    decoded = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    if frame_end == len(data):
                        break
                    raise WALCorruptionError(f"{path}: invalid JSON at {position}") from exc
                self._apply_record(result, record_type, decoded)
                position = frame_end
            if position < len(data):
                result.truncated_bytes += len(data) - position
        return result

    @staticmethod
    def _apply_record(result: WALReplay, record_type: int, payload) -> None:
        if record_type == SERIES:
            for ref, items in payload:
                labels = Labels(tuple((name, value) for name, value in items))
                result.series[int(ref)] = labels
                result.series_order.append(int(ref))
        elif record_type == SAMPLES:
            for ref, ts, value in payload:
                labels = result.series.get(int(ref))
                if labels is None:
                    raise WALCorruptionError(f"sample for unknown series ref {ref}")
                result.samples.append((labels, int(ts), float(value)))
        elif record_type == TOMBSTONES:
            for items, mint, maxt in payload:
                labels = Labels(tuple((name, value) for name, value in items))
                result.tombstones.append((labels, int(mint), int(maxt)))
        elif record_type == METADATA:
            result.metadata.extend(payload)
        elif record_type == EXEMPLARS:
            result.exemplars.extend(payload)
        else:
            raise WALCorruptionError(f"unknown record type {record_type}")


def read_checkpoint(path: str | os.PathLike[str]) -> dict | None:
    checkpoint = Path(path)
    if not checkpoint.exists():
        return None
    try:
        return json.loads(checkpoint.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WALCorruptionError(f"cannot read checkpoint {checkpoint}: {exc}") from exc


def write_checkpoint(
    path: str | os.PathLike[str],
    *,
    series: list[dict],
    last_segment: int,
    tombstones: list[dict] | None = None,
) -> None:
    """Atomically replace the checkpoint file."""
    checkpoint = Path(path)
    tmp = checkpoint.with_suffix(".tmp")
    payload = {
        "version": 1,
        "last_segment": last_segment,
        "series": series,
        "tombstones": tombstones or [],
    }
    tmp.write_text(
        json.dumps(payload, separators=(",", ":"), allow_nan=True),
        encoding="utf-8",
    )
    os.replace(tmp, checkpoint)


def checkpoint_series(head_data: list[tuple[Labels, list]]) -> list[dict]:
    return [
        {
            "labels": [[name, value] for name, value in labels.items()],
            "samples": [[sample.timestamp, sample.value] for sample in samples],
        }
        for labels, samples in head_data
    ]


def intervals_from_checkpoint(items) -> list[tuple[Labels, list[Interval]]]:
    out = []
    for item in items or []:
        labels = Labels(tuple((name, value) for name, value in item["labels"]))
        intervals = [Interval(int(mint), int(maxt)) for mint, maxt in item["intervals"]]
        out.append((labels, intervals))
    return out
