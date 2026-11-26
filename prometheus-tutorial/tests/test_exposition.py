from __future__ import annotations

import math

import pytest

from mini_prometheus.exposition import (
    ExpositionError,
    format_exposition,
    parse_exposition,
)
from mini_prometheus.labels import Labels
from mini_prometheus.model import MetricType


def parse(text, **kwargs):
    return parse_exposition(text, default_timestamp=1_700_000_000_000, **kwargs)


def test_simple_sample():
    payload = parse("http_requests_total 10\n")
    assert len(payload.samples) == 1
    sample = payload.samples[0]
    assert sample.labels.get_name() == "http_requests_total"
    assert sample.value == 10
    assert sample.timestamp is None


def test_labels_and_timestamp():
    payload = parse('http_requests_total{job="api",code="200"} 10 1700000000123\n')
    sample = payload.samples[0]
    assert sample.labels.to_dict() == {
        "__name__": "http_requests_total",
        "job": "api",
        "code": "200",
    }
    assert sample.timestamp == 1_700_000_000_123


def test_help_and_type_metadata():
    payload = parse(
        "# HELP http_requests_total Total requests\\nserved\n"
        "# TYPE http_requests_total counter\n"
        "http_requests_total 1\n"
    )
    meta = payload.metadata["http_requests_total"]
    assert meta.type == MetricType.COUNTER
    assert meta.help == "Total requests\nserved"


def test_type_after_sample_rejected():
    with pytest.raises(ExpositionError):
        parse("# TYPE x gauge\nx 1\n# TYPE x counter\n")


def test_conflicting_type_rejected():
    with pytest.raises(ExpositionError):
        parse("# TYPE x gauge\n# TYPE x counter\nx 1\n")


def test_unknown_type_rejected():
    with pytest.raises(ExpositionError):
        parse("# TYPE x nonsense\nx 1\n")


def test_metric_name_validation():
    with pytest.raises(ExpositionError):
        parse("1bad 1\n")


def test_quoted_utf8_metric_name():
    payload = parse('{"http.requests.total",job="api"} 3\n')
    assert payload.samples[0].labels.get_name() == "http.requests.total"


def test_label_escaping():
    payload = parse(r'x{a="line\nnext",b="quote\"",c="back\\slash"} 1' + "\n")
    labels = payload.samples[0].labels
    assert labels.get("a") == "line\nnext"
    assert labels.get("b") == 'quote"'
    assert labels.get("c") == "back\\slash"


def test_invalid_escape_rejected():
    with pytest.raises(ExpositionError):
        parse(r'x{a="bad\q"} 1' + "\n")


def test_values_special_floats():
    payload = parse("a +Inf\nb -Inf\nc NaN\nd 1e3\ne -0.5\n")
    values = [sample.value for sample in payload.samples]
    assert values[0] == math.inf
    assert values[1] == -math.inf
    assert math.isnan(values[2])
    assert values[3] == 1000
    assert values[4] == -0.5


def test_duplicate_identical_sample_ignored():
    payload = parse("x{a=\"1\"} 5\nx{a=\"1\"} 5\n")
    assert len(payload.samples) == 1


def test_duplicate_different_sample_rejected():
    with pytest.raises(ExpositionError):
        parse("x 5\nx 6\n")


def test_empty_label_value_is_dropped():
    payload = parse('x{a="",b="1"} 1\n')
    assert payload.samples[0].labels.to_dict() == {"__name__": "x", "b": "1"}


def test_comments_are_ignored():
    payload = parse("# a comment\nx 1\n# another\n")
    assert len(payload.samples) == 1


def test_sample_limit():
    with pytest.raises(ExpositionError):
        parse("a 1\nb 1\n", max_samples=1)


def test_openmetrics_requires_eof():
    with pytest.raises(ExpositionError):
        parse("x 1\n", openmetrics=True)


def test_openmetrics_accepts_eof():
    payload = parse("x 1\n# EOF\n", openmetrics=True)
    assert len(payload.samples) == 1


def test_openmetrics_sample_timestamp_is_seconds():
    # OpenMetrics uses fractional seconds, not milliseconds.
    payload = parse("x 1 1700000000.123\n# EOF\n", openmetrics=True)
    assert payload.samples[0].timestamp == 1_700_000_000_123


def test_openmetrics_exemplar_timestamp_is_seconds():
    payload = parse(
        'x_bucket{le="1"} 5 # {trace_id="abc"} 4.2 1700000000.5\n# EOF\n',
        openmetrics=True,
    )
    assert payload.samples[0].exemplars[0].timestamp == 1_700_000_000_500


def test_openmetrics_unit():
    payload = parse("# TYPE x gauge\n# UNIT x seconds\nx 1\n# EOF\n", openmetrics=True)
    assert payload.metadata["x"].unit == "seconds"


def test_exemplar_parsed():
    payload = parse(
        'x_bucket{le="1"} 5 # {trace_id="abc"} 4.2 1700000000000\n'
    )
    exemplar = payload.samples[0].exemplars[0]
    assert exemplar.labels.get("trace_id") == "abc"
    assert exemplar.value == 4.2
    assert exemplar.timestamp == 1_700_000_000_000


def test_exemplar_without_timestamp_uses_default():
    payload = parse('x 5 # {trace_id="abc"} 4.2\n')
    assert payload.samples[0].exemplars[0].timestamp == 1_700_000_000_000


def test_untyped_metric_unknown():
    payload = parse("x 1\n")
    assert payload.metadata["x"].type == MetricType.UNKNOWN


def test_format_exposition_roundtrip():
    original = parse(
        "# HELP x Help text\n# TYPE x counter\n"
        'x{a="b",c="d"} 2.5 1700000000000\n'
    )
    text = format_exposition(original)
    reparsed = parse_exposition(text, default_timestamp=0)
    assert reparsed.samples[0].labels == original.samples[0].labels
    assert reparsed.samples[0].value == 2.5


def test_format_escapes_label_values():
    sample = parse('x{a="line\\nnext"} 1\n').samples[0]
    text = format_exposition([sample])
    assert "\\n" in text
    assert parse_exposition(text).samples[0].labels.get("a") == "line\nnext"


def test_duplicate_metric_name_rejected():
    with pytest.raises(ExpositionError):
        parse('foo{__name__="bar"} 1\n')


def test_label_set_trailing_comma():
    payload = parse('x{a="1",} 1\n')
    assert payload.samples[0].labels.get("a") == "1"


def test_timestamp_is_integer():
    with pytest.raises(ExpositionError):
        parse("x 1 1.5\n")


def test_openmetrics_counter_total_metadata():
    payload = parse(
        "# TYPE requests counter\nrequests_total 5\n# EOF\n", openmetrics=True
    )
    assert payload.metadata["requests"].type == MetricType.COUNTER
    assert payload.samples[0].labels.get_name() == "requests_total"
