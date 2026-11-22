from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mini_k8s.apimachinery.cron import parse_cron
from mini_k8s.apimachinery.meta import merge_patch, validate_name
from mini_k8s.apimachinery.quantity import (
    format_quantity, parse_quantity, quantity_equal, quantity_leq,
)
from mini_k8s.apimachinery.selectors import (
    match_expression, match_field_selector, match_labels, parse_label_selector,
)
from mini_k8s.errors import ApiError


def test_cpu_millicores():
    assert parse_quantity("250m") == 0.25
    assert quantity_leq("250m", "0.5")
    assert not quantity_leq("1", "500m")


def test_memory_binary_and_decimal_suffixes():
    assert parse_quantity("1Ki") == 1024
    assert parse_quantity("2Mi") == 2 * 1024 * 1024
    assert parse_quantity("1G") == 1_000_000_000
    assert parse_quantity("1e3") == 1000


def test_invalid_quantity():
    with pytest.raises(ValueError):
        parse_quantity("12Xi")


def test_quantity_formatting():
    assert format_quantity(0.5, resource="cpu") == "500m"
    assert format_quantity(2.0, resource="cpu") == "2"
    assert format_quantity(1024**3) == "1Gi"


def test_quantity_equal():
    assert quantity_equal("1024Mi", "1Gi")


def test_label_selector_equality_and_set():
    labels = {"app": "web", "tier": "frontend", "debug": ""}
    assert match_labels(labels, "app=web,tier in (frontend,backend)")
    assert not match_labels(labels, "app in (api)")
    assert match_labels(labels, "debug")
    assert match_labels(labels, "!canary")
    assert not match_labels(labels, "canary")
    assert match_labels(labels, "app!=api")


def test_label_selector_invalid():
    with pytest.raises(ApiError):
        parse_label_selector("app in ()")
    with pytest.raises(ApiError):
        parse_label_selector("=web")


def test_field_selector_nested():
    pod = {"metadata": {"name": "p", "namespace": "default"}, "spec": {"nodeName": "n1"},
           "status": {"phase": "Running"}}
    assert match_field_selector(pod, "spec.nodeName=n1")
    assert match_field_selector(pod, "status.phase!=Pending")
    assert not match_field_selector(pod, "metadata.name=other")


def test_match_expression_operators():
    labels = {"zone": "a", "replicas": "3"}
    assert match_expression({"key": "zone", "operator": "In", "values": ["a"]}, labels)
    assert match_expression({"key": "zone", "operator": "NotIn", "values": ["b"]}, labels)
    assert match_expression({"key": "zone", "operator": "Exists"}, labels)
    assert match_expression({"key": "missing", "operator": "DoesNotExist"}, labels)
    assert match_expression({"key": "replicas", "operator": "Gt", "values": ["2"]}, labels)


def test_merge_patch_deletes_null():
    target = {"a": 1, "b": {"c": 2, "d": 3}}
    assert merge_patch(target, {"b": {"c": None}, "e": 4}) == {"a": 1, "b": {"d": 3}, "e": 4}


def test_name_validation_variants():
    validate_name("web", "Deployment")
    validate_name("web.default", "Service", dns_subdomain=True)
    validate_name("system:discovery", "ClusterRole")
    validate_name("widgets.example.com", "CustomResourceDefinition")
    with pytest.raises(ApiError):
        validate_name("Web", "Deployment")
    with pytest.raises(ApiError):
        validate_name("a.b", "Service")


def test_cron_next_after():
    schedule = parse_cron("*/5 * * * *")
    start = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    assert schedule.next_after(start) == datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)


def test_cron_weekday_and_month_names():
    schedule = parse_cron("0 9 * * mon-fri")
    assert schedule.matches(datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))  # Monday
    assert not schedule.matches(datetime(2026, 1, 4, 9, 0, tzinfo=timezone.utc))  # Sunday


def test_cron_rejects_bad_fields():
    with pytest.raises(ValueError):
        parse_cron("* * *")
    with pytest.raises(ValueError):
        parse_cron("60 * * * *")
