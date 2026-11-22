"""A dependency-free five-field cron parser.

Kubernetes CronJob uses the robfig/cron parser: five fields (minute, hour,
day-of-month, month, day-of-week), ``*``, ranges, lists and steps.  When both
day-of-month and day-of-week are restricted, a time matches if *either*
matches, matching traditional cron semantics.
"""

from __future__ import annotations

from datetime import datetime, timedelta

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

LIMITS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]


def _parse_atom(atom: str, low: int, high: int, names: dict[str, int] | None, field: str) -> int:
    atom = atom.strip().lower()
    if names and atom in names:
        return names[atom]
    try:
        value = int(atom)
    except ValueError as exc:
        raise ValueError(f"invalid {field} value: {atom!r}") from exc
    if not low <= value <= high:
        raise ValueError(f"{field} value {value} out of range {low}-{high}")
    return value


def _parse_field(text: str, index: int, names: dict[str, int] | None = None) -> set[int]:
    low, high = LIMITS[index]
    names_map = names if index in (3, 4) else None
    field = ["minute", "hour", "day-of-month", "month", "day-of-week"][index]
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty {field} field")
        step = 1
        if "/" in part:
            base, raw_step = part.split("/", 1)
            if not raw_step.isdigit() or int(raw_step) <= 0:
                raise ValueError(f"invalid step in {field}: {part!r}")
            step = int(raw_step)
        else:
            base = part
        if base == "*":
            start, end = low, high
        elif "-" in base:
            left, right = base.split("-", 1)
            start = _parse_atom(left, low, high, names_map, field)
            end = _parse_atom(right, low, high, names_map, field)
            if start > end:
                raise ValueError(f"invalid range in {field}: {base!r}")
        else:
            start = _parse_atom(base, low, high, names_map, field)
            end = high if step > 1 else start
        values.update(range(start, end + 1, step))
    if index == 4 and 7 in values:
        values.discard(7)
        values.add(0)
    return values


class CronSchedule:
    def __init__(self, expression: str):
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(f"expected 5 cron fields, got {len(fields)}")
        self.expression = expression
        self.minutes = _parse_field(fields[0], 0)
        self.hours = _parse_field(fields[1], 1)
        self.days = _parse_field(fields[2], 2)
        self.months = _parse_field(fields[3], 3, MONTHS)
        self.weekdays = _parse_field(fields[4], 4, WEEKDAYS)
        self.day_restricted = fields[2] != "*"
        self.weekday_restricted = fields[4] != "*"

    def matches(self, moment: datetime) -> bool:
        # Python's weekday(): Monday=0.  Cron: Sunday=0, Monday=1.
        cron_weekday = (moment.weekday() + 1) % 7
        if moment.minute not in self.minutes or moment.hour not in self.hours or moment.month not in self.months:
            return False
        if self.day_restricted and self.weekday_restricted:
            return moment.day in self.days or cron_weekday in self.weekdays
        if self.day_restricted:
            return moment.day in self.days
        if self.weekday_restricted:
            return cron_weekday in self.weekdays
        return True

    def next_after(self, moment: datetime, *, inclusive: bool = False) -> datetime:
        candidate = moment.replace(second=0, microsecond=0)
        if not inclusive:
            candidate += timedelta(minutes=1)
        # Five years is far beyond the supported pause horizon; fail loudly.
        for _ in range(60 * 24 * 366 * 5):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError(f"cron expression {self.expression!r} never fires")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CronSchedule({self.expression!r})"


def parse_cron(expression: str) -> CronSchedule:
    return CronSchedule(expression)
