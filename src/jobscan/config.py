"""Scanner configuration: typed values, defaults and validation.

Values are stored as strings in the database (see store.py) and validated here, so a bad value
is rejected with a clear message instead of failing in the middle of a run.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields

from .matching import MATCH_MODES
from .models import STATUSES

VERIFIED_WINDOW_SECONDS = 86_400  # Only this value has been confirmed against LinkedIn.


class ConfigError(ValueError):
    """Raised with one message per invalid field."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{key}: {message}" for key, message in self.errors.items()))


@dataclass(frozen=True)
class Config:
    match_mode: str = "contains"
    location_name: str = "India"
    geo_id: str = "102713980"
    time_window_seconds: int = VERIFIED_WINDOW_SECONDS
    max_pages_per_keyword: int = 10
    stop_after_empty_pages: int = 2
    delay_seconds_min: float = 8.0
    delay_seconds_max: float = 12.0
    keyword_pause_min: float = 30.0
    keyword_pause_max: float = 60.0
    retry_count: int = 3
    retry_wait_min: float = 30.0
    retry_wait_max: float = 60.0
    abort_after_consecutive_throttles: int = 5
    min_gap_between_runs_minutes: int = 60
    skipped_titles_retention_days: int = 7
    detail_max_per_run: int = 60
    detail_max_age_days: int = 3
    fit_recency_days: int = 1
    fit_min_years: int = 3
    fit_junior_words: str = "intern, internship, junior, jr, trainee, associate"
    fit_junior_levels: str = "Internship, Entry level"
    fit_excluded_companies: str = "Amazon, Intuit"
    fit_skip_statuses: str = "Applied, Closed, Skipped, Not relevant"
    fit_deny_keywords: str = "sales, warehouse, SAP functional"
    fit_excluded_industries: str = ""
    score_after_scan: int = 1
    score_weight_keyword: int = 40
    score_weight_requirement: float = 2.0
    score_weight_responsibility: float = 1.0
    score_weight_company: float = 0.0
    score_top_k: int = 1
    score_max_chunks: int = 40
    score_chunk_words: int = 40
    score_kw_floor: float = 0.08
    score_kw_ceiling: float = 0.20
    score_sem_floor: float = 0.50
    score_sem_ceiling: float = 0.85
    score_strong_min: int = 70
    score_partial_min: int = 50
    embed_model: str = "gemini-embedding-001"
    embed_dims: int = 768
    embed_batch_size: int = 20
    embed_texts_per_minute: int = 90
    embed_tokens_per_minute: int = 25_000
    embed_daily_limit: int = 1_000

    @property
    def embed_space(self) -> str:
        """Names the vector space. Vectors from different spaces are never compared."""
        return f"{self.embed_model}@{self.embed_dims}"

    @property
    def window_is_verified(self) -> bool:
        return self.time_window_seconds == VERIFIED_WINDOW_SECONDS

    def to_raw(self) -> dict[str, str]:
        return {f.name: str(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_raw(cls, raw: Mapping[str, str]) -> Config:
        """Build a Config from string values; missing keys fall back to defaults."""
        defaults = cls()
        values: dict[str, object] = {}
        errors: dict[str, str] = {}
        for key, (kind, low, high) in _NUMERIC_SPECS.items():
            text = raw.get(key)
            if text is None:
                values[key] = getattr(defaults, key)
                continue
            try:
                values[key] = _parse_number(text, kind, low, high)
            except ValueError as exc:
                errors[key] = str(exc)
        _parse_text_fields(raw, defaults, values, errors)
        for low_key, high_key in _RANGE_PAIRS:
            low_value, high_value = values.get(low_key), values.get(high_key)
            if (
                isinstance(low_value, float)
                and isinstance(high_value, float)
                and low_value > high_value
            ):
                errors[low_key] = f"must not exceed {high_key}"
        for low_key, high_key in _ORDERED_PAIRS:
            low_value, high_value = values.get(low_key), values.get(high_key)
            if (
                isinstance(low_value, int | float)
                and isinstance(high_value, int | float)
                and low_value >= high_value
            ):
                errors[low_key] = f"must be below {high_key}"
        if errors:
            raise ConfigError(errors)
        return cls(**values)  # type: ignore[arg-type]


# key -> (type, minimum, maximum)
_NUMERIC_SPECS: dict[str, tuple[type, float, float]] = {
    "time_window_seconds": (int, 3_600, 2_592_000),
    "max_pages_per_keyword": (int, 1, 100),
    "stop_after_empty_pages": (int, 1, 20),
    "delay_seconds_min": (float, 1, 120),
    "delay_seconds_max": (float, 1, 120),
    "keyword_pause_min": (float, 0, 600),
    "keyword_pause_max": (float, 0, 600),
    "retry_count": (int, 0, 10),
    "retry_wait_min": (float, 1, 600),
    "retry_wait_max": (float, 1, 600),
    "abort_after_consecutive_throttles": (int, 1, 50),
    "min_gap_between_runs_minutes": (int, 0, 1_440),
    "skipped_titles_retention_days": (int, 1, 365),
    "detail_max_per_run": (int, 0, 500),
    "detail_max_age_days": (int, 1, 30),
    "fit_recency_days": (int, 1, 365),
    "fit_min_years": (int, 0, 30),
    "score_after_scan": (int, 0, 1),
    "score_weight_keyword": (int, 0, 100),
    "score_weight_requirement": (float, 0, 10),
    "score_weight_responsibility": (float, 0, 10),
    "score_weight_company": (float, 0, 10),
    "score_top_k": (int, 1, 5),
    "score_max_chunks": (int, 5, 200),
    "score_chunk_words": (int, 10, 100),
    "score_kw_floor": (float, 0, 1),
    "score_kw_ceiling": (float, 0, 1),
    "score_sem_floor": (float, 0, 1),
    "score_sem_ceiling": (float, 0, 1),
    "score_strong_min": (int, 1, 100),
    "score_partial_min": (int, 1, 100),
    "embed_dims": (int, 128, 3072),
    "embed_batch_size": (int, 1, 100),
    "embed_texts_per_minute": (int, 1, 10_000),
    "embed_tokens_per_minute": (int, 1_000, 10_000_000),
    "embed_daily_limit": (int, 1, 1_000_000),
}
# Settings holding a comma-separated list. They are stored tidy: "a, b, c".
LIST_SETTINGS = (
    "fit_junior_words",
    "fit_junior_levels",
    "fit_excluded_companies",
    "fit_skip_statuses",
    "fit_deny_keywords",
    "fit_excluded_industries",
)
MAX_LIST_ITEMS = 100
MAX_LIST_ITEM_CHARS = 80
# (low, high): low must stay strictly below high.
_ORDERED_PAIRS = (
    ("score_kw_floor", "score_kw_ceiling"),
    ("score_sem_floor", "score_sem_ceiling"),
    ("score_partial_min", "score_strong_min"),
)
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_RANGE_PAIRS = (
    ("delay_seconds_min", "delay_seconds_max"),
    ("keyword_pause_min", "keyword_pause_max"),
    ("retry_wait_min", "retry_wait_max"),
)


def _parse_number(text: str, kind: type, low: float, high: float) -> int | float:
    cleaned = text.strip()
    try:
        number: int | float = int(cleaned) if kind is int else float(cleaned)
    except ValueError:
        raise ValueError(f"must be a {'whole number' if kind is int else 'number'}") from None
    if isinstance(number, float) and not math.isfinite(number):
        raise ValueError("must be a finite number")
    if not low <= number <= high:
        raise ValueError(f"must be between {low:g} and {high:g}")
    return number


def _parse_text_fields(
    raw: Mapping[str, str],
    defaults: Config,
    values: dict[str, object],
    errors: dict[str, str],
) -> None:
    mode = raw.get("match_mode", defaults.match_mode).strip()
    if mode in MATCH_MODES:
        values["match_mode"] = mode
    else:
        errors["match_mode"] = f"must be one of: {', '.join(MATCH_MODES)}"

    location = raw.get("location_name", defaults.location_name).strip()
    if 0 < len(location) <= 80:
        values["location_name"] = location
    else:
        errors["location_name"] = "must be 1 to 80 characters"

    geo_id = raw.get("geo_id", defaults.geo_id).strip()
    if geo_id.isascii() and geo_id.isdigit():
        values["geo_id"] = geo_id
    else:
        errors["geo_id"] = "must be digits only"

    model = raw.get("embed_model", defaults.embed_model).strip()
    if _MODEL_NAME.fullmatch(model):
        values["embed_model"] = model
    else:
        errors["embed_model"] = "letters, digits, dots, dashes and underscores only"

    for key in LIST_SETTINGS:
        try:
            items = split_list(raw.get(key, getattr(defaults, key)))
            if key == "fit_skip_statuses":
                items = _canonical_statuses(items)
            _check_list(items)
        except ValueError as exc:
            errors[key] = str(exc)
        else:
            values[key] = ", ".join(items)


def split_list(text: str) -> tuple[str, ...]:
    """Items of a comma or line separated list: trimmed, no blanks, no repeats (any case)."""
    items: dict[str, str] = {}
    for part in text.replace("\n", ",").split(","):
        item = " ".join(part.split())
        if item:
            items.setdefault(item.casefold(), item)
    return tuple(items.values())


def _check_list(items: tuple[str, ...]) -> None:
    if len(items) > MAX_LIST_ITEMS:
        raise ValueError(f"at most {MAX_LIST_ITEMS} items")
    if any(len(item) > MAX_LIST_ITEM_CHARS for item in items):
        raise ValueError(f"each item must be at most {MAX_LIST_ITEM_CHARS} characters")


def _canonical_statuses(items: tuple[str, ...]) -> tuple[str, ...]:
    by_name = {status.casefold(): status for status in STATUSES}
    unknown = [item for item in items if item.casefold() not in by_name]
    if unknown:
        raise ValueError(f"not a status: {', '.join(unknown)}")
    return tuple(by_name[item.casefold()] for item in items)
