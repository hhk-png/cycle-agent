"""Regressions found by a third independent audit (2026-11).

Each test encodes upstream Prometheus behavior that the first two audit rounds
missed.  Keep them small and behavioral; the point is to prevent a fix from
silently regressing.
"""

from __future__ import annotations

import math

import pytest

from mini_prometheus.config import RelabelConfig, parse_config
from mini_prometheus.labels import Labels, Matcher
from mini_prometheus.model import OutOfOrderError, PrometheusError
from mini_prometheus.promql.parser import ParseError, parse_expr
from mini_prometheus.scrape.manager import HttpResponse, ScrapeManager
from mini_prometheus.scrape.relabel import relabel_target


def L(name: str = "x", **labels: str) -> Labels:
    return Labels({"__name__": name, **labels})


def only(engine, query: str, timestamp: int):
    value = engine.query(query, timestamp).value
    assert len(value.items) == 1
    return value.items[0]


def test_bool_modifier_survives_binary_rhs(querier, engine):
    querier.add_series(L("x", i="1"), [(0, 1.0)])
    querier.add_series(L("y", i="1"), [(0, 10.0)])
    # y+1 = 11, so 1 > 11 is false; the result must still be a 0/1 sample,
    # not an empty vector (the bool modifier used to be lost here).
    item = only(engine, "x > bool y + 1", 0)
    assert item.value == 0.0
    assert "__name__" not in item.labels


def test_transforming_functions_drop_metric_name(querier, engine):
    querier.add_series(L("x"), [(0, 4.0)])
    for expression in ("abs(x)", "-x", "round(x, 1)", "clamp(x, 0, 2)", "clamp_min(x, 0)"):
        item = only(engine, expression, 0)
        assert "__name__" not in item.labels, expression


def test_go_expand_consumes_longest_name(querier, engine):
    querier.add_series(L("x", instance="host1:9100"), [(0, 1.0)])
    item = only(
        engine,
        'label_replace(x, "h", "$1x", "instance", "(host1):.*")',
        0,
    )
    # Go reads "$1x" as the (undefined) name "1x", not group 1 + "x"; the
    # empty replacement removes the destination label.
    assert "h" not in item.labels


def test_go_expand_longest_name_in_relabel():
    labels = {"__name__": "x", "instance": "host1:9100"}
    rules = [
        RelabelConfig(
            source_labels=["instance"],
            regex="(host1):.*",
            target_label="h",
            replacement="$1x",
        )
    ]
    result = relabel_target(labels, rules)
    assert result is not None and "h" not in result


def test_clamp_propagates_nan(querier, engine):
    querier.add_series(L("n"), [(0, math.nan)])
    for expression in ("clamp(n, 1, 5)", "clamp_min(n, 1)", "clamp_max(n, 5)"):
        assert math.isnan(only(engine, expression, 0).value), expression


def test_min_max_over_time_ignore_nan_unless_all(querier, engine):
    querier.add_series(
        L("m"), [(10_000, math.nan), (20_000, 5.0), (30_000, 3.0)]
    )
    assert only(engine, "min_over_time(m[1m])", 40_000).value == 3.0
    assert only(engine, "max_over_time(m[1m])", 40_000).value == 5.0

    querier.add_series(L("allnan"), [(10_000, math.nan), (20_000, math.nan)])
    assert math.isnan(only(engine, "min_over_time(allnan[1m])", 30_000).value)


def test_round_edge_cases_keep_series(querier, engine):
    querier.add_series(L("x"), [(0, 5.0)])
    assert math.isnan(only(engine, "round(x, 0)", 0).value)
    assert math.isnan(only(engine, "round(x, 1/0)", 0).value)
    assert only(engine, "round(x, 1)", 0).value == 5.0
    assert only(engine, "round(vector(-1.5), 1)", 0).value == -1.0


def test_last_over_time_keeps_metric_name(querier, engine):
    querier.add_series(L("x", job="a"), [(0, 1.0), (15_000, 2.0)])
    item = only(engine, "last_over_time(x[1m])", 20_000)
    assert item.labels.get("__name__") == "x"
    assert item.labels.get("job") == "a"


def _scrape_config(**job):
    scrape_job = {
        "job_name": "demo",
        "static_configs": [{"targets": ["127.0.0.1:9100"]}],
    }
    scrape_job.update(job)
    return parse_config(
        {
            "global": {"scrape_interval": "15s", "scrape_timeout": "5s"},
            "scrape_configs": [scrape_job],
        }
    )


def _body(text: str) -> HttpResponse:
    return HttpResponse(200, {"content-type": "text/plain"}, text.encode())


def test_sample_limit_is_checked_after_metric_relabel(tsdb):
    cfg = _scrape_config(
        sample_limit=2,
        metric_relabel_configs=[
            {"source_labels": ["__name__"], "regex": "dropme", "action": "drop"}
        ],
    )
    manager = ScrapeManager(
        tsdb,
        cfg,
        base_dir=".",
        fetcher=lambda *args: _body(
            "\n".join([f'dropme{{i="{i}"}} 1' for i in range(5)] + ["keepme 42"])
        ),
        start_threads=False,
    )
    manager.sync(now=1_700_000_000_000)
    report = manager.scrape_once(manager.targets()[0], timestamp=1_700_000_000_000)
    # After the drop rule only one sample remains, so the limit is not hit.
    assert report.status == "success", report.error
    series = tsdb.series([Matcher("__name__", "=", "keepme")], 0, 2**62)
    assert len(series) == 1


def test_honor_labels_renames_even_when_values_match(tsdb):
    cfg = _scrape_config()
    manager = ScrapeManager(
        tsdb,
        cfg,
        base_dir=".",
        fetcher=lambda *args: _body('m{job="demo"} 1\n'),
        start_threads=False,
    )
    manager.sync(now=1_700_000_000_000)
    report = manager.scrape_once(manager.targets()[0], timestamp=1_700_000_000_000)
    assert report.status == "success", report.error
    series = tsdb.series([Matcher("__name__", "=", "m")], 0, 2**62)
    assert len(series) == 1
    labels = series[0]
    assert labels.get("job") == "demo"
    # Upstream renames the scraped label whenever the target label exists,
    # even if both values are identical.
    assert labels.get("exported_job") == "demo"


def test_parser_rejects_late_bool_modifier():
    with pytest.raises(ParseError):
        parse_expr("a > on(x) bool b")
    with pytest.raises(ParseError):
        parse_expr("a > group_left bool b")


def test_quantile_sorts_nan_first(querier, engine):
    querier.add_series(
        L("x", i="1"), [(0, 1.0)]
    )
    querier.add_series(
        L("x", i="2"), [(0, math.nan)]
    )
    querier.add_series(
        L("x", i="3"), [(0, 3.0)]
    )
    # Sorted as [NaN, 1, 3]; the median of three values is 1.
    item = only(engine, "quantile(0.5, x)", 0)
    assert item.value == 1.0


def test_double_exponential_smoothing_needs_two_samples(querier, engine):
    querier.add_series(L("one"), [(0, 5.0)])
    assert engine.query("double_exponential_smoothing(one[1m], 0.5, 0.5)", 0).value.items == []
    with pytest.raises(PrometheusError):
        engine.query("double_exponential_smoothing(one[1m], 0, 0.5)", 0)


def test_bottomk_prefers_finite_over_nan(querier, engine):
    querier.add_series(L("x", i="nan"), [(0, math.nan)])
    querier.add_series(L("x", i="one"), [(0, 1.0)])
    # Prometheus' heap prefers real values over NaN for both topk/bottomk.
    item = only(engine, "bottomk(1, x)", 0)
    assert item.value == 1.0


def test_head_accepts_negative_timestamps(tsdb):
    labels = L("negative")
    tsdb.append(labels, -1000, 1.0)
    # A duplicate with the same value is ignored, not treated as an error.
    tsdb.append(labels, -1000, 1.0)
    with pytest.raises(OutOfOrderError):
        tsdb.append(labels, -2000, 2.0)
    series = tsdb.series([Matcher("__name__", "=", "negative")], -(2**63), 2**63 - 1)
    assert len(series) == 1
