from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from mini_prometheus.app import AppOptions, PrometheusApp


class MetricsHandler(BaseHTTPRequestHandler):
    counter = 0

    def do_GET(self):
        MetricsHandler.counter += 1
        body = (
            "# HELP requests_total Requests\n"
            "# TYPE requests_total counter\n"
            f"requests_total{{code=\"200\"}} {MetricsHandler.counter * 10}\n"
            "# TYPE latency_seconds histogram\n"
            'latency_seconds_bucket{le="0.1"} 90\n'
            'latency_seconds_bucket{le="1"} 99\n'
            'latency_seconds_bucket{le="+Inf"} 100\n'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def wait_for(predicate, timeout=8.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception:
            pass
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode())


def test_full_stack_live_scrape(tmp_path):
    metrics = ThreadingHTTPServer(("127.0.0.1", 0), MetricsHandler)
    threading.Thread(target=metrics.serve_forever, daemon=True).start()
    port = metrics.server_address[1]
    rules = tmp_path / "rules.yml"
    rules.write_text(
        yaml.safe_dump(
            {
                "groups": [
                    {
                        "name": "live",
                        "interval": "1s",
                        "rules": [
                            {"record": "requests:sum", "expr": "sum(requests_total)"},
                            {"alert": "TargetUp", "expr": "up == 1", "for": "0s"},
                        ],
                    }
                ]
            }
        )
    )
    config = tmp_path / "prometheus.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "global": {
                    "scrape_interval": "1s",
                    "scrape_timeout": "1s",
                    "evaluation_interval": "1s",
                },
                "scrape_configs": [
                    {
                        "job_name": "live",
                        "static_configs": [{"targets": [f"127.0.0.1:{port}"]}],
                    }
                ],
                "rule_files": [str(rules)],
            }
        )
    )
    app = PrometheusApp(
        AppOptions(
            config_file=str(config),
            data_dir=str(tmp_path / "data"),
            listen_address="127.0.0.1:0",
            start_threads=True,
            log_requests=False,
        )
    )
    app.start()
    try:
        base = app.url()

        def up():
            data = get_json(base + "/api/v1/query?query=up")
            return data["data"]["result"]

        result = wait_for(up)
        assert result[0]["value"][1] == "1"

        def target_up():
            data = get_json(base + "/api/v1/targets")
            target = data["data"]["activeTargets"][0]
            return target if target["health"] == "up" else None

        wait_for(target_up)

        def rules_evaluated():
            data = get_json(base + "/api/v1/rules")
            for rule in data["data"]["groups"][0]["rules"]:
                if rule["name"] == "requests:sum" and rule["lastEvaluation"]:
                    return rule
            return None

        rule = wait_for(rules_evaluated)
        assert rule["health"] in ("ok", "unknown")

        def alert_firing():
            data = get_json(base + "/api/v1/alerts")
            return data["data"]["alerts"]

        alerts = wait_for(alert_firing)
        assert alerts[0]["labels"]["alertname"] == "TargetUp"
        assert alerts[0]["state"] == "firing"

        def recorded():
            data = get_json(
                base + "/api/v1/query?query=" + urllib.parse.quote("requests:sum")
            )
            return data["data"]["result"]

        assert wait_for(recorded)[0]["metric"]["__name__"] == "requests:sum"
        metrics.counter = 0
    finally:
        app.shutdown()
        metrics.shutdown()


def test_reload_updates_rules(tmp_path):
    rules_a = tmp_path / "a.yml"
    rules_b = tmp_path / "b.yml"
    rules_a.write_text(
        yaml.safe_dump({"groups": [{"name": "a", "rules": [{"record": "ra", "expr": "1"}]}]})
    )
    rules_b.write_text(
        yaml.safe_dump({"groups": [{"name": "b", "rules": [{"record": "rb", "expr": "2"}]}]})
    )
    config = tmp_path / "prometheus.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "scrape_configs": [
                    {"job_name": "j", "static_configs": [{"targets": ["127.0.0.1:1"]}]}
                ],
                "rule_files": [str(rules_a)],
            }
        )
    )
    app = PrometheusApp(
        AppOptions(
            config_file=str(config),
            data_dir=str(tmp_path / "data"),
            listen_address="127.0.0.1:0",
            start_threads=False,
            log_requests=False,
        )
    )
    app.start()
    try:
        assert app.rules.groups[0].name == "a"
        config.write_text(
            yaml.safe_dump(
                {
                    "scrape_configs": [
                        {"job_name": "j", "static_configs": [{"targets": ["127.0.0.1:1"]}]}
                    ],
                    "rule_files": [str(rules_b)],
                }
            )
        )
        request = urllib.request.Request(app.url() + "/-/reload", method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        assert app.rules.groups[0].name == "b"
    finally:
        app.shutdown()
