"""Application assembly: config -> TSDB -> scrape -> rules -> HTTP."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .api.server import PrometheusServer
from .config import Config, load_config
from .otlp import OTLPOptions, OTLPReceiver, STRATEGY_NO_UTF8
from .promql.engine import Engine
from .rules.manager import RuleManager, load_rule_groups
from .rules.notifier import AlertmanagerNotifier
from .remote_client import RemoteWriter
from .scrape.manager import ScrapeManager
from .tsdb.tsdb import TSDB, TSDBOptions
from .util.timeutil import now_ms, parse_duration


@dataclass
class AppOptions:
    config_file: str = "prometheus.yml"
    data_dir: str = "data"
    retention: str = "15d"
    listen_address: str = "127.0.0.1:9090"
    enable_admin_api: bool = False
    enable_lifecycle: bool = True
    log_requests: bool = True
    start_threads: bool = True
    block_range: str = "2h"
    enable_otlp_receiver: bool = False
    otlp_translation_strategy: str = STRATEGY_NO_UTF8
    otlp_delta_to_cumulative: bool = False


class PrometheusApp:
    def __init__(self, options: AppOptions) -> None:
        self.options = options
        self.config_path = Path(options.config_file)
        self.retention = options.retention
        self.start_time = now_ms()
        self.last_reload_ok = True
        self._lock = threading.RLock()
        self._stopped = False
        self.host, self.port = parse_listen_address(options.listen_address)
        self.tsdb: TSDB | None = None
        self.scrape: ScrapeManager | None = None
        self.rules: RuleManager | None = None
        self.notifier: AlertmanagerNotifier | None = None
        self.engine: Engine | None = None
        self.server: PrometheusServer | None = None
        self.config: Config | None = None
        self.remote_writer: RemoteWriter | None = None
        self.otlp: OTLPReceiver | None = None
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> tuple[str, int]:
        with self._lock:
            self.config = load_config(self.config_path)
            retention_ms = parse_duration(self.retention)
            block_range_ms = parse_duration(self.options.block_range)
            self.tsdb = TSDB(
                TSDBOptions(
                    path=self.options.data_dir,
                    retention_ms=retention_ms,
                    block_range_ms=block_range_ms,
                )
            ).open()
            if self.options.enable_otlp_receiver:
                self.otlp = OTLPReceiver(
                    OTLPOptions(
                        translation_strategy=self.options.otlp_translation_strategy,
                        delta_to_cumulative=self.options.otlp_delta_to_cumulative,
                    )
                )
            base_dir = str(self.config_path.parent or Path("."))
            self.scrape = ScrapeManager(
                self.tsdb,
                self.config,
                base_dir=base_dir,
                start_threads=self.options.start_threads,
            )
            groups = load_rule_groups(
                self.config.rule_files,
                base_dir=base_dir,
                evaluation_interval_ms=parse_duration(
                    self.config.global_config.evaluation_interval
                ),
                default_query_offset_ms=parse_duration(
                    self.config.global_config.rule_query_offset
                ),
            )
            self.notifier = AlertmanagerNotifier(
                self.config.alerting, start_worker=self.options.start_threads
            )
            self.engine = Engine(
                self.tsdb,
                lookback_ms=parse_duration(
                    self.config.global_config.query_lookback_delta
                ),
                default_step_ms=parse_duration(
                    self.config.global_config.evaluation_interval
                ),
            )
            if self.config.remote_write:
                self.remote_writer = RemoteWriter(
                    self.config.remote_write,
                    start_worker=self.options.start_threads,
                )
                self.tsdb.add_watcher(self.remote_writer.observe)
            self.rules = RuleManager(
                self.tsdb,
                groups,
                evaluation_interval_ms=parse_duration(
                    self.config.global_config.evaluation_interval
                ),
                external_labels=self.config.global_config.external_labels,
                notifier=self.notifier,
                lookback_ms=parse_duration(
                    self.config.global_config.query_lookback_delta
                ),
            )
            self.server = PrometheusServer(self, host=self.host, port=self.port)
            address = self.server.start()
            if self.rules:
                self.rules.generator_url = f"http://{address[0]}:{address[1]}/graph"
            if self.scrape is not None:
                # Even without scrape threads we still need discovery state so
                # /targets, /metrics and manual scrape_once work.
                self.scrape.start()
            if self.rules and self.options.start_threads:
                self.rules.start()
            if self.options.start_threads:
                self._maintenance_thread = threading.Thread(
                    target=self._maintenance_loop,
                    name="tsdb-maintenance",
                    daemon=True,
                )
                self._maintenance_thread.start()
            return address

    def _maintenance_loop(self) -> None:
        while not self._maintenance_stop.wait(60):
            try:
                if self.tsdb is not None:
                    self.tsdb.compact()
            except Exception:
                continue

    def reload(self) -> None:
        with self._lock:
            if self._stopped or self.tsdb is None:
                raise RuntimeError("server is not running")
            new_config = load_config(self.config_path)
            base_dir = str(self.config_path.parent or Path("."))
            if new_config.rule_files:
                groups = load_rule_groups(
                    new_config.rule_files,
                    base_dir=base_dir,
                    evaluation_interval_ms=parse_duration(
                        new_config.global_config.evaluation_interval
                    ),
                    default_query_offset_ms=parse_duration(
                        new_config.global_config.rule_query_offset
                    ),
                )
            else:
                groups = []
            self.scrape.reload(new_config)
            self.rules.reload(groups)
            if self.notifier is not None:
                self.notifier.configurations = new_config.alerting
            self.config = new_config
            self.engine.lookback_ms = parse_duration(
                new_config.global_config.query_lookback_delta
            )
            self.rules.external_labels = new_config.global_config.external_labels
            if self.remote_writer is not None:
                self.remote_writer.configurations = new_config.remote_write
            elif new_config.remote_write and self.tsdb is not None:
                self.remote_writer = RemoteWriter(
                    new_config.remote_write,
                    start_worker=self.options.start_threads,
                )
                self.tsdb.add_watcher(self.remote_writer.observe)
            self.last_reload_ok = True

    def shutdown(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self._maintenance_stop.set()
        if self._maintenance_thread and self._maintenance_thread.is_alive():
            self._maintenance_thread.join(timeout=5)
        if self.server is not None:
            self.server.stop()
        if self.rules is not None:
            self.rules.stop()
        if self.notifier is not None:
            self.notifier.stop()
        if self.scrape is not None:
            self.scrape.stop()
        if self.remote_writer is not None:
            self.remote_writer.stop()
        if self.tsdb is not None:
            self.tsdb.close()

    def wait(self) -> None:
        if self.server is not None:
            self.server.wait()

    def url(self) -> str:
        if self.server is None:
            return f"http://{self.host}:{self.port}"
        host, port = self.server.server_address
        return f"http://{host}:{port}"


def parse_listen_address(value: str) -> tuple[str, int]:
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else "9090"
        return host, int(port)
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host or "0.0.0.0", int(port)
    return value, 9090
