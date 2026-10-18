"""Scanner configuration: typed values, defaults and validation.

Values are stored as strings in the database (see store.py) and validated here, so a bad value
is rejected with a clear message instead of failing in the middle of a run.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields

from .matching import MATCH_MODES

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
}
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
