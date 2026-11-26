from __future__ import annotations

import math

import pytest

from mini_prometheus.labels import Labels
from mini_prometheus.model import PrometheusError
from mini_prometheus.promql.engine import Engine
from mini_prometheus.promql.value import Matrix, Scalar, Vector
from mini_prometheus.util.timeutil import stale_nan


def L(name="x", **labels):
    return Labels({"__name__": name, **labels})


def q(engine, query, timestamp):
    return engine.query(query, timestamp).value


def scalar_value(engine, query, timestamp):
    value = q(engine, query, timestamp)
    assert isinstance(value, Scalar)
    return value.value


def vector_map(engine, query, timestamp):
    value = q(engine, query, timestamp)
    assert isinstance(value, Vector)
    return {item.labels: item.value for item in value.items}


def total(engine, query, timestamp):
    value = q(engine, query, timestamp)
    assert isinstance(value, Vector)
    return sum(item.value for item in value.items)


def only(engine, query, timestamp):
    value = q(engine, query, timestamp)
    assert isinstance(value, Vector)
    assert len(value.items) == 1
    return value.items[0].value


def test_instant_selector_uses_latest_sample(engine, querier):
    querier.add_series(L("x", job="a"), [(0, 1), (30_000, 2), (60_000, 3)])
    result = q(engine, "x", 90_000)
    assert len(result.items) == 1
    assert result.items[0].value == 3
    assert result.items[0].source_timestamp == 60_000


def test_lookback_window_is_left_open(engine, querier):
    querier.add_series(L("x"), [(0, 1)])
    # Prometheus 3 excludes a sample exactly at t-lookback.
    assert q(engine, "x", 5 * 60 * 1000 - 1).items[0].value == 1
    assert q(engine, "x", 5 * 60 * 1000).items == []


def test_stale_marker_hides_series(engine, querier):
    querier.add_series(L("x"), [(0, 1), (10_000, stale_nan())])
    assert q(engine, "x", 20_000).items == []


def test_stale_marker_then_new_sample(engine, querier):
    querier.add_series(L("x"), [(0, 1), (10_000, stale_nan()), (20_000, 3)])
    assert q(engine, "x", 25_000).items[0].value == 3


def test_matchers(engine, querier):
    querier.add_series(L("x", job="a", code="200"), [(0, 1)])
    querier.add_series(L("x", job="a", code="500"), [(0, 2)])
    querier.add_series(L("x", job="b", code="500"), [(0, 3)])
    assert len(q(engine, 'x{job="a"}', 1000).items) == 2
    assert len(q(engine, 'x{code=~"5.."}', 1000).items) == 2
    assert len(q(engine, 'x{job!="a"}', 1000).items) == 1


def test_offset(engine, querier):
    querier.add_series(L("x"), [(0, 1), (60_000, 2)])
    assert q(engine, "x offset 1m", 60_000).items[0].value == 1


def test_at_modifier(engine, querier):
    querier.add_series(L("x"), [(0, 1), (60_000, 2)])
    assert q(engine, "x @ 0", 60_000).items[0].value == 1


def test_range_selector_matrix(engine, querier):
    querier.add_series(L("x"), [(i * 15_000, float(i)) for i in range(10)])
    # (60s, 120s]: the sample exactly at 60s is excluded (Prometheus 3).
    result = q(engine, "x[1m]", 120_000)
    assert isinstance(result, Matrix)
    assert len(result.series[0].samples) == 4
    assert result.series[0].samples[0][0] == 75_000


def test_rate(engine, querier):
    querier.add_series(L("c"), [(i * 15_000, i * 2.0) for i in range(10)])
    result = q(engine, "rate(c[1m])", 120_000)
    assert result.items[0].value == pytest.approx(2 / 15)


def test_rate_drops_metric_name(engine, querier):
    # Two points are needed; with a left-open 1m window at least one sample
    # must lie strictly inside (0, 60s].
    querier.add_series(L("c", job="a"), [(0, 0), (30_000, 30), (60_000, 60)])
    item = q(engine, "rate(c[1m])", 60_000).items[0]
    assert "__name__" not in item.labels
    assert item.labels.get("job") == "a"


def test_rate_needs_two_samples(engine, querier):
    querier.add_series(L("c"), [(0, 1)])
    assert q(engine, "rate(c[1m])", 30_000).items == []


def test_increase(engine, querier):
    querier.add_series(L("c"), [(0, 0), (30_000, 30), (60_000, 60)])
    assert q(engine, "increase(c[1m])", 60_000).items[0].value == pytest.approx(60)


def test_counter_reset_rate(engine, querier):
    querier.add_series(L("c"), [(0, 10), (15_000, 12), (30_000, 2), (45_000, 4)])
    assert q(engine, "rate(c[1m])", 60_000).items[0].value == pytest.approx(6 / 45)


def test_resets_and_changes(engine, querier):
    querier.add_series(L("c"), [(0, 10), (15_000, 12), (30_000, 2), (45_000, 4)])
    # (0, 60s] excludes the sample at 0: 12 -> 2 -> 4.
    assert total(engine, "sum(resets(c[1m]))", 60_000) == 1
    assert total(engine, "sum(changes(c[1m]))", 60_000) == 2


def test_changes_does_not_count_nan_to_nan(engine, querier):
    querier.add_series(L("c"), [(15_000, math.nan), (30_000, math.nan), (45_000, 1.0)])
    assert total(engine, "sum(changes(c[1m]))", 60_000) == 1


def test_irate(engine, querier):
    querier.add_series(L("c"), [(0, 10), (15_000, 20), (30_000, 5)])
    assert q(engine, "irate(c[1m])", 45_000).items[0].value == pytest.approx(5 / 15)


def test_idelta(engine, querier):
    querier.add_series(L("c"), [(0, 10), (15_000, 20)])
    assert q(engine, "idelta(c[1m])", 15_000).items[0].value == 10


def test_delta_gauge(engine, querier):
    querier.add_series(L("g"), [(0, 10), (30_000, 5), (60_000, 0)])
    assert q(engine, "delta(g[1m])", 60_000).items[0].value == pytest.approx(-10)


def test_extrapolation_at_range_edges(engine, querier):
    # Samples do not reach either edge; Prometheus extrapolates to the edges.
    querier.add_series(L("c"), [(10_000, 0), (25_000, 15), (40_000, 30), (55_000, 45)])
    value = q(engine, "increase(c[1m])", 60_000).items[0].value
    assert value == pytest.approx(50)


def test_aggregations(engine, querier):
    querier.add_series(L("x", job="a", instance="1"), [(0, 1)])
    querier.add_series(L("x", job="a", instance="2"), [(0, 3)])
    querier.add_series(L("x", job="b", instance="3"), [(0, 10)])
    assert only(engine, "sum(x)", 0) == 14
    assert only(engine, "avg(x)", 0) == pytest.approx(14 / 3)
    assert only(engine, "count(x)", 0) == 3
    assert only(engine, "min(x)", 0) == 1
    assert only(engine, "max(x)", 0) == 10
    result = vector_map(engine, "sum by (job) (x)", 0)
    assert result == {Labels({"job": "a"}): 4, Labels({"job": "b"}): 10}


def test_aggregation_without(engine, querier):
    querier.add_series(L("x", job="a", instance="1"), [(0, 1)])
    querier.add_series(L("x", job="a", instance="2"), [(0, 3)])
    result = vector_map(engine, "sum without (instance) (x)", 0)
    assert result == {Labels({"job": "a"}): 4}


def test_topk_bottomk(engine, querier):
    for index in range(5):
        querier.add_series(L("x", i=str(index)), [(0, index)])
    top = vector_map(engine, "topk(2, x)", 0)
    assert sorted(top.values()) == [3, 4]
    bottom = vector_map(engine, "bottomk(2, x)", 0)
    assert sorted(bottom.values()) == [0, 1]


def test_quantile_aggregation(engine, querier):
    for index in range(5):
        querier.add_series(L("x", i=str(index)), [(0, index)])
    assert only(engine, "quantile(0.5, x)", 0) == 2
    assert only(engine, "quantile(0, x)", 0) == 0
    assert only(engine, "quantile(1, x)", 0) == 4


def test_count_values(engine, querier):
    querier.add_series(L("x", i="1"), [(0, 1)])
    querier.add_series(L("x", i="2"), [(0, 2)])
    querier.add_series(L("x", i="3"), [(0, 1)])
    result = vector_map(engine, 'count_values("v", x)', 0)
    assert result == {Labels({"v": "1"}): 2, Labels({"v": "2"}): 1}


def test_group_aggregation(engine, querier):
    querier.add_series(L("x", job="a"), [(0, 5)])
    querier.add_series(L("x", job="a"), [(0, 7)])
    assert vector_map(engine, "group by (job) (x)", 0) == {Labels({"job": "a"}): 1}


def test_aggregation_by_name_retains_metric_name(engine, querier):
    querier.add_series(L("a", job="x"), [(0, 1.0)])
    querier.add_series(L("b", job="x"), [(0, 2.0)])
    result = q(engine, "sum by (__name__) ({__name__=~\".+\"})", 0)
    assert {item.labels.get_name() for item in result.items} == {"a", "b"}


def test_stddev_stdvar(engine, querier):
    for index, value in enumerate([1, 2, 3, 4]):
        querier.add_series(L("x", i=str(index)), [(0, value)])
    assert only(engine, "stdvar(x)", 0) == pytest.approx(1.25)
    assert only(engine, "stddev(x)", 0) == pytest.approx(math.sqrt(1.25))


def test_scalar_arithmetic_and_power(engine, querier):
    assert scalar_value(engine, "1 + 2 * 3", 0) == 7
    assert scalar_value(engine, "2 ^ 3 ^ 2", 0) == 512
    assert scalar_value(engine, "-2^2", 0) == -4
    assert scalar_value(engine, "5 % 3", 0) == 2
    assert scalar_value(engine, "5 % -3", 0) == 2
    assert scalar_value(engine, "1 / 0", 0) == math.inf
    # Go's math.Pow/math.Mod edge cases must not leak Python exceptions.
    assert scalar_value(engine, "0 ^ -1", 0) == math.inf
    assert scalar_value(engine, "10 ^ 400", 0) == math.inf
    assert math.isnan(scalar_value(engine, "(1/0) % 2", 0))
    # Go math edge cases on the vector path: log(0)=-Inf, exp overflow=+Inf,
    # ceil(Inf)=+Inf, atanh(1)=+Inf.
    assert only(engine, "ln(vector(0))", 0) == -math.inf
    assert only(engine, "exp(vector(1000))", 0) == math.inf
    assert only(engine, "ceil(vector(1/0))", 0) == math.inf
    assert only(engine, "atanh(vector(1))", 0) == math.inf


def test_vector_scalar_arithmetic(engine, querier):
    querier.add_series(L("x", job="a"), [(0, 2)])
    result = q(engine, "x * 3", 0)
    assert result.items[0].value == 6
    assert "__name__" not in result.items[0].labels


def test_scalar_vector_arithmetic_order(engine, querier):
    querier.add_series(L("x"), [(0, 2)])
    assert q(engine, "10 - x", 0).items[0].value == 8


def test_comparison_filter(engine, querier):
    querier.add_series(L("x", i="1"), [(0, 1)])
    querier.add_series(L("x", i="2"), [(0, 5)])
    result = q(engine, "x > 2", 0)
    assert len(result.items) == 1
    assert result.items[0].value == 5
    assert result.items[0].labels.get_name() == "x"


def test_nan_comparison_follows_ieee(engine, querier):
    querier.add_series(L("x", i="nan"), [(0, math.nan)])
    querier.add_series(L("x", i="one"), [(0, 1.0)])
    # NaN != 1 is true; NaN == NaN is false.
    assert len(q(engine, "x != 1", 0).items) == 1
    assert q(engine, 'x{i="nan"} == x{i="nan"}', 0).items == []


def test_comparison_bool(engine, querier):
    querier.add_series(L("x", i="1"), [(0, 1)])
    querier.add_series(L("x", i="2"), [(0, 5)])
    result = vector_map(engine, "x > bool 2", 0)
    assert sorted(result.values()) == [0, 1]
    assert all("__name__" not in labels for labels in result)


def test_comparison_with_nan_is_false(engine, querier):
    querier.add_series(L("x"), [(0, math.nan)])
    assert q(engine, "x > 0", 0).items == []
    assert vector_map(engine, "x == bool x", 0) == {Labels.empty(): 0.0}


def test_vector_matching_on(engine, querier):
    querier.add_series(L("a", job="x", instance="1"), [(0, 10)])
    querier.add_series(L("b", job="x", instance="1"), [(0, 2)])
    result = q(engine, "a / on(job) b", 30_000)
    assert result.items[0].value == 5


def test_vector_matching_ignoring(engine, querier):
    querier.add_series(L("a", job="x", instance="1"), [(0, 10)])
    querier.add_series(L("b", job="x", instance="2"), [(0, 2)])
    result = q(engine, "a / ignoring(instance) b", 30_000)
    assert result.items[0].value == 5


def test_group_left(engine, querier):
    # group_left: many on the left, one on the right.
    querier.add_series(L("a", job="x", instance="1"), [(0, 10)])
    querier.add_series(L("a", job="x", instance="2"), [(0, 20)])
    querier.add_series(L("b", job="x", version="v2"), [(0, 2)])
    result = q(engine, "a / on(job) group_left(version) b", 30_000)
    assert len(result.items) == 2
    assert {item.labels.get("instance") for item in result.items} == {"1", "2"}
    assert {item.labels.get("version") for item in result.items} == {"v2"}
    assert sorted(item.value for item in result.items) == [5, 10]


def test_group_right(engine, querier):
    # group_right: one on the left, many on the right.
    querier.add_series(L("a", job="x", version="v2"), [(0, 10)])
    querier.add_series(L("b", job="x", instance="1"), [(0, 2)])
    querier.add_series(L("b", job="x", instance="2"), [(0, 4)])
    result = q(
        engine, "a / on(job) group_right(version) b", 30_000
    )
    assert len(result.items) == 2
    # group_right copies include labels from the one (left) side; labels not
    # included stay on the many (right) side.
    assert {item.labels.get("instance") for item in result.items} == {"1", "2"}
    assert {item.labels.get("version") for item in result.items} == {"v2"}
    assert sorted(item.value for item in result.items) == [2.5, 5]


def test_group_left_rejects_duplicate_right(engine, querier):
    querier.add_series(L("a", job="x", instance="1"), [(0, 10)])
    querier.add_series(L("b", job="x", version="v1"), [(0, 2)])
    querier.add_series(L("b", job="x", version="v2"), [(0, 4)])
    with pytest.raises(PrometheusError):
        q(engine, "a / on(job) group_left(version) b", 30_000)


def test_group_right_rejects_duplicate_left(engine, querier):
    querier.add_series(L("a", job="x", instance="1"), [(0, 10)])
    querier.add_series(L("a", job="x", instance="2"), [(0, 20)])
    querier.add_series(L("b", job="x", version="v2"), [(0, 2)])
    with pytest.raises(PrometheusError):
        q(engine, "a / on(job) group_right(instance) b", 30_000)


def test_many_to_many_without_group_rejected(engine, querier):
    querier.add_series(L("a", job="x", instance="1"), [(0, 10)])
    querier.add_series(L("a", job="x", instance="2"), [(0, 10)])
    querier.add_series(L("b", job="x", instance="1"), [(0, 2)])
    with pytest.raises(PrometheusError):
        q(engine, "a / on(job) b", 30_000)


def test_one_to_one_on_and_ignoring_restrict_result_labels(engine, querier):
    querier.add_series(L("a", job="x", instance="1"), [(0, 10)])
    querier.add_series(L("b", job="x", instance="1"), [(0, 2)])

    on_result = q(engine, "a / on(job) b", 30_000).items[0]
    assert on_result.value == 5
    assert on_result.labels == Labels({"job": "x"})

    ignoring_result = q(engine, "a / ignoring(instance) b", 30_000).items[0]
    assert ignoring_result.value == 5
    assert ignoring_result.labels == Labels({"job": "x"})

    # A filtering comparison also uses the resultMetric labels, so __name__
    # (not listed in on(job)) is dropped.
    comparison = q(engine, "a > on(job) b", 30_000).items[0]
    assert comparison.labels == Labels({"job": "x"})


def test_group_left_comparison_keeps_include_labels(engine, querier):
    querier.add_series(L("a", job="x", instance="1"), [(0, 10)])
    querier.add_series(L("b", job="x", version="v2"), [(0, 2)])

    result = q(engine, "a > on(job) group_left(version) b", 30_000).items[0]
    assert result.value == 10
    assert result.labels.get("version") == "v2"
    assert result.labels.get("instance") == "1"
    assert "__name__" not in result.labels

    bool_result = q(engine, "a > bool on(job) group_left(version) b", 30_000).items[0]
    assert bool_result.value == 1
    assert bool_result.labels.get("version") == "v2"
    assert "__name__" not in bool_result.labels


def test_range_function_uses_epoch_zero_end_time(engine, querier):
    # Counter rises by 60 per 60s, so rate=1 and increase over 5m=300.
    points = [(-300_000 + index * 60_000, float(index * 60)) for index in range(6)]
    querier.add_series(L("c"), points)
    # Evaluate one hour later; @ 0 and offset 60m both put the window at 0.
    assert only(engine, "rate(c[5m] @ 0)", 3_600_000) == pytest.approx(1.0)
    assert only(engine, "increase(c[5m] @ 0)", 3_600_000) == pytest.approx(300.0)
    assert only(engine, "rate(c[5m] offset 60m)", 3_600_000) == pytest.approx(1.0)


def test_timestamp_zero_is_not_treated_as_missing(engine, querier):
    querier.add_series(L("x"), [(0, 5)])
    assert only(engine, "timestamp(x)", 60_000) == 0.0


def test_division_by_negative_zero_keeps_ieee_sign(engine):
    assert scalar_value(engine, "1 / -0", 0) == math.inf * -1
    assert scalar_value(engine, "-1 / -0", 0) == math.inf


def test_absent_duplicate_equality_and_parentheses_drop_labels(engine, querier):
    duplicate = q(engine, 'absent(nope{a="1", a="2"})', 0).items[0]
    assert duplicate.labels == Labels.empty()
    parenthesized = q(engine, 'absent((nope{job="a"}))', 0).items[0]
    assert parenthesized.labels == Labels.empty()


def test_set_operations(engine, querier):
    querier.add_series(L("a", job="x"), [(0, 1)])
    querier.add_series(L("a", job="y"), [(0, 2)])
    querier.add_series(L("b", job="x"), [(0, 3)])
    assert set(vector_map(engine, "a and b", 0).values()) == {1}
    assert set(vector_map(engine, "a unless b", 0).values()) == {2}
    assert set(vector_map(engine, "a or b", 0).values()) == {1, 2}


def test_set_or_prefers_lhs_value(engine, querier):
    querier.add_series(L("a", job="x"), [(0, 1)])
    querier.add_series(L("b", job="x"), [(0, 3)])
    assert vector_map(engine, "a or b", 0) == {L("a", job="x"): 1}


def test_subquery_rate(engine, querier):
    querier.add_series(L("c"), [(i * 15_000, i * 2.0) for i in range(20)])
    value = q(engine, "rate(c[1m:15s])", 240_000)
    assert value.items[0].value == pytest.approx(2 / 15)


def test_subquery_scalar(engine, querier):
    result = q(engine, "vector(time())[1m:15s]", 60_000)
    assert isinstance(result, Matrix)
    # Left-open (0, 60] on the absolute 15s grid: 15, 30, 45, 60.
    assert [point[0] for point in result.series[0].samples] == [
        15_000,
        30_000,
        45_000,
        60_000,
    ]


def test_subquery_uses_absolute_step_grid(engine, querier):
    # At t=70s with [1m:15s], the grid is 15/30/45/60, not 25/40/55/70.
    result = q(engine, "vector(time())[1m:15s]", 70_000)
    assert [point[0] for point in result.series[0].samples] == [
        15_000,
        30_000,
        45_000,
        60_000,
    ]


def test_range_selector_is_left_open(engine, querier):
    querier.add_series(L("x"), [(0, 1), (15_000, 2), (30_000, 3), (45_000, 4), (60_000, 5)])
    # Windows are (t-range, t]: the sample exactly at t-30s is excluded.
    assert only(engine, "count_over_time(x[30s])", 60_000) == 2
    assert only(engine, "count_over_time(x[1m])", 60_000) == 4


def test_at_start_end_in_range_query(engine, querier):
    querier.add_series(L("x"), [(0, 1), (60_000, 2)])
    result = engine.query_range("x @ start()", 0, 60_000, 30_000).value
    assert [point[1] for point in result.series[0].samples] == [1, 1, 1]


def test_absent(engine, querier):
    result = q(engine, 'absent(nonexistent{job="api",instance="i1"})', 0)
    assert len(result.items) == 1
    assert result.items[0].value == 1
    assert result.items[0].labels.to_dict() == {"job": "api", "instance": "i1"}


def test_absent_present(engine, querier):
    querier.add_series(L("x"), [(0, 1)])
    assert q(engine, "absent(x)", 0).items == []


def test_absent_over_time(engine, querier):
    assert q(engine, "absent_over_time(x[5m])", 0).items[0].value == 1


def test_histogram_quantile(engine, querier):
    for upper, count in [("0.1", 50), ("0.5", 90), ("1", 95), ("+Inf", 100)]:
        querier.add_series(
            L("h_bucket", le=upper, job="a"), [(0, float(count))]
        )
    result = q(engine, "histogram_quantile(0.9, h_bucket)", 0)
    assert result.items[0].value == pytest.approx(0.5)
    assert "le" not in result.items[0].labels
    assert result.items[0].labels.get("job") == "a"


def test_histogram_quantile_edges(engine, querier):
    for upper, count in [("0.1", 50), ("0.5", 90), ("1", 95), ("+Inf", 100)]:
        querier.add_series(L("h_bucket", le=upper), [(0, float(count))])
    assert q(engine, "histogram_quantile(0, h_bucket)", 0).items[0].value == 0
    # The quantile falls in the +Inf bucket. Prometheus returns the upper
    # bound of the highest finite bucket (1), not +Inf.
    assert q(engine, "histogram_quantile(0.99, h_bucket)", 0).items[0].value == 1
    assert q(engine, "histogram_quantile(-0.1, h_bucket)", 0).items[0].value == -math.inf


def test_histogram_quantile_inf_bucket_uses_last_finite_bound(engine, querier):
    for upper, count in [("0.5", 20), ("2.5", 60), ("+Inf", 100)]:
        querier.add_series(L("h_bucket", le=upper), [(0, float(count))])
    value = q(engine, "histogram_quantile(0.9, h_bucket)", 0).items[0].value
    assert value == 2.5


def test_histogram_quantile_only_inf_bucket_is_nan(engine, querier):
    querier.add_series(L("h_bucket", le="+Inf"), [(0, 10.0)])
    assert math.isnan(q(engine, "histogram_quantile(0.5, h_bucket)", 0).items[0].value)


def test_histogram_quantile_missing_inf(engine, querier):
    querier.add_series(L("h_bucket", le="1"), [(0, 10)])
    assert math.isnan(q(engine, "histogram_quantile(0.5, h_bucket)", 0).items[0].value)


def test_histogram_quantile_zero_total(engine, querier):
    for upper in ("0.1", "+Inf"):
        querier.add_series(L("h_bucket", le=upper), [(0, 0)])
    assert math.isnan(q(engine, "histogram_quantile(0.5, h_bucket)", 0).items[0].value)


def test_label_replace(engine, querier):
    querier.add_series(L("up", instance="host1:9100"), [(0, 1)])
    result = q(engine, 'label_replace(up, "host", "$1", "instance", "([^:]+):.*")', 0)
    assert result.items[0].labels.get("host") == "host1"


def test_label_replace_is_anchored(engine, querier):
    querier.add_series(L("up", instance="a-a-a"), [(0, 1)])
    # Prometheus anchors the regex on both ends: "a" does not match "a-a-a",
    # so the series (and any existing destination label) is unchanged.
    result = q(engine, 'label_replace(up, "x", "b", "instance", "a")', 0)
    assert "x" not in result.items[0].labels


def test_label_replace_empty_expansion_deletes_destination(engine, querier):
    querier.add_series(L("up", job="api", x="old"), [(0, 1)])
    result = q(engine, 'label_replace(up, "x", "", "job", "(.*)")', 0)
    assert "x" not in result.items[0].labels


def test_label_replace_missing_group_expands_empty(engine, querier):
    querier.add_series(L("up", job="api", x="old"), [(0, 1)])
    result = q(engine, 'label_replace(up, "x", "$3", "job", "(a)(.*)")', 0)
    assert "x" not in result.items[0].labels


def test_label_join(engine, querier):
    querier.add_series(L("up", job="api", instance="host1"), [(0, 1)])
    result = q(engine, 'label_join(up, "id", ";", "job", "instance")', 0)
    assert result.items[0].labels.get("id") == "api;host1"


def test_timestamp_function(engine, querier):
    querier.add_series(L("x", job="a"), [(60_000, 1)])
    result = q(engine, "timestamp(x)", 90_000)
    assert result.items[0].value == 60.0
    assert "__name__" not in result.items[0].labels


def test_vector_scalar_conversions(engine, querier):
    assert q(engine, "vector(5)", 1000).items[0].value == 5
    assert q(engine, "vector(5)", 1000).items[0].timestamp == 1000
    assert scalar_value(engine, "scalar(vector(7))", 0) == 7
    querier.add_series(L("a"), [(0, 1)])
    assert scalar_value(engine, "scalar(a)", 0) == 1


def test_time_function(engine):
    assert scalar_value(engine, "time()", 12_345) == 12.345


def test_math_functions(engine, querier):
    querier.add_series(L("x"), [(0, -4)])
    assert q(engine, "abs(x)", 0).items[0].value == 4
    assert q(engine, "sqrt(abs(x))", 0).items[0].value == 2
    assert q(engine, "ceil(vector(1.2))", 0).items[0].value == 2
    assert q(engine, "floor(vector(1.8))", 0).items[0].value == 1
    assert q(engine, "round(vector(1.55), 0.1)", 0).items[0].value == pytest.approx(1.6)
    assert q(engine, "clamp(x, -2, 0)", 0).items[0].value == -2
    assert q(engine, "clamp_min(x, 1)", 0).items[0].value == 1


def test_date_functions(engine):
    timestamp = 1_700_000_000_000  # 2023-11-14T22:13:20Z, Tuesday

    def value(query):
        return q(engine, query, timestamp).items[0].value

    assert value("year()") == 2023
    assert value("month()") == 11
    assert value("day_of_month()") == 14
    assert value("day_of_week()") == 2
    assert value("hour()") == 22
    assert value("minute()") == 13


def test_quantile_over_time(engine, querier):
    querier.add_series(L("x"), [(0, 1), (15_000, 2), (30_000, 3), (45_000, 4)])
    # (0, 60s] contains 2, 3, 4; the median is 3.
    assert q(engine, "quantile_over_time(0.5, x[1m])", 60_000).items[0].value == 3


def test_over_time_functions(engine, querier):
    querier.add_series(L("x"), [(0, 1), (15_000, 2), (30_000, 3)])
    assert q(engine, "sum_over_time(x[1m])", 45_000).items[0].value == 6
    assert q(engine, "avg_over_time(x[1m])", 45_000).items[0].value == 2
    assert q(engine, "count_over_time(x[1m])", 45_000).items[0].value == 3
    assert q(engine, "last_over_time(x[1m])", 45_000).items[0].value == 3
    assert q(engine, "present_over_time(x[1m])", 45_000).items[0].value == 1
    assert q(engine, "max_over_time(x[1m])", 45_000).items[0].value == 3


def test_first_over_time_and_timestamp_helpers(engine, querier):
    querier.add_series(L("x", job="a"), [(0, 1), (15_000, 2), (30_000, 3)])
    first = q(engine, "first_over_time(x[1m])", 45_000).items[0]
    assert first.value == 1
    # first_over_time, like last_over_time, preserves the metric name.
    assert first.labels.get("__name__") == "x"

    ts_first = q(engine, "ts_of_first_over_time(x[1m])", 45_000).items[0]
    assert ts_first.value == 0.0
    assert "__name__" not in ts_first.labels

    ts_last = q(engine, "ts_of_last_over_time(x[1m])", 45_000).items[0]
    assert ts_last.value == 30.0
    assert "__name__" not in ts_last.labels


def test_integral_uses_trapezoids(engine, querier):
    querier.add_series(L("x", job="a"), [(0, 1), (15_000, 3), (30_000, 5)])
    # (1+3)/2*15s + (3+5)/2*15s = 30 + 60 = 90.
    result = q(engine, "integral(x[1m])", 45_000).items[0]
    assert result.value == 90
    assert "__name__" not in result.labels


def test_integral_single_sample_and_nan(engine, querier):
    querier.add_series(L("single"), [(0, 7)])
    assert only(engine, "integral(single[1m])", 30_000) == 0

    querier.add_series(L("gappy"), [(0, 0), (15_000, math.nan), (30_000, 4)])
    # Prometheus maps NaN to zero for integral(): 0 + (0+4)/2*15 = 30.
    assert only(engine, "integral(gappy[1m])", 45_000) == 30


def test_query_range_vector(engine, querier):
    querier.add_series(L("x", job="a"), [(0, 0), (30_000, 30), (60_000, 60)])
    result = engine.query_range("x", 0, 60_000, 30_000).value
    assert isinstance(result, Matrix)
    assert result.series[0].samples == [(0, 0.0), (30_000, 30.0), (60_000, 60.0)]


def test_query_range_scalar(engine):
    result = engine.query_range("time()", 0, 2000, 1000).value
    assert result.series[0].labels == Labels.empty()
    assert [value for _, value in result.series[0].samples] == [0, 1, 2]


def test_query_range_matrix_rejected(engine, querier):
    querier.add_series(L("x"), [(0, 1)])
    with pytest.raises(PrometheusError):
        engine.query_range("x[1m]", 0, 60_000, 30_000)


def test_query_range_step_validation(engine):
    with pytest.raises(PrometheusError):
        engine.query_range("time()", 0, 1000, 0)
    with pytest.raises(PrometheusError):
        engine.query_range("time()", 1000, 0, 1000)


def test_sorted_output(engine, querier):
    querier.add_series(L("x", i="2"), [(0, 2)])
    querier.add_series(L("x", i="1"), [(0, 1)])
    result = q(engine, "x", 0)
    assert [item.labels.get("i") for item in result.items] == ["1", "2"]


def test_sort_puts_nan_last_in_both_directions(engine, querier):
    querier.add_series(L("x", i="1"), [(0, 1.0)])
    querier.add_series(L("x", i="2"), [(0, 2.0)])
    querier.add_series(L("x", i="nan"), [(0, math.nan)])
    ascending = q(engine, "sort(x)", 0).items
    descending = q(engine, "sort_desc(x)", 0).items
    assert [item.labels.get("i") for item in ascending] == ["1", "2", "nan"]
    assert [item.labels.get("i") for item in descending] == ["2", "1", "nan"]


def test_double_exponential_smoothing(engine, querier):
    querier.add_series(L("x"), [(i * 15_000, float(i)) for i in range(10)])
    result = q(engine, "double_exponential_smoothing(x[2m], 0.5, 0.5)", 150_000)
    assert len(result.items) == 1
    assert math.isfinite(result.items[0].value)


def test_deriv_and_predict_linear(engine, querier):
    querier.add_series(L("x"), [(i * 15_000, i * 2.0) for i in range(10)])
    assert q(engine, "deriv(x[2m])", 120_000).items[0].value == pytest.approx(2 / 15)
    predicted = q(engine, "predict_linear(x[2m], 60)", 120_000).items[0].value
    assert predicted == pytest.approx(16 + 8)


def test_mad_over_time(engine, querier):
    querier.add_series(L("x"), [(0, 1), (15_000, 2), (30_000, 3), (45_000, 100)])
    assert q(engine, "mad_over_time(x[1m])", 60_000).items[0].value == 1


def test_limitk_and_limit_ratio(engine, querier):
    for index in range(10):
        querier.add_series(L("x", i=str(index)), [(0, index)])
    assert len(q(engine, "limitk(3, x)", 0).items) == 3
    selected = len(q(engine, "limit_ratio(0.5, x)", 0).items)
    assert 0 <= selected <= 10
