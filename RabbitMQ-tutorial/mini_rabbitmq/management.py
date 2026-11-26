"""管理 HTTP API 与 Prometheus 指标端点。

接口形状参考 RabbitMQ Management HTTP API，但只实现教程用到的子集。使用
标准库 http.server，避免额外依赖；生产环境应使用反向代理、TLS、细粒度
审计和独立的管理网络。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .auth import AuthStore
from .broker import Broker
from .server import AMQPServer, _is_loopback


LOG = logging.getLogger(__name__)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class ManagementServer:
    def __init__(
        self,
        broker: Broker,
        *,
        host: str = "127.0.0.1",
        port: int = 15672,
        amqp_server: AMQPServer | None = None,
        auth: AuthStore | None = None,
    ) -> None:
        self.broker = broker
        self.host = host
        self.port = port
        self.amqp_server = amqp_server
        self.auth = auth or broker.auth
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> int:
        with self._lock:
            if self._httpd is not None:
                return self.port
            manager = self

            class Handler(BaseHTTPRequestHandler):
                server_version = "MiniRabbitMQManagement/0.1"

                def log_message(self, format: str, *args: Any) -> None:
                    LOG.debug("%s - %s", self.address_string(), format % args)

                def do_GET(self) -> None:
                    self._handle("GET")

                def do_POST(self) -> None:
                    self._handle("POST")

                def do_PUT(self) -> None:
                    self._handle("PUT")

                def do_DELETE(self) -> None:
                    self._handle("DELETE")

                def _handle(self, method: str) -> None:
                    try:
                        user = self._authenticate()
                        if user is None:
                            return
                        manager._route(self, method, user)
                    except BrokenPipeError:
                        return
                    except Exception as exc:  # pragma: no cover - 防御性
                        LOG.exception("management request failed")
                        self._json(500, {"error": "internal_error", "reason": str(exc)})

                def _authenticate(self):
                    header = self.headers.get("Authorization", "")
                    if not header.startswith("Basic "):
                        self._json(
                            401,
                            {"error": "not_authorized"},
                            extra_headers={"WWW-Authenticate": 'Basic realm="mini-rabbitmq"'},
                        )
                        return None
                    try:
                        decoded = base64.b64decode(header[6:]).decode("utf-8")
                        username, password = decoded.split(":", 1)
                    except (ValueError, UnicodeDecodeError):
                        self._json(401, {"error": "not_authorized"})
                        return None
                    if username == "guest" and not _is_loopback(self.client_address[0]):
                        self._json(403, {"error": "guest_user_can_only_connect_from_localhost"})
                        return None
                    user = manager.auth.authenticate(username, password)
                    if user is None:
                        self._json(401, {"error": "not_authorized"})
                        return None
                    return user

                def _json(
                    self,
                    status: int,
                    payload: Any,
                    *,
                    extra_headers: dict[str, str] | None = None,
                ) -> None:
                    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    for key, value in (extra_headers or {}).items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(data)

                def _text(self, status: int, text: str, content_type: str) -> None:
                    data = text.encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def _body_json(self) -> dict[str, Any]:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    if length > 1_048_576:
                        raise ValueError("request body too large")
                    raw = self.rfile.read(length) if length else b"{}"
                    value = json.loads(raw.decode("utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("JSON body must be an object")
                    return value

            httpd = ThreadingHTTPServer((self.host, self.port), Handler)
            httpd.daemon_threads = True
            self.port = httpd.server_address[1]
            self._httpd = httpd
            self._thread = threading.Thread(
                target=httpd.serve_forever, name="management-http", daemon=True
            )
            self._thread.start()
            return self.port

    start_background = start

    def stop(self) -> None:
        with self._lock:
            httpd = self._httpd
            self._httpd = None
            thread = self._thread
            self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def __enter__(self) -> "ManagementServer":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # ---- 路由（由内部 Handler 调用） ---------------------------------
    def _route(self, handler: Any, method: str, user: Any) -> None:
        split = urlsplit(handler.path)
        path = split.path.rstrip("/") or "/"
        parts = [unquote(part) for part in path.split("/") if part]
        if path == "/api/overview":
            self._overview(handler, user)
            return
        if path == "/api/whoami":
            handler._json(200, {"name": user.name, "tags": sorted(user.tags)})
            return
        if path == "/api/connections":
            self._connections(handler, user)
            return
        if path == "/api/channels":
            self._channels(handler, user)
            return
        if path == "/api/vhosts":
            self._vhosts(handler, user)
            return
        if path == "/api/nodes":
            handler._json(200, [self._node(user)])
            return
        if path in ("/metrics", "/api/metrics"):
            self._metrics(handler, user)
            return
        if path.startswith("/api/health/checks/"):
            self._health(handler, user)
            return
        if parts[:2] == ["api", "queues"]:
            self._queue_route(handler, method, user, parts)
            return
        if parts[:2] == ["api", "exchanges"]:
            self._exchange_route(handler, method, user, parts)
            return
        if parts[:2] == ["api", "bindings"]:
            self._bindings_route(handler, user, parts)
            return
        if parts[:2] == ["api", "policies"]:
            self._policy_route(handler, method, user, parts)
            return
        handler._json(404, {"error": "not_found", "path": path})

    # ---- 概览 ---------------------------------------------------------
    def _accessible_vhosts(self, user: Any) -> list[str]:
        return [
            name
            for name in sorted(self.broker.vhosts)
            if self.auth.can_access_vhost(user.name, name)
        ]

    def _overview(self, handler: Any, user: Any) -> None:
        overview = self.broker.overview()
        totals = overview["totals"]
        connections = len(self.amqp_server.connections) if self.amqp_server else 0
        channels = len(self.amqp_server.channel_list()) if self.amqp_server else 0
        overview["object_totals"] = {
            "connections": connections,
            "channels": channels,
            "consumers": totals["consumers"],
            "queues": totals["queues"],
            "exchanges": totals["exchanges"],
        }
        overview["queue_totals"] = {
            "messages": totals["messages_ready"] + totals["messages_unacknowledged"],
            "messages_ready": totals["messages_ready"],
            "messages_unacknowledged": totals["messages_unacknowledged"],
        }
        overview["message_stats"] = self.broker.metrics.counters()
        overview["listeners"] = [
            {"protocol": "amqp", "port": self.amqp_server.port if self.amqp_server else None},
            {"protocol": "http", "port": self.port},
        ]
        overview["management_version"] = "0.1.0"
        handler._json(200, overview)

    def _connections(self, handler: Any, user: Any) -> None:
        rows = self.amqp_server.connection_list() if self.amqp_server else []
        rows = [
            row
            for row in rows
            if row.get("user") == user.name
            or user.has_tag("monitoring")
            or user.has_tag("administrator")
        ]
        handler._json(200, rows)

    def _channels(self, handler: Any, user: Any) -> None:
        rows = self.amqp_server.channel_list() if self.amqp_server else []
        rows = [
            row
            for row in rows
            if row.get("user") == user.name
            or user.has_tag("monitoring")
            or user.has_tag("administrator")
        ]
        handler._json(200, rows)

    def _vhosts(self, handler: Any, user: Any) -> None:
        rows = []
        for name in self._accessible_vhosts(user):
            vhost = self.broker.vhosts[name]
            rows.append(
                {
                    "name": name,
                    "tracing": False,
                    "cluster_state": {"state": "running"},
                    "totals": vhost.totals(),
                }
            )
        handler._json(200, rows)

    def _node(self, user: Any) -> dict[str, Any]:
        overview = self.broker.overview()
        return {
            "name": self.broker.node_name,
            "type": "disc",
            "running": True,
            "uptime": overview["uptime_seconds"],
            "mem_alarm": "memory" in overview["alarms"],
            "disk_free_alarm": "disk" in overview["alarms"],
            "alarms": list(overview["alarms"]),
            "partitions": [],
            "applications": [{"name": "rabbit", "version": "0.1.0"}],
            "auth": "PLAIN",
            "user": user.name,
        }

    # ---- 队列 ---------------------------------------------------------
    def _queue_route(self, handler: Any, method: str, user: Any, parts: list[str]) -> None:
        if len(parts) == 2:
            if method != "GET":
                handler._json(405, {"error": "method_not_allowed"})
                return
            vhosts = self._accessible_vhosts(user)
            rows = [
                queue.to_dict()
                for vhost in vhosts
                for queue in self.broker.vhosts[vhost].queues.values()
            ]
            handler._json(200, rows)
            return
        vhost = parts[2]
        if not self._vhost_allowed(handler, user, vhost):
            return
        if len(parts) == 3:
            if method != "GET":
                handler._json(405, {"error": "method_not_allowed"})
                return
            rows = [
                queue.to_dict()
                for queue in self.broker.vhosts[vhost].queues.values()
            ]
            handler._json(200, rows)
            return
        name = parts[3]
        if len(parts) >= 5 and parts[4] == "purge":
            if method != "POST":
                handler._json(405, {"error": "method_not_allowed"})
                return
            if not self._authorized(handler, user, vhost, "configure", name):
                return
            try:
                count = self.broker.vhosts[vhost].get_queue(name).purge()
                self.broker.note_state_change()
            except Exception as exc:
                handler._json(404, {"error": "not_found", "reason": str(exc)})
                return
            handler._json(200, {"messages_purged": count})
            return
        if method == "GET":
            queue = self.broker.vhosts[vhost].queues.get(name)
            if queue is None:
                handler._json(404, {"error": "not_found"})
                return
            handler._json(200, queue.to_dict())
            return
        if method == "DELETE":
            if not self._authorized(handler, user, vhost, "configure", name):
                return
            try:
                count = self.broker.delete_queue(name, vhost=vhost)
            except Exception as exc:
                handler._json(404, {"error": "not_found", "reason": str(exc)})
                return
            handler._json(200, {"messages_deleted": count})
            return
        handler._json(405, {"error": "method_not_allowed"})

    # ---- 交换机 -------------------------------------------------------
    def _exchange_route(self, handler: Any, method: str, user: Any, parts: list[str]) -> None:
        if len(parts) == 2:
            if method != "GET":
                handler._json(405, {"error": "method_not_allowed"})
                return
            rows = []
            for vhost in self._accessible_vhosts(user):
                for exchange in self.broker.vhosts[vhost].exchanges.values():
                    row = exchange.to_dict()
                    row["vhost"] = vhost
                    rows.append(row)
            handler._json(200, rows)
            return
        vhost = parts[2]
        if not self._vhost_allowed(handler, user, vhost):
            return
        if len(parts) == 3:
            rows = []
            for exchange in self.broker.vhosts[vhost].exchanges.values():
                row = exchange.to_dict()
                row["vhost"] = vhost
                rows.append(row)
            handler._json(200, rows)
            return
        name = parts[3]
        if len(parts) >= 5 and parts[4] == "publish":
            if method != "POST":
                handler._json(405, {"error": "method_not_allowed"})
                return
            if not self._authorized(handler, user, vhost, "write", name):
                return
            try:
                payload = handler._body_json()
                body = payload.get("payload", "")
                encoding = payload.get("payload_encoding", "string")
                if encoding == "base64":
                    import base64 as b64

                    body_bytes = b64.b64decode(body)
                else:
                    body_bytes = str(body).encode("utf-8")
                result = self.broker.publish(
                    name,
                    payload.get("routing_key", ""),
                    body_bytes,
                    properties=payload.get("properties") or {},
                    vhost=vhost,
                )
            except Exception as exc:
                handler._json(400, {"error": "bad_request", "reason": str(exc)})
                return
            handler._json(200, {"routed": bool(result.routed)})
            return
        if method != "GET":
            handler._json(405, {"error": "method_not_allowed"})
            return
        exchange = self.broker.vhosts[vhost].exchanges.get(name)
        if exchange is None:
            handler._json(404, {"error": "not_found"})
            return
        row = exchange.to_dict()
        row["vhost"] = vhost
        handler._json(200, row)

    # ---- 绑定 ---------------------------------------------------------
    def _bindings_route(self, handler: Any, user: Any, parts: list[str]) -> None:
        if main_method := getattr(handler, "command", "GET"):
            if main_method != "GET":
                handler._json(405, {"error": "method_not_allowed"})
                return
        if len(parts) == 2:
            rows = []
            for vhost in self._accessible_vhosts(user):
                rows.extend(self.broker.vhosts[vhost].bindings())
            handler._json(200, rows)
            return
        vhost = parts[2]
        if not self._vhost_allowed(handler, user, vhost):
            return
        handler._json(200, self.broker.vhosts[vhost].bindings())

    # ---- 策略 ---------------------------------------------------------
    def _policy_route(self, handler: Any, method: str, user: Any, parts: list[str]) -> None:
        if len(parts) == 2:
            if method != "GET":
                handler._json(405, {"error": "method_not_allowed"})
                return
            rows = []
            for vhost in self._accessible_vhosts(user):
                for policy in self.broker.vhosts[vhost].policies.to_list():
                    policy["vhost"] = vhost
                    rows.append(policy)
            handler._json(200, rows)
            return
        vhost = parts[2]
        if not self._vhost_allowed(handler, user, vhost):
            return
        if len(parts) == 3:
            rows = []
            for policy in self.broker.vhosts[vhost].policies.to_list():
                policy["vhost"] = vhost
                rows.append(policy)
            handler._json(200, rows)
            return
        name = parts[3]
        if not self._authorized(handler, user, vhost, "configure", ".*"):
            return
        if method == "GET":
            policy = self.broker.vhosts[vhost].policies.get(name)
            if policy is None:
                handler._json(404, {"error": "not_found"})
                return
            row = policy.to_dict()
            row["vhost"] = vhost
            handler._json(200, row)
        elif method == "PUT":
            try:
                body = handler._body_json()
                pattern = body.get("pattern")
                definition = body.get("definition")
                if not isinstance(pattern, str) or not isinstance(definition, dict):
                    raise ValueError("pattern and definition are required")
                result = self.broker.set_policy(
                    name,
                    pattern,
                    definition,
                    apply_to=body.get("apply-to", "all"),
                    priority=int(body.get("priority", 0)),
                    vhost=vhost,
                )
            except Exception as exc:
                handler._json(400, {"error": "bad_request", "reason": str(exc)})
                return
            result["vhost"] = vhost
            handler._json(201, result)
        elif method == "DELETE":
            try:
                self.broker.delete_policy(name, vhost=vhost)
            except Exception as exc:
                handler._json(404, {"error": "not_found", "reason": str(exc)})
                return
            handler._json(204, {})
        else:
            handler._json(405, {"error": "method_not_allowed"})

    # ---- 健康与指标 ---------------------------------------------------
    def _health(self, handler: Any, user: Any) -> None:
        overview = self.broker.overview()
        alarms = overview["alarms"]
        ok = not alarms
        status = 200 if ok else 503
        handler._json(
            status,
            {
                "status": "ok" if ok else "failed",
                "alarms": alarms,
                "node": self.broker.node_name,
                "checked_by": user.name,
            },
        )

    def _metrics(self, handler: Any, user: Any) -> None:
        lines: list[str] = []
        for vhost in self._accessible_vhosts(user):
            for queue in self.broker.vhosts[vhost].queues.values():
                labels = f'vhost="{_label(vhost)}",queue="{_label(queue.name)}"'
                lines.append(
                    f'mini_rabbitmq_queue_messages_ready{{{labels}}} {queue.messages_ready}'
                )
                lines.append(
                    f'mini_rabbitmq_queue_messages_unacked{{{labels}}} {queue.messages_unacknowledged}'
                )
                lines.append(
                    f'mini_rabbitmq_queue_consumers{{{labels}}} {queue.consumer_count}'
                )
        text = self.broker.metrics.render_prometheus(extra_lines=lines)
        handler._text(200, text, "text/plain; version=0.0.4; charset=utf-8")

    # ---- 授权辅助 -----------------------------------------------------
    def _vhost_allowed(self, handler: Any, user: Any, vhost: str) -> bool:
        if not self.auth.can_access_vhost(user.name, vhost):
            handler._json(403, {"error": "access_refused", "vhost": vhost})
            return False
        return True

    def _authorized(
        self, handler: Any, user: Any, vhost: str, action: str, resource: str
    ) -> bool:
        if not self.auth.authorize(user.name, vhost, action, resource):
            handler._json(403, {"error": "access_refused", "vhost": vhost})
            return False
        return True
