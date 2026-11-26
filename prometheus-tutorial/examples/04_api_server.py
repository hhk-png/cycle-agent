"""Start Mini-Prometheus, scrape a demo exporter, and call its HTTP API."""

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

import yaml

from demo_exporter import DemoExporter
from mini_prometheus.app import AppOptions, PrometheusApp


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode())


def run() -> None:
    exporter = DemoExporter()
    port = exporter.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "prometheus.yml"
            config.write_text(
                yaml.safe_dump(
                    {
                        "global": {
                            "scrape_interval": "15s",
                            "scrape_timeout": "5s",
                            "evaluation_interval": "15s",
                        },
                        "scrape_configs": [
                            {
                                "job_name": "demo",
                                "static_configs": [
                                    {"targets": [f"127.0.0.1:{port}"]}
                                ],
                            }
                        ],
                    }
                )
            )
            app = PrometheusApp(
                AppOptions(
                    config_file=str(config),
                    data_dir=str(Path(directory) / "data"),
                    listen_address="127.0.0.1:0",
                    start_threads=False,
                    log_requests=False,
                )
            )
            app.start()
            try:
                target = app.scrape.targets()[0]
                app.scrape.scrape_once(target, timestamp=1_700_000_000_000)
                app.scrape.scrape_once(target, timestamp=1_700_000_015_000)
                base = app.url()
                for query, time in (
                    ("up", "1700000015"),
                    ("sum(rate(demo_http_requests_total[1m]))", "1700000015"),
                    ("histogram_quantile(0.9, sum by (le) (rate(demo_request_duration_seconds_bucket[1m])))", "1700000015"),
                    ("demo_cpu_usage_ratio", "1700000015"),
                ):
                    url = f"{base}/api/v1/query?query={urllib.parse.quote(query)}&time={time}"
                    payload = get(url)
                    result = payload["data"]["result"]
                    rendered = [
                        (item["metric"].get("__name__", ""), item["value"][1])
                        for item in result
                    ]
                    print(f"  {query} -> {rendered}")
                targets = get(base + "/api/v1/targets")["data"]["activeTargets"]
                print(f"targets: {[(t['labels']['job'], t['health']) for t in targets]}")
                print(f"health: {urllib.request.urlopen(base + '/-/healthy').read().decode().strip()}")
            finally:
                app.shutdown()
    finally:
        exporter.stop()


if __name__ == "__main__":
    run()
