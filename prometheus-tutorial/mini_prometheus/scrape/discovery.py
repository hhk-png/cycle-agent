"""Service discovery providers used by the scrape manager.

Four providers are implemented behind one interface: static, file_sd,
http_sd and dns_sd.  Kubernetes/EC2/Consul discovery share the same contract
(``discover() -> [{labels}]``), which is the point the tutorial makes: adding a
provider is an adapter, not a scrape-loop change.
"""

from __future__ import annotations

import glob as globlib
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import yaml

from ..config import DNSSDConfig, FileSDConfig, HTTPSDConfig, ScrapeConfig, StaticConfig
from ..labels import Labels


class DiscoveryError(RuntimeError):
    pass


Fetcher = Callable[[str, dict[str, str], float], tuple[int, bytes]]
# (url, headers, timeout) -> (status, body)


@dataclass
class DiscoveredTarget:
    labels: dict[str, str]
    source: str = "static"

    def address(self) -> str:
        return self.labels.get("__address__", "")


@dataclass
class DiscoveryManager:
    base_dir: str = "."
    fetcher: Fetcher | None = None
    _http_cache: dict[str, list[DiscoveredTarget]] = field(default_factory=dict)

    def discover(self, job: ScrapeConfig) -> list[DiscoveredTarget]:
        targets: dict[str, DiscoveredTarget] = {}
        for config in job.static_configs:
            for target in self._from_static(job, config):
                targets[target.address() + "|" + target.labels.get("job", "")] = target
        for config in job.file_sd_configs:
            for target in self._from_files(job, config):
                targets[target.address() + "|" + target.labels.get("job", "")] = target
        for config in job.http_sd_configs:
            for target in self._from_http(job, config):
                targets[target.address() + "|" + target.labels.get("job", "")] = target
        for config in job.dns_sd_configs:
            for target in self._from_dns(job, config):
                targets[target.address() + "|" + target.labels.get("job", "")] = target
        return list(targets.values())

    # ---- static --------------------------------------------------------
    def _from_static(self, job: ScrapeConfig, config: StaticConfig) -> list[DiscoveredTarget]:
        out = []
        for target in config.targets:
            labels = self._base_labels(job)
            labels.update(config.labels)
            address, scheme = _split_address(target)
            labels["__address__"] = address
            if scheme:
                labels["__scheme__"] = scheme
            out.append(DiscoveredTarget(labels, "static"))
        return out

    def _base_labels(self, job: ScrapeConfig) -> dict[str, str]:
        labels = {
            "job": job.job_name,
            "__scheme__": job.scheme,
            "__metrics_path__": job.metrics_path,
        }
        for name, values in job.params.items():
            labels[f"__param_{name}"] = values[0] if values else ""
        return labels

    # ---- file_sd -------------------------------------------------------
    def _from_files(self, job: ScrapeConfig, config: FileSDConfig) -> list[DiscoveredTarget]:
        out = []
        for pattern in config.files:
            path_pattern = pattern
            if not Path(path_pattern).is_absolute():
                path_pattern = str(Path(self.base_dir) / path_pattern)
            for filename in sorted(globlib.glob(path_pattern)):
                try:
                    text = Path(filename).read_text(encoding="utf-8")
                    if filename.endswith(".json"):
                        groups = json.loads(text)
                    else:
                        groups = yaml.safe_load(text)
                except (OSError, ValueError) as exc:
                    raise DiscoveryError(f"cannot read file_sd file {filename}: {exc}") from exc
                if not isinstance(groups, list):
                    raise DiscoveryError(f"file_sd file {filename} must contain a list of groups")
                for group in groups:
                    if not isinstance(group, dict):
                        raise DiscoveryError(f"file_sd group in {filename} must be a mapping")
                    group_labels = {
                        str(k): str(v) for k, v in (group.get("labels") or {}).items()
                    }
                    for target in group.get("targets", []) or []:
                        labels = self._base_labels(job)
                        labels.update(group_labels)
                        address, scheme = _split_address(str(target))
                        labels["__address__"] = address
                        if scheme:
                            labels["__scheme__"] = scheme
                        labels["__meta_filepath"] = filename
                        out.append(DiscoveredTarget(labels, "file_sd"))
        return out

    # ---- http_sd -------------------------------------------------------
    def _from_http(self, job: ScrapeConfig, config: HTTPSDConfig) -> list[DiscoveredTarget]:
        fetcher = self.fetcher or _default_fetcher
        headers = {"Accept": "application/json"}
        headers.update(config.headers)
        try:
            status, body = fetcher(config.url, headers, 10.0)
            if status != 200:
                raise DiscoveryError(f"HTTP SD returned status {status}")
            groups = json.loads(body.decode("utf-8"))
        except (OSError, ValueError, DiscoveryError) as exc:
            # Prometheus keeps the last successful set on refresh failures.
            if config.url in self._http_cache:
                return list(self._http_cache[config.url])
            raise DiscoveryError(f"http_sd {config.url}: {exc}") from exc
        if not isinstance(groups, list):
            raise DiscoveryError(f"http_sd {config.url}: payload must be a list")
        out: list[DiscoveredTarget] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_labels = {
                str(k): str(v) for k, v in (group.get("labels") or {}).items()
            }
            for target in group.get("targets", []) or []:
                labels = self._base_labels(job)
                labels.update(group_labels)
                address, scheme = _split_address(str(target))
                labels["__address__"] = address
                if scheme:
                    labels["__scheme__"] = scheme
                labels["__meta_url"] = config.url
                out.append(DiscoveredTarget(labels, "http_sd"))
        self._http_cache[config.url] = list(out)
        return out

    # ---- dns_sd --------------------------------------------------------
    def _from_dns(self, job: ScrapeConfig, config: DNSSDConfig) -> list[DiscoveredTarget]:
        if config.type not in ("A", "AAAA"):
            raise DiscoveryError(
                f"dns_sd type {config.type!r} is not supported by Mini-Prometheus "
                "(A/AAAA only; SRV requires a DNS client library)"
            )
        family = socket.AF_INET if config.type == "A" else socket.AF_INET6
        out: list[DiscoveredTarget] = []
        for name in config.names:
            try:
                infos = socket.getaddrinfo(name, config.port or None, family, socket.SOCK_STREAM)
            except OSError as exc:
                raise DiscoveryError(f"dns_sd {name}: {exc}") from exc
            for info in infos:
                ip = info[4][0]
                port = info[4][1]
                labels = self._base_labels(job)
                labels["__address__"] = f"{ip}:{port}" if port else ip
                labels["__meta_dns_name"] = name
                out.append(DiscoveredTarget(labels, "dns_sd"))
        return out

    # ---- relabeling ----------------------------------------------------
    def targets_for(self, job: ScrapeConfig) -> list[DiscoveredTarget]:
        from .relabel import relabel_target

        out = []
        for discovered in self.discover(job):
            labels = relabel_target(discovered.labels, job.relabel_configs)
            if labels is None:
                continue
            address = labels.get("__address__", "")
            if not address:
                continue
            if labels.get("__scheme__", "http") not in ("http", "https"):
                raise DiscoveryError(f"job {job.job_name}: invalid __scheme__ {labels.get('__scheme__')!r}")
            if not labels.get("__metrics_path__", "/metrics").startswith("/"):
                raise DiscoveryError(
                    f"job {job.job_name}: __metrics_path__ must start with '/'"
                )
            labels.setdefault("instance", address)
            out.append(DiscoveredTarget(labels, discovered.source))
        return out


def _split_address(target: str) -> tuple[str, str]:
    if "://" in target:
        parsed = urlparse(target)
        if not parsed.netloc:
            raise DiscoveryError(f"invalid target address {target!r}")
        return parsed.netloc, parsed.scheme
    return target, ""


def _default_fetcher(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def labels_from_target(target: DiscoveredTarget) -> Labels:
    return Labels(
        (name, value)
        for name, value in target.labels.items()
        if not name.startswith("__") and value != ""
    )
