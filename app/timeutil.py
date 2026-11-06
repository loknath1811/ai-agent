"""Time helpers.

Rule used everywhere: datetimes stored in the database are NAIVE UTC.
We convert to the company timezone only for humans (emails, dashboard) and
for working-hours checks.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .config import get_settings


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(tz())


def local_to_utc(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=tz())
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def parse_due(value: Optional[str]) -> Optional[datetime]:
    """'2026-10-02' or '2026-10-02T15:30' (company local time) -> naive UTC.

    A date without a time means "end of the working day" on that date.
    """
    if not value:
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if len(value) <= 10:  # date only
        parsed = datetime.combine(
            parsed.date(), time(get_settings().work_end_hour, 0)
        )
    return local_to_utc(parsed)


def fmt_local(dt_utc: Optional[datetime]) -> str:
    if not dt_utc:
        return "no deadline"
    return to_local(dt_utc).strftime("%a %d %b %Y, %H:%M")


def is_work_time(now_utc: Optional[datetime] = None) -> bool:
    """True during working hours (and on working days) in company time."""
    s = get_settings()
    local = to_local(now_utc or utcnow())
    if s.skip_weekends and local.weekday() >= 5:
        return False
    return s.work_start_hour <= local.hour < s.work_end_hour


def add_working_days(start: date, n: int) -> date:
    """The date n WORKING days after `start`: Saturdays and Sundays do not count. (Public holidays are not
    known to the system, so they do count as working days.) Counted by code because the AI gets it wrong."""
    day = start
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day
