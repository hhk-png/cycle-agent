"""Prometheus-compatible HTTP API.

The JSON envelope, path layout and timestamp formats follow Prometheus 2.x/3.x
so Grafana, `promtool`-style clients and curl examples from the docs work with
only a URL change.  Features that need a real deployment (TSDB snapshots,
admin deletes) are gated behind ``--web.enable-admin-api`` just like upstream.
"""

from __future__ import annotations

import json
import math
import os
import platform
import sys
import threading
import time
import urllib.parse
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import yaml

from .. import __version__
from ..labels import Labels, Matcher
from ..model import ErrorType, PrometheusError, format_value
from ..promql.value import Matrix, Scalar, StringValue, Vector
from ..util.timeutil import (
    format_timestamp,
    now_ms,
    parse_duration,
    parse_timestamp,
)
from ..exposition import format_exposition, ParsedSample

# Prometheus uses storage.MinTime/MaxTime defaults for series/labels/values,
# so legal samples outside the wall-clock range (remote write, honor_timestamps,
# pre-1970 data) are still discoverable.
MIN_TIME = -(2**63)
MAX_TIME = 2**63 - 1

STATUS_FOR_ERROR = {
    ErrorType.BAD_DATA: 400,
    ErrorType.EXECUTION: 422,
    ErrorType.NOT_FOUND: 404,
    ErrorType.UNAVAILABLE: 503,
    ErrorType.TIMEOUT: 503,
    ErrorType.CANCELED: 499,
    ErrorType.INTERNAL: 500,
}

MAX_QUERY_RANGE_POINTS = 11000
MAX_BODY_BYTES = 64 * 1024 * 1024


def gunzip_limited(data: bytes, limit: int) -> bytes:
    """Decompress gzip data without allowing a bomb past ``limit``.

    ``gzip.decompress`` materializes the whole output before the caller can
    check its size.  Bounding each ``decompress`` call by the remaining budget
    keeps peak memory at the limit even for highly compressible payloads.
    """
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    remaining = data
    while remaining:
        chunk = decompressor.decompress(remaining, limit + 1 - len(out))
        out.extend(chunk)
        if len(out) > limit:
            raise ValueError("decompressed body is too large")
        remaining = decompressor.unconsumed_tail
        if not remaining:
            break
    out.extend(decompressor.flush(limit + 1 - len(out)))
    if len(out) > limit:
        raise ValueError("decompressed body is too large")
    if not decompressor.eof:
        raise ValueError("truncated gzip body")
    return bytes(out)


class PrometheusServer:
    def __init__(self, app, *, host: str = "127.0.0.1", port: int = 9090) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"

    @property
    def server_address(self) -> tuple[str, int]:
        if self._httpd is None:
            return self.host, self.port
        return self._httpd.server_address[0], self._httpd.server_address[1]

    def start(self) -> tuple[str, int]:
        handler = _make_handler(self.app)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mini-prometheus-http", daemon=True
        )
        self._thread.start()
        return self.server_address

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def wait(self) -> None:
        if self._thread:
            self._thread.join()


def _make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"MiniPrometheus/{__version__}"
        protocol_version = "HTTP/1.1"
        # socketserver applies this to each connection; it prevents a client
        # that never finishes its body from pinning a handler thread forever.
        timeout = 30

        def log_message(self, format, *args):  # noqa: A003
            if app.options.log_requests:
                sys.stderr.write(
                    "%s - - [%s] %s\n"
                    % (self.address_string(), self.log_date_time_string(), format % args)
                )

        # ---- primitives ------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _json(self, data: Any, status: int = 200, warnings: list[str] | None = None) -> None:
            payload = {"status": "success", "data": data}
            if warnings:
                payload["warnings"] = warnings
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send(status, body, "application/json")

        def _error(self, message: str, error_type: str = "internal", status: int = 500) -> None:
            body = json.dumps(
                {"status": "error", "errorType": error_type, "error": message},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(status, body, "application/json")

        def _read_body(self) -> bytes:
            if getattr(self, "_raw_body", None) is not None:
                return self._raw_body
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError as exc:
                raise PrometheusError(
                    "invalid Content-Length", error_type=ErrorType.BAD_DATA
                ) from exc
            if length < 0:
                raise PrometheusError(
                    "negative Content-Length", error_type=ErrorType.BAD_DATA
                )
            if length > MAX_BODY_BYTES:
                raise PrometheusError("request body too large", error_type=ErrorType.BAD_DATA)
            self._raw_body = self.rfile.read(length) if length else b""
            return self._raw_body

        def _params(self) -> dict[str, list[str]]:
            parsed = urllib.parse.urlparse(self.path)
            params: dict[str, list[str]] = {}
            if self.command in ("POST", "PUT"):
                body = self._read_body()
                content_type = self.headers.get("Content-Type", "")
                if body and (
                    "application/x-www-form-urlencoded" in content_type
                    or not content_type
                ):
                    params = urllib.parse.parse_qs(
                        body.decode("utf-8"), keep_blank_values=True
                    )
            # Go's Request.Form gives POST body values precedence over the
            # query string; _single() then picks the first value.
            for key, values in urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True
            ).items():
                params.setdefault(key, []).extend(values)
            return params

        def _path(self) -> str:
            return urllib.parse.urlparse(self.path).path

        def _match_selectors(self, params: dict[str, list[str]]) -> list[list[Matcher]]:
            raw = params.get("match[]", [])
            if not raw:
                raise PrometheusError("no match[] parameter provided", error_type=ErrorType.BAD_DATA)
            selectors = []
            for text in raw:
                from ..promql.parser import parse_expr

                expression = parse_expr(text)
                if expression.value_type != "vector":
                    raise PrometheusError(
                        f"match[] must be an instant vector selector, got {expression.value_type}",
                        error_type=ErrorType.BAD_DATA,
                    )
                from ..promql import ast

                if not isinstance(expression, ast.VectorSelector):
                    raise PrometheusError(
                        "match[] must be a plain vector selector",
                        error_type=ErrorType.BAD_DATA,
                    )
                selectors.append(expression.matchers)
            return selectors

        def _time(self, params: dict[str, list[str]], name: str, default: int | None = None) -> int:
            values = params.get(name)
            if not values or values[0] == "":
                if default is None:
                    raise PrometheusError(f"missing required parameter {name!r}", error_type=ErrorType.BAD_DATA)
                return default
            try:
                return parse_timestamp(values[0])
            except ValueError as exc:
                raise PrometheusError(f"invalid {name}: {exc}", error_type=ErrorType.BAD_DATA) from exc

        def _optional_time(self, params: dict[str, list[str]], name: str) -> int | None:
            values = params.get(name)
            if not values or values[0] == "":
                return None
            try:
                return parse_timestamp(values[0])
            except ValueError as exc:
                raise PrometheusError(f"invalid {name}: {exc}", error_type=ErrorType.BAD_DATA) from exc

        def _step(self, params: dict[str, list[str]]) -> int:
            values = params.get("step")
            if not values or not values[0]:
                raise PrometheusError("missing required parameter 'step'", error_type=ErrorType.BAD_DATA)
            text = values[0]
            try:
                return parse_duration(text)
            except ValueError:
                try:
                    seconds = float(text)
                except ValueError as exc:
                    raise PrometheusError(f"invalid step {text!r}", error_type=ErrorType.BAD_DATA) from exc
                if seconds <= 0:
                    raise PrometheusError("step must be positive", error_type=ErrorType.BAD_DATA)
                return int(round(seconds * 1000))

        def _single(self, params: dict[str, list[str]], name: str) -> str:
            values = params.get(name)
            if not values:
                raise PrometheusError(f"missing required parameter {name!r}", error_type=ErrorType.BAD_DATA)
            return values[0]

        # ---- dispatch --------------------------------------------------
        def do_OPTIONS(self):  # noqa: N802
            self._send(
                204,
                b"",
                "text/plain",
                {
                    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                },
            )

        def do_GET(self):  # noqa: N802
            self._handle()

        def do_HEAD(self):  # noqa: N802
            self._handle()

        def do_POST(self):  # noqa: N802
            self._handle()

        def do_PUT(self):  # noqa: N802
            self._handle()

        def _handle(self) -> None:
            try:
                params = self._params()
                self._dispatch(self._path(), params)
            except PrometheusError as exc:
                self._error(
                    str(exc),
                    exc.error_type.value,
                    STATUS_FOR_ERROR.get(exc.error_type, 500),
                )
            except ValueError as exc:
                self._error(str(exc), ErrorType.BAD_DATA.value, 400)
            except Exception as exc:  # pragma: no cover - defensive
                self._error(f"internal error: {exc}", ErrorType.INTERNAL.value, 500)

        def _dispatch(self, path: str, params: dict[str, list[str]]) -> None:
            app = self.application
            if path.startswith("/api/v1/label/") and path.endswith("/values"):
                name = urllib.parse.unquote(path[len("/api/v1/label/") : -len("/values")])
                return self._label_values(name, params)
            routes = {
                "/api/v1/query": self._query,
                "/api/v1/query_range": self._query_range,
                "/api/v1/series": self._series,
                "/api/v1/labels": self._labels,
                "/api/v1/metadata": self._metadata,
                "/api/v1/targets": self._targets,
                "/api/v1/targets/metadata": self._targets_metadata,
                "/api/v1/rules": self._rules,
                "/api/v1/alerts": self._alerts,
                "/api/v1/alertmanagers": self._alertmanagers,
                "/api/v1/query_exemplars": self._query_exemplars,
                "/api/v1/status/buildinfo": self._buildinfo,
                "/api/v1/status/runtimeinfo": self._runtimeinfo,
                "/api/v1/status/config": self._config,
                "/api/v1/status/flags": self._flags,
                "/api/v1/status/tsdb": self._tsdb_status,
                "/api/v1/status/walreplay": self._walreplay,
                "/api/v1/admin/tsdb/snapshot": self._snapshot,
                "/api/v1/admin/tsdb/delete_series": self._delete_series,
                "/api/v1/admin/tsdb/clean_tombstones": self._clean_tombstones,
                "/api/v1/write": self._remote_write,
                "/api/v1/read": self._remote_read,
                "/api/v1/otlp/v1/metrics": self._otlp_metrics,
                "/federate": self._federate,
                "/metrics": self._metrics,
                "/-/healthy": self._healthy,
                "/-/ready": self._ready,
                "/-/reload": self._reload,
                "/-/quit": self._quit,
                "/graph": self._graph,
                "/": self._root,
            }
            handler = routes.get(path)
            if handler is None:
                raise PrometheusError(f"not found: {path}", error_type=ErrorType.NOT_FOUND)
            handler(params)

        # ---- query -----------------------------------------------------
        def _query(self, params):
            query = self._single(params, "query")
            _t = self._optional_time(params, "time")
            timestamp = now_ms() if _t is None else _t
            result = self.application.engine.query(query, timestamp)
            data = _value_to_json(result.value, timestamp)
            self._json(data, warnings=result.warnings)

        def _query_range(self, params):
            query = self._single(params, "query")
            start = self._time(params, "start")
            end = self._time(params, "end")
            step = self._step(params)
            if step <= 0:
                raise PrometheusError("zero or negative query resolution step widths are not accepted", error_type=ErrorType.BAD_DATA)
            if end < start:
                raise PrometheusError("end timestamp must not be before start time", error_type=ErrorType.BAD_DATA)
            # Upstream checks ``(end-start)/step > 11000``, so 11,001 points
            # (0..11000 at step 1) are accepted and 11,002 are rejected.
            if (end - start) // step > MAX_QUERY_RANGE_POINTS:
                raise PrometheusError(
                    f"exceeded maximum resolution of {MAX_QUERY_RANGE_POINTS} points per timeseries",
                    error_type=ErrorType.BAD_DATA,
                )
            result = self.application.engine.query_range(query, start, end, step)
            data = _value_to_json(result.value, end)
            self._json(data, warnings=result.warnings)

        def _series(self, params):
            selectors = self._match_selectors(params)
            _s = self._optional_time(params, "start")
            _e = self._optional_time(params, "end")
            start = MIN_TIME if _s is None else _s
            end = MAX_TIME if _e is None else _e
            seen: dict[Labels, None] = {}
            for matchers in selectors:
                for labels in self.application.tsdb.series(matchers, start, end):
                    seen[labels] = None
            data = [labels.to_dict() for labels in sorted(seen)]
            self._json(data)

        def _labels(self, params):
            raw = params.get("match[]", [])
            _s = self._optional_time(params, "start")
            _e = self._optional_time(params, "end")
            start = MIN_TIME if _s is None else _s
            end = MAX_TIME if _e is None else _e
            if raw:
                names: set[str] = set()
                for matchers in self._match_selectors(params):
                    for labels in self.application.tsdb.series(matchers, start, end):
                        names.update(labels.keys())
            else:
                names = set(self.application.tsdb.label_names(start, end))
            self._json(sorted(names))

        def _label_values(self, name, params):
            raw = params.get("match[]", [])
            _s = self._optional_time(params, "start")
            _e = self._optional_time(params, "end")
            start = MIN_TIME if _s is None else _s
            end = MAX_TIME if _e is None else _e
            values: set[str] = set()
            if raw:
                for matchers in self._match_selectors(params):
                    for labels in self.application.tsdb.series(matchers, start, end):
                        value = labels.get(name)
                        if value:
                            values.add(value)
            else:
                values.update(
                    self.application.tsdb.label_values(name, start, end)
                )
            self._json(sorted(values))

        def _metadata(self, params):
            metric = params.get("metric", [None])[0]
            limit = int(params.get("limit", ["0"])[0] or 0)
            out: dict[str, list[dict]] = {}
            for target in self.application.scrape.targets():
                for name, meta in target.metadata.items():
                    if metric and name != metric:
                        continue
                    out.setdefault(name, [])
                    entry = {"type": meta.type.value, "help": meta.help, "unit": meta.unit}
                    if entry not in out[name]:
                        out[name].append(entry)
            if limit:
                out = dict(list(out.items())[:limit])
            self._json(out)

        # ---- targets ---------------------------------------------------
        def _targets(self, params):
            state = params.get("state", ["any"])[0]
            active = [target.to_dict() for target in self.application.scrape.targets()]
            data = {
                "activeTargets": active if state in ("any", "active") else [],
                "droppedTargets": [],
                "scrapePools": sorted({target.job_name for target in self.application.scrape.targets()}),
            }
            self._json(data)

        def _targets_metadata(self, params):
            metric = params.get("metric", [None])[0]
            match_target = params.get("match_target", [None])[0]
            limit = int(params.get("limit", ["0"])[0] or 0)
            target_filter = None
            if match_target:
                from ..promql.parser import parse_expr
                from ..promql import ast

                expression = parse_expr(match_target)
                if not isinstance(expression, ast.VectorSelector):
                    raise PrometheusError("match_target must be a vector selector", error_type=ErrorType.BAD_DATA)
                target_filter = expression.matchers
            out = []
            for target in self.application.scrape.targets():
                if target_filter and not all(
                    matcher.matches_labels(target.target_labels()) for matcher in target_filter
                ):
                    continue
                for name, meta in target.metadata.items():
                    if metric and name != metric:
                        continue
                    out.append(
                        {
                            "target": target.target_labels().to_dict(),
                            "metric": name,
                            "type": meta.type.value,
                            "help": meta.help,
                            "unit": meta.unit,
                        }
                    )
            if limit:
                out = out[:limit]
            self._json(out)

        # ---- rules/alerts ---------------------------------------------
        def _rules(self, params):
            rule_type = params.get("type", [None])[0]
            data = self.application.rules.to_api()
            if rule_type in ("alert", "alerting"):
                data["groups"] = [
                    {**group, "rules": [r for r in group["rules"] if r["type"] == "alerting"]}
                    for group in data["groups"]
                ]
            elif rule_type in ("record", "recording"):
                data["groups"] = [
                    {**group, "rules": [r for r in group["rules"] if r["type"] == "recording"]}
                    for group in data["groups"]
                ]
            self._json(data)

        def _alerts(self, params):
            alerts = [alert.to_api() for alert in self.application.rules.alerts()]
            self._json({"alerts": alerts})

        def _alertmanagers(self, params):
            active = []
            for config in self.application.config.alerting:
                for target in config.targets:
                    active.append({"url": f"{config.scheme}://{target}"})
            self._json({"activeAlertmanagers": active, "droppedAlertmanagers": []})

        def _query_exemplars(self, params):
            query = self._single(params, "query")
            _s = self._optional_time(params, "start")
            _e = self._optional_time(params, "end")
            start = 0 if _s is None else _s
            end = now_ms() if _e is None else _e
            from ..promql.parser import parse_expr
            from ..promql import ast

            expression = parse_expr(query)
            if not isinstance(expression, ast.VectorSelector):
                raise PrometheusError("query_exemplars requires a vector selector", error_type=ErrorType.BAD_DATA)
            data = []
            for target in self.application.scrape.targets():
                for labels, exemplar in target.exemplars:
                    if start <= exemplar.timestamp <= end and all(
                        matcher.matches_labels(labels) for matcher in expression.matchers
                    ):
                        data.append(
                            {
                                "seriesLabels": labels.to_dict(),
                                "exemplars": [exemplar.to_api()],
                            }
                        )
            self._json(data)

        # ---- status ----------------------------------------------------
        def _buildinfo(self, params):
            self._json(
                {
                    "version": __version__,
                    "revision": "mini",
                    "branch": "main",
                    "buildUser": "mini-prometheus",
                    "buildDate": "2026-11-01T00:00:00Z",
                    "goVersion": f"python{platform.python_version()}",
                }
            )

        def _runtimeinfo(self, params):
            self._json(
                {
                    "startTime": format_timestamp(self.application.start_time),
                    "CWD": os.getcwd(),
                    "GOMAXPROCS": os.cpu_count() or 1,
                    "GOOS": platform.system().lower(),
                    "GOARCH": platform.machine(),
                    "version": __version__,
                    "revision": "mini",
                    "storageRetention": self.application.retention,
                }
            )

        def _config(self, params):
            text = yaml.safe_dump(
                self.application.config.to_dict(), sort_keys=False, allow_unicode=True
            )
            self._json({"yaml": text})

        def _flags(self, params):
            self._json(
                {
                    "config.file": str(self.application.config_path),
                    "storage.tsdb.path": str(self.application.options.data_dir),
                    "storage.tsdb.retention.time": self.application.retention,
                    "web.listen-address": self.application.options.listen_address,
                    "web.enable-admin-api": str(self.application.options.enable_admin_api).lower(),
                }
            )

        def _tsdb_status(self, params):
            head = self.application.tsdb.head_stats()
            blocks = [block.to_json() for block in self.application.tsdb.blocks()]
            self._json(
                {
                    "headStats": {
                        "numSeries": head["numSeries"],
                        "numLabelPairs": sum(
                            len(labels) for labels in self.application.tsdb.head.all_label_sets()
                        ),
                        "chunkCount": head["numChunks"],
                        "minTime": head["minTime"],
                        "maxTime": head["maxTime"],
                        "numSamples": head["numSamples"],
                    },
                    "seriesCountByMetricName": _counts_by(
                        self.application.tsdb, "series", "__name__"
                    ),
                    "labelValueCountByLabelName": [
                        {"name": name, "value": len(self.application.tsdb.label_values(name))}
                        for name in self.application.tsdb.label_names()
                    ],
                    "memoryInBytesByLabelName": [],
                    "seriesCountByLabelValuePair": _counts_by(
                        self.application.tsdb, "pairs", None
                    ),
                    "blocks": blocks,
                }
            )

        def _walreplay(self, params):
            self._json({"state": "done", "read": 0, "current": 0, "total": 0})

        def _snapshot(self, params):
            self._require_admin()
            path = self.application.tsdb.snapshot()
            self._json({"name": os.path.basename(path)})

        def _delete_series(self, params):
            self._require_admin()
            selectors = self._match_selectors(params)
            _s = self._optional_time(params, "start")
            _e = self._optional_time(params, "end")
            # Default to Prometheus' MinTime so pre-1970 data is deleted too.
            start = -(2**63) if _s is None else _s
            end = 2**63 - 1 if _e is None else _e + 1  # API end is inclusive
            result = {}
            for matchers in selectors:
                counts = self.application.tsdb.delete_series(matchers, start, end)
                result.update(counts)
            self._json(result)

        def _clean_tombstones(self, params):
            self._require_admin()
            self.application.tsdb.clean_tombstones()
            self._send(200, b"", "text/plain")

        def _require_admin(self):
            if not self.application.options.enable_admin_api:
                raise PrometheusError(
                    "admin APIs are disabled; start with --web.enable-admin-api",
                    error_type=ErrorType.BAD_DATA,
                )

        # ---- ingestion -------------------------------------------------
        def _otlp_metrics(self, params):
            if self.command != "POST":
                raise PrometheusError(
                    "OTLP receiver requires POST", error_type=ErrorType.BAD_DATA
                )
            receiver = getattr(self.application, "otlp", None)
            if receiver is None:
                raise PrometheusError(
                    "OTLP receiver is disabled; start with --web.enable-otlp-receiver",
                    error_type=ErrorType.NOT_FOUND,
                )
            content_type = (
                self.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if "protobuf" in content_type:
                raise PrometheusError(
                    "Mini-Prometheus does not decode OTLP/protobuf; "
                    "send OTLP/JSON (Content-Type: application/json)",
                    error_type=ErrorType.BAD_DATA,
                )
            body = self._read_body()
            if content_type not in ("application/json", "text/json"):
                raise PrometheusError(
                    f"unsupported OTLP content type {content_type!r}; use application/json",
                    error_type=ErrorType.BAD_DATA,
                )
            encoding = self.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                try:
                    body = gunzip_limited(body, MAX_BODY_BYTES)
                except (OSError, ValueError, zlib.error) as exc:
                    raise PrometheusError(
                        f"invalid gzip body: {exc}", error_type=ErrorType.BAD_DATA
                    ) from exc
            elif encoding not in ("", "identity"):
                raise PrometheusError(
                    f"unsupported Content-Encoding {encoding!r}",
                    error_type=ErrorType.BAD_DATA,
                )
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PrometheusError(
                    f"invalid OTLP JSON: {exc}", error_type=ErrorType.BAD_DATA
                ) from exc
            receiver.stats["requests"] = receiver.stats.get("requests", 0) + 1
            result = receiver.translate(payload)
            appended, errors = self.application.tsdb.append_batch(result.samples)
            if errors:
                receiver.stats["appendErrors"] = (
                    receiver.stats.get("appendErrors", 0) + len(errors)
                )
            # OTLP/HTTP uses a bare 200 with an empty body on success.  The
            # response is not the Prometheus JSON envelope.
            self._send(200, b"", "application/json")

        def _remote_write(self, params):
            if self.command != "POST":
                raise PrometheusError("remote write requires POST", error_type=ErrorType.BAD_DATA)
            body = self._read_body()
            content_type = self.headers.get("Content-Type", "")
            if "application/json" in content_type:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PrometheusError(f"invalid remote write JSON: {exc}", error_type=ErrorType.BAD_DATA) from exc
                samples = []
                for series in payload.get("timeseries", []):
                    labels = Labels(series.get("labels", {}))
                    if not labels.get("__name__"):
                        continue
                    for sample in series.get("samples", []):
                        timestamp, value = sample
                        samples.append((labels, int(timestamp), float(value)))
                self.application.tsdb.append_batch(samples)
                self._send(204, b"", "text/plain")
                return
            if "application/x-protobuf" in content_type:
                from ..remote import decode_write_request

                try:
                    samples = decode_write_request(body, content_encoding=self.headers.get("Content-Encoding", ""))
                except Exception as exc:
                    raise PrometheusError(f"invalid protobuf write request: {exc}", error_type=ErrorType.BAD_DATA) from exc
                self.application.tsdb.append_batch(samples)
                self._send(204, b"", "text/plain")
                return
            raise PrometheusError(
                "unsupported remote write content type; use application/json or application/x-protobuf",
                error_type=ErrorType.BAD_DATA,
            )

        def _remote_read(self, params):
            if self.command != "POST":
                raise PrometheusError("remote read requires POST", error_type=ErrorType.BAD_DATA)
            body = self._read_body()
            content_type = self.headers.get("Content-Type", "")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PrometheusError(f"invalid remote read JSON: {exc}", error_type=ErrorType.BAD_DATA) from exc
            series_out = []
            for query in payload.get("queries", []):
                matchers = [
                    Matcher(item["name"], item.get("op", "="), item.get("value", ""))
                    for item in query.get("matchers", [])
                ]
                start = int(query.get("start_ms", 0))
                end = int(query.get("end_ms", now_ms()))
                for labels, samples in self.application.tsdb.select(matchers, start, end):
                    series_out.append(
                        {
                            "labels": labels.to_dict(),
                            "samples": [[sample.timestamp, sample.value] for sample in samples],
                        }
                    )
            self._json({"series": series_out})

        # ---- federation and self metrics -------------------------------
        def _federate(self, params):
            selectors = self._match_selectors(params)
            now = now_ms()
            seen: set[Labels] = set()
            samples = []
            for matchers in selectors:
                for labels, points in self.application.tsdb.select(matchers, now - 5 * 60 * 1000, now):
                    if labels in seen or not points:
                        continue
                    seen.add(labels)
                    latest = points[-1]
                    samples.append(ParsedSample(labels, latest.value, latest.timestamp))
            body = format_exposition(samples).encode("utf-8")
            self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")

        def _metrics(self, params):
            tsdb_stats = self.application.tsdb.stats
            head = self.application.tsdb.head_stats()
            lines = [
                "# HELP prometheus_build_info Build information.",
                "# TYPE prometheus_build_info gauge",
                f'prometheus_build_info{{version="{__version__}",revision="mini",branch="main"}} 1',
                "# HELP prometheus_tsdb_head_series Total number of series in the head block.",
                "# TYPE prometheus_tsdb_head_series gauge",
                f"prometheus_tsdb_head_series {head['numSeries']}",
                "# HELP prometheus_tsdb_head_samples_appended_total Total appended samples.",
                "# TYPE prometheus_tsdb_head_samples_appended_total counter",
                f"prometheus_tsdb_head_samples_appended_total {tsdb_stats['samplesAppended']}",
                "# HELP prometheus_tsdb_blocks_loaded Number of loaded blocks.",
                "# TYPE prometheus_tsdb_blocks_loaded gauge",
                f"prometheus_tsdb_blocks_loaded {len(self.application.tsdb.blocks())}",
                "# HELP prometheus_tsdb_storage_blocks_bytes Bytes used by blocks.",
                "# TYPE prometheus_tsdb_storage_blocks_bytes gauge",
                f"prometheus_tsdb_storage_blocks_bytes {sum(b.size_bytes() for b in self.application.tsdb.blocks())}",
                "# HELP prometheus_config_last_reload_successful Whether the last reload succeeded.",
                "# TYPE prometheus_config_last_reload_successful gauge",
                f"prometheus_config_last_reload_successful {1 if self.application.last_reload_ok else 0}",
                "# HELP prometheus_rule_evaluations_total Total rule evaluations.",
                "# TYPE prometheus_rule_evaluations_total counter",
                f"prometheus_rule_evaluations_total {self.application.rules.stats['evaluations']}",
                "# HELP prometheus_rule_evaluation_failures_total Total rule evaluation failures.",
                "# TYPE prometheus_rule_evaluation_failures_total counter",
                f"prometheus_rule_evaluation_failures_total {self.application.rules.stats['failures']}",
            ]
            otlp = getattr(self.application, "otlp", None)
            if otlp is not None:
                stats = otlp.stats
                lines.extend(
                    [
                        "# HELP prometheus_otlp_received_samples_total OTLP samples accepted.",
                        "# TYPE prometheus_otlp_received_samples_total counter",
                        f"prometheus_otlp_received_samples_total {stats['samples']}",
                        "# HELP prometheus_otlp_received_metric_points_total OTLP data points seen.",
                        "# TYPE prometheus_otlp_received_metric_points_total counter",
                        f"prometheus_otlp_received_metric_points_total {stats['metricPoints']}",
                        "# HELP prometheus_otlp_delta_resets_total Delta conversion resets.",
                        "# TYPE prometheus_otlp_delta_resets_total counter",
                        f"prometheus_otlp_delta_resets_total {stats['deltaResets']}",
                        "# HELP prometheus_otlp_append_errors_total OTLP samples rejected by TSDB append.",
                        "# TYPE prometheus_otlp_append_errors_total counter",
                        f"prometheus_otlp_append_errors_total {stats.get('appendErrors', 0)}",
                    ]
                )
            body = ("\n".join(lines) + "\n" + self.application.scrape.metrics()).encode("utf-8")
            self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")

        # ---- misc ------------------------------------------------------
        def _healthy(self, params):
            self._send(200, b"Mini-Prometheus is Healthy.\n", "text/plain")

        def _ready(self, params):
            self._send(200, b"Mini-Prometheus is Ready.\n", "text/plain")

        def _reload(self, params):
            if not self.application.options.enable_lifecycle:
                raise PrometheusError(
                    "lifecycle API is disabled; start with --web.enable-lifecycle",
                    error_type=ErrorType.NOT_FOUND,
                )
            if self.command not in ("POST", "PUT"):
                raise PrometheusError("reload requires POST", error_type=ErrorType.BAD_DATA)
            try:
                self.application.reload()
            except Exception as exc:
                self.application.last_reload_ok = False
                self._send(500, f"reload failed: {exc}\n".encode("utf-8"), "text/plain")
                return
            self._send(200, b"", "text/plain")

        def _quit(self, params):
            if not self.application.options.enable_lifecycle:
                raise PrometheusError(
                    "lifecycle API is disabled; start with --web.enable-lifecycle",
                    error_type=ErrorType.NOT_FOUND,
                )
            self._send(200, b"", "text/plain")
            threading.Thread(target=self.application.shutdown, daemon=True).start()

        def _graph(self, params):
            body = GRAPH_HTML.encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")

        def _root(self, params):
            self._send(302, b"", "text/plain", {"Location": "/graph"})

    Handler.application = app
    return Handler


def _value_to_json(value, timestamp: int):
    if isinstance(value, Vector):
        items = sorted(value.items, key=lambda item: item.labels)
        return {
            "resultType": "vector",
            "result": [
                {
                    "metric": item.labels.to_dict(),
                    "value": [item.timestamp / 1000.0, format_value(item.value)],
                }
                for item in items
            ],
        }
    if isinstance(value, Matrix):
        series = sorted(value.series, key=lambda item: item.labels)
        return {
            "resultType": "matrix",
            "result": [
                {
                    "metric": item.labels.to_dict(),
                    "values": [[ts / 1000.0, format_value(v)] for ts, v in item.samples],
                }
                for item in series
            ],
        }
    if isinstance(value, Scalar):
        return {"resultType": "scalar", "result": [timestamp / 1000.0, format_value(value.value)]}
    if isinstance(value, StringValue):
        return {"resultType": "string", "result": [timestamp / 1000.0, value.value]}
    raise PrometheusError(f"unknown result type {value.type}")


def _counts_by(tsdb, mode: str, name: str | None):
    counts: dict[str, int] = {}
    if mode == "series":
        for labels in tsdb.head.all_label_sets():
            value = labels.get(name or "")
            counts[value] = counts.get(value, 0) + 1
    else:
        for labels in tsdb.head.all_label_sets():
            for label_name, value in labels.items():
                key = f"{label_name}={value}"
                counts[key] = counts.get(key, 0) + 1
    return [{"name": key, "value": value} for key, value in sorted(counts.items())]


GRAPH_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Mini-Prometheus</title>
<style>
 body { font-family: system-ui, sans-serif; margin: 2rem; color: #222; }
 textarea { width: 100%; height: 4rem; font-family: ui-monospace, monospace; }
 table { border-collapse: collapse; margin-top: 1rem; }
 td, th { border-bottom: 1px solid #ddd; padding: .3rem .6rem; text-align: left; }
 code { background: #f4f4f4; padding: .1rem .3rem; }
 .muted { color: #777; }
</style>
</head>
<body>
<h1>Mini-Prometheus</h1>
<p class="muted">A small Prometheus-compatible server. Try
<code>up</code>, <code>rate(prometheus_target_scrapes_total[1m])</code> or
<code>sum by (job) (up)</code>.</p>
<form id="f">
 <textarea id="q">up</textarea>
 <p><label>Evaluation time <input id="t" size="18" placeholder="RFC3339 or unix seconds"></label>
 <button type="submit">Execute</button></p>
</form>
<div id="out"></div>
<script>
const form = document.getElementById('f');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = document.getElementById('q').value;
  const time = document.getElementById('t').value;
  const params = new URLSearchParams({query});
  if (time) params.set('time', time);
  const response = await fetch('/api/v1/query', {method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: params});
  const payload = await response.json();
  const out = document.getElementById('out');
  if (payload.status !== 'success') { out.textContent = payload.error; return; }
  if (payload.data.resultType === 'scalar') {
    out.innerHTML = '<pre>' + JSON.stringify(payload.data.result) + '</pre>'; return;
  }
  const rows = payload.data.result.map(item => {
    const metric = Object.entries(item.metric).map(([k,v]) => k + '="' + v + '"').join(', ');
    return '<tr><td>' + metric + '</td><td>' + item.value[1] + '</td></tr>';
  }).join('');
  out.innerHTML = '<table><tr><th>Series</th><th>Value</th></tr>' + rows + '</table>';
});
</script>
</body>
</html>
"""
