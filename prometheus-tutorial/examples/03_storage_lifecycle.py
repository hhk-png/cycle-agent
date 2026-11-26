"""TSDB lifecycle: WAL, head chunks, blocks, compaction and snapshots."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_bootstrap_root = str(_Path(__file__).resolve().parent.parent)
if _bootstrap_root not in _sys.path:
    _sys.path.insert(0, _bootstrap_root)



import tempfile
from pathlib import Path

from mini_prometheus.labels import Labels, Matcher
from mini_prometheus.tsdb import TSDB, TSDBOptions


def run() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "data"
        options = TSDBOptions(
            path=str(path),
            block_range_ms=60_000,
            retention_ms=10**15,
            wal_segment_size=64 * 1024,
            use_lockfile=False,
        )
        database = TSDB(options).open()
        for series_index in range(3):
            labels = Labels(
                {"__name__": "demo_latency_seconds", "instance": f"i{series_index}"}
            )
            for index in range(300):
                database.append(labels, index * 1000, float(index + series_index))
        print(f"head: {database.head_stats()}")
        block = database.flush_head(force=True)
        print(f"flushed block: {block.ulid[:10]}… samples={block.num_samples}")
        for index in range(50):
            database.append(Labels({"__name__": "demo_latency_seconds", "instance": "i0"}), 300_000 + index * 1000, float(index))
        database.compact(force=True)
        print(f"blocks after compaction: {[b.to_json() for b in database.blocks()]}")
        snapshot = database.snapshot(path.parent / "snapshot")
        print(f"snapshot: {snapshot}")
        result = database.select(
            [Matcher("__name__", "=", "demo_latency_seconds")], 0, 2**62
        )
        print(f"series readable: {len(result)}, samples: {sum(len(s) for _, s in result)}")
        wal_files = sorted(item.name for item in (path / "wal").iterdir())
        print(f"WAL segments after checkpoint/truncate: {wal_files}")
        database.close()

        reopened = TSDB(options).open()
        result = reopened.select(
            [Matcher("__name__", "=", "demo_latency_seconds")], 0, 2**62
        )
        print(
            f"after restart: head series={reopened.head.num_series}, "
            f"blocks={len(reopened.blocks())}, readable samples={sum(len(s) for _, s in result)}"
        )
        reopened.close()


if __name__ == "__main__":
    run()
