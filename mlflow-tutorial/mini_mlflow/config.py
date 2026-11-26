"""Configuration parsing with explicit validation and testable defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

import yaml

from .errors import InvalidParameter
from .utils import file_uri_to_path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _parse_int(value: Any, name: str, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParameter(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise InvalidParameter(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _sqlite_path(uri: str) -> Path:
    if not isinstance(uri, str) or not uri.strip():
        raise InvalidParameter("backend_store_uri must not be empty")
    uri = uri.strip()
    if uri.startswith("sqlite:///"):
        raw = uri[len("sqlite:///") :]
    elif uri.startswith("sqlite://"):
        raw = uri[len("sqlite://") :]
    elif "://" in uri:
        raise InvalidParameter(
            "MiniMLflow only ships a SQLite backend; use sqlite:///path/to/mlflow.db "
            "or implement a new SqliteStore-compatible adapter"
        )
    else:
        raw = uri
    if raw in {":memory:", ""}:
        raise InvalidParameter(
            "in-memory SQLite is not supported because each request thread uses "
            "its own connection; use a temporary file instead"
        )
    path = Path(unquote(raw)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _artifact_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise InvalidParameter("artifact_root must not be empty")
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme == "file":
        return file_uri_to_path(text)
    if parsed.scheme and re.match(r"^[A-Za-z]:", text) is None:
        raise InvalidParameter(
            "MiniMLflow's artifact repository is local-only; use file:///... "
            "or a plain path. S3/GCS adapters are an extension point."
        )
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


@dataclass(frozen=True)
class MiniConfig:
    """Runtime configuration.

    Relative paths are resolved once, at construction time, so a server that
    later changes its working directory cannot accidentally write to a
    different database.
    """

    backend_store_uri: str = "sqlite:///mlflow.db"
    artifact_root: str = "./mlartifacts"
    host: str = "127.0.0.1"
    port: int = 5000
    auth_token: str | None = None
    max_request_bytes: int = 64 * 1024 * 1024
    sqlite_busy_timeout_ms: int = 5000
    search_max_results: int = 1000
    trace_max_payload_bytes: int = 256 * 1024
    cors_origin: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend_store_uri", str(self.backend_store_uri))
        object.__setattr__(self, "artifact_root", str(self.artifact_root))
        if not self.host:
            raise InvalidParameter("host must not be empty")
        object.__setattr__(self, "port", _parse_int(self.port, "port", minimum=0, maximum=65535))
        object.__setattr__(
            self,
            "max_request_bytes",
            _parse_int(self.max_request_bytes, "max_request_bytes", minimum=1024, maximum=2**31 - 1),
        )
        object.__setattr__(
            self,
            "sqlite_busy_timeout_ms",
            _parse_int(self.sqlite_busy_timeout_ms, "sqlite_busy_timeout_ms", minimum=100, maximum=120000),
        )
        object.__setattr__(
            self,
            "search_max_results",
            _parse_int(self.search_max_results, "search_max_results", minimum=1, maximum=50000),
        )
        object.__setattr__(
            self,
            "trace_max_payload_bytes",
            _parse_int(self.trace_max_payload_bytes, "trace_max_payload_bytes", minimum=1024, maximum=10 * 1024 * 1024),
        )
        # Fail at construction, not on the first request in production.
        _sqlite_path(self.backend_store_uri)
        _artifact_path(self.artifact_root)
        if self.auth_token is not None and (not isinstance(self.auth_token, str) or not self.auth_token):
            raise InvalidParameter("auth_token must be a non-empty string or None")

    @classmethod
    def from_env(cls, **overrides: Any) -> "MiniConfig":
        """Build config from ``MINI_MLFLOW_*`` variables, then overrides.

        Standard ``MLFLOW_*`` names are accepted as a fallback where the
        meaning is unambiguous.  Precedence: overrides > MINI_MLFLOW_* >
        MLFLOW_* > defaults.
        """
        values: dict[str, Any] = {
            "backend_store_uri": _env(
                "MINI_MLFLOW_BACKEND_STORE_URI",
                _env("MLFLOW_BACKEND_STORE_URI", "sqlite:///mlflow.db"),
            ),
            "artifact_root": _env(
                "MINI_MLFLOW_ARTIFACT_ROOT",
                _env("MLFLOW_ARTIFACT_ROOT", "./mlartifacts"),
            ),
            "host": _env("MINI_MLFLOW_HOST", "127.0.0.1"),
            "port": _env("MINI_MLFLOW_PORT", "5000"),
            "auth_token": _env("MINI_MLFLOW_AUTH_TOKEN"),
            "max_request_bytes": _env("MINI_MLFLOW_MAX_REQUEST_BYTES", str(64 * 1024 * 1024)),
            "sqlite_busy_timeout_ms": _env("MINI_MLFLOW_SQLITE_BUSY_TIMEOUT_MS", "5000"),
            "search_max_results": _env("MINI_MLFLOW_SEARCH_MAX_RESULTS", "1000"),
            "trace_max_payload_bytes": _env("MINI_MLFLOW_TRACE_MAX_PAYLOAD_BYTES", str(256 * 1024)),
            "cors_origin": _env("MINI_MLFLOW_CORS_ORIGIN"),
        }
        unknown = set(overrides) - set(values)
        if unknown:
            raise InvalidParameter(f"unknown config overrides: {sorted(unknown)}")
        values.update(overrides)
        return cls(**values)

    @classmethod
    def from_file(cls, path: str | Path, **overrides: Any) -> "MiniConfig":
        config_path = Path(path)
        if not config_path.is_file():
            raise InvalidParameter(f"config file not found: {config_path}")
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise InvalidParameter(f"invalid YAML config: {exc}") from exc
        if not isinstance(raw, dict):
            raise InvalidParameter("config file must contain a YAML mapping")
        allowed = {
            "backend_store_uri",
            "artifact_root",
            "host",
            "port",
            "auth_token",
            "max_request_bytes",
            "sqlite_busy_timeout_ms",
            "search_max_results",
            "trace_max_payload_bytes",
            "cors_origin",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise InvalidParameter(f"unknown config keys: {sorted(unknown)}")
        base = cls.from_env()
        merged = {key: getattr(base, key) for key in allowed}
        merged.update(raw)
        merged.update(overrides)
        return cls(**merged)

    @property
    def backend_path(self) -> Path:
        return _sqlite_path(self.backend_store_uri)

    @property
    def artifact_root_path(self) -> Path:
        return _artifact_path(self.artifact_root)

    @property
    def tracking_uri(self) -> str:
        path = self.backend_path
        return "sqlite:///" + path.as_posix()

    def ensure_directories(self) -> None:
        self.backend_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root_path.mkdir(parents=True, exist_ok=True)

    def derive(self, **changes: Any) -> "MiniConfig":
        return replace(self, **changes)


def load_config(path: str | Path | None = None, **overrides: Any) -> MiniConfig:
    if path:
        return MiniConfig.from_file(path, **overrides)
    return MiniConfig.from_env(**overrides)
