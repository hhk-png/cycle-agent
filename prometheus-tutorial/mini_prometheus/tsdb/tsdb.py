"""The TSDB facade: head + WAL + persistent blocks.

Write path:  append -> head (XOR chunks) -> WAL (durability)
Flush path:  head chunks before the cut -> block -> checkpoint -> WAL truncate
Read path:   head + every block -> merge by timestamp -> tombstones applied
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from ..labels import Labels, Matcher, match_all
from ..model import Interval, Sample, merge_intervals
from ..util.timeutil import now_ms
from ..util.ulid import new_ulid
from .block import Block, BlockWriter, write_merged_block
from .head import Head
from .wal import WAL, checkpoint_series, read_checkpoint, write_checkpoint


class TSDBLockedError(RuntimeError):
    pass


MAX_TIMESTAMP_MS = (1 << 63) - 1


@dataclass
class TSDBOptions:
    path: str
    retention_ms: int = 15 * 24 * 60 * 60 * 1000
    block_range_ms: int = 2 * 60 * 60 * 1000
    wal_segment_size: int = 8 * 1024 * 1024
    wal_fsync: bool = False
    use_lockfile: bool = True
    head_chunk_samples: int = 120
    max_block_span: int | None = None

    @property
    def wal_dir(self) -> Path:
        return Path(self.path) / "wal"

    @property
    def blocks_dir(self) -> Path:
        return Path(self.path) / "blocks"

    @property
    def checkpoint_path(self) -> Path:
        return Path(self.path) / "checkpoint.json"

    @property
    def lock_path(self) -> Path:
        return Path(self.path) / "lock"


class TSDB:
    def __init__(self, options: TSDBOptions) -> None:
        self.options = options
        self._lock = threading.RLock()
        self._closed = False
        self._lock_file = None
        self.head = Head(chunk_samples=options.head_chunk_samples)
        self.wal = WAL(
            options.wal_dir,
            segment_size=options.wal_segment_size,
            fsync=options.wal_fsync,
        )
        self._blocks: list[Block] = []
        self._block_cache: dict[str, Block] = {}
        self._watchers: list = []
        self.stats = {
            "blocksFlushed": 0,
            "blocksCompacted": 0,
            "blocksDeleted": 0,
            "samplesAppended": 0,
            "outOfOrderSamples": 0,
            "duplicateSamples": 0,
            "walTruncations": 0,
        }

    # ---- lifecycle -----------------------------------------------------
    def open(self) -> "TSDB":
        path = Path(self.options.path)
        path.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        self.wal.open()
        checkpoint = read_checkpoint(self.options.checkpoint_path)
        min_segment = 0
        if checkpoint:
            min_segment = int(checkpoint.get("last_segment", -1)) + 1
            self._load_checkpoint(checkpoint)
        replay = self.wal.replay(min_segment=min_segment)
        if replay.truncated_bytes:
            # Never append new records after a torn tail in the same segment:
            # a later replay would stop at the torn frame and silently lose
            # everything written after it.  Roll first; replay continues into
            # the next segment.
            self.wal.roll()
        self._replay_wal(replay)
        self._blocks = self._load_blocks()
        return self

    def _acquire_lock(self) -> None:
        if not self.options.use_lockfile:
            return
        path = self.options.lock_path
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise TSDBLockedError(
                f"TSDB directory is locked by another process: {path}"
            ) from exc
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        self._lock_file = path

    def _load_checkpoint(self, checkpoint: dict) -> None:
        for item in checkpoint.get("series", []):
            labels = Labels(tuple((n, v) for n, v in item["labels"]))
            for timestamp, value in item["samples"]:
                self.head.append(labels, int(timestamp), float(value))
        for item in checkpoint.get("tombstones", []):
            labels = Labels(tuple((n, v) for n, v in item["labels"]))
            intervals = [Interval(int(a), int(b)) for a, b in item["intervals"]]
            self.head.add_tombstones(labels, intervals)

    def _replay_wal(self, replay) -> None:
        for _, labels in sorted(replay.series.items()):
            self.head.get_or_create(labels)
        for labels, timestamp, value in replay.samples:
            self.head.append(labels, timestamp, value)
        for labels, mint, maxt in replay.tombstones:
            self.head.add_tombstones(labels, [Interval(mint, maxt)])

    def _load_blocks(self) -> list[Block]:
        self.options.blocks_dir.mkdir(parents=True, exist_ok=True)
        blocks = []
        for path in sorted(self.options.blocks_dir.iterdir()):
            if not path.is_dir():
                continue
            try:
                block = Block(path)
            except (OSError, KeyError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"cannot load block {path}: {exc}") from exc
            blocks.append(block)
            self._block_cache[block.ulid] = block
        blocks.sort(key=lambda item: (item.min_time, item.ulid))
        return blocks

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.wal.close()
            self._closed = True
            if self._lock_file is not None:
                try:
                    self._lock_file.unlink()
                except OSError:
                    pass
                self._lock_file = None

    def __enter__(self) -> "TSDB":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- writes --------------------------------------------------------
    def add_watcher(self, callback) -> None:
        """Register a callback receiving successfully appended samples."""
        self._watchers.append(callback)

    def _notify(self, samples: list[tuple[Labels, int, float]]) -> None:
        for callback in list(self._watchers):
            try:
                callback(samples)
            except Exception:
                continue

    def append(self, labels: Labels, timestamp: int, value: float) -> int:
        with self._lock:
            is_new = self.head.ref_for_labels(labels) is None
            ref, appended = self.head.append(labels, timestamp, value)
            if not appended:
                # Identical duplicate: the chunk is unchanged, so the WAL and
                # watchers must not see a second copy.
                return ref
            if is_new:
                self.wal.log_series([(ref, labels)])
            self.wal.log_samples([(ref, timestamp, value)])
            self.stats["samplesAppended"] += 1
            self._notify([(labels, timestamp, value)])
            return ref

    def append_batch(
        self, samples: list[tuple[Labels, int, float]]
    ) -> tuple[int, list[str]]:
        """Append many samples; return (appended, per-sample error strings)."""
        appended = 0
        errors: list[str] = []
        if not samples:
            return appended, errors
        with self._lock:
            series_entries: list[tuple[int, Labels]] = []
            wal_samples: list[tuple[int, int, float]] = []
            newly_created: set[Labels] = set()
            for labels, timestamp, value in samples:
                known = labels in newly_created or self.head.ref_for_labels(labels) is not None
                try:
                    ref, appended = self.head.append(labels, timestamp, value)
                except Exception as exc:  # Duplicate / out-of-order
                    errors.append(str(exc))
                    if "out of order" in str(exc):
                        self.stats["outOfOrderSamples"] += 1
                    else:
                        self.stats["duplicateSamples"] += 1
                    continue
                if not appended:
                    # Identical duplicate: not an error, but nothing to WAL.
                    continue
                if not known:
                    series_entries.append((ref, labels))
                    newly_created.add(labels)
                wal_samples.append((ref, timestamp, value))
                appended += 1
            if series_entries:
                self.wal.log_series(series_entries)
            if wal_samples:
                self.wal.log_samples(wal_samples)
            self.stats["samplesAppended"] += appended
            if wal_samples:
                successful = [
                    (self.head.labels_for_ref(ref) or Labels.empty(), ts, value)
                    for ref, ts, value in wal_samples
                ]
                self._notify(successful)
        return appended, errors

    # ---- reads ---------------------------------------------------------
    def select(
        self,
        matchers: list[Matcher],
        mint: int,
        maxt: int,
        *,
        apply_tombstones: bool = True,
    ) -> list[tuple[Labels, list[Sample]]]:
        if mint > maxt:
            return []
        with self._lock:
            blocks = list(self._blocks)
        merged: dict[Labels, dict[int, tuple[float, int]]] = {}
        for block in blocks:
            for labels, samples in block.select(matchers, mint, maxt):
                target = merged.setdefault(labels, {})
                for sample in samples:
                    target[sample.timestamp] = (sample.value, 0)
        for labels, samples in self.head.select(
            matchers, mint, maxt, apply_tombstones=apply_tombstones
        ):
            target = merged.setdefault(labels, {})
            for sample in samples:
                target[sample.timestamp] = (sample.value, 1)
        results = []
        for labels in sorted(merged):
            points = merged[labels]
            if not points:
                continue
            results.append(
                (
                    labels,
                    [Sample(ts, points[ts][0]) for ts in sorted(points)],
                )
            )
        return results

    def series(
        self, matchers: list[Matcher], mint: int, maxt: int
    ) -> list[Labels]:
        out: dict[Labels, None] = {}
        with self._lock:
            blocks = list(self._blocks)
        for block in blocks:
            if block.max_time < mint or block.min_time > maxt:
                continue
            # Go through select() so chunk time ranges and tombstones are
            # honored; label_sets() alone would report series with no sample
            # in [mint, maxt] or with every sample deleted.
            for labels, _samples in block.select(matchers, mint, maxt):
                out[labels] = None
        for labels, _samples in self.head.select(matchers, mint, maxt):
            out[labels] = None
        return sorted(out)

    def label_names(self, mint: int = 0, maxt: int = MAX_TIMESTAMP_MS) -> list[str]:
        names: set[str] = set()
        with self._lock:
            blocks = list(self._blocks)
        for block in blocks:
            if block.max_time < mint or block.min_time > maxt:
                continue
            for labels, _samples in block.select([], mint, maxt):
                names.update(labels.keys())
        for labels, _samples in self.head.select([], mint, maxt):
            names.update(labels.keys())
        return sorted(names)

    def label_values(
        self, name: str, mint: int = 0, maxt: int = MAX_TIMESTAMP_MS
    ) -> list[str]:
        values: set[str] = set()
        with self._lock:
            blocks = list(self._blocks)
        for block in blocks:
            if block.max_time < mint or block.min_time > maxt:
                continue
            for labels, _samples in block.select([], mint, maxt):
                value = labels.get(name)
                if value:
                    values.add(value)
        for labels, _samples in self.head.select([], mint, maxt):
            value = labels.get(name)
            if value:
                values.add(value)
        return sorted(values)

    # ---- deletion ------------------------------------------------------
    def delete_series(
        self, matchers: list[Matcher], mint: int, maxt: int
    ) -> dict[str, int]:
        with self._lock:
            head_count = self.head.delete(matchers, mint, maxt)
            entries = []
            for labels in self.head.all_label_sets():
                if match_all(matchers, labels):
                    entries.append((labels, mint, maxt))
            if entries:
                self.wal.log_tombstones(entries)
            per_block = {}
            for block in self._blocks:
                count = block.delete(matchers, mint, maxt)
                if count:
                    per_block[block.ulid] = count
        return {"head": head_count, "blocks": per_block}

    def clean_tombstones(self) -> int:
        # Rewriting blocks to drop tombstoned samples is what real Prometheus
        # leaves to compaction; here a compaction pass already does it.
        removed = 0
        for block in self._blocks:
            if block.tombstones():
                removed += 1
        if removed:
            self.compact(force=True)
        return removed

    # ---- block lifecycle ----------------------------------------------
    def flush_head(self, *, force: bool = False) -> Block | None:
        """Flush whole head chunks into a block and truncate the WAL.

        With ``force=False`` only chunks ending before ``head.max - range`` are
        flushed, which keeps the head covering the most recent block range.
        """
        with self._lock:
            if self.head.num_series == 0:
                return None
            if force:
                cut = self.head.max_time
            else:
                cut = self.head.max_time - self.options.block_range_ms
                if cut <= self.head.min_time:
                    return None
            chunks = self.head.chunks_before(cut)
            if not chunks:
                return None
            tombstones = self._head_tombstones_before(cut)
            writer = BlockWriter()
            for labels, chunk in chunks:
                writer.add_chunk(labels, chunk)
            destination = self.options.blocks_dir / new_ulid()
            block = writer.write(destination, level=1, tombstones=tombstones)
            self.head.remove_chunks_before(cut)
            self._recalculate_head()
            self._blocks.append(block)
            self._blocks.sort(key=lambda item: (item.min_time, item.ulid))
            self._block_cache[block.ulid] = block
            self.stats["blocksFlushed"] += 1
            self._checkpoint_and_truncate_wal()
            return block

    def _head_tombstones_before(self, cut: int) -> dict[Labels, list[Interval]]:
        out: dict[Labels, list[Interval]] = {}
        tombstones = self.head.tombstones()
        for series in self.head.series():
            intervals = tombstones.get(series.ref)
            if not intervals:
                continue
            clipped = [
                Interval(max(interval.mint, series.min_time), min(interval.maxt, cut))
                for interval in intervals
                if interval.mint < cut
            ]
            clipped = [interval for interval in clipped if interval.mint < interval.maxt]
            if clipped:
                out[series.labels] = merge_intervals(clipped)
        return out

    def _checkpoint_and_truncate_wal(self) -> None:
        # Snapshot head under the TSDB lock, roll the WAL, then persist the
        # checkpoint.  Older segments can only be removed after the checkpoint
        # rename succeeds.
        series = checkpoint_series(self.head.data())
        tombstones = []
        for series_obj in self.head.series():
            intervals = self.head.tombstones().get(series_obj.ref)
            if intervals:
                tombstones.append(
                    {
                        "labels": [
                            [name, value] for name, value in series_obj.labels.items()
                        ],
                        "intervals": [[interval.mint, interval.maxt] for interval in intervals],
                    }
                )
        previous = self.wal.roll()
        write_checkpoint(
            self.options.checkpoint_path,
            series=series,
            last_segment=previous,
            tombstones=tombstones,
        )
        self.wal.truncate_through(previous)
        self.stats["walTruncations"] += 1

    def _recalculate_head(self) -> None:
        series = self.head.series()
        self.head._num_samples = sum(item.num_samples for item in series)  # type: ignore[attr-defined]
        if series:
            self.head._min_time = min(item.min_time for item in series)  # type: ignore[attr-defined]
            self.head._max_time = max(item.max_time for item in series)  # type: ignore[attr-defined]
        else:
            self.head._min_time = 0  # type: ignore[attr-defined]
            self.head._max_time = 0  # type: ignore[attr-defined]

    def compact(self, *, force: bool = False, now: int | None = None) -> dict:
        """Flush the head, merge blocks, and apply retention."""
        with self._lock:
            flushed = self.flush_head(force=force)
            merged = self._compact_blocks(force=force)
            deleted = self._apply_retention(now_ms() if now is None else now)
            return {
                "flushed": flushed.ulid if flushed else None,
                "compacted": [block.ulid for block in merged],
                "deleted": [block.ulid for block in deleted],
                "blocks": len(self._blocks),
            }

    def _compact_blocks(self, *, force: bool) -> list[Block]:
        blocks = sorted(self._blocks, key=lambda item: (item.min_time, item.ulid))
        if len(blocks) < 2:
            if force and blocks and blocks[0].tombstones():
                blocks = [blocks[0]]
            else:
                return []
        # Only force merges everything.  Merging disjoint blocks by count
        # would make retention coarse: the merged block's maxTime stays new
        # even though it contains old data.
        merge_all = force
        groups: list[list[Block]] = []
        if len(blocks) == 1:
            groups = [blocks]
        elif merge_all:
            groups = [blocks]
        else:
            current = [blocks[0]]
            for block in blocks[1:]:
                if block.min_time <= current[-1].max_time:
                    current.append(block)
                else:
                    if len(current) >= 2:
                        groups.append(current)
                    current = [block]
            if len(current) >= 2:
                groups.append(current)
        written: list[Block] = []
        for group in groups:
            if len(group) < 2 and not (force and group and group[0].tombstones()):
                continue
            level = max(block.level for block in group) + 1
            # all_samples() already applies tombstones, so the rewritten block
            # starts tombstone-free; this is how compaction reclaims space.
            destination = self.options.blocks_dir / new_ulid()
            merged = write_merged_block(destination, group, level=level)
            for block in group:
                self._blocks.remove(block)
                self._block_cache.pop(block.ulid, None)
                block.remove()
            self._blocks.append(merged)
            self._block_cache[merged.ulid] = merged
            written.append(merged)
            self.stats["blocksCompacted"] += 1
        self._blocks.sort(key=lambda item: (item.min_time, item.ulid))
        return written

    def _apply_retention(self, now: int) -> list[Block]:
        threshold = now - self.options.retention_ms
        deleted = []
        for block in list(self._blocks):
            if block.max_time < threshold:
                self._blocks.remove(block)
                self._block_cache.pop(block.ulid, None)
                block.remove()
                deleted.append(block)
                self.stats["blocksDeleted"] += 1
        return deleted

    def blocks(self) -> list[Block]:
        return list(self._blocks)

    def head_stats(self) -> dict:
        series = self.head.series()
        tombstones = sum(len(items) for items in self.head.tombstones().values())
        return {
            "numSeries": len(series),
            "numSamples": sum(item.num_samples for item in series),
            "numChunks": sum(len(item.chunks) for item in series),
            "minTime": self.head.min_time,
            "maxTime": self.head.max_time,
            "numTombstones": tombstones,
        }

    # ---- snapshots -----------------------------------------------------
    def snapshot(self, destination: str | os.PathLike[str] | None = None) -> str:
        with self._lock:
            root = (
                Path(destination)
                if destination is not None
                else Path(self.options.path) / "snapshots" / new_ulid()
            )
            root.mkdir(parents=True, exist_ok=False)
            for block in self._blocks:
                target = root / block.ulid
                target.mkdir()
                for name in ("meta.json", "index.json", "chunks.bin", "tombstones.json"):
                    source = block.path / name
                    if source.exists():
                        shutil.copy2(source, target / name)
            if self.head.num_series:
                writer = BlockWriter()
                for series in self.head.series():
                    for chunk in series.chunks:
                        writer.add_chunk(series.labels, chunk)
                # Head tombstones are keyed by ref; the block format keys them
                # by label set.  Without this, restoring a snapshot resurrects
                # deleted samples.
                head_tombstones: dict[Labels, list[Interval]] = {}
                for ref, intervals in self.head.tombstones().items():
                    labels = self.head.labels_for_ref(ref)
                    if labels is not None and intervals:
                        head_tombstones[labels] = intervals
                writer.write(
                    root / "head",
                    level=0,
                    tombstones=head_tombstones or None,
                )
            return str(root)

    def __repr__(self) -> str:
        return (
            f"TSDB(series={self.head.num_series}, blocks={len(self._blocks)}, "
            f"samples={self.head.num_samples})"
        )


def _merge_block_tombstones(blocks: list[Block]) -> dict[Labels, list[Interval]]:
    merged: dict[Labels, list[Interval]] = {}
    for block in blocks:
        for labels, intervals in block.tombstones().items():
            merged.setdefault(labels, []).extend(intervals)
    return {labels: merge_intervals(items) for labels, items in merged.items()}
