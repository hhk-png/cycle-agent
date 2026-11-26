from __future__ import annotations

import math

import pytest

from mini_prometheus.util.timeutil import (
    STALE_NAN,
    DurationError,
    format_duration,
    format_timestamp,
    is_stale,
    now_ms,
    parse_duration,
    parse_timestamp,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0", 0),
        ("1ms", 1),
        ("500ms", 500),
        ("1s", 1000),
        ("1m", 60_000),
        ("1h30m", 90 * 60_000),
        ("2d", 2 * 86_400_000),
        ("1w", 7 * 86_400_000),
        ("1y", 365 * 86_400_000),
        ("1h30m15s250ms", 90 * 60_000 + 15_250),
        ("10m0s", 600_000),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "1", "abc", "1x", "1s1h", "1m1m", "5m3", "1.5h", "-5m"],
)
def test_parse_duration_rejects_invalid(text):
    with pytest.raises((DurationError, ValueError)):
        parse_duration(text)


def test_format_duration_roundtrip():
    for value in (0, 1, 999, 1000, 61_000, 90 * 60_000, 7 * 86_400_000):
        text = format_duration(value)
        assert parse_duration(text) == value


def test_format_duration_largest_units():
    assert format_duration(90_000) == "1m30s"
    assert format_duration(3_600_000) == "1h"
    assert format_duration(0) == "0s"


def test_stale_nan_is_distinct_from_nan():
    assert math.isnan(STALE_NAN)
    assert math.isnan(float("nan"))
    assert is_stale(STALE_NAN)
    assert not is_stale(float("nan"))


def test_parse_timestamp_seconds():
    assert parse_timestamp("1700000000") == 1_700_000_000_000
    assert parse_timestamp("1700000000.5") == 1_700_000_000_500
    assert parse_timestamp(1700000001) == 1_700_000_001_000


def test_parse_timestamp_milliseconds():
    assert parse_timestamp("1700000000000") == 1_700_000_000_000


def test_parse_timestamp_rfc3339():
    assert parse_timestamp("2023-11-14T22:13:20Z") == 1_700_000_000_000
    assert parse_timestamp("2023-11-14T22:13:20.250Z") == 1_700_000_000_250


def test_parse_timestamp_default():
    assert parse_timestamp(None, default=42) == 42
    with pytest.raises(ValueError):
        parse_timestamp("nonsense")


def test_format_timestamp():
    assert format_timestamp(1_700_000_000_000) == "2023-11-14T22:13:20.000Z"


def test_now_ms_is_recent():
    value = now_ms()
    assert value > 1_600_000_000_000
