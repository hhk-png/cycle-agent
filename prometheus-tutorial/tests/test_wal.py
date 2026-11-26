from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_prometheus.labels import Labels
from mini_prometheus.tsdb.wal import (
    WAL,
    WALCorruptionError,
    read_checkpoint,
    write_checkpoint,
)


def labels(name="x", **extra):
    return Labels({"__name__": name, **extra})


def test_write_and_replay(tmp_path):
    wal = WAL(tmp_path / "wal", segment_size=1024)
    wal.open()
    wal.log_series([(1, labels("cpu", job="a"))])
    wal.log_samples([(1, 1000, 1.5), (1, 2000, 2.5)])
    wal.close()
    replay = WAL(tmp_path / "wal").replay()
    assert replay.series[1] == labels("cpu", job="a")
    assert replay.samples == [
        (labels("cpu", job="a"), 1000, 1.5),
        (labels("cpu", job="a"), 2000, 2.5),
    ]


def test_replay_across_segments(tmp_path):
    wal = WAL(tmp_path / "wal", segment_size=300)
    wal.open()
    wal.log_series([(1, labels("x"))])
    for index in range(100):
        wal.log_samples([(1, index, float(index))])
    wal.close()
    segments = sorted(
        int(path.name) for path in (tmp_path / "wal").iterdir() if path.name.isdigit()
    )
    assert len(segments) > 1
    replay = WAL(tmp_path / "wal").replay()
    assert len(replay.samples) == 100
    assert replay.samples[-1][2] == 99.0


def test_torn_tail_is_ignored(tmp_path):
    wal = WAL(tmp_path / "wal")
    wal.open()
    wal.log_series([(1, labels("x"))])
    wal.log_samples([(1, 1000, 1)])
    wal.close()
    segment = sorted((tmp_path / "wal").glob("[0-9]*"))[-1]
    with segment.open("ab") as handle:
        handle.write(b"\x00\x00\x00\x40\x00\x00")
    replay = WAL(tmp_path / "wal").replay()
    assert len(replay.samples) == 1
    assert replay.truncated_bytes >= 6


def test_torn_frame_header_accounts_tail_once(tmp_path):
    wal = WAL(tmp_path / "wal")
    wal.open()
    wal.log_series([(1, labels("x"))])
    wal.close()
    segment = sorted((tmp_path / "wal").glob("[0-9]*"))[-1]
    # 14 bytes: enough for the frame header, but the declared length (64)
    # runs past EOF. The tail must be counted exactly once, not twice.
    with segment.open("ab") as handle:
        handle.write(b"\x00\x00\x00\x40" + b"\x00" * 10)
    replay = WAL(tmp_path / "wal").replay()
    assert replay.truncated_bytes == 14


def test_crc_corruption_in_middle_raises(tmp_path):
    wal = WAL(tmp_path / "wal")
    wal.open()
    wal.log_series([(1, labels("x"))])
    wal.log_samples([(1, 1000, 1)])
    wal.log_samples([(1, 2000, 2)])
    wal.close()
    segment = sorted((tmp_path / "wal").glob("[0-9]*"))[-1]
    data = bytearray(segment.read_bytes())
    # Corrupt the payload of the first sample record (after header+frame).
    data[8 + 9 + 2] ^= 0xFF
    segment.write_bytes(data)
    with pytest.raises(WALCorruptionError):
        WAL(tmp_path / "wal").replay()


def test_roll_and_truncate(tmp_path):
    wal = WAL(tmp_path / "wal", segment_size=10000)
    wal.open()
    wal.log_series([(1, labels("x"))])
    previous = wal.roll()
    assert previous == 0
    wal.log_samples([(1, 1000, 1)])
    removed = wal.truncate_through(previous)
    assert removed == 1
    assert sorted(path.name for path in (tmp_path / "wal").glob("[0-9]*")) == ["00000001"]
    wal.close()


def test_truncate_deletes_old_segments(tmp_path):
    wal = WAL(tmp_path / "wal", segment_size=64)
    wal.open()
    wal.log_series([(1, labels("x"))])
    wal.log_samples([(1, 1000, 1)])
    previous = wal.roll()
    wal.log_samples([(1, 2000, 2)])
    wal.close()
    wal = WAL(tmp_path / "wal")
    wal.open()
    removed = wal.truncate_through(previous)
    assert removed >= 1
    wal.close()


def test_checkpoint_roundtrip(tmp_path):
    path = tmp_path / "checkpoint.json"
    write_checkpoint(
        path,
        series=[
            {
                "labels": [["__name__", "x"]],
                "samples": [[1000, 1.0], [2000, 2.0]],
            }
        ],
        last_segment=3,
        tombstones=[
            {"labels": [["__name__", "x"]], "intervals": [[0, 1000]]}
        ],
    )
    data = read_checkpoint(path)
    assert data["last_segment"] == 3
    assert data["series"][0]["samples"] == [[1000, 1.0], [2000, 2.0]]
    assert data["tombstones"][0]["intervals"] == [[0, 1000]]


def test_checkpoint_missing_returns_none(tmp_path):
    assert read_checkpoint(tmp_path / "absent.json") is None


def test_unknown_series_ref_is_corruption(tmp_path):
    wal = WAL(tmp_path / "wal")
    wal.open()
    wal.log_samples([(99, 1000, 1)])
    wal.close()
    with pytest.raises(WALCorruptionError):
        WAL(tmp_path / "wal").replay()

