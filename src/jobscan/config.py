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

VERIFIED_WINDOW_SECONDS = 86_400  # Only this value has been confirmed against LinkedIn.
MAX_FOCUS_CHARS = 4_000
DEFAULT_INTERVIEW_ROLES = (
    "Backend Engineer, Frontend Engineer, Full-stack Engineer, Mobile Engineer, "
    "Data Engineer, Data Scientist, ML / AI Engineer, DevOps / SRE Engineer, QA Engineer, "
    "Security Engineer, Engineering Manager, Technical Program Manager, Program Manager, "
    "Project Manager, Product Manager, Business Analyst, Designer"
)
MAX_INTERVIEW_ROLES = 50
MAX_INTERVIEW_ROLE_CHARS = 60
DEFAULT_ROLE_FOCUS = (
    "Roles I want: Technical Program Manager, Senior Program Manager or Delivery Manager in a "
    "technology company. The work is owning cross-team technical delivery: planning, "
    "dependencies, risks, vendor and API integrations, stakeholder updates.\n"
    "Not what I want: sales or business development; warehouse, supply chain or other "
    "operations programs; hands-on software engineering; non-technical, event, HR or "
    "recruitment program roles; pure product management.\n"
    "Domains: fintech, payments, SaaS and platform are preferred. Others are acceptable."
)


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
    fit_min_salary: int = 1_000_000
    fit_junior_words: str = "intern, internship, junior, jr, trainee, associate"
    fit_junior_levels: str = "Internship, Entry level"
    fit_excluded_companies: str = "Amazon, Intuit"
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
    legacy_scoring: int = 0
    ai_scoring: int = 1
    ai_base_url: str = "https://api.mistral.ai/v1"
    ai_model: str = "mistral-small-latest"
    ai_key_env: str = "MISTRAL_API_KEY"
    ai_role_focus: str = DEFAULT_ROLE_FOCUS
    ai_extra_rules: str = ""
    ai_max_missing: int = 2
    ai_max_words: int = 3_000
    ai_calls_per_minute: int = 4
    langfuse_on: int = 0
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_public_env: str = "LANGFUSE_PUBLIC_KEY"
    langfuse_secret_env: str = "LANGFUSE_SECRET_KEY"
    langfuse_send_text: int = 0
    naukri_location: str = "india"
    naukri_max_pages: int = 5
    naukri_delay_seconds_min: float = 6.0
    naukri_delay_seconds_max: float = 10.0
    naukri_max_job_pages: int = 40
    naukri_max_age_hours: int = 24
    naukri_abort_after_captchas: int = 3
    naukri_captcha_wait_seconds: int = 300
    interview_roles: str = DEFAULT_INTERVIEW_ROLES
    interview_stt_base_url: str = "https://api.groq.com/openai/v1"
    interview_stt_model: str = "whisper-large-v3"
    interview_stt_key_env: str = "GROQ_API_KEY"
    interview_ai_base_url: str = ""  # empty: the same as Fit scoring (AI)
    interview_ai_model: str = ""
    interview_ai_key_env: str = ""
    interview_reply_cap: int = 6_000
    interview_repeat_window: int = 5  # do not repeat questions from this many past interviews
    interview_amber_s: int = 120
    interview_over_s: int = 180
    interview_stop_s: int = 300
    interview_read_aloud: int = 1
    tailor_base_url: str = "https://api.groq.com/openai/v1"
    tailor_model: str = "openai/gpt-oss-120b"
    tailor_key_env: str = "GROQ_API_KEY"
    sync_email: str = ""
    sync_password_env: str = "JOBSYNC_APP_PASSWORD"
    sync_every_hours: int = 4
    sync_first_days: int = 30
    sync_use_ai: int = 1
    sites_location_words: str = (
        "India, Bengaluru, Bangalore, Hyderabad, Pune, Chennai, Mumbai, Gurugram, Gurgaon, "
        "Noida, Delhi"
    )
    sites_max_age_days: int = 60
    sites_delay_seconds: float = 3.0
    sites_fetch_descriptions: int = 1
    sites_max_roles_per_company: int = 500
    sites_daily: int = 1
    schedule_time: str = "08:00"
    schedule_timeout_minutes: int = 180
    schedule_notify_finish: int = 0

    @property
    def embed_space(self) -> str:
        """Names the vector space. Vectors from different spaces are never compared."""
        return f"{self.embed_model}@{self.embed_dims}"

    @property
    def interview_role_list(self) -> tuple[str, ...]:
        return split_list(self.interview_roles)

    @property
    def interview_ai(self) -> tuple[str, str, str]:
        """(address, model, key variable) for interview questions and feedback.

        Each one left empty in Interview Prep follows Fit scoring (AI).
        """
        return (
            self.interview_ai_base_url or self.ai_base_url,
            self.interview_ai_model or self.ai_model,
            self.interview_ai_key_env or self.ai_key_env,
        )

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
    "fit_min_salary": (int, 0, 1_000_000_000),
    "score_after_scan": (int, 0, 1),
    "sync_every_hours": (int, 1, 24),
    "sync_first_days": (int, 1, 90),
    "sync_use_ai": (int, 0, 1),
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
    "naukri_max_pages": (int, 1, 50),
    "naukri_delay_seconds_min": (float, 1, 120),
    "naukri_delay_seconds_max": (float, 1, 120),
    "naukri_max_job_pages": (int, 0, 500),
    "naukri_max_age_hours": (int, 1, 720),
    "naukri_abort_after_captchas": (int, 1, 20),
    "naukri_captcha_wait_seconds": (int, 30, 1_800),
    "sites_max_age_days": (int, 1, 365),
    "sites_delay_seconds": (float, 0, 60),
    "sites_fetch_descriptions": (int, 0, 1),
    "sites_max_roles_per_company": (int, 1, 5_000),
    "sites_daily": (int, 0, 1),
    "legacy_scoring": (int, 0, 1),
    "ai_scoring": (int, 0, 1),
    "ai_max_missing": (int, 0, 10),
    "ai_max_words": (int, 500, 20_000),
    "ai_calls_per_minute": (int, 1, 600),
    "interview_reply_cap": (int, 1_000, 16_000),
    "interview_repeat_window": (int, 0, 50),
    "interview_amber_s": (int, 30, 600),
    "interview_over_s": (int, 30, 600),
    "interview_stop_s": (int, 30, 600),
    "interview_read_aloud": (int, 0, 1),
    "langfuse_on": (int, 0, 1),
    "langfuse_send_text": (int, 0, 1),
    "schedule_timeout_minutes": (int, 5, 240),
    "schedule_notify_finish": (int, 0, 1),
}
# Settings holding a comma-separated list. They are stored tidy: "a, b, c".
LIST_SETTINGS = (
    "fit_junior_words",
    "fit_junior_levels",
    "fit_excluded_companies",
    "fit_deny_keywords",
    "fit_excluded_industries",
    "sites_location_words",
)
MAX_LIST_ITEMS = 100
MAX_LIST_ITEM_CHARS = 80
# (low, high): low must stay strictly below high.
_ORDERED_PAIRS = (
    ("score_kw_floor", "score_kw_ceiling"),
    ("score_sem_floor", "score_sem_ceiling"),
    ("score_partial_min", "score_strong_min"),
    ("interview_amber_s", "interview_over_s"),
    ("interview_over_s", "interview_stop_s"),
)
_CLOCK_TIME = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d")
_PLACE = re.compile(r"[a-z][a-z -]{0,59}")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,79}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,120}\.[A-Za-z]{2,}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SERVICE_URL = re.compile(
    r"https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/-]*)?|http://(?:localhost|127\.0\.0\.1)(?::\d{1,5})?(?:/[A-Za-z0-9._~/-]*)?"
)
_RANGE_PAIRS = (
    ("delay_seconds_min", "delay_seconds_max"),
    ("keyword_pause_min", "keyword_pause_max"),
    ("retry_wait_min", "retry_wait_max"),
    ("naukri_delay_seconds_min", "naukri_delay_seconds_max"),
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

    place = " ".join(raw.get("naukri_location", defaults.naukri_location).lower().split())
    if _PLACE.fullmatch(place):
        values["naukri_location"] = place
    else:
        errors["naukri_location"] = "letters, spaces and dashes only, up to 60 characters"

    model = raw.get("embed_model", defaults.embed_model).strip()
    if _MODEL_NAME.fullmatch(model):
        values["embed_model"] = model
    else:
        errors["embed_model"] = "letters, digits, dots, dashes and underscores only"

    url = raw.get("tailor_base_url", defaults.tailor_base_url).strip().rstrip("/")
    if _SERVICE_URL.fullmatch(url) and len(url) <= 200:
        values["tailor_base_url"] = url
    else:
        errors["tailor_base_url"] = (
            "an https:// address, for example https://api.groq.com/openai/v1"
        )

    tailor_model = raw.get("tailor_model", defaults.tailor_model).strip()
    if _MODEL_NAME.fullmatch(tailor_model):
        values["tailor_model"] = tailor_model
    else:
        errors["tailor_model"] = "letters, digits, dots, dashes, slashes and underscores only"

    key_env = raw.get("tailor_key_env", defaults.tailor_key_env).strip()
    if _ENV_NAME.fullmatch(key_env):
        values["tailor_key_env"] = key_env
    else:
        errors["tailor_key_env"] = "a variable name: letters, digits and underscores"

    ai_url = raw.get("ai_base_url", defaults.ai_base_url).strip().rstrip("/")
    if _SERVICE_URL.fullmatch(ai_url) and len(ai_url) <= 200:
        values["ai_base_url"] = ai_url
    else:
        errors["ai_base_url"] = "an https:// address, for example https://api.mistral.ai/v1"

    ai_model = raw.get("ai_model", defaults.ai_model).strip()
    if _MODEL_NAME.fullmatch(ai_model):
        values["ai_model"] = ai_model
    else:
        errors["ai_model"] = "letters, digits, dots, dashes, slashes and underscores only"

    ai_key = raw.get("ai_key_env", defaults.ai_key_env).strip()
    if _ENV_NAME.fullmatch(ai_key):
        values["ai_key_env"] = ai_key
    else:
        errors["ai_key_env"] = "a variable name: letters, digits and underscores"

    _parse_interview_fields(raw, defaults, values, errors)

    lf_url = raw.get("langfuse_base_url", defaults.langfuse_base_url).strip().rstrip("/")
    if _SERVICE_URL.fullmatch(lf_url) and len(lf_url) <= 200:
        values["langfuse_base_url"] = lf_url
    else:
        errors["langfuse_base_url"] = "an https:// address, for example https://cloud.langfuse.com"

    for key in ("langfuse_public_env", "langfuse_secret_env"):
        name = raw.get(key, getattr(defaults, key)).strip()
        if _ENV_NAME.fullmatch(name):
            values[key] = name
        else:
            errors[key] = "a variable name: letters, digits and underscores"

    for key in ("ai_role_focus", "ai_extra_rules"):
        text = raw.get(key, getattr(defaults, key)).replace("\r\n", "\n").strip()
        if len(text) <= MAX_FOCUS_CHARS:
            values[key] = text
        else:
            errors[key] = f"at most {MAX_FOCUS_CHARS} characters"

    email = raw.get("sync_email", defaults.sync_email).strip()
    if not email or _EMAIL.fullmatch(email):
        values["sync_email"] = email
    else:
        errors["sync_email"] = "an email address like name@gmail.com, or empty to keep sync off"

    password_env = raw.get("sync_password_env", defaults.sync_password_env).strip()
    if _ENV_NAME.fullmatch(password_env):
        values["sync_password_env"] = password_env
    else:
        errors["sync_password_env"] = "a variable name: letters, digits and underscores"

    when = raw.get("schedule_time", defaults.schedule_time).strip()
    if _CLOCK_TIME.fullmatch(when):
        hour, minute = when.split(":")
        values["schedule_time"] = f"{int(hour):02d}:{minute}"
    else:
        errors["schedule_time"] = "use 24-hour HH:MM, for example 08:00"

    for key in LIST_SETTINGS:
        try:
            items = split_list(raw.get(key, getattr(defaults, key)))
            _check_list(items)
        except ValueError as exc:
            errors[key] = str(exc)
        else:
            values[key] = ", ".join(items)


def _parse_interview_fields(
    raw: Mapping[str, str],
    defaults: Config,
    values: dict[str, object],
    errors: dict[str, str],
) -> None:
    try:
        roles = split_list(raw.get("interview_roles", defaults.interview_roles))
        if len(roles) > MAX_INTERVIEW_ROLES:
            raise ValueError(f"at most {MAX_INTERVIEW_ROLES} roles")
        if any(len(r) > MAX_INTERVIEW_ROLE_CHARS for r in roles):
            raise ValueError(f"each role can have at most {MAX_INTERVIEW_ROLE_CHARS} characters")
    except ValueError as exc:
        errors["interview_roles"] = str(exc)
    else:
        values["interview_roles"] = ", ".join(roles)

    for prefix, may_be_empty in (("interview_stt", False), ("interview_ai", True)):
        url_key, model_key, env_key = f"{prefix}_base_url", f"{prefix}_model", f"{prefix}_key_env"
        url = raw.get(url_key, getattr(defaults, url_key)).strip().rstrip("/")
        if (may_be_empty and not url) or (_SERVICE_URL.fullmatch(url) and len(url) <= 200):
            values[url_key] = url
        else:
            errors[url_key] = "an https:// address, for example https://api.groq.com/openai/v1"
        model = raw.get(model_key, getattr(defaults, model_key)).strip()
        if (may_be_empty and not model) or _MODEL_NAME.fullmatch(model):
            values[model_key] = model
        else:
            errors[model_key] = "letters, digits, dots, dashes, slashes and underscores only"
        env = raw.get(env_key, getattr(defaults, env_key)).strip()
        if (may_be_empty and not env) or _ENV_NAME.fullmatch(env):
            values[env_key] = env
        else:
            errors[env_key] = "a variable name: letters, digits and underscores"


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
