from __future__ import annotations

import time

import pytest

from mini_rabbitmq.streams import Stream, SuperStream


def test_append_assigns_monotonic_offsets():
    stream = Stream("s")
    assert stream.append("a").offset == 0
    assert stream.append("b").offset == 1
    assert stream.append("c").offset == 2
    assert stream.first_offset == 0
    assert stream.last_offset == 2
    assert stream.next_offset == 3


def test_read_does_not_remove_entries():
    stream = Stream("s")
    stream.append("a")
    stream.append("b")
    first = stream.read(0, 10)
    second = stream.read(0, 10)
    assert [e.body for e in first] == [b"a", b"b"]
    assert [e.body for e in second] == [b"a", b"b"]


def test_read_count_and_byte_limit():
    stream = Stream("s")
    for i in range(10):
        stream.append(b"x" * 100)
    assert len(stream.read(0, 3)) == 3
    limited = stream.read(0, 10, max_bytes=250)
    assert len(limited) == 2


def test_subscribe_first_last_next():
    stream = Stream("s")
    for body in (b"a", b"b", b"c"):
        stream.append(body)
    first = stream.subscribe("c1", start="first")
    assert first.read(2)[0].body == b"a"
    last = stream.subscribe("c2", start="last")
    assert last.read(1)[0].body == b"c"
    nxt = stream.subscribe("c3", start="next")
    assert nxt.read(1) == []
    stream.append(b"d")
    assert nxt.read(1)[0].body == b"d"


def test_multiple_consumers_have_independent_offsets():
    stream = Stream("s")
    for i in range(3):
        stream.append(f"m{i}")
    first = stream.subscribe("a", start="first")
    second = stream.subscribe("b", start="first")
    assert [e.body for e in first.read(1)] == [b"m0"]
    assert [e.body for e in second.read(2)] == [b"m0", b"m1"]
    assert [e.body for e in first.read(1)] == [b"m1"]


def test_consumer_seek():
    stream = Stream("s")
    for i in range(3):
        stream.append(f"m{i}")
    consumer = stream.subscribe("c", start="next")
    consumer.seek(1)
    assert consumer.read(1)[0].body == b"m1"
    with pytest.raises(ValueError):
        consumer.seek(-1)


def test_retention_by_size():
    stream = Stream("s", max_length_bytes=300)
    for i in range(10):
        stream.append(b"x" * 100)
    assert stream.stats()["size_bytes"] <= 300
    assert stream.first_offset is not None and stream.first_offset > 0


def test_retention_by_age():
    stream = Stream("s", max_age_seconds=1)
    old = stream.append(b"old")
    old.timestamp = time.time() - 10
    stream.append(b"new")
    assert stream.first_offset == 1
    assert [e.body for e in stream.read(0)] == [b"new"]


def test_read_before_first_offset_clamps():
    stream = Stream("s")
    for i in range(5):
        stream.append(f"m{i}")
    stream._entries = stream._entries[3:]
    entries = stream.read(0, 10)
    assert [e.body for e in entries] == [b"m3", b"m4"]


def test_empty_stream():
    stream = Stream("s")
    assert stream.first_offset is None
    assert stream.last_offset is None
    assert stream.read(0) == []
    assert stream.subscribe("c", start="first").read(1) == []


def test_super_stream_partitioning_is_deterministic():
    super_stream = SuperStream("orders", partitions=3)
    first = super_stream.partition_for("customer-1")
    second = super_stream.partition_for("customer-1")
    assert first is second
    entry = super_stream.append("customer-1", b"x")
    assert any(entry in partition._entries for partition in super_stream.partitions)


def test_super_stream_distribution():
    super_stream = SuperStream("events", partitions=4)
    for i in range(100):
        super_stream.append(f"key-{i}", b"x")
    used = [p for p in super_stream.partitions if p.stats()["entries"]]
    assert len(used) >= 3


def test_super_stream_stats():
    super_stream = SuperStream("s", partitions=2)
    super_stream.append("a", b"x")
    stats = super_stream.stats()
    assert stats["name"] == "s"
    assert len(stats["partitions"]) == 2
    assert stats["entries"] == 1


def test_invalid_stream_subscription_start():
    with pytest.raises(ValueError):
        Stream("s").subscribe(start="middle")


def test_stream_stats_fields():
    stream = Stream("s", max_age_seconds=60, max_length_bytes=1000)
    stream.append("a", headers={"k": "v"})
    stats = stream.stats()
    assert stats["entries"] == 1
    assert stats["appended_total"] == 1
    assert stats["max_age_seconds"] == 60
    assert stats["max_length_bytes"] == 1000
