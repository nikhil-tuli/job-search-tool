from __future__ import annotations

import pytest

from jobscan.config import Config, ConfigError


def test_defaults_match_the_agreed_design() -> None:
    cfg = Config()
    assert cfg.match_mode == "contains"
    assert (cfg.location_name, cfg.geo_id) == ("India", "102713980")
    assert cfg.time_window_seconds == 86_400
    assert cfg.max_pages_per_keyword == 10
    assert cfg.stop_after_empty_pages == 2
    assert (cfg.delay_seconds_min, cfg.delay_seconds_max) == (8.0, 12.0)
    assert (cfg.keyword_pause_min, cfg.keyword_pause_max) == (30.0, 60.0)
    assert cfg.retry_count == 3
    assert (cfg.retry_wait_min, cfg.retry_wait_max) == (30.0, 60.0)
    assert cfg.abort_after_consecutive_throttles == 5
    assert cfg.min_gap_between_runs_minutes == 60
    assert cfg.skipped_titles_retention_days == 7


def test_empty_raw_gives_defaults() -> None:
    assert Config.from_raw({}) == Config()


def test_round_trip_through_raw() -> None:
    cfg = Config(max_pages_per_keyword=5, delay_seconds_min=2.5, match_mode="exact")
    assert Config.from_raw(cfg.to_raw()) == cfg


def test_partial_override_keeps_other_defaults() -> None:
    cfg = Config.from_raw({"max_pages_per_keyword": "4"})
    assert cfg.max_pages_per_keyword == 4
    assert cfg.retry_count == 3


def test_values_are_stripped_and_typed() -> None:
    cfg = Config.from_raw({"retry_count": " 2 ", "delay_seconds_min": " 3 ", "geo_id": " 123 "})
    assert cfg.retry_count == 2
    assert cfg.delay_seconds_min == 3.0
    assert isinstance(cfg.delay_seconds_min, float)
    assert cfg.geo_id == "123"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("time_window_seconds", "3599"),
        ("time_window_seconds", "2592001"),
        ("max_pages_per_keyword", "0"),
        ("max_pages_per_keyword", "101"),
        ("stop_after_empty_pages", "0"),
        ("delay_seconds_min", "0.5"),
        ("delay_seconds_max", "121"),
        ("retry_count", "-1"),
        ("retry_count", "11"),
        ("abort_after_consecutive_throttles", "0"),
        ("min_gap_between_runs_minutes", "-5"),
        ("skipped_titles_retention_days", "0"),
    ],
)
def test_out_of_range_values_are_rejected(key: str, value: str) -> None:
    with pytest.raises(ConfigError) as info:
        Config.from_raw({key: value})
    assert key in info.value.errors
    assert "between" in info.value.errors[key]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_pages_per_keyword", "ten"),
        ("max_pages_per_keyword", "3.5"),
        ("max_pages_per_keyword", ""),
        ("delay_seconds_min", "fast"),
        ("delay_seconds_min", "nan"),
        ("delay_seconds_max", "inf"),
    ],
)
def test_non_numeric_values_are_rejected(key: str, value: str) -> None:
    with pytest.raises(ConfigError) as info:
        Config.from_raw({key: value})
    assert key in info.value.errors


@pytest.mark.parametrize(
    "raw",
    [
        {"delay_seconds_min": "20", "delay_seconds_max": "10"},
        {"keyword_pause_min": "90", "keyword_pause_max": "30"},
        {"retry_wait_min": "70", "retry_wait_max": "60"},
    ],
)
def test_min_above_max_is_rejected(raw: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="must not exceed"):
        Config.from_raw(raw)


def test_equal_min_and_max_is_allowed() -> None:
    cfg = Config.from_raw({"delay_seconds_min": "10", "delay_seconds_max": "10"})
    assert cfg.delay_seconds_min == cfg.delay_seconds_max == 10.0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("match_mode", "fuzzy"),
        ("location_name", ""),
        ("location_name", "x" * 81),
        ("geo_id", "abc"),
        ("geo_id", ""),
        ("geo_id", "12 3"),
    ],
)
def test_bad_text_fields_are_rejected(key: str, value: str) -> None:
    with pytest.raises(ConfigError) as info:
        Config.from_raw({key: value})
    assert key in info.value.errors


def test_all_errors_are_reported_together() -> None:
    with pytest.raises(ConfigError) as info:
        Config.from_raw({"retry_count": "x", "geo_id": "y", "match_mode": "z"})
    assert set(info.value.errors) == {"retry_count", "geo_id", "match_mode"}
    assert "retry_count" in str(info.value)


def test_window_verification_flag() -> None:
    assert Config().window_is_verified
    assert not Config(time_window_seconds=172_800).window_is_verified
