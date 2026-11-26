"""Small, dependency-free helpers used across MiniMLflow."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator
from urllib.parse import quote, unquote, urlparse

from .errors import InvalidParameter

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_NAME = 250
_MAX_KEY = 250
_MAX_PARAM_VALUE = 6000
_MAX_TAG_VALUE = 5000


def now_ms() -> int:
    return int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1000)


def now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    """Return a 32-character lowercase hexadecimal identifier."""
    return os.urandom(16).hex()


def is_id(value: str) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def new_logged_model_id() -> str:
    """Return an MLflow-style logged-model id (``m-`` + 32 hex chars)."""
    return "m-" + new_id()


def is_logged_model_id(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"m-[0-9a-f]{32}", value))


def validate_identifier(value: str, *, what: str = "id", allow_dash: bool = True) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParameter(f"{what} must be a non-empty string")
    pattern = r"^[A-Za-z0-9_.\-]+$" if allow_dash else r"^[A-Za-z0-9_.]+$"
    if not re.fullmatch(pattern, value):
        raise InvalidParameter(f"{what} contains invalid characters: {value!r}")
    return value


def validate_name(value: str, *, what: str = "name", max_len: int = _MAX_NAME) -> str:
    if not isinstance(value, str):
        raise InvalidParameter(f"{what} must be a string")
    value = value.strip()
    if not value:
        raise InvalidParameter(f"{what} must not be empty")
    if len(value) > max_len:
        raise InvalidParameter(f"{what} must be at most {max_len} characters")
    if _CONTROL_RE.search(value):
        raise InvalidParameter(f"{what} must not contain control characters")
    return value


def validate_key(value: str, *, what: str = "key") -> str:
    if not isinstance(value, str):
        raise InvalidParameter(f"{what} must be a string")
    if not value:
        raise InvalidParameter(f"{what} must not be empty")
    if len(value) > _MAX_KEY:
        raise InvalidParameter(f"{what} must be at most {_MAX_KEY} characters")
    if _CONTROL_RE.search(value):
        raise InvalidParameter(f"{what} contains control characters")
    return value


def coerce_param_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        raise InvalidParameter("parameter values cannot be None")
    if isinstance(value, (dict, list, tuple, set)):
        raise InvalidParameter("parameter values must be scalar; serialize structures first")
    text = str(value)
    if len(text) > _MAX_PARAM_VALUE:
        raise InvalidParameter(f"parameter value exceeds {_MAX_PARAM_VALUE} characters")
    return text


def coerce_tag_value(value: Any) -> str:
    if value is None:
        raise InvalidParameter("tag values cannot be None")
    if isinstance(value, (dict, list, tuple, set)):
        text = compact_json(value)
    else:
        text = str(value)
    if len(text) > _MAX_TAG_VALUE:
        raise InvalidParameter(f"tag value exceeds {_MAX_TAG_VALUE} characters")
    return text


def coerce_metric(value: Any) -> float:
    if isinstance(value, bool):
        raise InvalidParameter("metric value must be numeric; booleans are not accepted")
    if not isinstance(value, (int, float)):
        try:
            value = float(value)  # numeric strings are accepted
        except (TypeError, ValueError) as exc:
            raise InvalidParameter(f"metric value must be numeric, got {value!r}") from exc
    result = float(value)
    if not math.isfinite(result):
        raise InvalidParameter("metric value must be finite (NaN and Infinity are rejected)")
    return result


def validate_metric_step(step: Any) -> int:
    if isinstance(step, bool):
        raise InvalidParameter("metric step must be a non-negative integer")
    try:
        result = int(step)
    except (TypeError, ValueError) as exc:
        raise InvalidParameter("metric step must be a non-negative integer") from exc
    if result < 0:
        raise InvalidParameter("metric step must be >= 0")
    if result > 2**63 - 1:
        raise InvalidParameter("metric step is outside the signed 64-bit range")
    return result


def validate_timestamp(timestamp: Any) -> int:
    if timestamp is None:
        return now_ms()
    if isinstance(timestamp, bool):
        raise InvalidParameter("timestamp must be an integer number of milliseconds")
    try:
        result = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise InvalidParameter("timestamp must be an integer number of milliseconds") from exc
    if result < 0:
        raise InvalidParameter("timestamp must be >= 0")
    if result > 2**63 - 1:
        raise InvalidParameter("timestamp is outside the signed 64-bit range")
    return result


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_join(root: str | Path, relative: str | Path) -> Path:
    """Join an untrusted relative path onto ``root`` and enforce containment.

    Rejects absolute paths, drive letters, backslashes on POSIX, NUL bytes and
    escapes via ``..``.  The check is done both lexically and after resolving
    symlinks.
    """
    raw = str(relative).replace("\\", "/")
    if "\x00" in raw:
        raise InvalidParameter("artifact path contains a NUL byte")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise InvalidParameter("artifact path must be relative")
    pure = PurePosixPath(raw)
    if any(part == ".." for part in pure.parts):
        raise InvalidParameter("artifact path must not contain '..'")
    if any(":" in part for part in pure.parts):
        # Windows alternate data streams and cross-platform portability.
        raise InvalidParameter("artifact path must not contain ':'")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in part) for part in pure.parts):
        raise InvalidParameter("artifact path must not contain control characters")
    if os.name == "nt":
        reserved = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        for part in pure.parts:
            if part.split(".", 1)[0].upper() in reserved:
                raise InvalidParameter(f"artifact path contains a reserved Windows name: {part!r}")
    root_path = Path(root).resolve()
    candidate = (root_path / Path(*pure.parts)).resolve(strict=False)
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise InvalidParameter("artifact path escapes the artifact root") from exc
    return candidate


def path_to_file_uri(path: str | Path) -> str:
    return Path(path).resolve().as_uri()


def file_uri_to_path(uri: str | Path) -> Path:
    text = str(uri)
    parsed = urlparse(text)
    if parsed.scheme == "file":
        raw = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            if os.name == "nt" and re.fullmatch(r"[A-Za-z]:", parsed.netloc):
                # Tolerate the non-standard file://D:/path spelling.
                raw = parsed.netloc + (raw or "/")
            else:
                # UNC paths are uncommon in the tutorial; fail clearly rather
                # than silently reinterpreting a host as part of the path.
                raise InvalidParameter(f"unsupported file URI authority: {parsed.netloc}")
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", raw):
            raw = raw[1:]
        return Path(raw).resolve()
    if parsed.scheme:
        raise InvalidParameter(f"unsupported artifact URI scheme: {parsed.scheme}")
    return Path(text).resolve()


def encode_page_token(offset: int) -> str:
    raw = compact_json({"offset": offset}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_page_token(token: str | None, *, max_offset: int = 1_000_000) -> int:
    if not token:
        return 0
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        offset = payload["offset"]
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError
    except Exception as exc:  # noqa: BLE001 - external input
        raise InvalidParameter("invalid page_token") from exc
    if offset < 0 or offset > max_offset:
        raise InvalidParameter("page_token offset out of range")
    return offset


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, destination)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return destination


def atomic_write_text(path: str | Path, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


def copy_file_atomic(source: str | Path, destination: str | Path) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(destination.parent))
    os.close(fd)
    try:
        with open(source, "rb") as src, open(tmp_name, "wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_name, destination)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return destination


def truncate_text(text: str, limit: int = 64 * 1024) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    encoded = text.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore") + "\n...[truncated]"


def json_safe(value: Any, *, max_depth: int = 8, _depth: int = 0) -> Any:
    """Convert arbitrary Python values into a JSON-safe representation.

    Used by tracing where request payloads may contain custom objects.  We
    never call ``repr`` on a secret blindly: callers should redact first, then
    this function guarantees serializability.
    """
    if _depth > max_depth:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return {"type": "bytes", "size": len(value)}
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:200]:
            result[str(key)] = json_safe(item, max_depth=max_depth, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item, max_depth=max_depth, _depth=_depth + 1) for item in list(value)[:200]]
    if hasattr(value, "tolist"):
        try:
            return json_safe(value.tolist(), max_depth=max_depth, _depth=_depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return f"<{type(value).__name__}>"


_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "access_key",
    "private_key",
    "cookie",
    "set-cookie",
}


def redact(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            sensitive = normalized in _SENSITIVE_KEYS or normalized.endswith(
                ("_password", "_passwd", "_secret", "_token", "_api_key", "_apikey", "_private_key", "_credential")
            )
            if sensitive:
                result[str(key)] = "***REDACTED***"
            else:
                result[str(key)] = redact(item, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


def format_timestamp(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return "-"
    return _dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_pairs(value: Any, *, what: str, stringify: bool = True) -> dict[str, Any]:
    """Accept either a mapping or MLflow's list-of-{key,value} shape.

    ``stringify=False`` preserves values so the service layer can validate
    them properly (for example rejecting a nested dict as a parameter value
    instead of silently storing its Python repr).
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return {
            str(key): (str(item) if stringify else item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        result: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, dict) or "key" not in item or "value" not in item:
                raise InvalidParameter(f"{what} entries must contain key and value")
            result[str(item["key"])] = str(item["value"]) if stringify else item["value"]
        return result
    raise InvalidParameter(f"{what} must be a mapping or a list of key/value objects")


def pairs_to_list(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": key, "value": value} for key, value in sorted(mapping.items())]


def chunked(iterable: Iterable[str], size: int) -> Iterator[list[str]]:
    chunk: list[str] = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def quote_path(path: str) -> str:
    return quote(path, safe="/")
