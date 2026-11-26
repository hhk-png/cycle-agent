"""Example 10: ingest OTLP/JSON and query the translated series.

The example starts Mini-Prometheus with the OTLP receiver enabled, posts a
small OTLP payload (resource attributes, a cumulative sum, a gauge and a
histogram), then queries the resulting Prometheus series.

Run with:

    python examples/10_otlp_ingest.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_bootstrap_root = str(_Path(__file__).resolve().parent.parent)
if _bootstrap_root not in _sys.path:
    _sys.path.insert(0, _bootstrap_root)

import json
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from mini_prometheus.app import AppOptions, PrometheusApp
from mini_prometheus.util.timeutil import now_ms


def otlp_payload(timestamp_ns: int) -> dict:
    point = {"timeUnixNano": str(timestamp_ns)}
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "checkout"}},
                        {
                            "key": "service.instance.id",
                            "value": {"stringValue": "checkout-0"},
                        },
                        {
                            "key": "deployment.environment",
                            "value": {"stringValue": "prod"},
                        },
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "checkout.http", "version": "1.4.0"},
                        "metrics": [
                            {
                                "name": "http.server.requests",
                                "unit": "1",
                                "sum": {
                                    "aggregationTemporality": 2,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            **point,
                                            "asInt": "120",
                                            "attributes": [
                                                {
                                                    "key": "http.request.method",
                                                    "value": {"stringValue": "GET"},
                                                }
                                            ],
                                        }
                                    ],
                                },
                            },
                            {
                                "name": "queue.depth",
                                "unit": "1",
                                "gauge": {
                                    "dataPoints": [{**point, "asDouble": 7.0}]
                                },
                            },
                            {
                                "name": "http.server.duration",
                                "unit": "s",
                                "histogram": {
                                    "aggregationTemporality": 2,
                                    "dataPoints": [
                                        {
                                            **point,
                                            "count": "10",
                                            "sum": 2.5,
                                            "explicitBounds": [0.1, 0.5, 1.0],
                                            "bucketCounts": ["4", "4", "1", "1"],
                                        }
                                    ],
                                },
                            },
                        ],
                    }
                ],
            }
        ]
    }


def post_json(url: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200, response.status


def query(base: str, expression: str, timestamp: float) -> dict:
    params = urllib.parse.urlencode({"query": expression, "time": timestamp})
    with urllib.request.urlopen(f"{base}/api/v1/query?{params}", timeout=10) as response:
        return json.loads(response.read().decode())


def run() -> None:
    timestamp_ms = now_ms()
    timestamp_ns = timestamp_ms * 1_000_000
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / "prometheus.yml"
        config.write_text(
            "global:\n"
            "  scrape_interval: 15s\n"
            "  evaluation_interval: 15s\n"
            "scrape_configs: []\n",
            encoding="utf-8",
        )
        app = PrometheusApp(
            AppOptions(
                config_file=str(config),
                data_dir=str(root / "data"),
                listen_address="127.0.0.1:0",
                start_threads=False,
                enable_otlp_receiver=True,
                log_requests=False,
            )
        )
        app.start()
        try:
            base = app.url()
            post_json(
                f"{base}/api/v1/otlp/v1/metrics",
                otlp_payload(timestamp_ns),
            )

            requests = query(
                base, '{"http.server.requests_total"}', timestamp_ms / 1000
            )["data"]["result"]
            print("OTLP counter ->", requests[0]["metric"], requests[0]["value"])

            depth = query(base, '{"queue.depth"}', timestamp_ms / 1000)["data"][
                "result"
            ]
            print("OTLP gauge   ->", depth[0]["metric"], depth[0]["value"])

            # Single OTLP snapshot: cumulative buckets are used directly. In
            # production, query sum by (le) (rate(..._bucket[5m])) instead.
            quantile = query(
                base,
                'histogram_quantile(0.5, {"http.server.duration_seconds_bucket"})',
                timestamp_ms / 1000,
            )["data"]["result"]
            print("OTLP p50 (s) ->", quantile[0]["metric"], quantile[0]["value"])

            series = query(
                base,
                'count by (job, "deployment.environment") '
                '({"http.server.requests_total"})',
                timestamp_ms / 1000,
            )["data"]["result"]
            print("service labels ->", series[0]["metric"], series[0]["value"])

            with urllib.request.urlopen(f"{base}/metrics", timeout=10) as response:
                for line in response.read().decode().splitlines():
                    if line.startswith("prometheus_otlp_received"):
                        print(line)
        finally:
            app.shutdown()


if __name__ == "__main__":
    run()
