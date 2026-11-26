"""Regressions for semantic bugs found by an independent Mini-Prometheus audit."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from mini_prometheus.config import RelabelConfig
from mini_prometheus.labels import Labels, Matcher
from mini_prometheus.model import PrometheusError
from mini_prometheus.promql.engine import Engine
from mini_prometheus.promql.value import Scalar, Vector
from mini_prometheus.rules.manager import AlertingRule, RuleGroup, RuleManager
from mini_prometheus.scrape.relabel import relabel_target
from mini_prometheus.tsdb.block import Block
from mini_prometheus.promql.parser import parse_expr


def L(name: str = "x", **labels: str) -> Labels:
    return Labels({"__name__": name, **labels})


def add_series(tsdb, name: str, labels: dict, points: list[tuple[int, float]]) -> Labels:
    result = Labels({"__name__": name, **labels})
    for timestamp, value in points:
        tsdb.append(result, timestamp, value)
    return result


def only(engine: Engine, query: str, timestamp: int) -> float:
    value = engine.query(query, timestamp).value
    assert isinstance(value, Vector)
    assert len(value.items) == 1
    return value.items[0].value


def test_scalar_scalar_comparison_uses_bool(engine):
    assert engine.query("1 > bool 0", 0).value == Scalar(1.0)
    assert engine.query("1 == bool 2", 0).value == Scalar(0.0)


def test_double_exponential_smoothing_matches_prometheus(querier, engine):
    querier.add_series(
        L("x"), [(0, 15.0), (1000, 30.0), (2000, 25.0), (3000, 40.0)]
    )
    value = engine.query(
        "double_exponential_smoothing(x[1h], 0.5, 0.5)", 3000
    ).value
    assert isinstance(value, Vector)
    assert value.items[0].value == pytest.approx(42.5)


def test_label_replace_is_anchored_and_deletes_on_empty(engine, querier):
    querier.add_series(L("up", job="api", x="old"), [(0, 1)])
    # No full match: unchanged, including the existing dst label.
    result = engine.query('label_replace(up, "x", "b", "job", "p")', 0).value
    assert result.items[0].labels.get("x") == "old"
    # Full match with an empty expansion deletes dst.
    result = engine.query('label_replace(up, "x", "", "job", "(.*)")', 0).value
    assert "x" not in result.items[0].labels


def test_label_replace_missing_capture_group_is_empty(engine, querier):
    querier.add_series(L("up", job="api", x="old"), [(0, 1)])
    result = engine.query(
        'label_replace(up, "x", "$3", "job", "(a)(.*)")', 0
    ).value
    assert "x" not in result.items[0].labels


def test_group_left_deletes_include_label_missing_on_one_side(engine, querier):
    querier.add_series(L("foo", x="1", y="leftY"), [(0, 1)])
    querier.add_series(L("bar", x="1"), [(0, 2)])
    result = engine.query("foo * on(x) group_left(y) bar", 0).value
    assert isinstance(result, Vector)
    assert "y" not in result.items[0].labels


def test_duplicate_series_error_even_without_matching_group(engine, querier):
    querier.add_series(L("foo", x="A"), [(0, 1)])
    querier.add_series(L("bar", x="B", i="1"), [(0, 2)])
    querier.add_series(L("bar", x="B", i="2"), [(0, 3)])
    with pytest.raises(PrometheusError):
        engine.query("foo * on(x) bar", 0)


def test_round_ties_toward_positive_infinity(engine):
    assert only(engine, "round(vector(-1.5))", 0) == -1.0
    assert only(engine, "round(vector(-2.5))", 0) == -2.0
    assert only(engine, "round(vector(1.5))", 0) == 2.0


def test_relabel_missing_capture_group_expands_empty():
    rule = RelabelConfig(
        source_labels=["instance"],
        target_label="x",
        regex="(.*)",
        replacement="$2",
    )
    result = relabel_target({"instance": "host"}, [rule])
    assert "x" not in result
    named = RelabelConfig(
        source_labels=["instance"],
        target_label="x",
        regex="(.*)",
        replacement="${nosuch}",
    )
    assert "x" not in relabel_target({"instance": "host"}, [named])


def test_keep_firing_for_does_not_expire_one_interval_early(tsdb):
    add_series(tsdb, "x", {"job": "a"}, [(0, 10.0)])
    rule = AlertingRule(
        "High",
        "x > 0",
        parse_expr("x > 0"),
        {},
        {},
        for_ms=0,
        keep_firing_for_ms=60_000,
    )
    manager = RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [rule])])
    manager.evaluate(0)
    assert manager.alerts()[0].state == "firing"
    add_series(tsdb, "x", {"job": "a"}, [(60_000, 0.0)])
    manager.evaluate(60_000)
    assert len(manager.alerts()) == 1
    manager.evaluate(120_000)
    assert manager.alerts() == []


def test_min_max_ignore_nan_when_finite_exists(querier, engine):
    querier.add_series(L("x", job="a"), [(0, math.nan)])
    querier.add_series(L("x", job="b"), [(0, 5.0)])
    assert only(engine, "max(x)", 0) == 5.0
    assert only(engine, "min(x)", 0) == 5.0


def test_topk_nan_parameter_is_empty(querier, engine):
    querier.add_series(L("x", job="a"), [(0, 1.0)])
    result = engine.query("topk(0/0, x)", 0).value
    assert isinstance(result, Vector)
    assert result.items == []


def test_histogram_quantile_phi_above_one_returns_inf_without_inf_bucket(querier, engine):
    querier.add_series(L("h_bucket", le="1"), [(0, 1.0)])
    querier.add_series(L("h_bucket", le="2"), [(0, 2.0)])
    value = only(engine, "histogram_quantile(2, h_bucket)", 0)
    assert math.isinf(value) and value > 0


def test_absent_drops_labels_with_non_equality_matcher(engine):
    result = engine.query(
        'absent(nonexistent{job="a",job=~"b"})', 0
    ).value
    assert len(result.items) == 1
    assert "job" not in result.items[0].labels


def test_sort_by_label_uses_natural_order(querier, engine):
    querier.add_series(L("x", h="a10"), [(0, 1.0)])
    querier.add_series(L("x", h="a2"), [(0, 1.0)])
    result = engine.query('sort_by_label(x, "h")', 0).value
    assert [item.labels.get("h") for item in result.items] == ["a2", "a10"]


def test_series_and_labels_respect_tombstones_and_time_range(tsdb):
    add_series(tsdb, "m", {"k": "a"}, [(0, 1.0)])
    add_series(tsdb, "m", {"k": "b"}, [(0, 1.0), (1000, 2.0)])
    tsdb.flush_head(force=True)
    tsdb.delete_series([Matcher("k", "=", "a")], 0, 1000)

    series = tsdb.series([Matcher("__name__", "=", "m")], 0, 1000)
    assert [labels.get("k") for labels in series] == ["b"]
    # "a" has no sample at/after 500, so it must not appear either.
    series = tsdb.series([Matcher("__name__", "=", "m")], 500, 1000)
    assert [labels.get("k") for labels in series] == ["b"]
    assert tsdb.label_values("k", 0, 1000) == ["b"]


def test_snapshot_preserves_head_tombstones(tsdb, tmp_path):
    add_series(tsdb, "x", {"job": "a"}, [(0, 1.0), (1000, 2.0)])
    tsdb.head.delete([Matcher("__name__", "=", "x")], 0, 2000)
    destination = tmp_path / "snapshot"
    snapshot = Path(tsdb.snapshot(destination))
    head_block = Block(snapshot / "head")
    assert head_block.select([], 0, 2000) == []
