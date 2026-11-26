"""Send a Prometheus protobuf+Snappy remote-write request to Mini-Prometheus."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_bootstrap_root = str(_Path(__file__).resolve().parent.parent)
if _bootstrap_root not in _sys.path:
    _sys.path.insert(0, _bootstrap_root)



import json
import tempfile
import urllib.request
from pathlib import Path

import yaml

from mini_prometheus.app import AppOptions, PrometheusApp
from mini_prometheus.labels import Labels
from mini_prometheus.remote import encode_write_request, snappy_compress


def run() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "prometheus.yml"
        config.write_text(
            yaml.safe_dump(
                {
                    "global": {"scrape_interval": "15s", "scrape_timeout": "5s"},
                    "scrape_configs": [],
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
            now = 1_700_000_000_000
            series = [
                (
                    Labels({"__name__": "remote_jobs_total", "job": "batch"}),
                    [(now, 10.0), (now + 15_000, 20.0)],
                )
            ]
            body = snappy_compress(encode_write_request(series))
            request = urllib.request.Request(
                app.url() + "/api/v1/write",
                data=body,
                headers={
                    "Content-Type": "application/x-protobuf",
                    "Content-Encoding": "snappy",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                print(f"remote write status: {response.status}")
            query = "remote_jobs_total"
            url = f"{app.url()}/api/v1/query?query={query}&time=1700000000"
            with urllib.request.urlopen(url, timeout=10) as response:
                payload = json.loads(response.read().decode())
            print(f"query result: {payload['data']['result']}")
            read_body = json.dumps(
                {
                    "queries": [
                        {
                            "start_ms": 0,
                            "end_ms": now + 60_000,
                            "matchers": [
                                {"name": "__name__", "op": "=", "value": "remote_jobs_total"}
                            ],
                        }
                    ]
                }
            ).encode()
            request = urllib.request.Request(
                app.url() + "/api/v1/read",
                data=read_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode())
            print(f"remote read series: {len(payload['data']['series'])}, samples: {len(payload['data']['series'][0]['samples'])}")
        finally:
            app.shutdown()


if __name__ == "__main__":
    run()
