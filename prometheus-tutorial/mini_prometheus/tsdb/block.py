"""Persistent blocks and a block writer.

A block is a directory named by ULID containing:

* ``meta.json``  -- time range, series/sample counts, compaction level;
* ``index.json`` -- label sets and chunk references (offset/length/time);
* ``chunks.bin`` -- length-prefixed XOR chunks;
* ``tombstones.json`` (optional) -- deletion intervals.

The index is JSON rather than Prometheus' custom binary index.  That is the
main deliberate deviation in the storage layer: it keeps the format
inspectable while preserving the same access pattern (postings by label ->
chunk metadata -> chunk bytes).
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..labels import Labels, Matcher, match_all
from ..model import Interval, Sample, merge_intervals
from ..util.ulid import new_ulid
from .chunkenc import XORChunk

BLOCK_VERSION = 1
META_FILE = "meta.json"
INDEX_FILE = "index.json"
CHUNKS_FILE = "chunks.bin"
TOMBSTONES_FILE = "tombstones.json"

_CHUNK_RECORD = struct.Struct(">QI")  # ref, length


@dataclass(slots=True)
class ChunkMeta:
    ref: int
    offset: int
    length: int
    min_time: int
    max_time: int
    num_samples: int

    def to_json(self) -> dict:
        return {
            "ref": self.ref,
            "offset": self.offset,
            "length": self.length,
            "minTime": self.min_time,
            "maxTime": self.max_time,
            "numSamples": self.num_samples,
        }

    @classmethod
    def from_json(cls, data: dict) -> "ChunkMeta":
        return cls(
            int(data["ref"]),
            int(data["offset"]),
            int(data["length"]),
            int(data["minTime"]),
            int(data["maxTime"]),
            int(data.get("numSamples", 0)),
        )


@dataclass(slots=True)
class BlockSeries:
    labels: Labels
    chunks: list[ChunkMeta] = field(default_factory=list)


class BlockWriter:
    def __init__(self) -> None:
        self._series: dict[Labels, list[tuple[bytes, int, int, int]]] = {}
        self._num_samples = 0
        self._num_chunks = 0

    def add_chunk(self, labels: Labels, chunk: XORChunk) -> None:
        self._series.setdefault(labels, []).append(
            (chunk.snapshot_bytes(), chunk.min_time, chunk.max_time, chunk.num_samples)
        )
        self._num_samples += chunk.num_samples
        self._num_chunks += 1

    def add_samples(self, labels: Labels, samples: Iterable[Sample]) -> None:
        chunk = XORChunk()
        for sample in samples:
            chunk.append(sample.timestamp, sample.value)
        if chunk.num_samples:
            self.add_chunk(labels, chunk)

    @property
    def num_series(self) -> int:
        return len(self._series)

    @property
    def num_samples(self) -> int:
        return self._num_samples

    def write(
        self,
        directory: str | os.PathLike[str],
        *,
        level: int = 1,
        sources: list[str] | None = None,
        tombstones: dict[Labels, list[Interval]] | None = None,
        ulid: str | None = None,
    ) -> "Block":
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=False)
        block_id = ulid or new_ulid()
        series_list: list[dict] = []
        chunks_path = path / CHUNKS_FILE
        offset = 0
        min_time = None
        max_time = None
        with open(chunks_path, "wb") as chunks_file:
            ref = 1
            for labels in sorted(self._series):
                metas: list[ChunkMeta] = []
                for data, chunk_mint, chunk_maxt, num in self._series[labels]:
                    chunks_file.write(data)
                    metas.append(
                        ChunkMeta(ref, offset, len(data), chunk_mint, chunk_maxt, num)
                    )
                    offset += len(data)
                    ref += 1
                    min_time = chunk_mint if min_time is None else min(min_time, chunk_mint)
                    max_time = chunk_maxt if max_time is None else max(max_time, chunk_maxt)
                series_list.append(
                    {
                        "labels": [[name, value] for name, value in labels.items()],
                        "chunks": [meta.to_json() for meta in metas],
                    }
                )
        index = {"version": BLOCK_VERSION, "series": series_list}
        (path / INDEX_FILE).write_text(json.dumps(index, separators=(",", ":")), "utf-8")
        meta = {
            "ulid": block_id,
            "minTime": min_time or 0,
            "maxTime": max_time or 0,
            "version": BLOCK_VERSION,
            "stats": {
                "numSeries": len(series_list),
                "numSamples": self._num_samples,
                "numChunks": self._num_chunks,
            },
            "compaction": {"level": level, "sources": sources or []},
        }
        (path / META_FILE).write_text(json.dumps(meta, indent=2), "utf-8")
        if tombstones:
            _write_tombstones(path / TOMBSTONES_FILE, tombstones)
        return Block(path)


class Block:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.meta = json.loads((self.path / META_FILE).read_text("utf-8"))
        self._index: list[BlockSeries] | None = None
        self._lock = threading.RLock()
        self._chunk_cache: dict[tuple[Labels, int], list[tuple[int, float]]] = {}
        self._tombstones: dict[Labels, list[Interval]] | None = None

    # ---- metadata ------------------------------------------------------
    @property
    def ulid(self) -> str:
        return self.meta["ulid"]

    @property
    def min_time(self) -> int:
        return int(self.meta["minTime"])

    @property
    def max_time(self) -> int:
        return int(self.meta["maxTime"])

    @property
    def num_series(self) -> int:
        return int(self.meta["stats"]["numSeries"])

    @property
    def num_samples(self) -> int:
        return int(self.meta["stats"]["numSamples"])

    @property
    def level(self) -> int:
        return int(self.meta.get("compaction", {}).get("level", 1))

    @property
    def sources(self) -> list[str]:
        return list(self.meta.get("compaction", {}).get("sources", []))

    def index(self) -> list[BlockSeries]:
        if self._index is None:
            with self._lock:
                if self._index is None:
                    raw = json.loads((self.path / INDEX_FILE).read_text("utf-8"))
                    self._index = [
                        BlockSeries(
                            Labels(tuple((n, v) for n, v in item["labels"])),
                            [ChunkMeta.from_json(chunk) for chunk in item["chunks"]],
                        )
                        for item in raw["series"]
                    ]
        return self._index

    def label_sets(self) -> list[Labels]:
        return [series.labels for series in self.index()]

    # ---- tombstones ----------------------------------------------------
    def tombstones(self) -> dict[Labels, list[Interval]]:
        if self._tombstones is None:
            path = self.path / TOMBSTONES_FILE
            if path.exists():
                self._tombstones = _read_tombstones(path)
            else:
                self._tombstones = {}
        return self._tombstones

    def delete(self, matchers: list[Matcher], mint: int, maxt: int) -> int:
        count = 0
        with self._lock:
            for series in self.index():
                if match_all(matchers, series.labels):
                    existing = self.tombstones().setdefault(series.labels, [])
                    existing.append(Interval(mint, maxt))
                    self._tombstones[series.labels] = merge_intervals(existing)
                    count += 1
            if count:
                _write_tombstones(self.path / TOMBSTONES_FILE, self._tombstones or {})
        return count

    def _is_deleted(self, labels: Labels, timestamp: int) -> bool:
        for interval in self.tombstones().get(labels, ()):
            if interval.contains(timestamp):
                return True
        return False

    # ---- query ---------------------------------------------------------
    def chunks_for(self, series: BlockSeries) -> list[tuple[ChunkMeta, list[tuple[int, float]]]]:
        out = []
        data_file = None
        try:
            for meta in series.chunks:
                key = (series.labels, meta.offset)
                cached = self._chunk_cache.get(key)
                if cached is None:
                    if data_file is None:
                        data_file = open(self.path / CHUNKS_FILE, "rb")
                    data_file.seek(meta.offset)
                    data = data_file.read(meta.length)
                    if len(data) != meta.length:
                        raise IOError(
                            f"short chunk read in {self.path}: expected "
                            f"{meta.length}, got {len(data)}"
                        )
                    cached = XORChunk.from_bytes(data).samples()
                    if len(self._chunk_cache) > 256:
                        self._chunk_cache.clear()
                    self._chunk_cache[key] = cached
                out.append((meta, cached))
        finally:
            if data_file is not None:
                data_file.close()
        return out

    def select(
        self, matchers: list[Matcher], mint: int, maxt: int
    ) -> list[tuple[Labels, list[Sample]]]:
        if self.max_time < mint or self.min_time > maxt:
            return []
        results = []
        for series in self.index():
            if not match_all(matchers, series.labels):
                continue
            samples: list[Sample] = []
            for meta, points in self.chunks_for(series):
                if meta.max_time < mint or meta.min_time > maxt:
                    continue
                for timestamp, value in points:
                    if mint <= timestamp <= maxt and not self._is_deleted(
                        series.labels, timestamp
                    ):
                        samples.append(Sample(timestamp, value))
            if samples:
                results.append((series.labels, samples))
        return results

    def all_samples(self) -> list[tuple[Labels, list[Sample]]]:
        return self.select([], self.min_time, self.max_time)

    def remove(self) -> None:
        shutil.rmtree(self.path)

    def to_json(self) -> dict:
        return {
            "ulid": self.ulid,
            "minTime": self.min_time,
            "maxTime": self.max_time,
            "numSeries": self.num_series,
            "numSamples": self.num_samples,
            "level": self.level,
            "sizeBytes": self.size_bytes(),
        }

    def size_bytes(self) -> int:
        return sum(
            file.stat().st_size for file in self.path.iterdir() if file.is_file()
        )

    def __repr__(self) -> str:
        return f"Block({self.ulid}, {self.min_time}..{self.max_time}, series={self.num_series})"


def _write_tombstones(path: Path, tombstones: dict[Labels, list[Interval]]) -> None:
    payload = {
        "series": [
            {
                "labels": [[name, value] for name, value in labels.items()],
                "intervals": [[interval.mint, interval.maxt] for interval in intervals],
            }
            for labels, intervals in tombstones.items()
            if intervals
        ]
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), "utf-8")
    os.replace(tmp, path)


def _read_tombstones(path: Path) -> dict[Labels, list[Interval]]:
    raw = json.loads(path.read_text("utf-8"))
    out: dict[Labels, list[Interval]] = {}
    for item in raw.get("series", []):
        labels = Labels(tuple((n, v) for n, v in item["labels"]))
        out[labels] = [Interval(int(a), int(b)) for a, b in item["intervals"]]
    return out


def write_merged_block(
    destination: Path,
    blocks: list[Block],
    *,
    level: int,
    tombstones: dict[Labels, list[Interval]] | None = None,
) -> Block:
    """Merge blocks into a new block, deduplicating identical timestamps."""
    writer = BlockWriter()
    merged: dict[Labels, dict[int, float]] = {}
    for block in blocks:
        for labels, samples in block.all_samples():
            target = merged.setdefault(labels, {})
            for sample in samples:
                target[sample.timestamp] = sample.value
    for labels in sorted(merged):
        points = merged[labels]
        chunk = XORChunk()
        for timestamp in sorted(points):
            chunk.append(timestamp, points[timestamp])
            if chunk.num_samples >= 120:
                writer.add_chunk(labels, chunk)
                chunk = XORChunk()
        if chunk.num_samples:
            writer.add_chunk(labels, chunk)
    return writer.write(
        destination,
        level=level,
        sources=[block.ulid for block in blocks],
        tombstones=tombstones,
    )
