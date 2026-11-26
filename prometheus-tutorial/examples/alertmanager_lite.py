"""A small Alertmanager-compatible receiver for the examples.

Implements the v2 alerts endpoint, deduplication by fingerprint, grouping by
alertname, and a simple inhibition rule: a critical alert inhibits warnings
with the same ``service`` label.  It is a teaching aid, not a replacement for
Alertmanager (no silences, HA gossip, or real notification integrations).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/v2/alerts":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            alerts = json.loads(self.rfile.read(length) or b"[]")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        receiver: AlertmanagerLite = self.server.receiver  # type: ignore[attr-defined]
        for alert in alerts:
            receiver.receive(alert)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        receiver: AlertmanagerLite = self.server.receiver  # type: ignore[attr-defined]
        if self.path == "/api/v2/alerts":
            self._json(receiver.active())
        elif self.path == "/-/ready":
            self._json({"status": "ready"})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


class AlertmanagerLite:
    def __init__(self, port: int = 0) -> None:
        self.port = port
        self.alerts: dict[str, dict] = {}
        self.history: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self._server.receiver = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="alertmanager-lite", daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def receive(self, alert: dict) -> None:
        labels = alert.get("labels", {})
        fingerprint = alert.get("fingerprint") or _fingerprint(labels)
        alert["fingerprint"] = fingerprint
        alert["receivedAt"] = time.time()
        self.history.append(alert)
        if _resolved(alert):
            self.alerts.pop(fingerprint, None)
        else:
            self.alerts[fingerprint] = alert

    def active(self) -> list[dict]:
        return [alert for alert in self.alerts.values() if not _resolved(alert)]

    def groups(self) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for alert in self.active():
            grouped.setdefault(alert["labels"].get("alertname", ""), []).append(alert)
        return grouped

    def inhibited(self) -> list[tuple[dict, dict]]:
        """Return (warning, critical) pairs where critical suppresses warning."""
        out = []
        actives = self.active()
        for warning in actives:
            if warning["labels"].get("severity") != "warning":
                continue
            for critical in actives:
                if critical["labels"].get("severity") != "critical":
                    continue
                if (
                    critical["labels"].get("service")
                    and critical["labels"].get("service") == warning["labels"].get("service")
                ):
                    out.append((warning, critical))
                    break
        return out


def _fingerprint(labels: dict[str, str]) -> str:
    import hashlib

    material = "\xff".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _resolved(alert: dict) -> bool:
    ends_at = alert.get("endsAt", "")
    if not ends_at:
        return False
    try:
        import datetime

        endpoint = datetime.datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    return endpoint < now
