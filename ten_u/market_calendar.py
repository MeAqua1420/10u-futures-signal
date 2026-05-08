from __future__ import annotations

from datetime import datetime, date, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from ten_u.models import ms_to_utc


US_EASTERN = ZoneInfo("America/New_York")


def us_eastern_date(timestamp_ms: int) -> date:
    return ms_to_utc(timestamp_ms).astimezone(US_EASTERN).date()


def is_us_market_non_workday(timestamp_ms: int) -> bool:
    return is_us_market_non_workday_date(us_eastern_date(timestamp_ms))


def is_us_market_non_workday_date(day: date) -> bool:
    if day.weekday() >= 5:
        return True
    return _nyse_is_closed(day.isoformat())


def recent_us_market_non_workdays(count: int, end_timestamp_ms: int | None = None) -> list[date]:
    if count <= 0:
        raise ValueError("count must be positive")
    if end_timestamp_ms is None:
        current = datetime.now(US_EASTERN).date()
    else:
        current = us_eastern_date(end_timestamp_ms)
    days: list[date] = []
    while len(days) < count:
        if is_us_market_non_workday_date(current):
            days.append(current)
        current -= timedelta(days=1)
    return sorted(days)


def us_eastern_day_bounds_ms(day: date) -> tuple[int, int]:
    start = datetime.combine(day, time.min, tzinfo=US_EASTERN)
    end = datetime.combine(day, time.max, tzinfo=US_EASTERN)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


@lru_cache(maxsize=4096)
def _nyse_is_closed(day_iso: str) -> bool:
    day = date.fromisoformat(day_iso)
    try:
        import pandas_market_calendars as market_calendars
    except ImportError:
        return day in _fallback_nyse_full_holidays(day.year)
    nyse = market_calendars.get_calendar("NYSE")
    schedule = nyse.schedule(start_date=day_iso, end_date=day_iso)
    return schedule.empty


@lru_cache(maxsize=64)
def _fallback_nyse_full_holidays(year: int) -> frozenset[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _good_friday(year),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
        _observed(date(year + 1, 1, 1)),
    }
    return frozenset(day for day in holidays if day.year == year)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    day = date(year, month, 1)
    offset = (weekday - day.weekday()) % 7
    return day + timedelta(days=offset + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _good_friday(year: int) -> date:
    return _easter_sunday(year) - timedelta(days=2)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)
