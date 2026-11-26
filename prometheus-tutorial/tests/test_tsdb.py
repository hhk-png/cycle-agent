from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from mini_prometheus.labels import Labels, Matcher
from mini_prometheus.model import DuplicateSampleError, OutOfOrderError
from mini_prometheus.tsdb import TSDB, TSDBOptions
from mini_prometheus.util.timeutil import stale_nan


def metric(name="x", **labels):
    return Labels({"__name__": name, **labels})


def make_tsdb(tmp_path, **overrides):
    options = TSDBOptions(
        path=str(tmp_path / "data"),
        retention_ms=10**15,
        block_range_ms=3600 * 1000,
        wal_segment_size=32 * 1024,
        use_lockfile=False,
    )
    for key, value in overrides.items():
        setattr(options, key, value)
    return TSDB(options).open()


def test_append_and_select(tsdb):
    labels = metric("cpu", job="a")
    tsdb.append(labels, 1000, 1.0)
    tsdb.append(labels, 2000, 2.0)
    result = tsdb.select([Matcher("__name__", "=", "cpu")], 0, 5000)
    assert len(result) == 1
    assert [sample.value for sample in result[0][1]] == [1.0, 2.0]


def test_select_time_range(tsdb):
    labels = metric("cpu")
    for index in range(10):
        tsdb.append(labels, index * 1000, float(index))
    result = tsdb.select([], 3000, 6000)
    assert [sample.timestamp for sample in result[0][1]] == [3000, 4000, 5000, 6000]


def test_matchers_filter_series(tsdb):
    tsdb.append(metric("cpu", job="a"), 1000, 1)
    tsdb.append(metric("cpu", job="b"), 1000, 2)
    tsdb.append(metric("mem", job="a"), 1000, 3)
    result = tsdb.select(
        [Matcher("__name__", "=", "cpu"), Matcher("job", "=", "a")], 0, 2000
    )
    assert len(result) == 1
    assert result[0][0].get("job") == "a"


def test_duplicate_same_value_is_ignored(tsdb):
    labels = metric("cpu")
    tsdb.append(labels, 1000, 1.0)
    tsdb.append(labels, 1000, 1.0)
    assert [s.value for s in tsdb.select([], 0, 2000)[0][1]] == [1.0]
    # The duplicate must not be counted in head stats or the WAL/appended total.
    assert tsdb.head.num_samples == 1
    assert tsdb.stats["samplesAppended"] == 1


def test_head_time_bounds_support_epoch_and_negative_samples(tmp_path):
    epoch = make_tsdb(tmp_path / "epoch")
    labels = metric("epoch")
    epoch.append(labels, 0, 1.0)
    epoch.append(labels, 60_000, 2.0)
    assert epoch.head.min_time == 0
    assert epoch.head.max_time == 60_000
    epoch.close()

    negative = make_tsdb(tmp_path / "negative")
    labels = metric("negative")
    negative.append(labels, -60_000, 1.0)
    negative.append(labels, -30_000, 2.0)
    assert negative.head.min_time == -60_000
    assert negative.head.max_time == -30_000
    negative.close()


def test_duplicate_different_value_raises(tsdb):
    labels = metric("cpu")
    tsdb.append(labels, 1000, 1.0)
    with pytest.raises(DuplicateSampleError):
        tsdb.append(labels, 1000, 2.0)


def test_out_of_order_raises(tsdb):
    labels = metric("cpu")
    tsdb.append(labels, 2000, 1.0)
    with pytest.raises(OutOfOrderError):
        tsdb.append(labels, 1000, 2.0)


def test_append_batch_reports_errors_and_keeps_good_samples(tsdb):
    labels = metric("cpu")
    appended, errors = tsdb.append_batch(
        [
            (labels, 1000, 1.0),
            (labels, 1000, 2.0),
            (labels, 2000, 3.0),
        ]
    )
    assert appended == 2
    assert len(errors) == 1
    assert [s.value for s in tsdb.select([], 0, 3000)[0][1]] == [1.0, 3.0]


def test_head_chunks_are_cut(tmp_path):
    database = make_tsdb(tmp_path)
    labels = metric("cpu")
    for index in range(250):
        database.append(labels, index * 1000, float(index))
    series = database.head.series()[0]
    assert len(series.chunks) >= 3
    assert [s.value for s in database.select([], 0, 10**9)[0][1]][-1] == 249.0
    database.close()


def test_restart_recovers_wal(tmp_path):
    database = make_tsdb(tmp_path)
    labels = metric("cpu", job="a")
    for index in range(20):
        database.append(labels, index * 1000, float(index))
    database.close()
    reopened = make_tsdb(tmp_path)
    result = reopened.select([], 0, 10**9)
    assert len(result) == 1
    assert len(result[0][1]) == 20
    reopened.close()


def test_flush_head_writes_block_and_truncates_wal(tmp_path):
    database = make_tsdb(tmp_path)
    labels = metric("cpu")
    for index in range(10):
        database.append(labels, index * 1000, float(index))
    block = database.flush_head(force=True)
    assert block is not None
    assert database.head.num_series == 0
    assert len(database.blocks()) == 1
    assert database.select([], 0, 10**9)[0][1][-1].value == 9.0
    database.close()
    reopened = make_tsdb(tmp_path)
    assert len(reopened.blocks()) == 1
    assert reopened.head.num_series == 0
    assert len(reopened.select([], 0, 10**9)[0][1]) == 10
    reopened.close()


def test_flush_partial_keeps_recent_head(tmp_path):
    # Whole chunks before the cut move to the block; newer chunks stay in the
    # head.  250 one-second samples produce three 120-sample chunks.
    database = make_tsdb(tmp_path, block_range_ms=30_000)
    labels = metric("cpu")
    for index in range(250):
        database.append(labels, index * 1000, float(index))
    block = database.flush_head(force=False)
    assert block is not None
    assert database.head.num_series == 1
    assert database.head.min_time > 0
    assert len(database.select([], 0, 10**9)[0][1]) == 250
    database.close()


def test_compaction_merges_blocks(tmp_path):
    database = make_tsdb(tmp_path)
    labels = metric("cpu")
    for index in range(20):
        database.append(labels, index * 1000, float(index))
        if index in (4, 9, 14, 19):
            database.flush_head(force=True)
    assert len(database.blocks()) == 4
    database.compact(force=True)
    assert len(database.blocks()) == 1
    samples = database.select([], 0, 10**9)[0][1]
    assert [sample.value for sample in samples] == [float(i) for i in range(20)]
    database.close()


def test_retention_deletes_old_blocks(tmp_path):
    database = make_tsdb(tmp_path, retention_ms=10_000)
    labels = metric("cpu")
    database.append(labels, 1000, 1)
    database.flush_head(force=True)
    database.append(labels, 100_000, 2)
    database.flush_head(force=True)
    assert len(database.blocks()) == 2
    database.compact(force=False, now=1_000_000)
    assert len(database.blocks()) == 0
    database.close()


def test_delete_series_in_head(tsdb):
    labels = metric("cpu")
    tsdb.append(labels, 1000, 1)
    tsdb.append(labels, 2000, 2)
    counts = tsdb.delete_series([Matcher("__name__", "=", "cpu")], 0, 2001)
    assert counts["head"] == 1
    assert tsdb.select([], 0, 5000) == []


def test_delete_series_in_block(tmp_path):
    database = make_tsdb(tmp_path)
    labels = metric("cpu")
    for index in range(5):
        database.append(labels, index * 1000, float(index))
    database.flush_head(force=True)
    database.delete_series([Matcher("__name__", "=", "cpu")], 0, 3000)
    assert [s.value for s in database.select([], 0, 10**9)[0][1]] == [3.0, 4.0]
    database.close()
    reopened = make_tsdb(tmp_path)
    assert [s.value for s in reopened.select([], 0, 10**9)[0][1]] == [3.0, 4.0]
    reopened.close()


def test_clean_tombstones_rewrites_block(tmp_path):
    database = make_tsdb(tmp_path)
    labels = metric("cpu")
    for index in range(5):
        database.append(labels, index * 1000, float(index))
    database.flush_head(force=True)
    database.delete_series([Matcher("__name__", "=", "cpu")], 0, 3000)
    database.clean_tombstones()
    block = database.blocks()[0]
    assert block.tombstones() == {}
    assert [s.value for s in database.select([], 0, 10**9)[0][1]] == [3.0, 4.0]
    database.close()


def test_snapshot_copies_blocks_and_head(tmp_path):
    database = make_tsdb(tmp_path)
    labels = metric("cpu")
    for index in range(5):
        database.append(labels, index * 1000, float(index))
    database.flush_head(force=True)
    database.append(labels, 10_000, 10)
    target = tmp_path / "snapshot"
    path = database.snapshot(target)
    assert (Path(path) / database.blocks()[0].ulid / "meta.json").exists()
    assert (Path(path) / "head" / "meta.json").exists()
    database.close()


def test_label_apis(tsdb):
    tsdb.append(metric("cpu", job="a"), 1000, 1)
    tsdb.append(metric("cpu", job="b"), 1000, 2)
    tsdb.append(metric("mem", job="a"), 1000, 3)
    assert tsdb.label_names() == ["__name__", "job"]
    assert tsdb.label_values("job") == ["a", "b"]
    assert tsdb.label_values("__name__") == ["cpu", "mem"]
    result = tsdb.series([Matcher("job", "=", "a")], 0, 2000)
    assert {labels.get_name() for labels in result} == {"cpu", "mem"}


def test_head_stats(tsdb):
    tsdb.append(metric("cpu"), 1000, 1)
    tsdb.append(metric("cpu"), 2000, 2)
    stats = tsdb.head_stats()
    assert stats["numSeries"] == 1
    assert stats["numSamples"] == 2
    assert stats["numChunks"] == 1
    assert stats["minTime"] == 1000
    assert stats["maxTime"] == 2000


def test_stale_marker_is_stored(tsdb):
    labels = metric("cpu")
    tsdb.append(labels, 1000, 1)
    tsdb.append(labels, 2000, stale_nan())
    samples = tsdb.select([], 0, 5000)[0][1]
    assert len(samples) == 2
    assert samples[1].is_stale()


def test_lockfile_prevents_second_open(tmp_path):
    options = TSDBOptions(path=str(tmp_path / "data"), use_lockfile=True)
    first = TSDB(options).open()
    second = TSDB(options)
    with pytest.raises(Exception):
        second.open()
    first.close()


def test_block_meta_is_json(tmp_path):
    database = make_tsdb(tmp_path)
    database.append(metric("cpu"), 1000, 1)
    database.flush_head(force=True)
    meta = json.loads((database.blocks()[0].path / "meta.json").read_text("utf-8"))
    assert meta["stats"]["numSeries"] == 1
    assert meta["stats"]["numSamples"] == 1
    assert meta["version"] == 1
    database.close()


def test_out_of_order_within_batch(tsdb):
    labels = metric("cpu")
    appended, errors = tsdb.append_batch(
        [(labels, 3000, 1), (labels, 2000, 2), (labels, 4000, 3)]
    )
    assert appended == 2
    assert "out of order" in errors[0]


def test_select_empty_range(tsdb):
    tsdb.append(metric("cpu"), 5000, 1)
    assert tsdb.select([], 0, 1000) == []
    assert tsdb.select([], 6000, 5000) == []


def test_many_series(tsdb):
    for index in range(100):
        tsdb.append(metric("cpu", instance=f"i{index}"), 1000, float(index))
    result = tsdb.select([Matcher("__name__", "=", "cpu")], 0, 2000)
    assert len(result) == 100
    assert tsdb.label_values("instance")[0] == "i0"


def test_append_after_torn_wal_tail_is_not_lost(tmp_path):
    database = make_tsdb(tmp_path)
    labels = metric("x")
    database.append(labels, 1000, 1.0)
    database.close()
    segment = sorted((tmp_path / "data" / "wal").glob("[0-9]*"))[-1]
    with segment.open("ab") as handle:
        handle.write(b"\x00\x00\x00\x40\x00\x00")
    database = make_tsdb(tmp_path)
    database.append(labels, 2000, 2.0)
    database.close()
    reopened = make_tsdb(tmp_path)
    values = [sample.value for sample in reopened.select([], 0, 5000)[0][1]]
    assert values == [1.0, 2.0]
    reopened.close()


def test_snapshot_does_not_break_live_append(tmp_path):
    database = make_tsdb(tmp_path)
    labels = metric("x")
    database.append(labels, 1000, 1.0)
    database.snapshot(tmp_path / "snap")
    database.append(labels, 2000, 2.0)
    assert [sample.value for sample in database.select([], 0, 5000)[0][1]] == [1.0, 2.0]
    database.close()
