from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import yaml

from mini_prometheus.app import AppOptions, PrometheusApp
from mini_prometheus.labels import Labels
from mini_prometheus.model import MetricMetadata, MetricType
from mini_prometheus.remote import encode_write_request, snappy_compress
from mini_prometheus.util.timeutil import now_ms


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
            return exc.code, json.loads(exc.read().decode())

    def raw_get(self, path, **params):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode(), dict(response.headers)

    def post(self, path, data=None, headers=None, raw=False):
        body = data
        if data is not None and not isinstance(data, bytes):
            body = urllib.parse.urlencode(data, doseq=True).encode()
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers or {}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read()
                return response.status, payload if raw else json.loads(payload or b"{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, payload if raw else json.loads(payload or b"{}")


def make_app(tmp_path, admin=False, alerting=False, rule_files=True):
    rules = tmp_path / "rules.yml"
    rules.write_text(
        yaml.safe_dump(
            {
                "groups": [
                    {
                        "name": "g",
                        "rules": [
                            {"record": "x:sum", "expr": "sum(x)"},
                            {
                                "alert": "High",
                                "expr": "x > 0",
                                "for": "0s",
                                "labels": {"severity": "page"},
                                "annotations": {"summary": "x is {{ $value }}"},
                            },
                        ],
                    }
                ]
            }
        )
    )
    config = {
        "global": {
            "scrape_interval": "15s",
            "scrape_timeout": "5s",
            "evaluation_interval": "15s",
            "external_labels": {"cluster": "test"},
        },
        "scrape_configs": [
            {
                "job_name": "demo",
                "static_configs": [{"targets": ["127.0.0.1:9100"]}],
            }
        ],
    }
    if rule_files:
        config["rule_files"] = [str(rules)]
    if alerting:
        config["alerting"] = {
            "alertmanagers": [{"static_configs": [{"targets": ["am:9093"]}]}]
        }
    path = tmp_path / "prometheus.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    app = PrometheusApp(
        AppOptions(
            config_file=str(path),
            data_dir=str(tmp_path / "data"),
            listen_address="127.0.0.1:0",
            start_threads=False,
            enable_admin_api=admin,
            log_requests=False,
        )
    )
    app.start()
    return app, Client(app.url())


@pytest.fixture()
def api(tmp_path):
    app, client = make_app(tmp_path)
    labels = Labels({"__name__": "x", "job": "api", "instance": "i1"})
    for index in range(5):
        app.tsdb.append(labels, 1_700_000_000_000 + index * 15_000, float(index + 1))
    yield app, client
    app.shutdown()


def test_query_vector(api):
    app, client = api
    status, payload = client.get("/api/v1/query", query="x", time="1700000060")
    assert status == 200
    assert payload["status"] == "success"
    data = payload["data"]
    assert data["resultType"] == "vector"
    assert data["result"][0]["metric"]["__name__"] == "x"
    assert data["result"][0]["value"] == [1_700_000_060.0, "5"]


def test_query_scalar(api):
    _, client = api
    status, payload = client.get("/api/v1/query", query="1+2", time="1700000060")
    assert status == 200
    assert payload["data"] == {"resultType": "scalar", "result": [1_700_000_060.0, "3"]}


def test_query_string(api):
    _, client = api
    _, payload = client.get("/api/v1/query", query='"hello"', time="0")
    assert payload["data"]["resultType"] == "string"
    assert payload["data"]["result"][1] == "hello"


def test_query_matrix_expression(api):
    _, client = api
    status, payload = client.get("/api/v1/query", query="x[1m]", time="1700000060")
    assert status == 200
    assert payload["data"]["resultType"] == "matrix"
    # Left-open (0, 60s]: four of the five 15s-spaced samples qualify.
    assert len(payload["data"]["result"][0]["values"]) == 4


def test_query_bad_expression(api):
    _, client = api
    status, payload = client.get("/api/v1/query", query="x{", time="0")
    assert status == 400
    assert payload["status"] == "error"
    assert payload["errorType"] == "bad_data"


def test_query_missing_parameter(api):
    _, client = api
    status, payload = client.get("/api/v1/query")
    assert status == 400
    assert "query" in payload["error"]


def test_query_invalid_time(api):
    _, client = api
    status, payload = client.get("/api/v1/query", query="x", time="not-a-time")
    assert status == 400


def test_query_unknown_path(api):
    _, client = api
    status, payload = client.get("/api/v1/nope")
    assert status == 404
    assert payload["errorType"] == "not_found"


def test_query_range(api):
    _, client = api
    status, payload = client.get(
        "/api/v1/query_range",
        query="x",
        start="1700000000",
        end="1700000060",
        step="15s",
    )
    assert status == 200
    values = payload["data"]["result"][0]["values"]
    assert len(values) == 5
    assert values[0] == [1_700_000_000.0, "1"]


def test_query_range_too_many_points(api):
    _, client = api
    status, payload = client.get(
        "/api/v1/query_range",
        query="x",
        start="0",
        end="100000",
        step="1s",
    )
    assert status == 400
    assert "maximum resolution" in payload["error"]


def test_query_range_bad_step(api):
    _, client = api
    status, payload = client.get(
        "/api/v1/query_range", query="x", start="0", end="10", step="0"
    )
    assert status == 400


def test_series(api):
    _, client = api
    status, payload = client.get(
        "/api/v1/series", **{"match[]": 'x{job="api"}', "start": "0", "end": "2000000000"}
    )
    assert status == 200
    assert payload["data"][0]["job"] == "api"


def test_series_requires_match(api):
    _, client = api
    status, payload = client.get("/api/v1/series")
    assert status == 400


def test_labels_and_values(api):
    _, client = api
    _, payload = client.get("/api/v1/labels")
    assert {"__name__", "job", "instance"} <= set(payload["data"])
    _, payload = client.get("/api/v1/label/job/values")
    assert payload["data"] == ["api"]
    _, payload = client.get("/api/v1/label/__name__/values")
    assert "x" in payload["data"]


def test_labels_filtered_by_match(api):
    _, client = api
    _, payload = client.get("/api/v1/labels", **{"match[]": 'x{job="none"}'})
    assert payload["data"] == []


def test_targets(api):
    app, client = api
    _, payload = client.get("/api/v1/targets")
    target = payload["data"]["activeTargets"][0]
    assert target["scrapePool"] == "demo"
    assert target["labels"]["job"] == "demo"
    assert target["health"] == "unknown"
    assert payload["data"]["scrapePools"] == ["demo"]


def test_targets_metadata(api):
    app, client = api
    target = app.scrape.targets()[0]
    target.metadata = {"x": MetricMetadata("x", MetricType.GAUGE, "help", "")}
    _, payload = client.get("/api/v1/metadata")
    assert payload["data"]["x"][0]["type"] == "gauge"
    _, payload = client.get("/api/v1/targets/metadata", metric="x")
    assert payload["data"][0]["metric"] == "x"


def test_rules_and_alerts(api):
    app, client = api
    app.rules.evaluate(1_700_000_060_000)
    status, payload = client.get("/api/v1/rules")
    assert status == 200
    rules = payload["data"]["groups"][0]["rules"]
    assert [rule["type"] for rule in rules] == ["recording", "alerting"]
    _, alerts = client.get("/api/v1/alerts")
    assert alerts["data"]["alerts"][0]["labels"]["alertname"] == "High"
    _, filtered = client.get("/api/v1/rules", type="alert")
    assert len(filtered["data"]["groups"][0]["rules"]) == 1


def test_alertmanagers(api, tmp_path):
    (tmp_path / "second").mkdir()
    app, client = make_app(tmp_path / "second", alerting=True)
    _, payload = client.get("/api/v1/alertmanagers")
    assert payload["data"]["activeAlertmanagers"][0]["url"] == "http://am:9093"
    app.shutdown()


def test_status_endpoints(api):
    _, client = api
    for path in (
        "/api/v1/status/buildinfo",
        "/api/v1/status/runtimeinfo",
        "/api/v1/status/config",
        "/api/v1/status/flags",
        "/api/v1/status/tsdb",
        "/api/v1/status/walreplay",
    ):
        status, payload = client.get(path)
        assert status == 200, path
        assert payload["status"] == "success"
    _, build = client.get("/api/v1/status/buildinfo")
    assert build["data"]["version"]
    _, config = client.get("/api/v1/status/config")
    assert "scrape_configs" in config["data"]["yaml"]


def test_health_and_ready(api):
    _, client = api
    status, body, headers = client.raw_get("/-/healthy")
    assert status == 200 and "Healthy" in body
    status, body, _ = client.raw_get("/-/ready")
    assert status == 200 and "Ready" in body


def test_graph_ui(api):
    _, client = api
    status, body, headers = client.raw_get("/graph")
    assert status == 200
    assert "Mini-Prometheus" in body
    assert headers.get("Content-Type", "").startswith("text/html")


def test_metrics_endpoint(api):
    app, client = api
    status, body, headers = client.raw_get("/metrics")
    assert status == 200
    assert "prometheus_tsdb_head_series" in body
    assert "prometheus_build_info" in body
    assert "prometheus_target_scrapes_total" in body


def test_federate(api):
    app, client = api
    app.tsdb.append(Labels({"__name__": "live", "job": "f"}), now_ms(), 7)
    status, body, headers = client.raw_get("/federate", **{"match[]": "live"})
    assert status == 200
    assert 'live{job="f"}' in body
    assert "7" in body


def test_cors_header(api):
    _, client = api
    _, _, headers = client.raw_get("/-/healthy")
    assert headers.get("Access-Control-Allow-Origin") == "*"


def test_admin_api_disabled(api):
    _, client = api
    status, payload = client.post("/api/v1/admin/tsdb/snapshot")
    assert status == 400
    assert "admin" in payload["error"]


def test_admin_snapshot_and_delete(tmp_path):
    (tmp_path / "admin").mkdir()
    app, client = make_app(tmp_path / "admin", admin=True)
    app.tsdb.append(Labels({"__name__": "x"}), 1000, 1)
    status, payload = client.post("/api/v1/admin/tsdb/snapshot")
    assert status == 200
    assert (Path(app.tsdb.options.path) / "snapshots" / payload["data"]["name"]).exists()
    status, payload = client.post(
        "/api/v1/admin/tsdb/delete_series", {"match[]": "x"}
    )
    assert status == 200
    assert app.tsdb.select([], 0, 2000) == []
    app.shutdown()


def test_remote_write_json(api):
    app, client = api
    body = json.dumps(
        {
            "timeseries": [
                {
                    "labels": {"__name__": "remote", "job": "rw"},
                    "samples": [[1_700_000_000_000, 42]],
                }
            ]
        }
    ).encode()
    status, _ = client.post(
        "/api/v1/write", body, headers={"Content-Type": "application/json"}, raw=True
    )
    assert status == 204
    result = app.tsdb.select([], 1_700_000_000_000, 1_700_000_000_000)
    assert any(labels.get_name() == "remote" for labels, _ in result)


def test_remote_write_protobuf(api):
    app, client = api
    series = [
        (Labels({"__name__": "proto", "job": "rw"}), [(1_700_000_000_000, 7.0)])
    ]
    body = snappy_compress(encode_write_request(series))
    status, _ = client.post(
        "/api/v1/write",
        body,
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "snappy",
        },
        raw=True,
    )
    assert status == 204
    result = app.tsdb.select([], 1_700_000_000_000, 1_700_000_000_000)
    assert any(labels.get_name() == "proto" for labels, _ in result)


def test_remote_write_bad_content_type(api):
    _, client = api
    status, payload = client.post(
        "/api/v1/write", b"x", headers={"Content-Type": "text/plain"}
    )
    assert status == 400


def test_remote_read(api):
    app, client = api
    body = json.dumps(
        {
            "queries": [
                {
                    "start_ms": 0,
                    "end_ms": 2_000_000_000_000,
                    "matchers": [{"name": "__name__", "op": "=", "value": "x"}],
                }
            ]
        }
    ).encode()
    status, payload = client.post(
        "/api/v1/read", body, headers={"Content-Type": "application/json"}
    )
    assert status == 200
    assert payload["data"]["series"][0]["labels"]["job"] == "api"


def test_reload(api, tmp_path):
    app, client = api
    status, _ = client.post("/-/reload", raw=True)
    assert status == 200
    assert app.last_reload_ok is True


def test_query_with_post_form(api):
    _, client = api
    status, payload = client.post(
        "/api/v1/query",
        {"query": "x", "time": "1700000060"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 200
    assert payload["data"]["result"][0]["value"][1] == "5"


def test_options_preflight(api):
    _, client = api
    request = urllib.request.Request(client.base + "/api/v1/query", method="OPTIONS")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 204
        assert "POST" in response.headers["Access-Control-Allow-Methods"]


def test_series_default_range_includes_future_samples(api):
    app, client = api
    app.tsdb.append(
        Labels({"__name__": "future", "job": "api"}), 4_102_444_800_000, 1.0
    )
    status, payload = client.get("/api/v1/series", **{"match[]": "future"})
    assert status == 200
    assert any(item.get("__name__") == "future" for item in payload["data"])


def test_query_range_accepts_11000_points(api):
    _, client = api
    # 0s .. 10999s at 1s step is exactly 11,000 evaluation points, the
    # documented cap; it must be accepted rather than rejected off by one.
    status, payload = client.get(
        "/api/v1/query_range",
        query="vector(1)",
        start="0",
        end="10999",
        step="1",
    )
    assert status == 200
    assert len(payload["data"]["result"][0]["values"]) == 11_000


def test_query_range_11001_points_are_accepted(api):
    _, client = api
    # Upstream rejects only when (end-start)/step > 11000, so 0..11000 at
    # step 1 (11,001 points) is still accepted; 11,002 is rejected.
    status, payload = client.get(
        "/api/v1/query_range",
        query="vector(1)",
        start="0",
        end="11000",
        step="1",
    )
    assert status == 200
    assert len(payload["data"]["result"][0]["values"]) == 11_001

    status, _ = client.get(
        "/api/v1/query_range",
        query="vector(1)",
        start="0",
        end="11001",
        step="1",
    )
    assert status == 400
