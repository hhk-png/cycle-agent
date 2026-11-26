from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest
import yaml

from mini_prometheus.app import AppOptions, PrometheusApp
from mini_prometheus.otlp import (
    OTLPError,
    OTLPOptions,
    OTLPReceiver,
    STRATEGY_NO_UTF8,
    STRATEGY_UNDERSCORE,
)


def resource_payload(metrics, resource=None, scope=None):
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": resource
                    if resource is not None
                    else [
                        {"key": "service.name", "value": {"stringValue": "api"}},
                        {"key": "service.instance.id", "value": {"stringValue": "api-0"}},
                        {"key": "k8s.pod.name", "value": {"stringValue": "api-0"}},
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": scope
                        if scope is not None
                        else {"name": "my.lib", "version": "1.2.3"},
                        "metrics": metrics,
                    }
                ],
            }
        ]
    }


def cumulative_sum(name="http.server.requests", unit="1", value=42, attrs=None):
    return {
        "name": name,
        "unit": unit,
        "sum": {
            "aggregationTemporality": 2,
            "isMonotonic": True,
            "dataPoints": [
                {
                    "timeUnixNano": "1700000000000000000",
                    "asInt": str(value),
                    "attributes": attrs or [],
                }
            ],
        },
    }


def test_cumulative_sum_translation_and_labels():
    receiver = OTLPReceiver()
    result = receiver.translate(resource_payload([cumulative_sum()]))
    labels, timestamp, value = result.samples[0]
    assert timestamp == 1_700_000_000_000
    assert value == 42.0
    assert labels.get("__name__") == "http.server.requests_total"
    assert labels.get("job") == "api"
    assert labels.get("instance") == "api-0"
    assert labels.get("k8s.pod.name") == "api-0"
    assert labels.get("otel_scope_name") == "my.lib"
    assert labels.get("otel_scope_version") == "1.2.3"
    # The original resource keys are represented by job/instance, not kept
    # as duplicate labels.
    assert labels.get("service_name") == ""
    assert labels.get("service_instance_id") == ""


def test_service_namespace_is_prefixed_to_job():
    payload = resource_payload(
        [cumulative_sum()],
        resource=[
            {"key": "service.name", "value": {"stringValue": "api"}},
            {"key": "service.namespace", "value": {"stringValue": "prod"}},
        ],
    )
    labels = OTLPReceiver().translate(payload).samples[0][0]
    assert labels.get("job") == "prod/api"


def test_gauge_and_unit_suffix():
    payload = resource_payload(
        [
            {
                "name": "request.duration",
                "unit": "s",
                "gauge": {
                    "dataPoints": [
                        {"timeUnixNano": "1700000000000000000", "asDouble": 0.25}
                    ]
                },
            }
        ]
    )
    labels, _, value = OTLPReceiver().translate(payload).samples[0]
    assert labels.get("__name__") == "request.duration_seconds"
    assert value == 0.25


def test_underscore_strategy_escapes_metric_name():
    payload = resource_payload([cumulative_sum(name="http.server.requests")])
    receiver = OTLPReceiver(
        OTLPOptions(translation_strategy=STRATEGY_UNDERSCORE)
    )
    labels = receiver.translate(payload).samples[0][0]
    assert labels.get("__name__") == "http_server_requests_total"


def test_no_utf8_strategy_preserves_utf8_name():
    payload = resource_payload([cumulative_sum(name="http.server.requests")])
    labels = OTLPReceiver(
        OTLPOptions(translation_strategy=STRATEGY_NO_UTF8)
    ).translate(payload).samples[0][0]
    assert labels.get("__name__") == "http.server.requests_total"


def test_histogram_expands_to_classic_buckets():
    payload = resource_payload(
        [
            {
                "name": "request.duration",
                "unit": "s",
                "histogram": {
                    "aggregationTemporality": 2,
                    "dataPoints": [
                        {
                            "timeUnixNano": "1700000000000000000",
                            "count": "4",
                            "sum": 1.2,
                            "explicitBounds": [0.1, 0.5],
                            "bucketCounts": ["1", "2", "1"],
                        }
                    ],
                },
            }
        ]
    )
    samples = OTLPReceiver().translate(payload).samples
    by_name = {}
    for labels, _, value in samples:
        by_name.setdefault(labels.get("__name__"), []).append(
            (labels.get("le"), value)
        )
    assert by_name["request.duration_seconds_bucket"] == [
        ("0.1", 1.0),
        ("0.5", 3.0),
        ("+Inf", 4.0),
    ]
    assert by_name["request.duration_seconds_sum"] == [("", 1.2)]
    assert by_name["request.duration_seconds_count"] == [("", 4.0)]


def test_histogram_count_is_derived_when_missing():
    payload = resource_payload(
        [
            {
                "name": "latency",
                "histogram": {
                    "aggregationTemporality": 2,
                    "dataPoints": [
                        {
                            "timeUnixNano": "1700000000000000000",
                            "explicitBounds": [1.0],
                            "bucketCounts": ["2", "3"],
                        }
                    ],
                },
            }
        ]
    )
    samples = OTLPReceiver().translate(payload).samples
    count = [
        value
        for labels, _, value in samples
        if labels.get("__name__") == "latency_count"
    ]
    assert count == [5.0]


def test_summary_expands_to_quantiles():
    payload = resource_payload(
        [
            {
                "name": "request.duration",
                "unit": "s",
                "summary": {
                    "dataPoints": [
                        {
                            "timeUnixNano": "1700000000000000000",
                            "count": "10",
                            "sum": 5.0,
                            "quantileValues": [
                                {"quantile": 0.5, "value": 0.4},
                                {"quantile": 0.99, "value": 1.5},
                            ],
                        }
                    ]
                },
            }
        ]
    )
    samples = OTLPReceiver().translate(payload).samples
    names = {labels.get("__name__") for labels, _, _ in samples}
    assert names == {
        "request.duration_seconds",
        "request.duration_seconds_count",
        "request.duration_seconds_sum",
    }
    quantiles = [
        (labels.get("quantile"), value)
        for labels, _, value in samples
        if labels.get("__name__") == "request.duration_seconds"
    ]
    assert quantiles == [("0.5", 0.4), ("0.99", 1.5)]


def test_delta_is_rejected_without_feature():
    payload = resource_payload(
        [
            {
                "name": "requests",
                "unit": "1",
                "sum": {
                    "aggregationTemporality": 1,
                    "isMonotonic": True,
                    "dataPoints": [
                        {
                            "startTimeUnixNano": "1000000000",
                            "timeUnixNano": "2000000000",
                            "asInt": "5",
                        }
                    ],
                },
            }
        ]
    )
    with pytest.raises(OTLPError, match="otlp-deltatocumulative"):
        OTLPReceiver().translate(payload)


def test_delta_conversion_accumulates_and_resets_on_new_start():
    payload = resource_payload(
        [
            {
                "name": "requests",
                "unit": "1",
                "sum": {
                    "aggregationTemporality": 1,
                    "isMonotonic": True,
                    "dataPoints": [
                        {
                            "startTimeUnixNano": "1000000000",
                            "timeUnixNano": "2000000000",
                            "asInt": "5",
                        },
                        {
                            "startTimeUnixNano": "1000000000",
                            "timeUnixNano": "3000000000",
                            "asInt": "7",
                        },
                        {
                            "startTimeUnixNano": "4000000000",
                            "timeUnixNano": "5000000000",
                            "asInt": "2",
                        },
                    ],
                },
            }
        ]
    )
    receiver = OTLPReceiver(OTLPOptions(delta_to_cumulative=True))
    samples = receiver.translate(payload).samples
    assert [value for _, _, value in samples] == [5.0, 12.0, 2.0]
    assert [ts for _, ts, _ in samples] == [2000, 3000, 5000]
    assert receiver.stats["deltaResets"] == 1


def test_out_of_order_delta_is_dropped_without_resetting_state():
    payload = resource_payload(
        [
            {
                "name": "requests",
                "sum": {
                    "aggregationTemporality": 1,
                    "isMonotonic": True,
                    "dataPoints": [
                        {
                            "startTimeUnixNano": "1000000000",
                            "timeUnixNano": "2000000000",
                            "asInt": "5",
                        },
                        {
                            "startTimeUnixNano": "1000000000",
                            "timeUnixNano": "1000000000",
                            "asInt": "3",
                        },
                        {
                            "startTimeUnixNano": "1000000000",
                            "timeUnixNano": "3000000000",
                            "asInt": "7",
                        },
                    ],
                },
            }
        ]
    )
    receiver = OTLPReceiver(OTLPOptions(delta_to_cumulative=True))
    result = receiver.translate(payload)
    assert [value for _, _, value in result.samples] == [5.0, 12.0]
    assert receiver.stats["droppedOutOfOrderDeltas"] == 1
    assert any("out-of-order" in warning for warning in result.warnings)


def test_delta_histogram_accumulates_buckets():
    def point(start, ts, buckets):
        return {
            "startTimeUnixNano": start,
            "timeUnixNano": ts,
            "count": str(sum(buckets)),
            "sum": float(sum(buckets)),
            "explicitBounds": [1.0],
            "bucketCounts": [str(value) for value in buckets],
        }

    payload = resource_payload(
        [
            {
                "name": "latency",
                "histogram": {
                    "aggregationTemporality": 1,
                    "dataPoints": [
                        point("1000000000", "2000000000", [1, 1]),
                        point("1000000000", "3000000000", [2, 1]),
                    ],
                },
            }
        ]
    )
    samples = OTLPReceiver(OTLPOptions(delta_to_cumulative=True)).translate(
        payload
    ).samples
    latest = {}
    for labels, ts, value in samples:
        if ts == 3000:
            latest[(labels.get("__name__"), labels.get("le"))] = value
    assert latest[("latency_bucket", "1")] == 3.0  # 1 + 2
    assert latest[("latency_bucket", "+Inf")] == 5.0
    assert latest[("latency_count", "")] == 5.0


def test_null_data_points_are_treated_as_empty():
    payload = resource_payload(
        [
            {"name": "empty.gauge", "gauge": {"dataPoints": None}},
            {
                "name": "empty.histogram",
                "histogram": {"dataPoints": None, "aggregationTemporality": 2},
            },
        ]
    )
    result = OTLPReceiver().translate(payload)
    assert result.samples == []


def test_two_delta_histograms_do_not_share_state():
    def hist(name, bucket):
        return {
            "name": name,
            "histogram": {
                "aggregationTemporality": 1,
                "dataPoints": [
                    {
                        "startTimeUnixNano": "1000000000",
                        "timeUnixNano": "2000000000",
                        "count": str(bucket * 2),
                        "sum": str(bucket * 2),
                        "explicitBounds": [1.0],
                        "bucketCounts": [str(bucket), str(bucket)],
                    }
                ],
            },
        }

    payload = resource_payload([hist("latency", 1), hist("size", 10)])
    samples = OTLPReceiver(OTLPOptions(delta_to_cumulative=True)).translate(
        payload
    ).samples
    values = {}
    for labels, _, value in samples:
        if labels.get("le") == "+Inf":
            values[labels.get("__name__")] = value
    assert values["latency_bucket"] == 2.0
    assert values["size_bucket"] == 20.0


def test_retried_delta_with_same_timestamp_is_not_double_counted():
    payload = resource_payload(
        [
            {
                "name": "requests",
                "sum": {
                    "aggregationTemporality": 1,
                    "isMonotonic": True,
                    "dataPoints": [
                        {
                            "startTimeUnixNano": "1000000000",
                            "timeUnixNano": "2000000000",
                            "asInt": "5",
                        },
                        {
                            "startTimeUnixNano": "1000000000",
                            "timeUnixNano": "2000000000",
                            "asInt": "5",
                        },
                        {
                            "startTimeUnixNano": "1000000000",
                            "timeUnixNano": "3000000000",
                            "asInt": "7",
                        },
                    ],
                },
            }
        ]
    )
    receiver = OTLPReceiver(OTLPOptions(delta_to_cumulative=True))
    samples = receiver.translate(payload).samples
    assert [value for _, _, value in samples] == [5.0, 12.0]
    assert receiver.stats["droppedOutOfOrderDeltas"] == 1


def test_failed_translation_rolls_back_delta_state():
    delta_sum = {
        "name": "requests",
        "sum": {
            "aggregationTemporality": 1,
            "isMonotonic": True,
            "dataPoints": [
                {
                    "startTimeUnixNano": "1000000000",
                    "timeUnixNano": "2000000000",
                    "asInt": "5",
                }
            ],
        },
    }
    exponential = {
        "name": "latency",
        "exponentialHistogram": {"dataPoints": [{}], "aggregationTemporality": 2},
    }
    receiver = OTLPReceiver(OTLPOptions(delta_to_cumulative=True))
    with pytest.raises(OTLPError):
        receiver.translate(resource_payload([delta_sum, exponential]))
    # The failed request must not leave the sum at 5.
    retry = receiver.translate(resource_payload([delta_sum])).samples
    assert [value for _, _, value in retry] == [5.0]


def test_histogram_layout_error_does_not_mutate_state():
    def point(start, ts, buckets):
        return {
            "startTimeUnixNano": start,
            "timeUnixNano": ts,
            "count": str(sum(buckets)),
            "sum": float(sum(buckets)),
            "explicitBounds": [1.0] if len(buckets) == 2 else [1.0, 2.0],
            "bucketCounts": [str(value) for value in buckets],
        }

    receiver = OTLPReceiver(OTLPOptions(delta_to_cumulative=True))

    def payload(p):
        return resource_payload(
            [
                {
                    "name": "latency",
                    "histogram": {
                        "aggregationTemporality": 1,
                        "dataPoints": [p],
                    },
                }
            ]
        )

    receiver.translate(payload(point("1000000000", "2000000000", [1, 1])))
    with pytest.raises(OTLPError, match="layout"):
        receiver.translate(payload(point("1000000000", "3000000000", [2, 2, 2])))
    samples = receiver.translate(
        payload(point("1000000000", "4000000000", [1, 1]))
    ).samples
    counts = [
        value for labels, _, value in samples if labels.get("__name__") == "latency_count"
    ]
    assert counts == [4.0]


def test_delta_histogram_same_bucket_count_but_changed_bounds_is_rejected():
    def point(start, ts, bounds, buckets):
        return {
            "startTimeUnixNano": start,
            "timeUnixNano": ts,
            "count": str(sum(buckets)),
            "sum": float(sum(buckets)),
            "explicitBounds": bounds,
            "bucketCounts": [str(value) for value in buckets],
        }

    receiver = OTLPReceiver(OTLPOptions(delta_to_cumulative=True))

    def payload(p):
        return resource_payload(
            [
                {
                    "name": "latency_bounds",
                    "histogram": {
                        "aggregationTemporality": 1,
                        "dataPoints": [p],
                    },
                }
            ]
        )

    receiver.translate(payload(point("1000000000", "2000000000", [1.0, 2.0], [1, 1, 0])))
    with pytest.raises(OTLPError, match="layout"):
        receiver.translate(
            payload(point("1000000000", "3000000000", [10.0, 20.0], [1, 1, 0]))
        )
    # The rejected point must not mutate state: a valid point with the
    # original bounds still accumulates normally.
    samples = receiver.translate(
        payload(point("1000000000", "4000000000", [1.0, 2.0], [1, 1, 0]))
    ).samples
    counts = [
        value
        for labels, _, value in samples
        if labels.get("__name__") == "latency_bounds_count"
    ]
    assert counts == [4.0]


def test_lone_surrogate_metric_name_is_rejected():
    payload = resource_payload(
        [
            {
                "name": "bad\ud800name",
                "gauge": {"dataPoints": [{"timeUnixNano": "1700000000000000000", "asInt": "1"}]},
            }
        ]
    )
    with pytest.raises(OTLPError, match="UTF-8|surrogate"):
        OTLPReceiver().translate(payload)


def test_lone_surrogate_attribute_value_is_rejected():
    payload = resource_payload(
        [cumulative_sum()],
        resource=[
            {"key": "service.name", "value": {"stringValue": "api"}},
            {"key": "bad", "value": {"stringValue": "x\ud800"}},
        ],
    )
    with pytest.raises(OTLPError, match="UTF-8"):
        OTLPReceiver().translate(payload)


def test_exponential_histogram_is_rejected_explicitly():
    payload = resource_payload(
        [
            {
                "name": "latency",
                "exponentialHistogram": {
                    "aggregationTemporality": 2,
                    "dataPoints": [{"timeUnixNano": "1700000000000000000"}],
                },
            }
        ]
    )
    with pytest.raises(OTLPError, match="native-histogram"):
        OTLPReceiver().translate(payload)


def test_colliding_attribute_names_are_reported_not_merged():
    payload = resource_payload(
        [cumulative_sum()],
        resource=[
            {"key": "service.name", "value": {"stringValue": "api"}},
            {"key": "a.b", "value": {"stringValue": "first"}},
            {"key": "a_b", "value": {"stringValue": "second"}},
        ],
    )
    result = OTLPReceiver(
        OTLPOptions(translation_strategy=STRATEGY_UNDERSCORE)
    ).translate(payload)
    labels = result.samples[0][0]
    assert labels.get("a_b") == "first"
    assert any("collision" in warning for warning in result.warnings)


class Client:
    def __init__(self, base):
        self.base = base

    def get(self, path, **params):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def raw_get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read()

    def post(self, path, body, headers):
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)


def make_otlp_app(tmp_path, enabled=True, delta=False):
    config = {
        "global": {
            "scrape_interval": "15s",
            "scrape_timeout": "5s",
            "evaluation_interval": "15s",
        },
        "scrape_configs": [],
    }
    path = tmp_path / "prometheus.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    app = PrometheusApp(
        AppOptions(
            config_file=str(path),
            data_dir=str(tmp_path / "data"),
            listen_address="127.0.0.1:0",
            start_threads=False,
            enable_otlp_receiver=enabled,
            otlp_delta_to_cumulative=delta,
            log_requests=False,
        )
    )
    app.start()
    return app, Client(app.url())


def push_otlp(client, payload, content_type="application/json", encoding=None):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": content_type}
    if encoding == "gzip":
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    return client.post("/api/v1/otlp/v1/metrics", body, headers)


def test_otlp_endpoint_ingests_and_query_works(tmp_path):
    app, client = make_otlp_app(tmp_path)
    try:
        status, body, _ = push_otlp(client, resource_payload([cumulative_sum()]))
        assert status == 200
        assert body == b""
        status, payload = client.get(
            "/api/v1/query",
            query='{"http.server.requests_total"}',
            time="1700000000",
        )
        assert status == 200
        assert payload["data"]["result"][0]["metric"]["job"] == "api"
        assert payload["data"]["result"][0]["value"] == [1_700_000_000.0, "42"]
        _, metrics = client.raw_get("/metrics")
        assert "prometheus_otlp_received_samples_total 1" in metrics.decode()
    finally:
        app.shutdown()


def test_otlp_endpoint_gzip_body(tmp_path):
    app, client = make_otlp_app(tmp_path)
    try:
        status, _, _ = push_otlp(
            client, resource_payload([cumulative_sum()]), encoding="gzip"
        )
        assert status == 200
    finally:
        app.shutdown()


def test_otlp_endpoint_truncated_gzip_is_bad_data(tmp_path):
    app, client = make_otlp_app(tmp_path)
    try:
        body = gzip.compress(json.dumps(resource_payload([cumulative_sum()])).encode())[:-5]
        status, _, _ = client.post(
            "/api/v1/otlp/v1/metrics",
            body,
            {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        assert status == 400
    finally:
        app.shutdown()


def test_otlp_endpoint_is_404_when_disabled(tmp_path):
    app, client = make_otlp_app(tmp_path, enabled=False)
    try:
        status, body, _ = push_otlp(client, resource_payload([cumulative_sum()]))
        assert status == 404
        payload = json.loads(body.decode())
        assert payload["errorType"] == "not_found"
    finally:
        app.shutdown()


def test_otlp_endpoint_rejects_protobuf(tmp_path):
    app, client = make_otlp_app(tmp_path)
    try:
        status, _, _ = client.post(
            "/api/v1/otlp/v1/metrics",
            b"\x00",
            {"Content-Type": "application/x-protobuf"},
        )
        assert status == 400
    finally:
        app.shutdown()


def test_otlp_endpoint_rejects_invalid_json(tmp_path):
    app, client = make_otlp_app(tmp_path)
    try:
        status, _, _ = client.post(
            "/api/v1/otlp/v1/metrics",
            b"{not json",
            {"Content-Type": "application/json"},
        )
        assert status == 400
    finally:
        app.shutdown()


def test_otlp_endpoint_delta_requires_feature(tmp_path):
    app, client = make_otlp_app(tmp_path)
    try:
        delta_payload = resource_payload(
            [
                {
                    "name": "requests",
                    "sum": {
                        "aggregationTemporality": 1,
                        "isMonotonic": True,
                        "dataPoints": [
                            {
                                "startTimeUnixNano": "1000000000",
                                "timeUnixNano": "2000000000",
                                "asInt": "5",
                            }
                        ],
                    },
                }
            ]
        )
        status, _, _ = push_otlp(client, delta_payload)
        assert status == 400
    finally:
        app.shutdown()


def test_otlp_endpoint_delta_feature_converts(tmp_path):
    app, client = make_otlp_app(tmp_path, delta=True)
    try:
        delta_payload = resource_payload(
            [
                {
                    "name": "requests",
                    "sum": {
                        "aggregationTemporality": 1,
                        "isMonotonic": True,
                        "dataPoints": [
                            {
                                "startTimeUnixNano": "1000000000",
                                "timeUnixNano": "2000000000",
                                "asInt": "5",
                            },
                            {
                                "startTimeUnixNano": "1000000000",
                                "timeUnixNano": "3000000000",
                                "asInt": "7",
                            },
                        ],
                    },
                }
            ]
        )
        status, _, _ = push_otlp(client, delta_payload)
        assert status == 200
        _, payload = client.get(
            "/api/v1/query", query='{"requests_total"}', time="3"
        )
        assert payload["data"]["result"][0]["value"] == [3.0, "12"]
    finally:
        app.shutdown()
