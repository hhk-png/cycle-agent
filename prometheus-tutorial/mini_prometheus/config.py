"""Configuration parsing and validation.

The subset mirrors Prometheus' YAML closely enough that the examples in the
tutorial are portable: global defaults, scrape_configs with static/file/http/
dns discovery, relabeling, rule_files, alerting and remote storage.
"""

from __future__ import annotations

import glob as globlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import ConfigError, MetricType
from .util.timeutil import parse_duration

SUPPORTED_SD = {"static_configs", "file_sd_configs", "http_sd_configs", "dns_sd_configs"}
RELABLE_ACTIONS = {
    "replace", "keep", "drop", "hashmod", "labelmap", "labeldrop", "labelkeep",
    "lowercase", "uppercase", "keepequal", "dropequal",
}
DEFAULT_SCRAPE_INTERVAL = "1m"
DEFAULT_SCRAPE_TIMEOUT = "10s"
DEFAULT_EVALUATION_INTERVAL = "1m"


@dataclass
class TLSConfig:
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    server_name: str = ""
    insecure_skip_verify: bool = False


@dataclass
class BasicAuth:
    username: str = ""
    password: str = ""
    password_file: str = ""


@dataclass
class RelabelConfig:
    source_labels: list[str] = field(default_factory=list)
    separator: str = ";"
    target_label: str = ""
    regex: str = "(.*)"
    replacement: str = "$1"
    modulus: int = 0
    action: str = "replace"

    @classmethod
    def from_dict(cls, data: dict) -> "RelabelConfig":
        if not isinstance(data, dict):
            raise ConfigError("relabel_configs entries must be mappings")
        action = str(data.get("action", "replace")).lower()
        if action not in RELABLE_ACTIONS:
            raise ConfigError(f"unknown relabel action {action!r}")
        sources = data.get("source_labels", []) or []
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            raise ConfigError("relabel source_labels must be a list of strings")
        modulus = int(data.get("modulus", 0))
        if action == "hashmod" and modulus <= 0:
            raise ConfigError("hashmod requires a positive modulus")
        target = str(data.get("target_label", "") or "")
        if action in ("replace", "hashmod", "lowercase", "uppercase", "keepequal", "dropequal"):
            if not target:
                raise ConfigError(f"relabel action {action!r} requires target_label")
        if action in ("keepequal", "dropequal") and not sources:
            raise ConfigError(f"relabel action {action!r} requires source_labels")
        if action == "replace" and not target and data.get("target_label") is not None:
            raise ConfigError("replace requires a non-empty target_label")
        regex = str(data.get("regex", "(.*)"))
        try:
            re.compile(regex)
        except re.error as exc:
            raise ConfigError(f"invalid relabel regex {regex!r}: {exc}") from exc
        return cls(
            source_labels=list(sources),
            separator=str(data.get("separator", ";")),
            target_label=target,
            regex=regex,
            replacement=str(data.get("replacement", "$1")),
            modulus=modulus,
            action=action,
        )


@dataclass
class StaticConfig:
    targets: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "StaticConfig":
        targets = data.get("targets", [])
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise ConfigError("static_configs.targets must be a list of strings")
        labels = data.get("labels", {}) or {}
        if not isinstance(labels, dict):
            raise ConfigError("static_configs.labels must be a mapping")
        for name, value in labels.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ConfigError("static_configs labels must be string:string")
        return cls(list(targets), dict(labels))


@dataclass
class FileSDConfig:
    files: list[str] = field(default_factory=list)
    refresh_interval: str = "5m"


@dataclass
class HTTPSDConfig:
    url: str = ""
    refresh_interval: str = "1m"
    headers: dict[str, str] = field(default_factory=dict)
    basic_auth: BasicAuth | None = None


@dataclass
class DNSSDConfig:
    names: list[str] = field(default_factory=list)
    refresh_interval: str = "30s"
    type: str = "A"
    port: int = 0


@dataclass
class ScrapeConfig:
    job_name: str
    scrape_interval: str = DEFAULT_SCRAPE_INTERVAL
    scrape_timeout: str = DEFAULT_SCRAPE_TIMEOUT
    metrics_path: str = "/metrics"
    scheme: str = "http"
    params: dict[str, list[str]] = field(default_factory=dict)
    static_configs: list[StaticConfig] = field(default_factory=list)
    file_sd_configs: list[FileSDConfig] = field(default_factory=list)
    http_sd_configs: list[HTTPSDConfig] = field(default_factory=list)
    dns_sd_configs: list[DNSSDConfig] = field(default_factory=list)
    relabel_configs: list[RelabelConfig] = field(default_factory=list)
    metric_relabel_configs: list[RelabelConfig] = field(default_factory=list)
    honor_labels: bool = False
    honor_timestamps: bool = True
    sample_limit: int = 0
    label_limit: int = 0
    label_name_length_limit: int = 0
    label_value_length_limit: int = 0
    body_size_limit: int = 0
    basic_auth: BasicAuth | None = None
    bearer_token: str = ""
    bearer_token_file: str = ""
    tls_config: TLSConfig = field(default_factory=TLSConfig)
    proxy_url: str = ""
    follow_redirects: bool = True

    @classmethod
    def from_dict(cls, data: dict, defaults: "GlobalConfig") -> "ScrapeConfig":
        if not isinstance(data, dict):
            raise ConfigError("scrape_configs entries must be mappings")
        job_name = data.get("job_name")
        if not job_name or not isinstance(job_name, str):
            raise ConfigError("scrape_config requires a job_name")
        interval = str(data.get("scrape_interval", defaults.scrape_interval))
        timeout = str(data.get("scrape_timeout", defaults.scrape_timeout))
        _validate_duration(interval, "scrape_interval")
        _validate_duration(timeout, "scrape_timeout")
        if parse_duration(timeout) > parse_duration(interval):
            raise ConfigError(
                f"job {job_name}: scrape_timeout must not exceed scrape_interval"
            )
        scheme = str(data.get("scheme", "http")).lower()
        if scheme not in ("http", "https"):
            raise ConfigError(f"job {job_name}: invalid scheme {scheme!r}")
        metrics_path = str(data.get("metrics_path", "/metrics"))
        if not metrics_path.startswith("/"):
            raise ConfigError(f"job {job_name}: metrics_path must start with '/'")
        params = data.get("params", {}) or {}
        if not isinstance(params, dict):
            raise ConfigError(f"job {job_name}: params must be a mapping")
        normalised_params: dict[str, list[str]] = {}
        for key, value in params.items():
            if isinstance(value, str):
                normalised_params[str(key)] = [value]
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                normalised_params[str(key)] = list(value)
            else:
                raise ConfigError(f"job {job_name}: params values must be strings or lists")
        static_configs = [StaticConfig.from_dict(item) for item in data.get("static_configs", []) or []]
        file_sd = []
        for item in data.get("file_sd_configs", []) or []:
            if not isinstance(item, dict):
                raise ConfigError(f"job {job_name}: file_sd_configs entries must be mappings")
            files = item.get("files", [])
            if not files or not isinstance(files, list):
                raise ConfigError(f"job {job_name}: file_sd_configs requires files")
            file_sd.append(
                FileSDConfig(
                    [str(path) for path in files],
                    str(item.get("refresh_interval", "5m")),
                )
            )
        http_sd = []
        for item in data.get("http_sd_configs", []) or []:
            if not isinstance(item, dict) or not item.get("url"):
                raise ConfigError(f"job {job_name}: http_sd_configs requires url")
            http_sd.append(
                HTTPSDConfig(
                    str(item["url"]),
                    str(item.get("refresh_interval", "1m")),
                    {str(k): str(v) for k, v in (item.get("headers") or {}).items()},
                )
            )
        dns_sd = []
        for item in data.get("dns_sd_configs", []) or []:
            if not isinstance(item, dict):
                raise ConfigError(f"job {job_name}: dns_sd_configs entries must be mappings")
            names = item.get("names", [])
            if not names:
                raise ConfigError(f"job {job_name}: dns_sd_configs requires names")
            dns_sd.append(
                DNSSDConfig(
                    [str(name) for name in names],
                    str(item.get("refresh_interval", "30s")),
                    str(item.get("type", "A")).upper(),
                    int(item.get("port", 0)),
                )
            )
        known = SUPPORTED_SD | {
            "job_name", "scrape_interval", "scrape_timeout", "metrics_path",
            "scheme", "params", "relabel_configs", "metric_relabel_configs",
            "honor_labels", "honor_timestamps", "sample_limit", "label_limit",
            "label_name_length_limit", "label_value_length_limit",
            "body_size_limit", "basic_auth", "bearer_token", "bearer_token_file",
            "tls_config", "proxy_url", "follow_redirects", "authorization",
            "kubernetes_sd_configs",
        }
        for key in data:
            if key not in known:
                # Unknown keys are warnings in upstream for some settings and
                # hard errors for many; being strict keeps examples honest.
                raise ConfigError(f"job {job_name}: unknown scrape config key {key!r}")
        if data.get("kubernetes_sd_configs"):
            raise ConfigError(
                "kubernetes_sd_configs is not implemented in Mini-Prometheus; "
                "use file_sd in the labs and see chapter 11 for the real config"
            )
        basic_auth = None
        if data.get("basic_auth"):
            auth = data["basic_auth"]
            basic_auth = BasicAuth(
                str(auth.get("username", "")),
                str(auth.get("password", "")),
                str(auth.get("password_file", "")),
            )
        tls = data.get("tls_config") or {}
        return cls(
            job_name=job_name,
            scrape_interval=interval,
            scrape_timeout=timeout,
            metrics_path=metrics_path,
            scheme=scheme,
            params=normalised_params,
            static_configs=static_configs,
            file_sd_configs=file_sd,
            http_sd_configs=http_sd,
            dns_sd_configs=dns_sd,
            relabel_configs=[RelabelConfig.from_dict(item) for item in data.get("relabel_configs", []) or []],
            metric_relabel_configs=[
                RelabelConfig.from_dict(item)
                for item in data.get("metric_relabel_configs", []) or []
            ],
            honor_labels=bool(data.get("honor_labels", False)),
            honor_timestamps=bool(data.get("honor_timestamps", True)),
            sample_limit=int(data.get("sample_limit", 0) or 0),
            label_limit=int(data.get("label_limit", 0) or 0),
            label_name_length_limit=int(data.get("label_name_length_limit", 0) or 0),
            label_value_length_limit=int(data.get("label_value_length_limit", 0) or 0),
            body_size_limit=int(data.get("body_size_limit", 0) or 0),
            basic_auth=basic_auth,
            bearer_token=str(data.get("bearer_token", "") or ""),
            bearer_token_file=str(data.get("bearer_token_file", "") or ""),
            tls_config=TLSConfig(
                str(tls.get("ca_file", "")),
                str(tls.get("cert_file", "")),
                str(tls.get("key_file", "")),
                str(tls.get("server_name", "")),
                bool(tls.get("insecure_skip_verify", False)),
            ),
            proxy_url=str(data.get("proxy_url", "") or ""),
            follow_redirects=bool(data.get("follow_redirects", True)),
        )


@dataclass
class GlobalConfig:
    scrape_interval: str = DEFAULT_SCRAPE_INTERVAL
    scrape_timeout: str = DEFAULT_SCRAPE_TIMEOUT
    evaluation_interval: str = DEFAULT_EVALUATION_INTERVAL
    external_labels: dict[str, str] = field(default_factory=dict)
    query_lookback_delta: str = "5m"
    scrape_protocols: list[str] = field(default_factory=lambda: ["PrometheusText1.0.0"])
    # Prometheus 2.53+: default for rule groups that do not set query_offset.
    rule_query_offset: str = "0s"


@dataclass
class AlertmanagerConfig:
    targets: list[str] = field(default_factory=list)
    scheme: str = "http"
    timeout: str = "10s"
    api_version: str = "v2"
    path_prefix: str = ""
    basic_auth: BasicAuth | None = None


@dataclass
class RemoteWriteConfig:
    url: str
    name: str = ""
    remote_timeout: str = "30s"
    headers: dict[str, str] = field(default_factory=dict)
    batch_size: int = 500
    protocol: str = "json"  # "json" (Mini) or "prometheus" (protobuf+snappy)
    queue_capacity: int = 10000
    max_retries: int = 5
    tls_config: TLSConfig = field(default_factory=TLSConfig)
    basic_auth: BasicAuth | None = None


@dataclass
class RemoteReadConfig:
    url: str
    remote_timeout: str = "30s"
    read_recent: bool = True
    protocol: str = "json"


@dataclass
class Config:
    global_config: GlobalConfig = field(default_factory=GlobalConfig)
    scrape_configs: list[ScrapeConfig] = field(default_factory=list)
    rule_files: list[str] = field(default_factory=list)
    alerting: list[AlertmanagerConfig] = field(default_factory=list)
    remote_write: list[RemoteWriteConfig] = field(default_factory=list)
    remote_read: list[RemoteReadConfig] = field(default_factory=list)
    source: str = ""

    def job(self, name: str) -> ScrapeConfig | None:
        for item in self.scrape_configs:
            if item.job_name == name:
                return item
        return None

    def to_dict(self) -> dict:
        return {
            "global": {
                "scrape_interval": self.global_config.scrape_interval,
                "scrape_timeout": self.global_config.scrape_timeout,
                "evaluation_interval": self.global_config.evaluation_interval,
                "external_labels": self.global_config.external_labels,
                "query_lookback_delta": self.global_config.query_lookback_delta,
                "scrape_protocols": self.global_config.scrape_protocols,
                "rule_query_offset": self.global_config.rule_query_offset,
            },
            "scrape_configs": [
                {
                    "job_name": job.job_name,
                    "scrape_interval": job.scrape_interval,
                    "scrape_timeout": job.scrape_timeout,
                    "metrics_path": job.metrics_path,
                    "scheme": job.scheme,
                    "static_configs": [
                        {"targets": item.targets, "labels": item.labels}
                        for item in job.static_configs
                    ],
                    "file_sd_configs": [
                        {"files": item.files, "refresh_interval": item.refresh_interval}
                        for item in job.file_sd_configs
                    ],
                    "relabel_configs": [item.__dict__ for item in job.relabel_configs],
                    "metric_relabel_configs": [item.__dict__ for item in job.metric_relabel_configs],
                    "honor_labels": job.honor_labels,
                    "honor_timestamps": job.honor_timestamps,
                }
                for job in self.scrape_configs
            ],
            "rule_files": self.rule_files,
            "alerting": [
                {"targets": item.targets, "scheme": item.scheme, "api_version": item.api_version}
                for item in self.alerting
            ],
            "remote_write": [
                {"url": item.url, "name": item.name, "protocol": item.protocol}
                for item in self.remote_write
            ],
            "remote_read": [{"url": item.url} for item in self.remote_read],
        }


def _validate_duration(value: str, name: str) -> int:
    try:
        ms = parse_duration(value)
    except ValueError as exc:
        raise ConfigError(f"invalid {name} {value!r}: {exc}") from exc
    if ms <= 0:
        raise ConfigError(f"{name} must be positive")
    return ms


def _validate_nonnegative_duration(value: str, name: str) -> int:
    """Like _validate_duration, but ``0s`` is meaningful (query offsets)."""
    try:
        ms = parse_duration(value)
    except ValueError as exc:
        raise ConfigError(f"invalid {name} {value!r}: {exc}") from exc
    if ms < 0:
        raise ConfigError(f"{name} must not be negative")
    return ms


def load_config(path: str | os.PathLike[str]) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    config = parse_config(data)
    config.source = str(path)
    return config


def parse_config(data: dict) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be a YAML mapping")
    global_data = data.get("global", {}) or {}
    glob = GlobalConfig(
        scrape_interval=str(global_data.get("scrape_interval", DEFAULT_SCRAPE_INTERVAL)),
        scrape_timeout=str(global_data.get("scrape_timeout", DEFAULT_SCRAPE_TIMEOUT)),
        evaluation_interval=str(global_data.get("evaluation_interval", DEFAULT_EVALUATION_INTERVAL)),
        external_labels={
            str(k): str(v) for k, v in (global_data.get("external_labels") or {}).items()
        },
        query_lookback_delta=str(global_data.get("query_lookback_delta", "5m")),
        scrape_protocols=list(global_data.get("scrape_protocols", ["PrometheusText1.0.0"])),
        rule_query_offset=str(global_data.get("rule_query_offset", "0s")),
    )
    _validate_duration(glob.scrape_interval, "global.scrape_interval")
    _validate_duration(glob.scrape_timeout, "global.scrape_timeout")
    _validate_duration(glob.evaluation_interval, "global.evaluation_interval")
    _validate_duration(glob.query_lookback_delta, "global.query_lookback_delta")
    _validate_nonnegative_duration(glob.rule_query_offset, "global.rule_query_offset")
    if parse_duration(glob.scrape_timeout) > parse_duration(glob.scrape_interval):
        raise ConfigError("global.scrape_timeout must not exceed global.scrape_interval")
    scrape_configs = []
    seen_jobs: set[str] = set()
    for item in data.get("scrape_configs", []) or []:
        job = ScrapeConfig.from_dict(item, glob)
        if job.job_name in seen_jobs:
            raise ConfigError(f"duplicate job_name {job.job_name!r}")
        seen_jobs.add(job.job_name)
        scrape_configs.append(job)
    rule_files = data.get("rule_files", []) or []
    if isinstance(rule_files, str):
        rule_files = [rule_files]
    if not isinstance(rule_files, list):
        raise ConfigError("rule_files must be a list")
    alerting: list[AlertmanagerConfig] = []
    alerting_data = data.get("alerting", {}) or {}
    for item in alerting_data.get("alertmanagers", []) or []:
        static = item.get("static_configs", []) if isinstance(item, dict) else []
        targets = []
        for group in static:
            targets.extend(str(target) for target in group.get("targets", []))
        auth = item.get("basic_auth") if isinstance(item, dict) else None
        alerting.append(
            AlertmanagerConfig(
                targets,
                str(item.get("scheme", "http")),
                str(item.get("timeout", "10s")),
                str(item.get("api_version", "v2")),
                str(item.get("path_prefix", "")),
                BasicAuth(str(auth.get("username", "")), str(auth.get("password", "")))
                if auth
                else None,
            )
        )
    remote_write = []
    for item in data.get("remote_write", []) or []:
        if not isinstance(item, dict) or not item.get("url"):
            raise ConfigError("remote_write entries require url")
        remote_write.append(
            RemoteWriteConfig(
                url=str(item["url"]),
                name=str(item.get("name", "")),
                remote_timeout=str(item.get("remote_timeout", "30s")),
                headers={str(k): str(v) for k, v in (item.get("headers") or {}).items()},
                batch_size=int(item.get("batch_size", 500)),
                protocol=str(item.get("protocol", "json")),
                queue_capacity=int(item.get("queue_capacity", 10000)),
                max_retries=int(item.get("max_retries", 5)),
            )
        )
    remote_read = []
    for item in data.get("remote_read", []) or []:
        if isinstance(item, dict) and item.get("url"):
            remote_read.append(
                RemoteReadConfig(
                    str(item["url"]),
                    str(item.get("remote_timeout", "30s")),
                    bool(item.get("read_recent", True)),
                    str(item.get("protocol", "json")),
                )
            )
    # rule_files are resolved relative to the configuration directory when
    # the rule manager loads; validating here would use the process CWD and
    # break configs started from another directory.
    return Config(glob, scrape_configs, [str(item) for item in rule_files], alerting, remote_write, remote_read)


def expand_rule_files(patterns: list[str], base_dir: str) -> list[str]:
    out: list[str] = []
    for pattern in patterns:
        if not os.path.isabs(pattern):
            pattern = os.path.join(base_dir, pattern)
        out.extend(sorted(globlib.glob(pattern)))
    return out
