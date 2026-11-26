"""Scrape manager: discovery -> relabel -> fetch -> parse -> append.

One thread per target mirrors Prometheus' scrape pool.  Tests call
``scrape_once`` directly so the whole path is deterministic and no wall clock
or network is involved.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .. import USER_AGENT
from ..config import Config, ScrapeConfig
from ..exposition import ExpositionError, parse_exposition
from ..labels import Labels
from ..model import MetricMetadata
from ..util.timeutil import format_timestamp, now_ms, parse_duration, stale_nan
from ..tsdb.tsdb import TSDB
from .discovery import DiscoveredTarget, DiscoveryManager
from .relabel import relabel_samples

UP = "up"
SCRAPE_DURATION = "scrape_duration_seconds"
SCRAPE_SAMPLES = "scrape_samples_scraped"
SCRAPE_SAMPLES_POST = "scrape_samples_post_metric_relabeling"
SCRAPE_SERIES_ADDED = "scrape_series_added"
SCRAPE_TIMEOUT = "scrape_timeout_seconds"
SCRAPE_BODY_SIZE = "scrape_body_size_bytes"


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Fetcher = Callable[[str, dict[str, str], float], HttpResponse | tuple]


@dataclass
class ScrapeReport:
    target: str
    job: str
    status: str
    error: str = ""
    duration: float = 0.0
    samples: int = 0
    series_added: int = 0
    timestamp: int = 0
    body_size: int = 0

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "job": self.job,
            "status": self.status,
            "error": self.error,
            "duration": self.duration,
            "samples": self.samples,
            "seriesAdded": self.series_added,
            "timestamp": self.timestamp,
        }


@dataclass
class TargetState:
    job_name: str
    labels: dict[str, str]
    scrape_config: ScrapeConfig
    url: str
    health: str = "unknown"
    last_error: str = ""
    last_scrape: int = 0
    last_duration: float = 0.0
    last_samples: int = 0
    scrape_count: int = 0
    failure_count: int = 0
    series: set[Labels] = field(default_factory=set)
    metadata: dict[str, MetricMetadata] = field(default_factory=dict)
    exemplars: list = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    @property
    def key(self) -> str:
        return target_key(self.job_name, self.labels)

    def target_labels(self) -> Labels:
        return Labels(
            (name, value)
            for name, value in self.labels.items()
            if not name.startswith("__") and value
        )

    def display_url(self) -> str:
        return self.url

    def to_dict(self) -> dict:
        return {
            "discoveredLabels": {
                name: value for name, value in self.labels.items()
            },
            "labels": self.target_labels().to_dict(),
            "scrapePool": self.job_name,
            "scrapeUrl": self.url,
            "health": self.health,
            "lastError": self.last_error,
            "lastScrape": format_timestamp(self.last_scrape) if self.last_scrape else "",
            "lastScrapeDuration": self.last_duration,
            "lastScrapeTimestamp": self.last_scrape,
            "lastSamples": self.last_samples,
            "scrapeCount": self.scrape_count,
            "failureCount": self.failure_count,
        }


def target_key(job: str, labels: dict[str, str]) -> str:
    material = job + "\xff" + "\xfe".join(
        f"{name}={value}" for name, value in sorted(labels.items())
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


class ScrapeManager:
    def __init__(
        self,
        tsdb: TSDB,
        config: Config,
        *,
        base_dir: str = ".",
        fetcher: Fetcher | None = None,
        clock: Callable[[], float] = time.time,
        start_threads: bool = True,
        sync_interval: float = 15.0,
    ) -> None:
        self.tsdb = tsdb
        self.config = config
        self.base_dir = base_dir
        self.fetcher = fetcher
        self.clock = clock
        self.start_threads = start_threads
        self.sync_interval = sync_interval
        self.discovery = DiscoveryManager(base_dir=base_dir, fetcher=self._discovery_fetcher)
        self._targets: dict[str, TargetState] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sync_thread: threading.Thread | None = None
        self.stats = {
            "scrapes": 0,
            "failures": 0,
            "samplesAppended": 0,
            "targetSyncs": 0,
        }

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self.sync(now=now_ms())
            self._stop.clear()
            if self.start_threads:
                for target in self._targets.values():
                    self._start_target_thread(target)
                self._sync_thread = threading.Thread(
                    target=self._sync_loop, name="discovery-sync", daemon=True
                )
                self._sync_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            targets = list(self._targets.values())
        for target in targets:
            target.stop_event.set()
        for target in targets:
            if target.thread and target.thread.is_alive():
                target.thread.join(timeout=5)
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5)

    def _sync_loop(self) -> None:
        while not self._stop.wait(self.sync_interval):
            try:
                self.sync(now=now_ms())
            except Exception:
                # A discovery error must not kill the scheduler; the API shows
                # the last successful target set and logs the error.
                continue

    def reload(self, config: Config) -> None:
        with self._lock:
            for target in list(self._targets.values()):
                self._stale_target(target, now_ms())
            self.stop_targets()
            self.config = config
            self._targets.clear()
            self.sync(now=now_ms())
            if self.start_threads:
                for target in self._targets.values():
                    self._start_target_thread(target)
        self.stats["targetSyncs"] += 1

    def stop_targets(self) -> None:
        for target in list(self._targets.values()):
            target.stop_event.set()
        for target in list(self._targets.values()):
            if target.thread and target.thread.is_alive():
                target.thread.join(timeout=5)

    # ---- discovery -----------------------------------------------------
    def sync(self, now: int | None = None) -> tuple[list[TargetState], list[TargetState]]:
        now = now_ms() if now is None else now
        desired: dict[str, tuple[ScrapeConfig, DiscoveredTarget]] = {}
        for job in self.config.scrape_configs:
            for discovered in self.discovery.targets_for(job):
                key = target_key(job.job_name, discovered.labels)
                desired[key] = (job, discovered)
        added: list[TargetState] = []
        removed: list[TargetState] = []
        with self._lock:
            for key in list(self._targets):
                if key not in desired:
                    target = self._targets.pop(key)
                    target.stop_event.set()
                    if target.thread and target.thread.is_alive():
                        target.thread.join(timeout=5)
                    self._stale_target(target, now)
                    removed.append(target)
            for key, (job, discovered) in desired.items():
                if key in self._targets:
                    continue
                url = _build_url(job, discovered.labels)
                target = TargetState(
                    job.job_name, dict(discovered.labels), job, url
                )
                self._targets[key] = target
                if self.start_threads:
                    self._start_target_thread(target)
                added.append(target)
            self.stats["targetSyncs"] += 1
        return added, removed

    def targets(self) -> list[TargetState]:
        with self._lock:
            return sorted(self._targets.values(), key=lambda item: item.key)

    def get_target(self, key: str) -> TargetState | None:
        return self._targets.get(key)

    def _start_target_thread(self, target: TargetState) -> None:
        if target.thread and target.thread.is_alive():
            return
        target.stop_event.clear()
        target.thread = threading.Thread(
            target=self._target_loop,
            args=(target,),
            name=f"scrape-{target.job_name}",
            daemon=True,
        )
        target.thread.start()

    def _target_loop(self, target: TargetState) -> None:
        interval = parse_duration(target.scrape_config.scrape_interval) / 1000.0
        next_start = self.clock()
        while not target.stop_event.is_set():
            wait = next_start - self.clock()
            if wait > 0 and target.stop_event.wait(wait):
                return
            start_wall = self.clock()
            try:
                self.scrape_once(target, timestamp=int(start_wall * 1000))
            except Exception:
                pass
            next_start = start_wall + interval
            if next_start < self.clock() - interval:
                next_start = self.clock()

    # ---- scraping ------------------------------------------------------
    def scrape_once(
        self, target: TargetState | str, *, timestamp: int | None = None
    ) -> ScrapeReport:
        if isinstance(target, str):
            state = self._targets.get(target)
            if state is None:
                raise KeyError(f"unknown target {target}")
            target = state
        start_ms = now_ms() if timestamp is None else timestamp
        start = self.clock()
        config = target.scrape_config
        timeout = parse_duration(config.scrape_timeout) / 1000.0
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/plain;version=0.0.4;q=0.7,application/openmetrics-text;version=1.0.0;q=0.5,*/*;q=0.1",
            "Accept-Encoding": "identity",
        }
        if config.bearer_token:
            headers["Authorization"] = f"Bearer {config.bearer_token}"
        elif config.bearer_token_file:
            try:
                token = open(config.bearer_token_file, encoding="utf-8").read().strip()
                headers["Authorization"] = f"Bearer {token}"
            except OSError as exc:
                return self._failure(target, start_ms, start, f"cannot read bearer token: {exc}")
        elif config.basic_auth:
            password = config.basic_auth.password
            if config.basic_auth.password_file:
                try:
                    password = open(config.basic_auth.password_file, encoding="utf-8").read().rstrip("\n")
                except OSError as exc:
                    return self._failure(target, start_ms, start, f"cannot read password: {exc}")
            credentials = base64.b64encode(
                f"{config.basic_auth.username}:{password}".encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {credentials}"
        try:
            response = self._fetch(target.url, headers, timeout, config)
        except Exception as exc:
            return self._failure(target, start_ms, start, _short_error(exc))
        elapsed = max(self.clock() - start, 0.0)
        if config.body_size_limit and len(response.body) > config.body_size_limit:
            return self._failure(
                target, start_ms, start, f"body size {len(response.body)} exceeds limit", elapsed, len(response.body)
            )
        if response.status not in (200,):
            return self._failure(
                target, start_ms, start, f"server returned HTTP status {response.status}", elapsed, len(response.body)
            )
        content_type = response.headers.get("content-type", response.headers.get("Content-Type", ""))
        openmetrics = content_type.startswith("application/openmetrics-text") or (
            b"# EOF" in response.body
        )
        try:
            text = response.body.decode("utf-8", errors="replace")
            payload = parse_exposition(
                text,
                default_timestamp=start_ms,
                openmetrics=openmetrics,
            )
        except (ExpositionError, UnicodeDecodeError) as exc:
            return self._failure(target, start_ms, start, f"parse error: {exc}", elapsed, len(response.body))
        target_labels = target.target_labels()
        merged_samples: list[tuple[Labels, int, float]] = []
        seen: set[Labels] = set()
        for sample in payload.samples:
            labels = self._merge_labels(sample.labels, target_labels, config.honor_labels)
            if config.metric_relabel_configs:
                labels = relabel_samples(labels, config.metric_relabel_configs)
                if labels is None:
                    continue
            if not labels.get("__name__"):
                continue
            if config.label_limit and len(labels) > config.label_limit:
                return self._failure(
                    target, start_ms, start, f"label limit exceeded ({config.label_limit})", elapsed, len(response.body)
                )
            if config.label_name_length_limit and any(
                len(name) > config.label_name_length_limit for name, _ in labels.items()
            ):
                return self._failure(target, start_ms, start, "label name length limit exceeded")
            if config.label_value_length_limit and any(
                len(value) > config.label_value_length_limit for _, value in labels.items()
            ):
                return self._failure(target, start_ms, start, "label value length limit exceeded")
            sample_ts = sample.timestamp if config.honor_timestamps and sample.timestamp is not None else start_ms
            merged_samples.append((labels, sample_ts, sample.value))
            seen.add(labels)
        # Prometheus enforces sample_limit after metric relabeling, not on the
        # raw exposition.  Dropping samples with metric_relabel_configs must
        # not fail the scrape.
        if config.sample_limit and len(merged_samples) > config.sample_limit:
            return self._failure(
                target,
                start_ms,
                start,
                f"sample limit exceeded ({config.sample_limit}); scrape aborted",
                elapsed,
                len(response.body),
            )
        appended, errors = self.tsdb.append_batch(merged_samples)
        duration = max(self.clock() - start, 0.0)
        series_added = len(seen - target.series)
        summary = [
            # ``up`` reflects scrape success (fetch + parse), not whether
            # every sample was accepted.  Out-of-order/duplicate samples are
            # append errors and must not turn up into 0.
            (UP, 1.0),
            (SCRAPE_DURATION, duration),
            (SCRAPE_SAMPLES, float(len(payload.samples))),
            (SCRAPE_SAMPLES_POST, float(len(merged_samples))),
            (SCRAPE_SERIES_ADDED, float(series_added)),
            (SCRAPE_TIMEOUT, timeout),
            (SCRAPE_BODY_SIZE, float(len(response.body))),
        ]
        self.tsdb.append_batch(
            [
                (target_labels.set("__name__", name), start_ms, value)
                for name, value in summary
            ]
        )
        if target.series:
            for stale_labels in target.series - seen:
                self.tsdb.append(stale_labels, start_ms, stale_nan())
        target.series = seen
        target.metadata = payload.metadata
        target.exemplars = []
        for series_labels, exemplar in payload.exemplars:
            merged = self._merge_labels(series_labels, target_labels, config.honor_labels)
            target.exemplars.append((merged, exemplar))
        target.health = "up"
        # Append errors are warnings, not scrape failures; upstream keeps
        # lastError empty and exposes the errors through counters.
        target.last_error = ""
        target.last_scrape = start_ms
        target.last_duration = duration
        target.last_samples = len(merged_samples)
        target.scrape_count += 1
        self.stats["scrapes"] += 1
        self.stats["samplesAppended"] += appended
        if errors:
            self.stats["appendErrors"] = self.stats.get("appendErrors", 0) + len(errors)
        return ScrapeReport(
            target.url,
            target.job_name,
            "success",
            "; ".join(errors[:3]) if errors else "",
            duration,
            len(merged_samples),
            series_added,
            start_ms,
            len(response.body),
        )

    def _merge_labels(self, scraped: Labels, target: Labels, honor_labels: bool) -> Labels:
        result = scraped.to_dict()
        for name, value in target.items():
            if honor_labels:
                if name not in result:
                    result[name] = value
                continue
            if name == "__name__":
                continue
            if name in result:
                # Upstream renames the scraped label whenever the target label
                # exists, even if both values are equal.  Keep prefixing until
                # the name is free (exported_job -> exported_exported_job).
                exported = f"exported_{name}"
                while exported in result:
                    exported = f"exported_{exported}"
                result[exported] = result.pop(name)
            result[name] = value
        return Labels(result)

    def _failure(
        self,
        target: TargetState,
        start_ms: int,
        start: float,
        error: str,
        duration: float | None = None,
        body_size: int = 0,
    ) -> ScrapeReport:
        elapsed = max(self.clock() - start, 0.0) if duration is None else duration
        target.health = "down"
        target.last_error = error
        target.last_scrape = start_ms
        target.last_duration = elapsed
        target.last_samples = 0
        target.scrape_count += 1
        target.failure_count += 1
        self.stats["scrapes"] += 1
        self.stats["failures"] += 1
        labels = target.target_labels()
        self.tsdb.append_batch(
            [
                (labels.set("__name__", UP), start_ms, 0.0),
                (labels.set("__name__", SCRAPE_DURATION), start_ms, elapsed),
                (labels.set("__name__", SCRAPE_BODY_SIZE), start_ms, float(body_size)),
            ]
        )
        # A failed scrape is treated like an empty scrape: previously seen
        # series get a stale marker so they disappear immediately.  Without
        # this, old values linger until lookback and hide the failure.
        if target.series:
            for series_labels in target.series:
                self.tsdb.append(series_labels, start_ms, stale_nan())
            target.series.clear()
        return ScrapeReport(
            target.url, target.job_name, "error", error, elapsed, 0, 0, start_ms, body_size
        )

    def _stale_target(self, target: TargetState, timestamp: int) -> None:
        if not target.series:
            return
        for labels in target.series:
            self.tsdb.append(labels, timestamp, stale_nan())
        target.series.clear()

    def _fetch(
        self, url: str, headers: dict[str, str], timeout: float, config: ScrapeConfig
    ) -> HttpResponse:
        if self.fetcher is not None:
            result = self.fetcher(url, headers, timeout)
            if isinstance(result, HttpResponse):
                return result
            if len(result) == 2:
                return HttpResponse(int(result[0]), {}, bytes(result[1]))
            return HttpResponse(int(result[0]), dict(result[1]), bytes(result[2]))
        context = _ssl_context(config)
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                body = response.read(
                    config.body_size_limit + 1 if config.body_size_limit else -1
                )
                return HttpResponse(
                    response.status,
                    {name.lower(): value for name, value in response.headers.items()},
                    body,
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                exc.code,
                {name.lower(): value for name, value in (exc.headers or {}).items()},
                exc.read() if exc.fp else b"",
            )

    def _discovery_fetcher(self, url: str, headers: dict[str, str], timeout: float):
        if self.fetcher is not None:
            result = self.fetcher(url, headers, timeout)
            if isinstance(result, HttpResponse):
                return result.status, result.body
            if len(result) == 2:
                return int(result[0]), bytes(result[1])
            return int(result[0]), bytes(result[2])
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()

    # ---- introspection -------------------------------------------------
    def metrics(self) -> str:
        lines = [
            "# HELP prometheus_target_scrapes_total Total number of scrapes.",
            "# TYPE prometheus_target_scrapes_total counter",
        ]
        for target in self.targets():
            labels = target.target_labels()
            label_text = _label_string(labels)
            lines.append(
                f"prometheus_target_scrapes_total{label_text} {target.scrape_count}"
            )
            lines.append(f"up{label_text} {1 if target.health == 'up' else 0}")
        lines.append("# HELP prometheus_target_scrape_pool_targets Current targets per pool.")
        lines.append("# TYPE prometheus_target_scrape_pool_targets gauge")
        for job in self.config.scrape_configs:
            count = sum(1 for target in self.targets() if target.job_name == job.job_name)
            lines.append(
                f'prometheus_target_scrape_pool_targets{{job="{job.job_name}"}} {count}'
            )
        return "\n".join(lines) + "\n"


def _build_url(job: ScrapeConfig, labels: dict[str, str]) -> str:
    scheme = labels.get("__scheme__", job.scheme)
    address = labels.get("__address__", "")
    path = labels.get("__metrics_path__", job.metrics_path)
    params: dict[str, list[str]] = {name: list(values) for name, values in job.params.items()}
    for name, value in labels.items():
        if not name.startswith("__param_"):
            continue
        key = name[len("__param_") :]
        configured = params.get(key)
        # Discovery exposes only the first configured value as a label.  If
        # relabeling did not change it, keep all configured values.
        if configured is None or configured[:1] != [value]:
            params[key] = [value]
    query = urllib.parse.urlencode(params, doseq=True)
    return f"{scheme}://{address}{path}" + (f"?{query}" if query else "")


def _ssl_context(config: ScrapeConfig) -> ssl.SSLContext | None:
    tls = config.tls_config
    if not (tls.ca_file or tls.cert_file or tls.insecure_skip_verify or tls.server_name):
        return None
    context = ssl.create_default_context(cafile=tls.ca_file or None)
    if tls.cert_file:
        context.load_cert_chain(tls.cert_file, tls.key_file or None)
    if tls.insecure_skip_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _short_error(exc: Exception) -> str:
    if isinstance(exc, socket.timeout):
        return "context deadline exceeded"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__


def _label_string(labels: Labels) -> str:
    if not labels:
        return ""
    body = ",".join(f'{name}="{value}"' for name, value in labels.items())
    return "{" + body + "}"
