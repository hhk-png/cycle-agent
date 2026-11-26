"""Alertmanager notifier (v2 API).

Alerts are queued and delivered by a background worker.  Failures are retried
with exponential backoff; 4xx responses (other than 429) are treated as
permanent and dropped, which is the same practical rule Alertmanager clients
use to avoid retry storms caused by malformed alerts.
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from ..config import AlertmanagerConfig
from ..util.timeutil import format_timestamp


@dataclass
class Notification:
    alerts: list[dict]
    attempts: int = 0
    next_attempt: float = 0.0


Sender = Callable[[str, dict[str, str], bytes, float], int]


class AlertmanagerNotifier:
    def __init__(
        self,
        configurations: list[AlertmanagerConfig],
        *,
        sender: Sender | None = None,
        max_retries: int = 5,
        clock: Callable[[], float] = time.time,
        start_worker: bool = True,
    ) -> None:
        self.configurations = configurations
        self.sender = sender or _default_sender
        self.max_retries = max_retries
        self.clock = clock
        self._queue: queue.Queue[Notification] = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.stats = {"sent": 0, "failed": 0, "dropped": 0, "retried": 0}
        if start_worker and configurations:
            self._worker = threading.Thread(
                target=self._run, name="alertmanager-notifier", daemon=True
            )
            self._worker.start()

    def enqueue(self, alerts: list[dict]) -> None:
        if not alerts or not self.configurations:
            return
        self._queue.put(Notification(alerts))

    def stop(self) -> None:
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)

    def flush(self) -> None:
        """Synchronously deliver everything currently queued (tests/labs)."""
        while True:
            try:
                notification = self._queue.get_nowait()
            except queue.Empty:
                return
            self._deliver(notification)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                notification = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._deliver(notification)

    def _deliver(self, notification: Notification) -> None:
        payload = json.dumps(notification.alerts).encode("utf-8")
        for config in self.configurations:
            url = _alertmanager_url(config)
            headers = {"Content-Type": "application/json"}
            if config.basic_auth and config.basic_auth.username:
                token = base64.b64encode(
                    f"{config.basic_auth.username}:{config.basic_auth.password}".encode()
                ).decode()
                headers["Authorization"] = f"Basic {token}"
            try:
                status = self.sender(
                    url, headers, payload, _seconds(config.timeout)
                )
            except Exception:
                status = 0
            if 200 <= status < 300:
                self.stats["sent"] += 1
                continue
            if 400 <= status < 500 and status != 429:
                self.stats["dropped"] += 1
                continue
            notification.attempts += 1
            if notification.attempts > self.max_retries:
                self.stats["dropped"] += 1
                continue
            self.stats["failed"] += 1
            self.stats["retried"] += 1
            delay = min(2 ** notification.attempts, 30)
            timer = threading.Timer(delay, self._queue.put, args=(notification,))
            timer.daemon = True
            timer.start()


def _alertmanager_url(config: AlertmanagerConfig) -> str:
    target = config.targets[0] if config.targets else "localhost:9093"
    if "://" in target:
        base = target
    else:
        base = f"{config.scheme}://{target}"
    prefix = config.path_prefix.rstrip("/")
    version = "v1" if config.api_version == "v1" else "v2"
    return f"{base}{prefix}/api/{version}/alerts"


def _seconds(value: str) -> float:
    from ..util.timeutil import parse_duration

    return parse_duration(value) / 1000.0


def _default_sender(url: str, headers: dict[str, str], body: bytes, timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def alert_payload(
    labels: dict[str, str],
    annotations: dict[str, str],
    *,
    starts_at: int,
    ends_at: int,
    generator_url: str = "",
    value: float | None = None,
) -> dict:
    payload = {
        "labels": labels,
        "annotations": annotations,
        "startsAt": format_timestamp(starts_at),
        "endsAt": format_timestamp(ends_at),
        "generatorURL": generator_url,
    }
    return payload
