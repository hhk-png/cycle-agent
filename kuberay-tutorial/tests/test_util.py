from datetime import datetime, timezone

import pytest

from minikuberay.clock import ManualClock, iso, parse_iso
from minikuberay.util import (
    age_seconds,
    format_cpu,
    format_memory,
    human_duration,
    labels_match,
    make_name,
    parse_cpu,
    parse_duration_seconds,
    parse_memory,
    parse_resource,
    ray_supports_autoscaler_v2,
    resource_quantity,
    semver_tuple,
    stable_hash,
)


def test_parse_cpu_units():
    assert parse_cpu("500m") == 500
    assert parse_cpu("2") == 2000
    assert parse_cpu(1.5) == 1500
    assert parse_cpu(None) == 0


def test_parse_memory_units():
    assert parse_memory("1Gi") == 2**30
    assert parse_memory("512Mi") == 512 * 2**20
    assert parse_memory("1G") == 10**9
    assert parse_memory(1024) == 1024
    with pytest.raises(ValueError):
        parse_memory("10Xi")


def test_format_quantities():
    assert format_cpu(2000) == "2"
    assert format_cpu(500) == "500m"
    assert format_memory(2**30) == "1Gi"
    assert format_memory(512 * 2**20) == "512Mi"


def test_resource_quantity_and_sum():
    assert resource_quantity({"cpu": "250m"}, "cpu") == 250
    assert resource_quantity({"nvidia.com/gpu": "2"}, "nvidia.com/gpu") == 2
    assert parse_resource("1Gi", "memory") == 2**30


def test_labels_match_flat_and_object_form():
    labels = {"ray.io/cluster": "c", "ray.io/node-type": "head", "ray.io/serve": "true"}
    assert labels_match({"ray.io/cluster": "c"}, labels)
    assert not labels_match({"ray.io/cluster": "other"}, labels)
    assert labels_match({"matchLabels": {"ray.io/cluster": "c"}}, labels)
    assert labels_match({"matchExpressions": [{"key": "ray.io/serve", "operator": "In", "values": ["true"]}]}, labels)
    assert labels_match({"matchExpressions": [{"key": "missing", "operator": "DoesNotExist"}]}, labels)
    assert not labels_match({"matchExpressions": [{"key": "missing", "operator": "Exists"}]}, labels)


def test_semver_and_autoscaler_support():
    assert semver_tuple("2.59.0") == (2, 59, 0)
    assert semver_tuple("v2.47") == (2, 47, 0)
    assert semver_tuple("latest") is None
    assert ray_supports_autoscaler_v2("2.59.0")
    assert not ray_supports_autoscaler_v2("2.46.9")


def test_stable_hash_and_make_name():
    assert stable_hash({"a": 1}, 5) == stable_hash({"a": 1}, 5)
    assert stable_hash({"a": 1}, 5) != stable_hash({"a": 2}, 5)
    name = make_name("demo", "abcde")
    assert name == "demo-abcde"
    assert len(make_name("x" * 100, "abcde")) <= 63


def test_duration_parsing_and_formatting():
    assert parse_duration_seconds("30m") == 1800
    assert parse_duration_seconds("1h") == 3600
    assert parse_duration_seconds("45s") == 45
    assert parse_duration_seconds("10") == 10
    with pytest.raises(ValueError):
        parse_duration_seconds("soon")
    assert human_duration(5) == "5s"
    assert human_duration(65) == "1m05s"
    assert human_duration(3665) == "1h01m"


def test_clock_and_iso_roundtrip():
    clock = ManualClock()
    start = clock.now()
    clock.advance(10)
    assert (clock.now() - start).total_seconds() == 10
    parsed = parse_iso(iso(clock.now()))
    assert parsed == clock.now()


def test_age_seconds():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    created = iso(now)
    assert age_seconds(created, now) == 0
    assert age_seconds(created, datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)) == 10
