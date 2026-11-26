"""Outgoing remote-write client.

The TSDB notifies a writer with every successfully appended sample.  Samples
are queued, batched, and delivered as either the tutorial's JSON envelope or
the real Prometheus protobuf+Snappy format.  Delivery failures are retried
with exponential backoff; permanent 4xx errors drop the batch.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .config import RemoteWriteConfig
from .labels import Labels
from .remote import encode_write_request, snappy_compress


@dataclass
class _Batch:
    samples: list[tuple[Labels, int, float]]
    attempts: int = 0


Sender = Callable[[str, dict[str, str], bytes, float], int]


class RemoteWriter:
    def __init__(
        self,
        configurations: list[RemoteWriteConfig],
        *,
        sender: Sender | None = None,
        start_worker: bool = True,
        flush_interval: float = 1.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.configurations = configurations
        self.sender = sender or _default_sender
        self.flush_interval = flush_interval
        self.clock = clock
        self._queue: queue.Queue[_Batch] = queue.Queue()
        self._pending: list[tuple[Labels, int, float]] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.stats = {"queued": 0, "sent": 0, "failed": 0, "dropped": 0, "retried": 0}
        if start_worker and configurations:
            self._worker = threading.Thread(
                target=self._run, name="remote-write", daemon=True
            )
            self._worker.start()

    # ---- ingestion -----------------------------------------------------
    def observe(self, samples: list[tuple[Labels, int, float]]) -> None:
        if not self.configurations:
            return
        with self._lock:
            self._pending.extend(samples)
            self.stats["queued"] += len(samples)
            if len(self._pending) >= min(
                config.batch_size for config in self.configurations
            ):
                self._queue.put(_Batch(self._pending))
                self._pending = []

    def flush(self) -> None:
        with self._lock:
            if self._pending:
                self._queue.put(_Batch(self._pending))
                self._pending = []
        while True:
            try:
                batch = self._queue.get_nowait()
            except queue.Empty:
                return
            self._deliver(batch)

    def stop(self) -> None:
        self._stop.set()
        self.flush()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.flush_interval)
            self.flush()

    # ---- delivery ------------------------------------------------------
    def _deliver(self, batch: _Batch) -> None:
        for config in self.configurations:
            body = _encode(config, batch.samples)
            headers = {"Content-Type": "application/json"}
            if config.protocol == "prometheus":
                headers = {
                    "Content-Type": "application/x-protobuf",
                    "Content-Encoding": "snappy",
                }
            headers.update(config.headers)
            try:
                status = self.sender(config.url, headers, body, 30.0)
            except Exception:
                status = 0
            if 200 <= status < 300:
                self.stats["sent"] += len(batch.samples)
                continue
            if 400 <= status < 500 and status != 429:
                self.stats["dropped"] += len(batch.samples)
                continue
            batch.attempts += 1
            if batch.attempts > config.max_retries:
                self.stats["dropped"] += len(batch.samples)
                continue
            self.stats["failed"] += 1
            self.stats["retried"] += len(batch.samples)
            timer = threading.Timer(
                min(2 ** batch.attempts, 30), self._queue.put, args=(batch,)
            )
            timer.daemon = True
            timer.start()


def _encode(config: RemoteWriteConfig, samples: list[tuple[Labels, int, float]]) -> bytes:
    grouped: dict[Labels, list[tuple[int, float]]] = {}
    for labels, timestamp, value in samples:
        grouped.setdefault(labels, []).append((timestamp, value))
    if config.protocol == "prometheus":
        return snappy_compress(
            encode_write_request(
                [(labels, points) for labels, points in grouped.items()]
            )
        )
    return json.dumps(
        {
            "timeseries": [
                {
                    "labels": labels.to_dict(),
                    "samples": [[timestamp, value] for timestamp, value in points],
                }
                for labels, points in grouped.items()
            ]
        }
    ).encode("utf-8")


def _default_sender(url: str, headers: dict[str, str], body: bytes, timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0
