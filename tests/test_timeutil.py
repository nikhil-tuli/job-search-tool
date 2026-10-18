from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobscan.timeutil import (
    IST,
    format_display,
    from_iso,
    has_time_of_day,
    now_ist,
    parse_relative,
    posted_from_relative,
    to_iso,
)

FETCHED = datetime(2026, 10, 15, 9, 5, tzinfo=IST)


def test_ist_offset_is_five_thirty() -> None:
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


def test_now_ist_is_timezone_aware_in_ist() -> None:
    assert now_ist().utcoffset() == timedelta(hours=5, minutes=30)


def test_to_iso_converts_other_zones_to_ist() -> None:
    utc_moment = datetime(2026, 10, 15, 3, 35, tzinfo=timezone.utc)
    assert to_iso(utc_moment) == "2026-10-15T09:05:00+05:30"


def test_to_iso_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_iso(datetime(2026, 10, 15, 9, 5))


def test_from_iso_round_trip() -> None:
    assert from_iso(to_iso(FETCHED)) == FETCHED


def test_from_iso_rejects_missing_offset() -> None:
    with pytest.raises(ValueError, match="offset"):
        from_iso("2026-10-15T09:05:00")


def test_format_display_with_and_without_time() -> None:
    stored = "2026-10-15T09:05:00+05:30"
    assert format_display(stored) == "2026-10-15 09:05"
    assert format_display(stored, with_time=False) == "2026-10-15"


def test_format_display_empty_values() -> None:
    assert format_display(None) == ""
    assert format_display("") == ""


@pytest.mark.parametrize(
    ("text", "delta", "precision"),
    [
        ("21 hours ago", timedelta(hours=21), "hour"),
        ("1 hour ago", timedelta(hours=1), "hour"),
        ("9 minutes ago", timedelta(minutes=9), "minute"),
        ("30 seconds ago", timedelta(seconds=30), "minute"),
        ("1 day ago", timedelta(days=1), "day"),
        ("2 weeks ago", timedelta(weeks=2), "week"),
        ("3 months ago", timedelta(days=90), "month"),
        ("1 year ago", timedelta(days=365), "year"),
        ("  Reposted 4 HOURS ago ", timedelta(hours=4), "hour"),
        ("Just now", timedelta(0), "minute"),
        ("moments ago", timedelta(0), "minute"),
    ],
)
def test_parse_relative(text: str, delta: timedelta, precision: str) -> None:
    age = parse_relative(text)
    assert age is not None
    assert age.delta == delta
    assert age.precision == precision


@pytest.mark.parametrize("text", [None, "", "yesterday", "ago", "hours ago", "x hours ago"])
def test_parse_relative_unrecognised(text: str | None) -> None:
    assert parse_relative(text) is None


def test_posted_from_relative_hours() -> None:
    posted, precision = posted_from_relative(FETCHED, "3 hours ago")
    assert posted == datetime(2026, 10, 15, 6, 5, tzinfo=IST)
    assert precision == "hour"


def test_posted_from_relative_crosses_midnight() -> None:
    posted, _ = posted_from_relative(FETCHED, "11 hours ago")
    assert posted == datetime(2026, 10, 14, 22, 5, tzinfo=IST)


def test_posted_from_relative_unknown() -> None:
    assert posted_from_relative(FETCHED, "recently") == (None, "unknown")
    assert posted_from_relative(FETCHED, None) == (None, "unknown")


@pytest.mark.parametrize(
    ("precision", "expected"),
    [
        ("minute", True),
        ("hour", True),
        ("day", False),
        ("week", False),
        ("month", False),
        ("year", False),
        ("unknown", False),
    ],
)
def test_has_time_of_day(precision: str, expected: bool) -> None:
    assert has_time_of_day(precision) is expected


def test_is_day_and_weekday_short() -> None:
    from jobscan.timeutil import is_day, weekday_short

    assert is_day("2026-10-18")
    for bad in ("2026-10-1", "2026-02-30", "18-10-2026", "", " 2026-10-18"):
        assert not is_day(bad)
    assert weekday_short("2026-10-18") == "Sun"
    assert weekday_short("nonsense") == ""
