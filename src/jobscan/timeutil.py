"""Time helpers.

All stored and displayed times are IST (UTC+05:30). IST has no daylight saving, so a fixed
offset is exact and avoids a timezone-database dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")

# Precision labels for a posted time derived from LinkedIn's relative text.
TIME_PRECISIONS = ("minute", "hour")
DATE_PRECISIONS = ("day", "week", "month", "year")
PRECISION_UNKNOWN = "unknown"

_UNITS: dict[str, tuple[timedelta, str]] = {
    "second": (timedelta(seconds=1), "minute"),
    "minute": (timedelta(minutes=1), "minute"),
    "hour": (timedelta(hours=1), "hour"),
    "day": (timedelta(days=1), "day"),
    "week": (timedelta(weeks=1), "week"),
    "month": (timedelta(days=30), "month"),
    "year": (timedelta(days=365), "year"),
}
_RELATIVE_RE = re.compile(
    r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)
_JUST_NOW_RE = re.compile(r"\b(just now|moments? ago)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RelativeAge:
    """How long ago something was posted, as LinkedIn states it."""

    delta: timedelta
    precision: str


def now_ist() -> datetime:
    return datetime.now(IST)


def to_iso(moment: datetime) -> str:
    """ISO 8601 in IST, seconds precision, for example 2026-10-15T09:05:00+05:30."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime is not allowed; attach a timezone")
    return moment.astimezone(IST).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp without offset: {value!r}")
    return moment.astimezone(IST)


def format_display(value: str | None, *, with_time: bool = True) -> str:
    """Render a stored ISO timestamp as 'YYYY-MM-DD HH:MM' (or date only) in IST."""
    if not value:
        return ""
    moment = from_iso(value)
    return moment.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def parse_relative(text: str | None) -> RelativeAge | None:
    """Parse text such as '21 hours ago' or '1 week ago'. Returns None if unrecognised."""
    if not text:
        return None
    if _JUST_NOW_RE.search(text):
        return RelativeAge(timedelta(0), "minute")
    match = _RELATIVE_RE.search(text)
    if match is None:
        return None
    unit_delta, precision = _UNITS[match.group(2).lower()]
    return RelativeAge(unit_delta * int(match.group(1)), precision)


def posted_from_relative(fetched_on: datetime, text: str | None) -> tuple[datetime | None, str]:
    """Posted time = fetch time minus the card's relative age. Returns (time, precision)."""
    age = parse_relative(text)
    if age is None:
        return None, PRECISION_UNKNOWN
    return fetched_on - age.delta, age.precision


def has_time_of_day(precision: str) -> bool:
    """True when the posted time is accurate enough to show hours and minutes."""
    return precision in TIME_PRECISIONS


def is_day(value: str) -> bool:
    """True for a real calendar date written exactly as YYYY-MM-DD."""
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def weekday_short(day: str) -> str:
    """Three-letter weekday of a YYYY-MM-DD date, or an empty string if it is not one."""
    return date.fromisoformat(day).strftime("%a") if is_day(day) else ""
