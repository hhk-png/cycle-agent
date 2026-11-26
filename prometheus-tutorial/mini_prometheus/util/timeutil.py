"""Time and duration handling.

All internal timestamps are integer milliseconds since the Unix epoch, which
is exactly what Prometheus uses on the wire (`[<unix_seconds>, "<value>"]`)
once seconds are multiplied by 1000.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import struct

MILLIS_PER_SECOND = 1000
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24

# Prometheus encodes "stale" as a special NaN bit pattern, not as an ordinary
# NaN: 0x7FF0000000000002. Keeping the distinction lets a query tell "the
# series vanished" apart from "the exporter reported NaN".
STALE_NAN_BITS = 0x7FF0000000000002
STALE_NAN = struct.unpack(">d", struct.pack(">Q", STALE_NAN_BITS))[0]


def is_stale(value: float) -> bool:
    return struct.unpack(">Q", struct.pack(">d", value))[0] == STALE_NAN_BITS


def stale_nan() -> float:
    return STALE_NAN


_DURATION_RE = re.compile(
    r"^(?:(?P<y>\d+)y)?(?:(?P<w>\d+)w)?(?:(?P<d>\d+)d)?"
    r"(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?(?:(?P<ms>\d+)ms)?$"
)
_DURATION_PART = re.compile(r"(\d+)(ms|y|w|d|h|m|s)")

_UNITS_MS = {
    "ms": 1,
    "s": 1000,
    "m": 60 * 1000,
    "h": 60 * 60 * 1000,
    "d": 24 * 60 * 60 * 1000,
    "w": 7 * 24 * 60 * 60 * 1000,
    "y": 365 * 24 * 60 * 60 * 1000,
}
_UNIT_ORDER = ["y", "w", "d", "h", "m", "s", "ms"]


class DurationError(ValueError):
    pass


def parse_duration(text: str) -> int:
    """Parse a Prometheus duration such as ``1h30m`` or ``500ms`` to ms.

    Upstream accepts units in descending order only and requires at least one
    unit.  We additionally accept a bare integer as milliseconds, which makes
    the API friendlier for programmatic use, but the PromQL parser rejects it.
    """
    if not isinstance(text, str):
        raise DurationError(f"duration must be a string, got {type(text).__name__}")
    text = text.strip()
    if text == "0":
        return 0
    if not text:
        raise DurationError("empty duration")
    pos = 0
    total = 0
    last_index = -1
    found = False
    while pos < len(text):
        match = _DURATION_PART.match(text, pos)
        if not match:
            raise DurationError(f"invalid duration {text!r}")
        number, unit = match.groups()
        index = _UNIT_ORDER.index(unit)
        if index <= last_index:
            raise DurationError(f"duration units out of order or repeated in {text!r}")
        last_index = index
        total += int(number) * _UNITS_MS[unit]
        found = True
        pos = match.end()
    if not found:
        raise DurationError(f"invalid duration {text!r}")
    return total


def format_duration(ms: int) -> str:
    """Render milliseconds using the largest sensible Prometheus units."""
    if ms == 0:
        return "0s"
    sign = "-" if ms < 0 else ""
    ms = abs(int(ms))
    parts: list[str] = []
    for unit in ("y", "w", "d", "h", "m", "s", "ms"):
        factor = _UNITS_MS[unit]
        if ms >= factor:
            count, ms = divmod(ms, factor)
            parts.append(f"{count}{unit}")
    return sign + "".join(parts)


def parse_timestamp(text: str | float | int | None, *, default: int | None = None) -> int:
    """Parse an API timestamp: RFC3339, unix seconds, or unix milliseconds.

    Prometheus' HTTP API takes either RFC3339 or a float number of seconds.
    We treat integer strings with >= 13 digits as milliseconds, which is a
    benign convenience for the CLI but never changes the meaning of legal
    Prometheus input.
    """
    if text is None or text == "":
        if default is None:
            raise ValueError("missing timestamp")
        return default
    if isinstance(text, (int, float)):
        value = float(text)
        if isinstance(text, int) and abs(text) >= 10**12:
            return int(text)
        return int(round(value * 1000))
    text = text.strip()
    try:
        value = float(text)
    except ValueError:
        pass
    else:
        if abs(value) < 10**11:
            return int(round(value * 1000))
        return int(round(value))
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"cannot parse timestamp {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def format_timestamp(ms: int) -> str:
    """RFC3339 with milliseconds, as used in alert payloads."""
    dt = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


def seconds_to_ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def ms_to_seconds(ms: int) -> float:
    return ms / 1000.0


def is_valid_timestamp(ms: int) -> bool:
    return not math.isnan(ms) and -2**63 <= ms < 2**63
