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
    assert (cfg.detail_max_per_run, cfg.detail_max_age_days) == (60, 3)


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
        ("detail_max_per_run", "-1"),
        ("detail_max_per_run", "501"),
        ("detail_max_age_days", "0"),
        ("detail_max_age_days", "31"),
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


# ---- scoring settings ----------------------------------------------------------------------


def test_scoring_defaults() -> None:
    cfg = Config()
    assert cfg.score_after_scan == 1
    assert (cfg.score_weight_keyword, cfg.score_weight_requirement) == (40, 2.0)
    assert (cfg.score_weight_responsibility, cfg.score_weight_company) == (1.0, 0.0)
    assert (cfg.score_kw_floor, cfg.score_kw_ceiling) == (0.08, 0.20)
    assert (cfg.score_sem_floor, cfg.score_sem_ceiling) == (0.50, 0.85)
    assert (cfg.score_partial_min, cfg.score_strong_min) == (50, 70)
    assert cfg.embed_model == "gemini-embedding-001"
    assert cfg.embed_space == "gemini-embedding-001@768"


def test_scoring_settings_round_trip() -> None:
    cfg = Config(score_weight_keyword=25, score_top_k=2, embed_dims=512, embed_model="m-1")
    assert Config.from_raw(cfg.to_raw()) == cfg
    assert cfg.embed_space == "m-1@512"


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ({"score_kw_floor": "0.3", "score_kw_ceiling": "0.2"}, "score_kw_floor"),
        ({"score_sem_floor": "0.9", "score_sem_ceiling": "0.9"}, "score_sem_floor"),
        ({"score_partial_min": "80", "score_strong_min": "70"}, "score_partial_min"),
        ({"embed_model": "bad model!"}, "embed_model"),
        ({"embed_model": ""}, "embed_model"),
        ({"score_weight_keyword": "101"}, "score_weight_keyword"),
        ({"score_weight_keyword": "-1"}, "score_weight_keyword"),
        ({"score_top_k": "0"}, "score_top_k"),
        ({"embed_batch_size": "0"}, "embed_batch_size"),
        ({"embed_daily_limit": "abc"}, "embed_daily_limit"),
    ],
)
def test_invalid_scoring_settings_are_rejected(raw: dict[str, str], key: str) -> None:
    with pytest.raises(ConfigError) as caught:
        Config.from_raw(raw)
    assert key in caught.value.errors


def test_ordered_pair_message_names_the_other_setting() -> None:
    with pytest.raises(ConfigError) as caught:
        Config.from_raw({"score_partial_min": "80", "score_strong_min": "70"})
    assert "must be below" in caught.value.errors["score_partial_min"]
