from datetime import datetime, timezone

import pytest

from minikuberay.models.raycronjob import next_schedule, parse_cron, validate_cron


def next_utc(schedule, after, tz="UTC"):
    return next_schedule(schedule, after, tz)


def dt(year=2026, month=1, day=1, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_every_five_minutes():
    assert next_utc("*/5 * * * *", dt()) == dt(minute=5)
    assert next_utc("*/5 * * * *", dt(minute=4)) == dt(minute=5)
    assert next_utc("*/5 * * * *", dt(minute=5)) == dt(minute=10)


def test_lists_ranges_and_steps():
    assert next_utc("0,15,30,45 * * * *", dt(minute=16)) == dt(minute=30)
    assert next_utc("10-20/5 * * * *", dt(minute=11)) == dt(minute=15)
    assert next_utc("0 9-17 * * *", dt(hour=8)) == dt(hour=9)
    assert next_utc("0 9-17 * * *", dt(hour=17)) == dt(day=2, hour=9)


def test_named_months_and_days():
    assert next_utc("0 0 1 JAN *", dt(year=2025, month=12, day=31)) == dt(year=2026, month=1, day=1)
    # 2026-01-01 is a Thursday; next Monday is Jan 5.
    assert next_utc("0 0 * * MON", dt()) == dt(day=5)


def test_day_of_month_or_day_of_week():
    # Both restricted: either may match.  From Jan 1 12:00, Jan 2 00:00 is the
    # next Friday; from Jan 12 the 13th matches the day-of-month.
    assert next_utc("0 0 13 * FRI", dt(day=1, hour=12)) == dt(day=2)
    assert next_utc("0 0 13 * FRI", dt(day=12)) == dt(day=13)


def test_timezone_conversion():
    # 00:00 in Shanghai is 16:00 UTC the previous day.
    result = next_utc("0 0 * * *", dt(day=1, hour=12), "Asia/Shanghai")
    assert result == dt(day=1, hour=16)


def test_leap_day():
    result = next_utc("0 0 29 FEB *", dt(year=2026, month=3, day=1))
    assert result == dt(year=2028, month=2, day=29)


def test_invalid_schedules():
    for schedule in ("", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 7", "*/0 * * * *", "a b c d e"):
        with pytest.raises(ValueError):
            validate_cron(schedule)


def test_parse_cron_shape():
    parsed = parse_cron("*/2 0 * * *")
    assert 0 in parsed["minute"] and 58 in parsed["minute"] and 1 not in parsed["minute"]
    assert parsed["dom_wild"] is True
