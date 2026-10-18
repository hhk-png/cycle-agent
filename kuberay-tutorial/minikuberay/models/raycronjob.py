"""A small cron implementation compatible with robfig/cron's standard parser.

RayCronJob uses five fields: minute hour day-of-month month day-of-week.
``timeZone`` supplies the location; schedules containing TZ/CRON_TZ are
rejected, matching KubeRay validation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Windows Python does not ship the IANA database unless the ``tzdata`` wheel is
# installed.  These fallbacks keep the common cases working in a bare
# environment.  When the real database is available it always wins; DST is only
# approximate for the fallback entries.
_TZ_FALLBACK_HOURS = {
    "UTC": 0,
    "ETC/UTC": 0,
    "GMT": 0,
    "Z": 0,
    "ASIA/SHANGHAI": 8,
    "ASIA/HONG_KONG": 8,
    "ASIA/SINGAPORE": 8,
    "ASIA/TOKYO": 9,
    "ASIA/SEOUL": 9,
    "ASIA/KOLKATA": 5.5,
    "AMERICA/NEW_YORK": -5,
    "AMERICA/CHICAGO": -6,
    "AMERICA/DENVER": -7,
    "AMERICA/LOS_ANGELES": -8,
    "EUROPE/LONDON": 0,
    "EUROPE/BERLIN": 1,
    "EUROPE/PARIS": 1,
    "AUSTRALIA/SYDNEY": 10,
}

MONTH_NAMES = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}

FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def load_timezone(name: str | None):
    if not name:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    if name.upper() in _TZ_FALLBACK_HOURS:
        hours = _TZ_FALLBACK_HOURS[name.upper()]
        return timezone(timedelta(hours=hours), name)
    raise ValueError(f"invalid time zone: {name!r}")


def _parse_value(token: str, names: dict[str, int] | None, low: int, high: int) -> int:
    upper = token.upper()
    if names and upper in names:
        value = names[upper]
    else:
        try:
            value = int(token, 10)
        except ValueError as exc:
            raise ValueError(f"invalid cron field value: {token!r}") from exc
    if value < low or value > high:
        raise ValueError(f"cron value {value} out of range [{low}, {high}]")
    return value


def _parse_field(field: str, low: int, high: int, names: dict[str, int] | None = None) -> tuple[set[int], bool]:
    """Return (allowed values, wildcard?) for one cron field."""
    values: set[int] = set()
    wildcard = field == "*"
    for part in field.split(","):
        if not part:
            raise ValueError("empty cron field segment")
        step = 1
        base = part
        if "/" in part:
            base, step_text = part.split("/", 1)
            try:
                step = int(step_text, 10)
            except ValueError as exc:
                raise ValueError(f"invalid cron step: {part!r}") from exc
            if step <= 0:
                raise ValueError(f"cron step must be positive: {part!r}")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _parse_value(start_text, names, low, high)
            end = _parse_value(end_text, names, low, high)
            if start > end:
                raise ValueError(f"invalid cron range: {part!r}")
        else:
            start = end = _parse_value(base, names, low, high)
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"empty cron field: {field!r}")
    return values, wildcard


def parse_cron(schedule: str) -> dict:
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError("expected exactly 5 fields: minute hour day-of-month month day-of-week")
    minute, minute_wild = _parse_field(fields[0], *FIELD_RANGES[0])
    hour, hour_wild = _parse_field(fields[1], *FIELD_RANGES[1])
    dom, dom_wild = _parse_field(fields[2], *FIELD_RANGES[2])
    month, month_wild = _parse_field(fields[3], *FIELD_RANGES[3], MONTH_NAMES)
    dow, dow_wild = _parse_field(fields[4], *FIELD_RANGES[4], DOW_NAMES)
    # Python's weekday(): Monday=0.  Cron's day-of-week: Sunday=0.
    dow_python = {(value - 1) % 7 for value in dow}
    return {
        "minute": minute,
        "hour": hour,
        "dom": dom,
        "month": month,
        "dow": dow_python,
        "dom_wild": dom_wild,
        "dow_wild": dow_wild,
        "minute_wild": minute_wild,
        "hour_wild": hour_wild,
        "month_wild": month_wild,
    }


def validate_cron(schedule: str) -> None:
    parse_cron(schedule)


def _matches_day(day: datetime, parsed: dict) -> bool:
    dom_match = day.day in parsed["dom"]
    dow_match = day.weekday() in parsed["dow"]
    # Standard cron: when both dom and dow are restricted, either may match.
    if parsed["dom_wild"] or parsed["dow_wild"]:
        return dom_match and dow_match
    return dom_match or dow_match


def next_schedule(schedule: str, after: datetime, timezone_name: str | None = None) -> datetime:
    parsed = parse_cron(schedule)
    tz = load_timezone(timezone_name)
    current = after.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Four years covers leap-day schedules (Feb 29).
    limit = current + timedelta(days=366 * 4 + 1)
    while current < limit:
        if current.month not in parsed["month"]:
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                current = current.replace(month=current.month + 1, day=1, hour=0, minute=0)
            continue
        if not _matches_day(current, parsed):
            current = (current + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if current.hour not in parsed["hour"]:
            current = (current + timedelta(hours=1)).replace(minute=0)
            continue
        if current.minute not in parsed["minute"]:
            current = current + timedelta(minutes=1)
            continue
        return current.astimezone(timezone.utc)
    raise ValueError(f"no matching schedule within four years: {schedule!r}")


def next_schedule_after(schedule: str, after: datetime, timezone_name: str | None = None) -> datetime:
    """Alias used by the controller for readability."""
    return next_schedule(schedule, after, timezone_name)
