from __future__ import annotations

import math
import random

import pytest

from mini_prometheus.tsdb.chunkenc import XORChunk


def test_single_sample_roundtrip():
    chunk = XORChunk()
    chunk.append(1_700_000_000_000, 42.5)
    decoded = XORChunk.from_bytes(chunk.bytes())
    assert decoded.samples() == [(1_700_000_000_000, 42.5)]


def test_empty_chunk_roundtrip():
    chunk = XORChunk()
    assert XORChunk.from_bytes(chunk.bytes()).samples() == []


def test_random_roundtrip():
    rng = random.Random(7)
    for _ in range(50):
        chunk = XORChunk()
        timestamp = 1_700_000_000_000
        value = 0.0
        expected = []
        for index in range(rng.randint(1, 120)):
            timestamp += rng.choice([1000, 1000, 1000, 5000, 14500, 60000])
            value += rng.uniform(-1, 1)
            if index % 17 == 0:
                value = float(rng.randint(-1000, 1000))
            chunk.append(timestamp, value)
            expected.append((timestamp, value))
        assert XORChunk.from_bytes(chunk.bytes()).samples() == expected


def test_constant_values_compress():
    chunk = XORChunk()
    for index in range(100):
        chunk.append(index * 1000, 1.0)
    assert len(chunk.bytes()) < 100


def test_nan_and_infinity_roundtrip():
    chunk = XORChunk()
    values = [math.nan, math.inf, -math.inf, -0.0, 0.0]
    for index, value in enumerate(values):
        chunk.append(index, value)
    decoded = XORChunk.from_bytes(chunk.bytes()).samples()
    assert len(decoded) == len(values)
    assert math.isnan(decoded[0][1])
    assert decoded[1][1] == math.inf
    assert decoded[2][1] == -math.inf
    assert math.copysign(1, decoded[3][1]) == -1


def test_timestamps_must_increase():
    chunk = XORChunk()
    chunk.append(10, 1)
    with pytest.raises(ValueError):
        chunk.append(10, 2)
    with pytest.raises(ValueError):
        chunk.append(9, 2)


def test_large_timestamp_jumps():
    chunk = XORChunk()
    timestamps = [0, 1, 100_000_000, 100_000_001, 200_000_000_000]
    for timestamp in timestamps:
        chunk.append(timestamp, timestamp % 7)
    decoded = XORChunk.from_bytes(chunk.bytes()).samples()
    assert [ts for ts, _ in decoded] == timestamps


def test_cannot_append_after_seal():
    chunk = XORChunk()
    chunk.append(1, 1)
    chunk.bytes()
    with pytest.raises(RuntimeError):
        chunk.append(2, 2)


def test_metadata_after_decode():
    chunk = XORChunk()
    chunk.append(100, 1)
    chunk.append(200, 2)
    decoded = XORChunk.from_bytes(chunk.bytes())
    assert decoded.min_time == 100
    assert decoded.max_time == 200
    assert decoded.num_samples == 2
