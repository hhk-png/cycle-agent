"""A tiny model scoring server.

This is not a production inference gateway.  It demonstrates the contract:
a model URI is resolved once, inputs are normalized, and predictions are
returned without exposing the Python object over the network.  Compare this
with MLflow's ``mlflow models serve`` and Seldon/KServe, which add batching,
GPU scheduling, concurrency limits, metrics and traffic splitting.
"""

from __future__ import annotations

import hmac
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlparse

from .client import MiniClient
from .errors import InvalidParameter, MiniMlflowError, Unauthorized
from .models import PyFuncModel, load_model
from .version import __version__

LOGGER = logging.getLogger("mini_mlflow.scoring")


def records_from_payload(payload: Mapping[str, Any], model: PyFuncModel) -> Any:
    if "dataframe_split" in payload:
        split = payload["dataframe_split"]
        if not isinstance(split, dict) or "columns" not in split or "data" not in split:
            raise InvalidParameter("dataframe_split must contain columns and data")
        columns = list(split["columns"])
        rows = split["data"]
        if not isinstance(rows, list):
            raise InvalidParameter("dataframe_split.data must be a list")
        return [dict(zip(columns, row)) for row in rows]
    if "dataframe_records" in payload:
        records = payload["dataframe_records"]
        if not isinstance(records, list):
            raise InvalidParameter("dataframe_records must be a list")
        return records
    if "instances" in payload:
        instances = payload["instances"]
        if not isinstance(instances, list):
            raise InvalidParameter("instances must be a list")
        if not instances:
            return []
        if all(isinstance(item, dict) for item in instances):
            return instances
        names = []
        if model.signature and model.signature.inputs:
            names = [column.name for column in model.signature.inputs]
        elif getattr(model.model, "feature_names_", None):
            names = list(model.model.feature_names_)
        else:
            raise InvalidParameter("list instances require a model signature with column names")
        return [dict(zip(names, row)) if isinstance(row, list) else row for row in instances]
    if "inputs" in payload:
        inputs = payload["inputs"]
        if isinstance(inputs, (list, dict)):
            return inputs
        raise InvalidParameter("inputs must be a list or mapping for this model")
    if isinstance(payload, dict) and ("columns" in payload or "data" in payload):
        return records_from_payload({"dataframe_split": payload}, model)
    return payload


class _ScoringHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "ScoringServer"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/ping", "/health"}:
            self._json({"status": "ok", "version": __version__, "model": self.server.model_uri})
        elif path == "/version":
            self._json({"version": __version__})
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/invocations":
            self._json({"error": "not found"}, status=404)
            return
        try:
            self._authenticate()
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise InvalidParameter("request body is required")
            if length > self.server.max_request_bytes:
                # Read a bounded prefix before answering.  Closing with unread
                # bytes makes Windows send RST, so the client sees
                # ConnectionAbortedError instead of the 413 JSON body.
                self._drain_body(length)
                self.close_connection = True
                self._json({"error": "request too large"}, status=413)
                return
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise InvalidParameter("JSON body must be an object")
            data = records_from_payload(payload, self.server.model)
            predictions = self.server.model.predict(data)
            self._json({"predictions": predictions})
        except Unauthorized as exc:
            # Auth may reject before the body is read; drain it so the 401 is
            # readable instead of turning into a TCP reset on Windows.
            self._drain_body()
            self._json({"error": exc.message}, status=401)
        except (MiniMlflowError, ValueError, TypeError) as exc:
            self._json({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            LOGGER.exception("scoring request failed")
            self._json({"error": "internal server error"}, status=500)

    def _drain_body(self, length: int | None = None) -> None:
        if length is None:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or 0)
            except ValueError:
                self.close_connection = True
                return
        if length <= 0:
            return
        remaining = min(length, self.server.max_request_bytes)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            self.close_connection = True
            return
        if length > self.server.max_request_bytes:
            self.close_connection = True

    def _authenticate(self) -> None:
        token = self.server.auth_token
        if not token:
            return
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or not hmac.compare_digest(header[7:], token):
            raise Unauthorized("invalid bearer token")

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class ScoringServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        model: PyFuncModel,
        *,
        model_uri: str,
        auth_token: str | None = None,
        max_request_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.model = model
        self.model_uri = model_uri
        self.auth_token = auth_token
        self.max_request_bytes = max_request_bytes
        super().__init__(address, _ScoringHandler)


def create_scoring_server(
    model_uri: str,
    *,
    host: str = "127.0.0.1",
    port: int = 5001,
    tracking_uri: str | None = None,
    auth_token: str | None = None,
    client: MiniClient | None = None,
) -> ScoringServer:
    if client is None and model_uri.startswith(("runs:/", "models:/")):
        client = MiniClient(tracking_uri)
    model = load_model(model_uri, client=client)
    return ScoringServer((host, port), model, model_uri=model_uri, auth_token=auth_token)


def serve_model(
    model_uri: str,
    *,
    host: str = "127.0.0.1",
    port: int = 5001,
    tracking_uri: str | None = None,
    auth_token: str | None = None,
) -> None:
    server = create_scoring_server(
        model_uri,
        host=host,
        port=port,
        tracking_uri=tracking_uri,
        auth_token=auth_token,
    )
    actual_host, actual_port = server.server_address[:2]
    print(f"MiniMLflow scoring server for {model_uri}")
    print(f"Listening on http://{actual_host}:{actual_port}/invocations")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
