"""A tiny metrics exporter used by the examples.

Serves Prometheus text format on /metrics.  Counters advance on every scrape,
which makes rate()/increase() demonstrations non-trivial without any external
dependencies or wall-clock assumptions.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        exporter: DemoExporter = self.server.exporter  # type: ignore[attr-defined]
        if self.path.startswith("/health"):
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/metrics"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if exporter.failing:
            body = b"# this is not a metric line\n"
            self.send_response(200)
        else:
            body = exporter.render().encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DemoExporter:
    def __init__(self, port: int = 0) -> None:
        self.port = port
        self.scrapes = 0
        self.failing = False
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self._server.exporter = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="demo-exporter", daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def render(self) -> str:
        self.scrapes += 1
        requests = self.scrapes * 25
        errors = self.scrapes
        buckets = [(0.05, 100), (0.1, 180), (0.5, 260), (1.0, 290), (float("inf"), 300)]
        lines = [
            "# HELP demo_http_requests_total Total HTTP requests.",
            "# TYPE demo_http_requests_total counter",
            f'demo_http_requests_total{{method="GET",code="200"}} {requests}',
            f'demo_http_requests_total{{method="GET",code="500"}} {errors}',
            "# HELP demo_request_duration_seconds Request latency.",
            "# TYPE demo_request_duration_seconds histogram",
        ]
        for upper, count in buckets:
            label = "+Inf" if upper == float("inf") else str(upper)
            # Counters must grow between scrapes for rate()/increase() to be
            # meaningful in the examples.
            lines.append(
                f'demo_request_duration_seconds_bucket{{le="{label}"}} '
                f"{count * self.scrapes}"
            )
        lines.extend(
            [
                f"demo_request_duration_seconds_sum {42.5 * self.scrapes}",
                f"demo_request_duration_seconds_count {300 * self.scrapes}",
                "# HELP demo_cpu_usage_ratio CPU usage ratio.",
                "# TYPE demo_cpu_usage_ratio gauge",
                f'demo_cpu_usage_ratio{{core="0"}} {0.4 + self.scrapes * 0.01:.2f}',
                f'demo_cpu_usage_ratio{{core="1"}} {0.2 + self.scrapes * 0.005:.2f}',
                "# HELP demo_build_info Build information.",
                "# TYPE demo_build_info gauge",
                'demo_build_info{version="1.0.0",revision="abc"} 1',
                "# HELP demo_queue_depth Queue depth.",
                "# TYPE demo_queue_depth gauge",
                f"demo_queue_depth {self.scrapes % 5}",
            ]
        )
        return "\n".join(lines) + "\n"


def _main() -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Run the tutorial demo exporter.")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    exporter = DemoExporter(args.port)
    port = exporter.start()
    print(f"demo exporter listening on http://127.0.0.1:{port}/metrics")
    print("press Ctrl+C to stop")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        exporter.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
