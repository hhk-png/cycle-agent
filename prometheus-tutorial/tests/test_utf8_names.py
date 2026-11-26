from __future__ import annotations

import pytest

from mini_prometheus.exposition import (
    ExpositionError,
    format_exposition,
    parse_exposition,
)
from mini_prometheus.labels import (
    LABEL_NAME_RE,
    METRIC_NAME_RE,
    Labels,
    format_labels,
    is_legacy_label_name,
    is_legacy_metric_name,
)
from mini_prometheus.model import ParseError
from mini_prometheus.promql.engine import Engine, InMemoryQuerier
from mini_prometheus.promql.parser import parse_expr


def test_legacy_and_utf8_classification():
    assert is_legacy_metric_name("http_requests_total")
    assert not is_legacy_metric_name("http.server.duration")
    assert is_legacy_label_name("service_name")
    assert not is_legacy_label_name("service.name")
    assert METRIC_NAME_RE.match("a:b")
    assert LABEL_NAME_RE.match("_x")
    assert not LABEL_NAME_RE.match("9x")


def test_format_labels_quotes_only_non_legacy_names():
    legacy = Labels({"__name__": "http_requests_total", "job": "api"})
    assert format_labels(legacy) == 'http_requests_total{job="api"}'

    utf8 = Labels(
        {
            "__name__": "http.server.duration",
            "service.name": "api",
            "method": "GET",
        }
    )
    rendered = format_labels(utf8)
    assert rendered.startswith('{"http.server.duration",')
    assert '"service.name"="api"' in rendered
    assert 'method="GET"' in rendered


def test_format_labels_escapes_quotes_and_newlines():
    labels = Labels({"__name__": 'metric"name', "a.b": 'line\n"two"'})
    rendered = format_labels(labels)
    assert '\\"name' in rendered
    assert 'line\\n\\"two\\"' in rendered


def test_exposition_accepts_quoted_metric_and_label_names():
    text = (
        '# HELP "http.server.duration" Request duration.\n'
        '# TYPE "http.server.duration" histogram\n'
        '{"http.server.duration_bucket", "service.name"="api", method="GET", le="0.5"} 3\n'
        '{"http.server.duration_bucket", "service.name"="api", method="GET", le="+Inf"} 4\n'
    )
    payload = parse_exposition(text)
    assert payload.families["http.server.duration"].type.value == "histogram"
    assert payload.families["http.server.duration"].help == "Request duration."
    assert payload.samples[0].labels.get("service.name") == "api"
    assert payload.samples[0].labels.get("__name__") == "http.server.duration_bucket"


def test_exposition_quoted_label_name_without_metric_name_is_rejected():
    with pytest.raises(ExpositionError):
        parse_exposition('{"service.name"="api"} 1\n')


def test_format_labels_uses_braces_for_bare_utf8_name():
    labels = Labels({"__name__": "queue.depth"})
    assert format_labels(labels) == '{"queue.depth"}'
    payload = parse_exposition(format_labels(labels) + " 7\n")
    assert payload.samples[0].labels == labels


def test_exposition_roundtrip_preserves_raw_cr_in_label_value():
    # A literal CR must not be treated as a line separator by the parser.
    payload = parse_exposition('{"a.b", note="line\rinside"} 1\n')
    assert payload.samples[0].labels.get("note") == "line\rinside"
    rendered = format_exposition(payload)
    assert parse_exposition(rendered).samples[0].labels.get("note") == "line\rinside"


def test_exposition_roundtrip_utf8_names():
    source = '{"http.server.duration_bucket", "service.name"="api", le="+Inf"} 4\n'
    payload = parse_exposition(source)
    rendered = format_exposition(payload)
    reparsed = parse_exposition(rendered)
    assert reparsed.samples[0].labels == payload.samples[0].labels
    assert reparsed.samples[0].value == payload.samples[0].value


def test_promql_selector_with_quoted_label_and_metric_names():
    expr = parse_expr('{"http.server.duration_bucket", "service.name"="api", le="+Inf"}')
    names = {matcher.name for matcher in expr.matchers}
    assert names == {"__name__", "service.name", "le"}
    assert expr.name == "http.server.duration_bucket"


def test_promql_quoted_name_only_selector():
    expr = parse_expr('{"service.name"="api"}')
    assert expr.name == ""
    assert [m.name for m in expr.matchers] == ["service.name"]


def test_promql_quoted_grouping_labels():
    expr = parse_expr('sum by ("service.name") ({"http.server.duration_bucket"})')
    assert expr.grouping == ["service.name"]
    assert expr.without is False


def test_promql_quoted_metric_name_cannot_be_duplicated():
    with pytest.raises(ParseError):
        parse_expr('{"a.b", "a.b"}')
    with pytest.raises(ParseError):
        parse_expr('{"a.b", __name__="a.b"}')


def test_promql_empty_quoted_label_name_is_rejected():
    with pytest.raises(ParseError):
        parse_expr('{""="x"}')


def test_promql_quoted_metric_name_must_be_first():
    with pytest.raises(ParseError):
        parse_expr('{job="api", "http.server.duration"}')


def test_promql_quoted_labels_in_on_and_group_modifiers():
    expr = parse_expr('a * on ("service.name") group_left ("deployment.environment") b')
    assert expr.matching.labels == ["service.name"]
    assert expr.matching.include == ["deployment.environment"]
    expr = parse_expr('a and on ("service.name") b')
    assert expr.matching.labels == ["service.name"]


def test_engine_binary_match_on_utf8_labels(tsdb):
    tsdb.append(
        Labels({"__name__": "a", "service.name": "api"}),
        1_700_000_000_000,
        2.0,
    )
    tsdb.append(
        Labels(
            {
                "__name__": "b",
                "service.name": "api",
                "deployment.environment": "prod",
            }
        ),
        1_700_000_000_000,
        3.0,
    )
    engine = Engine(tsdb)
    result = engine.query(
        'a * on ("service.name") group_left ("deployment.environment") b',
        1_700_000_000_000,
    )
    assert len(result.value.items) == 1
    assert result.value.items[0].value == 6.0
    assert result.value.items[0].labels.get("deployment.environment") == "prod"


def test_engine_aggregates_by_utf8_label(tsdb):
    labels = Labels(
        {
            "__name__": "http.server.duration_bucket",
            "service.name": "api",
            "le": "+Inf",
        }
    )
    tsdb.append(labels, 1_700_000_000_000, 4.0)
    engine = Engine(tsdb)
    result = engine.query(
        'sum by ("service.name") ({"http.server.duration_bucket"})',
        1_700_000_000_000,
    )
    assert len(result.value.items) == 1
    assert result.value.items[0].labels.get("service.name") == "api"
    assert result.value.items[0].value == 4.0


def test_label_replace_can_create_utf8_label(tsdb):
    tsdb.append(
        Labels({"__name__": "x", "job": "api"}),
        1_700_000_000_000,
        1.0,
    )
    engine = Engine(tsdb)
    query = 'label_replace(x, "service.name", "$1", "job", "(.*)")'
    result = engine.query(query, 1_700_000_000_000)
    assert result.value.items[0].labels.get("service.name") == "api"
    # The new label is computed per query; group by it to prove the quoted
    # grouping syntax consumes it end to end.
    result = engine.query(
        'sum by ("service.name") '
        '(label_replace(x, "service.name", "$1", "job", "(.*)"))',
        1_700_000_000_000,
    )
    assert len(result.value.items) == 1
    assert result.value.items[0].labels.get("service.name") == "api"
