"""Dependency-free HTTP server and REST surface.

The server is intentionally thin: every request maps to one service call.
That keeps the API honest -- if a behavior is not available in the service
layer, it is not magically hidden in an HTTP handler.

Production differences are documented, not simulated: MiniMLflow uses a
single bearer token, buffers artifact uploads, and has no RBAC, rate limiting,
or OIDC integration.  Chapter 20 explains what must change before exposing
this beyond a trusted network.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import mimetypes
import re
import socket
import sqlite3
import ssl
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import MiniConfig
from .entities import Trace
from .errors import (
    InvalidParameter,
    MethodNotAllowed,
    MiniMlflowError,
    PayloadTooLarge,
    ResourceNotFound,
    Unauthorized,
)
from .gateway import Gateway
from .tracking import TrackingService
from .ui import (
    render_error,
    render_experiment,
    render_home,
    render_prompts,
    render_registry,
    render_run,
    render_trace,
    render_traces,
)
from .version import __version__

LOGGER = logging.getLogger("mini_mlflow.server")
_COOKIE_NAME = "mini_mlflow_token"


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def _json_response(payload: Any, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status=status, body=body)


def _html_response(html_text: str, status: int = 200) -> Response:
    return Response(
        status=status,
        body=html_text.encode("utf-8"),
        content_type="text/html; charset=utf-8",
    )


class MiniMlflowHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        config: MiniConfig,
        *,
        service: TrackingService | None = None,
        gateway: Gateway | None = None,
    ) -> None:
        self.config = config
        self.service = service or TrackingService(config)
        self.gateway = gateway
        super().__init__(server_address, MiniMlflowHandler)

    def server_close(self) -> None:
        self.service.webhooks.stop_dispatcher()
        super().server_close()
        self.service.close()


class MiniMlflowHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: MiniMlflowHTTPServer
    _pending_cookie: str | None = None
    _raw_path: str = ""

    # ------------------------------------------------------------------
    # HTTP entry points
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._apply_cors()
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        if path != "/health":
            try:
                self._authenticate("HEAD", path, query)
            except MiniMlflowError as exc:
                self._send(_json_response(exc.to_dict(), status=exc.http_status), head=True)
                return
        self._send(Response(status=200, body=b"", content_type="text/plain; charset=utf-8"), head=True)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        message = format % args
        # Never persist a UI bootstrap token from the query string.
        message = re.sub(r"(token=)[^&\s]+", r"\1REDACTED", message)
        LOGGER.info("%s - %s", self.address_string(), message)

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------
    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        self._raw_path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        self._pending_cookie = None
        self._body_read = False
        try:
            self._authenticate(method, path, query)
            body = self._read_json() if method in {"POST", "PATCH"} else {}
            response = self._route(method, path, query, body)
        except MiniMlflowError as exc:
            # Auth/size rejects happen before the body is consumed.  Closing
            # with unread bytes makes Windows send RST, which clients surface
            # as ConnectionAbortedError instead of the 401 body.  Drain the
            # bounded body first so the error response is readable.
            if method in {"POST", "PATCH"} and not self._body_read:
                self._drain_request_body()
            if path.startswith("/ui"):
                self._send(_html_response(render_error(exc.message), status=exc.http_status))
            else:
                self._send(_json_response(exc.to_dict(), status=exc.http_status))
            return
        except (BrokenPipeError, ConnectionResetError):
            return
        except sqlite3.InterfaceError as exc:
            # A JSON value with an impossible SQLite binding (e.g. a list where
            # an ID string is required) is bad input, not a server bug.
            payload = {"error_code": "INVALID_PARAMETER_VALUE", "message": f"invalid parameter type: {exc}"}
            self._send(_json_response(payload, status=400))
            return
        except Exception:  # noqa: BLE001
            LOGGER.exception("unhandled error while serving %s %s", method, path)
            payload = {"error_code": "INTERNAL_ERROR", "message": "internal server error"}
            self._send(_json_response(payload, status=500))
            return
        self._send(response)

    def _read_json(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise InvalidParameter("Content-Length is required for POST/PATCH requests")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise InvalidParameter("invalid Content-Length") from exc
        if length < 0:
            raise InvalidParameter("invalid Content-Length")
        if length > self.server.config.max_request_bytes:
            raise PayloadTooLarge(
                f"request body exceeds {self.server.config.max_request_bytes} bytes"
            )
        raw = self.rfile.read(length)
        self._body_read = True
        if len(raw) != length:
            raise InvalidParameter("request body is shorter than Content-Length")
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidParameter(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise InvalidParameter("JSON body must be an object")
        return payload

    def _drain_request_body(self) -> None:
        """Discard a bounded unread request body before closing the socket."""
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._body_read = True
            return
        try:
            remaining = int(length_header)
        except ValueError:
            self._body_read = True
            return
        if remaining <= 0:
            self._body_read = True
            return
        remaining = min(remaining, self.server.config.max_request_bytes)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass
        self._body_read = True

    def _send(self, response: Response, *, head: bool = False) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in response.headers.items():
            self.send_header(key, value)
        if response.status >= 400:
            # Auth/size rejects may happen before the request body is drained.
            # Closing avoids parsing the next request from leftover bytes.
            self.send_header("Connection", "close")
            self.close_connection = True
        if self._pending_cookie:
            self.send_header("Set-Cookie", self._pending_cookie)
            self._pending_cookie = None
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(response.body)

    def _cors_headers(self) -> dict[str, str]:
        origin = self.server.config.cors_origin
        if not origin:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "600",
        }

    def _apply_cors(self) -> None:
        # CORS headers are attached in _send; OPTIONS writes them itself.
        return

    def _authenticate(self, method: str, path: str, query: Mapping[str, str]) -> None:
        token = self.server.config.auth_token
        if not token:
            return
        if path == "/health":
            return
        def token_matches(supplied: Any) -> bool:
            if not isinstance(supplied, str):
                return False
            # compare_digest rejects non-ASCII str; compare UTF-8 bytes so a
            # malformed header is an auth failure, not a 500.
            return hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))

        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            supplied = authorization[len("Bearer ") :]
            if token_matches(supplied):
                return
        # Browser cookies are for read-only UI/artifact views. Mutations must
        # use a Bearer token, which also avoids classic CSRF on POST.
        if method in {"GET", "HEAD"}:
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                name, _, value = part.strip().partition("=")
                if name == _COOKIE_NAME and token_matches(unquote(value)):
                    return
            query_token = query.get("token")
            if path.startswith("/ui") and query_token and token_matches(query_token):
                self._pending_cookie = (
                    f"{_COOKIE_NAME}={quote(token, safe='')}; HttpOnly; SameSite=Lax; Path=/"
                )
                return
        raise Unauthorized("missing or invalid bearer token")

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _route(self, method: str, path: str, query: Mapping[str, str], body: dict[str, Any]) -> Response:
        service = self.server.service

        # Health and UI -------------------------------------------------
        if path == "/health" and method in {"GET", "HEAD"}:
            return _json_response(
                {
                    "status": "ok",
                    "version": __version__,
                    "backend": "sqlite",
                    "artifact_store": "file",
                    "auth_required": bool(self.server.config.auth_token),
                    "gateway": self.server.gateway is not None,
                    "webhooks": True,
                }
            )
        if path == "/" and method == "GET":
            return Response(status=302, headers={"Location": "/ui"}, content_type="text/plain")
        if method == "GET" and path == "/ui":
            return _html_response(render_home(service, filter_string=query.get("filter")))
        if method == "GET" and path.startswith("/ui/experiments/"):
            experiment_id = unquote(path.split("/", 3)[3])
            return _html_response(
                render_experiment(service, experiment_id, filter_string=query.get("filter"))
            )
        if method == "GET" and path.startswith("/ui/runs/"):
            run_id = unquote(path.split("/", 3)[3])
            return _html_response(render_run(service, run_id))
        if method == "GET" and path == "/ui/traces":
            return _html_response(render_traces(service, filter_string=query.get("filter")))
        if method == "GET" and path.startswith("/ui/traces/"):
            trace_id = unquote(path.split("/", 3)[3])
            return _html_response(render_trace(service, trace_id))
        if method == "GET" and path == "/ui/registry":
            return _html_response(render_registry(service))
        if method == "GET" and path == "/ui/prompts":
            return _html_response(render_prompts(service))
        if path == "/favicon.ico":
            return Response(status=204, body=b"", content_type="image/x-icon")

        # Experiments ---------------------------------------------------
        if path == "/api/2.0/mlflow/experiments/create" and method == "POST":
            experiment = service.create_experiment(
                _required(body, "name"),
                artifact_location=body.get("artifact_location"),
                tags=body.get("tags"),
            )
            return _json_response({"experiment": experiment.to_dict()})
        if path == "/api/2.0/mlflow/experiments/get" and method == "GET":
            experiment = service.get_experiment(_required(query, "experiment_id"))
            return _json_response({"experiment": experiment.to_dict()})
        if path == "/api/2.0/mlflow/experiments/get-by-name" and method == "GET":
            experiment = service.get_experiment_by_name(_required(query, "experiment_name"))
            if experiment is None:
                raise ResourceNotFound("experiment not found")
            return _json_response({"experiment": experiment.to_dict()})
        if path == "/api/2.0/mlflow/experiments/search" and method == "POST":
            experiments, token = service.search_experiments(
                body.get("filter"),
                max_results=_int(body.get("max_results", 1000), "max_results"),
                order_by=body.get("order_by"),
                page_token=body.get("page_token"),
            )
            return _json_response(
                {
                    "experiments": [item.to_dict() for item in experiments],
                    "next_page_token": token or "",
                }
            )
        if path == "/api/2.0/mlflow/experiments/delete" and method == "POST":
            service.delete_experiment(_required(body, "experiment_id"))
            return _json_response({})
        if path == "/api/2.0/mlflow/experiments/restore" and method == "POST":
            service.restore_experiment(_required(body, "experiment_id"))
            return _json_response({})
        if path == "/api/2.0/mlflow/experiments/set-experiment-tag" and method == "POST":
            service.set_experiment_tag(
                _required(body, "experiment_id"), _required(body, "key"), _required(body, "value")
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/experiments/update" and method == "POST":
            experiment = service.update_experiment(
                _required(body, "experiment_id"), name=body.get("name")
            )
            return _json_response({"experiment": experiment.to_dict()})

        # Runs ----------------------------------------------------------
        if path == "/api/2.0/mlflow/runs/create" and method == "POST":
            run = service.start_run(
                experiment_id=body.get("experiment_id"),
                experiment_name=body.get("experiment_name"),
                run_name=body.get("run_name"),
                tags=body.get("tags"),
                run_id=body.get("run_id"),
            )
            return _json_response({"run": run.to_dict()})
        if path == "/api/2.0/mlflow/runs/get" and method == "POST":
            run = service.get_run(
                _required(body, "run_id"),
                include_deleted=bool(body.get("include_deleted", False)),
            )
            return _json_response({"run": run.to_dict()})
        if path == "/api/2.0/mlflow/runs/update" and method == "POST":
            info = service.update_run(
                _required(body, "run_id"),
                run_name=body.get("run_name"),
                status=body.get("status"),
            )
            return _json_response({"run_info": info.to_dict()})
        if path == "/api/2.0/mlflow/runs/end" and method == "POST":
            info = service.end_run(_required(body, "run_id"), body.get("status", "FINISHED"))
            return _json_response({"run_info": info.to_dict()})
        if path == "/api/2.0/mlflow/runs/delete" and method == "POST":
            service.delete_run(_required(body, "run_id"))
            return _json_response({})
        if path == "/api/2.0/mlflow/runs/restore" and method == "POST":
            service.restore_run(_required(body, "run_id"))
            return _json_response({})
        if path == "/api/2.0/mlflow/runs/search" and method == "POST":
            runs, token = service.search_runs(
                body.get("filter"),
                max_results=_int(body.get("max_results", 1000), "max_results"),
                order_by=body.get("order_by"),
                page_token=body.get("page_token"),
                experiment_ids=body.get("experiment_ids"),
            )
            return _json_response(
                {"runs": [run.to_dict() for run in runs], "next_page_token": token or ""}
            )
        if path == "/api/2.0/mlflow/runs/log-parameter" and method == "POST":
            service.log_param(
                _required(body, "run_id"), _required(body, "key"), _required(body, "value")
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/runs/log-metric" and method == "POST":
            service.log_metric(
                _required(body, "run_id"),
                _required(body, "key"),
                _required(body, "value"),
                timestamp=body.get("timestamp"),
                step=_int(body.get("step", 0), "step"),
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/runs/log-batch" and method == "POST":
            service.log_batch(
                _required(body, "run_id"),
                metrics=body.get("metrics"),
                params=body.get("params"),
                tags=body.get("tags"),
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/runs/set-tag" and method == "POST":
            service.set_tag(
                _required(body, "run_id"), _required(body, "key"), _required(body, "value")
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/runs/delete-tag" and method == "POST":
            service.delete_tag(_required(body, "run_id"), _required(body, "key"))
            return _json_response({})
        if path == "/api/2.0/mlflow/runs/log-input" and method == "POST":
            run_id = _required(body, "run_id")
            kwargs = {key: value for key, value in body.items() if key != "run_id"}
            service.log_input(run_id, **kwargs)
            return _json_response({})
        if path == "/api/2.0/mlflow/metrics/get-history" and method == "GET":
            history = service.get_metric_history(
                _required(query, "run_id"), _required(query, "metric_key")
            )
            return _json_response({"metrics": [metric.to_dict() for metric in history]})

        # Artifacts -----------------------------------------------------
        if path == "/api/2.0/mlflow/artifacts/list" and method == "GET":
            files = service.list_artifacts(
                _required(query, "run_id"), query.get("path") or None
            )
            return _json_response({"files": [item.to_dict() for item in files]})
        if path == "/api/2.0/mlflow/artifacts/log" and method == "POST":
            run_id = _required(body, "run_id")
            relative = _required(body, "path")
            encoded = _required(body, "content_b64")
            if not isinstance(encoded, str):
                raise InvalidParameter("content_b64 must be a string")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise InvalidParameter("content_b64 is not valid base64") from exc
            if len(data) > self.server.config.max_request_bytes:
                raise PayloadTooLarge("decoded artifact exceeds request size limit")
            saved = service.log_artifact_bytes(run_id, relative, data, overwrite=True)
            return _json_response({"path": saved})
        if path == "/api/2.0/mlflow/artifacts/get" and method == "GET":
            run_id = _required(query, "run_id")
            artifact_path = _required(query, "path")
            data = service.read_artifact(run_id, artifact_path)
            guessed = mimetypes.guess_type(artifact_path)[0] or "application/octet-stream"
            # Never serve user artifacts as HTML/JS: that would turn the
            # tracking server into a stored-XSS origin.
            content_type = "application/octet-stream" if guessed.startswith(("text/html", "image/svg", "application/javascript")) else guessed
            return Response(
                status=200,
                body=data,
                content_type=content_type,
                headers={
                    "Content-Disposition": (
                        "attachment; filename=\""
                        + quote(Path(artifact_path).name, safe="")
                        + "\""
                    )
                },
            )

        # Registered models --------------------------------------------
        if path == "/api/2.0/mlflow/registered-models/create" and method == "POST":
            model = service.registry.create_registered_model(
                _required(body, "name"),
                description=body.get("description", ""),
                tags=_pairs_dict(body.get("tags")),
            )
            return _json_response({"registered_model": model.to_dict()})
        if path == "/api/2.0/mlflow/registered-models/get" and method == "GET":
            model = service.registry.get_registered_model(_required(query, "name"))
            return _json_response({"registered_model": model.to_dict()})
        if path == "/api/2.0/mlflow/registered-models/search" and method == "POST":
            models = service.registry.search_registered_models(
                body.get("filter"),
                max_results=_int(body.get("max_results", 1000), "max_results"),
                order_by=body.get("order_by"),
            )
            return _json_response(
                {"registered_models": [model.to_dict() for model in models], "next_page_token": ""}
            )
        if path == "/api/2.0/mlflow/registered-models/rename" and method == "POST":
            model = service.registry.rename_registered_model(
                _required(body, "name"), _required(body, "new_name")
            )
            return _json_response({"registered_model": model.to_dict()})
        if path == "/api/2.0/mlflow/registered-models/update" and method == "POST":
            model = service.registry.update_registered_model(
                _required(body, "name"), description=body.get("description")
            )
            return _json_response({"registered_model": model.to_dict()})
        if path == "/api/2.0/mlflow/registered-models/delete" and method == "POST":
            service.registry.delete_registered_model(
                _required(body, "name"), force=bool(body.get("force", False))
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/registered-models/set-tag" and method == "POST":
            service.registry.set_registered_model_tag(
                _required(body, "name"), _required(body, "key"), _required(body, "value")
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/registered-models/delete-tag" and method == "POST":
            service.registry.delete_registered_model_tag(
                _required(body, "name"), _required(body, "key")
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/registered-models/aliases/set" and method == "POST":
            service.registry.set_registered_model_alias(
                _required(body, "name"), _required(body, "alias"), _int(_required(body, "version"), "version")
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/registered-models/aliases/delete" and method == "POST":
            service.registry.delete_registered_model_alias(
                _required(body, "name"), _required(body, "alias")
            )
            return _json_response({})

        # Model versions ------------------------------------------------
        if path == "/api/2.0/mlflow/model-versions/create" and method == "POST":
            version = service.registry.create_model_version(
                _required(body, "name"),
                source=_required(body, "source"),
                run_id=body.get("run_id", ""),
                description=body.get("description", ""),
                tags=_pairs_dict(body.get("tags")),
            )
            return _json_response({"model_version": version.to_dict()})
        if path == "/api/2.0/mlflow/model-versions/get" and method == "GET":
            version = service.registry.get_model_version(
                _required(query, "name"), _required(query, "version")
            )
            return _json_response({"model_version": version.to_dict()})
        if path == "/api/2.0/mlflow/model-versions/get-by-alias" and method == "GET":
            version = service.registry.get_model_version_by_alias(
                _required(query, "name"), _required(query, "alias")
            )
            return _json_response({"model_version": version.to_dict()})
        if path == "/api/2.0/mlflow/model-versions/search" and method == "POST":
            versions = service.registry.search_model_versions(
                body.get("filter"),
                name=body.get("name"),
                max_results=_int(body.get("max_results", 1000), "max_results"),
                order_by=body.get("order_by"),
            )
            return _json_response(
                {"model_versions": [version.to_dict() for version in versions], "next_page_token": ""}
            )
        if path == "/api/2.0/mlflow/model-versions/update" and method == "POST":
            version = service.registry.update_model_version(
                _required(body, "name"),
                _int(_required(body, "version"), "version"),
                description=body.get("description"),
            )
            return _json_response({"model_version": version.to_dict()})
        if path == "/api/2.0/mlflow/model-versions/delete" and method == "POST":
            service.registry.delete_model_version(
                _required(body, "name"), _int(_required(body, "version"), "version")
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/model-versions/set-tag" and method == "POST":
            service.registry.set_model_version_tag(
                _required(body, "name"),
                _int(_required(body, "version"), "version"),
                _required(body, "key"),
                _required(body, "value"),
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/model-versions/delete-tag" and method == "POST":
            service.registry.delete_model_version_tag(
                _required(body, "name"),
                _int(_required(body, "version"), "version"),
                _required(body, "key"),
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/model-versions/transition-stage" and method == "POST":
            version = service.registry.transition_model_version_stage(
                _required(body, "name"),
                _int(_required(body, "version"), "version"),
                _required(body, "stage"),
            )
            return _json_response({"model_version": version.to_dict()})

        # Logged models (MLflow 3) --------------------------------------
        if path == "/api/2.0/mlflow/logged-models" and method == "POST":
            model = service.create_logged_model(
                body.get("name"),
                experiment_id=str(body.get("experiment_id", "0")),
                model_type=body.get("model_type") or "",
                source_run_id=body.get("source_run_id"),
                tags=_pairs_dict(body.get("tags")),
                params=_pairs_dict(body.get("params")),
                artifact_location=body.get("artifact_location"),
            )
            return _json_response({"model": model.to_dict()})
        if path == "/api/2.0/mlflow/logged-models/search" and method == "POST":
            models, token = service.search_logged_models(
                body.get("experiment_ids"),
                filter_string=body.get("filter"),
                max_results=_int(body.get("max_results", 100), "max_results"),
                order_by=body.get("order_by"),
                page_token=body.get("page_token"),
            )
            payload: dict[str, Any] = {
                "models": [model.to_dict() for model in models],
            }
            if token:
                payload["next_page_token"] = token
            return _json_response(payload)
        if path.startswith("/api/2.0/mlflow/logged-models/"):
            prefix = "/api/2.0/mlflow/logged-models/"
            remainder = path[len(prefix) :]
            parts = [unquote(part) for part in remainder.split("/") if part]
            # ``path`` was already unquoted once by _handle.  For tag keys we
            # need exactly one decode of the raw path: otherwise a key that
            # literally contains "%2F" is decoded twice and targets the wrong
            # tag.  Split the raw path and unquote each segment once.
            raw_remainder = (
                self._raw_path[len(prefix) :]
                if self._raw_path.startswith(prefix)
                else remainder
            )
            raw_parts = [
                unquote(part) for part in raw_remainder.split("/") if part
            ]
            if len(parts) == 1 and method == "GET":
                model = service.get_logged_model(parts[0])
                return _json_response({"model": model.to_dict()})
            if len(parts) == 1 and method == "PATCH":
                model = service.finalize_logged_model(
                    parts[0],
                    _required(body, "status"),
                    status_message=body.get("status_message", ""),
                )
                return _json_response({"model": model.to_dict()})
            if len(parts) == 1 and method == "DELETE":
                service.delete_logged_model(parts[0])
                return _json_response({})
            if len(parts) == 2 and parts[1] == "tags" and method == "PATCH":
                service.set_logged_model_tags(parts[0], _pairs_dict(body.get("tags")))
                return _json_response({})
            if len(raw_parts) >= 3 and raw_parts[1] == "tags" and method == "DELETE":
                # A tag key may contain '/' or percent-encoded characters.
                # Re-join the raw segments after /tags/ and decode each once.
                model_id = raw_parts[0]
                key = "/".join(raw_parts[2:])
                service.delete_logged_model_tag(model_id, key)
                return _json_response({})
            if len(parts) == 2 and parts[1] == "params" and method == "POST":
                model = service.log_logged_model_params(
                    parts[0], _pairs_dict(body.get("params"))
                )
                return _json_response({"model": model.to_dict()})
            if len(parts) == 2 and parts[1] == "metrics" and method == "POST":
                metrics = body.get("metrics")
                if not isinstance(metrics, list):
                    raise InvalidParameter("metrics must be a list")
                model = service.log_logged_model_metrics(
                    parts[0],
                    metrics,
                    step=_int(body.get("step", 0), "step"),
                    timestamp=body.get("timestamp"),
                )
                return _json_response({"model": model.to_dict()})

        # Traces --------------------------------------------------------
        if path == "/api/2.0/mlflow/traces/log" and method == "POST":
            try:
                trace = Trace.from_dict(_required(body, "trace"))
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidParameter(f"invalid trace payload: {exc}") from exc
            saved = service.log_trace(trace)
            return _json_response({"trace": saved.to_dict()})
        if path == "/api/2.0/mlflow/traces/get" and method == "GET":
            trace = service.get_trace(_required(query, "trace_id"))
            return _json_response({"trace": trace.to_dict()})
        if path == "/api/2.0/mlflow/traces/search" and method == "POST":
            traces = service.search_traces(
                body.get("experiment_ids"),
                filter_string=body.get("filter"),
                max_results=_int(body.get("max_results", 100), "max_results"),
                order_by=body.get("order_by"),
            )
            return _json_response({"traces": [trace.to_dict() for trace in traces]})

        # Assessments (feedback / expectations) -------------------------
        if path == "/api/2.0/mlflow/traces/assessments/log" and method == "POST":
            assessment = service.log_feedback(
                _required(body, "trace_id"),
                _required(body, "name"),
                _required(body, "value"),
                source_type=body.get("source_type", "HUMAN"),
                source_id=body.get("source_id", ""),
                rationale=body.get("rationale", ""),
                metadata=body.get("metadata"),
                is_expectation=_bool(body.get("is_expectation"), "is_expectation"),
                valid=_bool(body.get("valid"), "valid", default=True),
            )
            return _json_response({"assessment": assessment.to_dict()})
        if path == "/api/2.0/mlflow/traces/assessments/get" and method == "GET":
            assessment = service.get_assessment(_required(query, "assessment_id"))
            return _json_response({"assessment": assessment.to_dict()})
        if path == "/api/2.0/mlflow/traces/assessments/list" and method == "GET":
            assessments = service.list_assessments(_required(query, "trace_id"))
            return _json_response(
                {"assessments": [item.to_dict() for item in assessments]}
            )
        if path == "/api/2.0/mlflow/traces/assessments/delete" and method == "POST":
            service.delete_assessment(_required(body, "assessment_id"))
            return _json_response({})

        # Prompt registry ----------------------------------------------
        if path == "/api/2.0/mlflow/prompts/create" and method == "POST":
            prompt = service.prompts.register_prompt(
                _required(body, "name"),
                _required(body, "template"),
                commit_message=body.get("commit_message", ""),
                tags=_pairs_dict(body.get("tags")),
            )
            return _json_response({"prompt": prompt.to_dict()})
        if path == "/api/2.0/mlflow/prompts/get" and method == "GET":
            name = _required(query, "name")
            if query.get("alias") and query.get("version"):
                raise InvalidParameter("pass alias or version, not both")
            if query.get("alias"):
                prompt = service.prompts.get_by_alias(name, query["alias"])
            elif query.get("version"):
                prompt = service.prompts.get(name, query["version"])
            else:
                prompt = service.prompts.get(name)
            return _json_response({"prompt": prompt.to_dict()})
        if path == "/api/2.0/mlflow/prompts/versions/list" and method == "GET":
            versions = service.prompts.list_versions(_required(query, "name"))
            return _json_response(
                {"prompt_versions": [item.to_dict() for item in versions]}
            )
        if path == "/api/2.0/mlflow/prompts/search" and method == "POST":
            prompts = service.prompts.search_prompts(
                body.get("filter"),
                max_results=_int(body.get("max_results", 1000), "max_results"),
                order_by=body.get("order_by"),
            )
            return _json_response(
                {"prompts": [item.to_dict() for item in prompts], "next_page_token": ""}
            )
        if path == "/api/2.0/mlflow/prompts/aliases/set" and method == "POST":
            service.prompts.set_alias(
                _required(body, "name"),
                _required(body, "alias"),
                _required(body, "version"),
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/prompts/aliases/delete" and method == "POST":
            service.prompts.delete_alias(
                _required(body, "name"), _required(body, "alias")
            )
            return _json_response({})
        if path == "/api/2.0/mlflow/prompts/delete" and method == "POST":
            service.prompts.delete_prompt(_required(body, "name"))
            return _json_response({})

        # Evaluation datasets -------------------------------------------
        if path == "/api/2.0/mlflow/datasets/create" and method == "POST":
            dataset = service.datasets.create_dataset(
                _required(body, "name"),
                body.get("records") or [],
                source=body.get("source", ""),
                source_type=body.get("source_type", ""),
                tags=_pairs_dict(body.get("tags")),
            )
            return _json_response({"dataset": dataset.to_dict()})
        if path == "/api/2.0/mlflow/datasets/get" and method == "GET":
            dataset = service.datasets.get_dataset(_required(query, "dataset_id"))
            return _json_response({"dataset": dataset.to_dict()})
        if path == "/api/2.0/mlflow/datasets/search" and method == "POST":
            datasets = service.datasets.search_datasets(
                body.get("filter"),
                max_results=_int(body.get("max_results", 100), "max_results"),
                order_by=body.get("order_by"),
            )
            return _json_response(
                {"datasets": [item.to_dict() for item in datasets]}
            )
        if path == "/api/2.0/mlflow/datasets/merge-records" and method == "POST":
            dataset = service.datasets.merge_records(
                _required(body, "dataset_id"),
                body.get("records") or [],
            )
            return _json_response({"dataset": dataset.to_dict()})
        if path == "/api/2.0/mlflow/datasets/delete" and method == "POST":
            service.datasets.delete_dataset(_required(body, "dataset_id"))
            return _json_response({})

        # Model monitoring ---------------------------------------------
        if path == "/api/2.0/mlflow/monitors/create" and method == "POST":
            monitor = service.monitoring.create_monitor(
                _required(body, "name"),
                body.get("baseline_records") or [],
                _required(body, "features"),
                model_name=body.get("model_name", ""),
                model_version=body.get("model_version", ""),
                dataset_id=body.get("dataset_id", ""),
                thresholds=body.get("thresholds"),
                tags=_pairs_dict(body.get("tags")),
            )
            return _json_response({"monitor": monitor.to_dict()})
        if path == "/api/2.0/mlflow/monitors/get" and method == "GET":
            monitor = service.monitoring.get_monitor(_required(query, "monitor_id"))
            return _json_response({"monitor": monitor.to_dict()})
        if path == "/api/2.0/mlflow/monitors/list" and method == "GET":
            monitors = service.monitoring.list_monitors()
            return _json_response(
                {"monitors": [item.to_dict() for item in monitors]}
            )
        if path == "/api/2.0/mlflow/monitors/delete" and method == "POST":
            service.monitoring.delete_monitor(_required(body, "monitor_id"))
            return _json_response({})
        if path == "/api/2.0/mlflow/monitors/windows/create" and method == "POST":
            window = service.monitoring.record_window(
                _required(body, "monitor_id"),
                body.get("records") or [],
                window_start=_int(body.get("window_start", 0), "window_start"),
                window_end=_int(body.get("window_end", 0), "window_end"),
            )
            return _json_response({"window": window.to_dict()})
        if path == "/api/2.0/mlflow/monitors/windows/list" and method == "GET":
            windows = service.monitoring.list_windows(
                _required(query, "monitor_id"),
                limit=_int(query.get("limit", 100), "limit"),
            )
            return _json_response(
                {"windows": [item.to_dict() for item in windows]}
            )

        # Webhooks -----------------------------------------------------
        if path == "/api/2.0/mlflow/webhooks/create" and method == "POST":
            webhook = service.webhooks.create_webhook(
                _required(body, "name"),
                _required(body, "url"),
                _required(body, "events"),
                secret=body.get("secret", ""),
                description=body.get("description", ""),
            )
            return _json_response({"webhook": webhook.to_dict()})
        if path == "/api/2.0/mlflow/webhooks/get" and method == "GET":
            webhook = service.webhooks.get_webhook(_required(query, "webhook_id"))
            return _json_response({"webhook": webhook.to_dict()})
        if path == "/api/2.0/mlflow/webhooks/list" and method == "GET":
            webhooks = service.webhooks.list_webhooks()
            return _json_response(
                {
                    "webhooks": [webhook.to_dict() for webhook in webhooks],
                    "next_page_token": "",
                }
            )
        if path == "/api/2.0/mlflow/webhooks/update" and method == "POST":
            webhook = service.webhooks.update_webhook(
                _required(body, "webhook_id"),
                name=body.get("name"),
                url=body.get("url"),
                events=body.get("events"),
                secret=body.get("secret"),
                description=body.get("description"),
                status=body.get("status"),
            )
            return _json_response({"webhook": webhook.to_dict()})
        if path == "/api/2.0/mlflow/webhooks/delete" and method == "POST":
            service.webhooks.delete_webhook(_required(body, "webhook_id"))
            return _json_response({})
        if path == "/api/2.0/mlflow/webhooks/test" and method == "POST":
            delivery = service.webhooks.test_webhook(_required(body, "webhook_id"))
            return _json_response({"delivery": delivery.to_dict()})
        if path == "/api/2.0/mlflow/webhooks/deliveries" and method == "GET":
            deliveries = service.webhooks.list_deliveries(
                webhook_id=query.get("webhook_id"),
                limit=_int(query.get("limit", 100), "limit"),
            )
            return _json_response(
                {"deliveries": [delivery.to_dict() for delivery in deliveries]}
            )

        # AI gateway ---------------------------------------------------
        if path == "/gateway/routes" and method == "GET":
            if self.server.gateway is None:
                raise ResourceNotFound("gateway is not configured")
            return _json_response(
                {
                    "routes": self.server.gateway.routes(),
                    "providers": [
                        spec.to_dict()
                        for spec in self.server.gateway.config.providers.values()
                    ],
                }
            )
        if path == "/gateway/stats" and method == "GET":
            if self.server.gateway is None:
                raise ResourceNotFound("gateway is not configured")
            return _json_response({"stats": self.server.gateway.stats()})
        if path == "/gateway/invocations" and method == "POST":
            if self.server.gateway is None:
                raise ResourceNotFound("gateway is not configured")
            response = self.server.gateway.complete(
                _required(body, "route"),
                _required(body, "messages"),
                params=body.get("params"),
            )
            return _json_response(response.to_dict())

        if path.startswith("/api/"):
            raise ResourceNotFound(f"unknown API route {path}")
        raise ResourceNotFound(f"not found: {path}")


def _required(mapping: Mapping[str, Any], key: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise InvalidParameter(f"missing required field {key!r}")
    return mapping[key]


def _int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise InvalidParameter(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParameter(f"{field_name} must be an integer") from exc


def _bool(value: Any, field_name: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise InvalidParameter(f"{field_name} must be a boolean")
    return value


def _pairs_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, list):
        result = {}
        for item in value:
            if not isinstance(item, dict) or "key" not in item:
                raise InvalidParameter("tags must be a mapping or a list of {key,value} objects")
            result[str(item["key"])] = item.get("value")
        return result
    raise InvalidParameter("tags must be a mapping or a list of {key,value} objects")


def create_server(
    config: MiniConfig | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    service: TrackingService | None = None,
    gateway: Gateway | None = None,
) -> MiniMlflowHTTPServer:
    cfg = config or MiniConfig.from_env()
    bind_host = host if host is not None else cfg.host
    bind_port = port if port is not None else cfg.port
    return MiniMlflowHTTPServer(
        (bind_host, bind_port), cfg, service=service, gateway=gateway
    )


def serve(
    config: MiniConfig | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    gateway: Gateway | None = None,
) -> None:
    server = create_server(config, host=host, port=port, gateway=gateway)
    if tls_cert:
        if not tls_key:
            raise InvalidParameter("--tls-key is required with --tls-cert")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    actual_host, actual_port = server.server_address[:2]
    scheme = "https" if tls_cert else "http"
    print(f"MiniMLflow listening on {scheme}://{actual_host}:{actual_port}")
    print(f"Backend: {server.service.config.backend_store_uri}")
    print(f"Artifacts: {server.service.config.artifact_root_path}")
    server.service.webhooks.start_dispatcher()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
