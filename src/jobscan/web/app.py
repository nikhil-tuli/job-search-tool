"""Flask application: roles list and edit, run log, configuration, CSV export."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.wrappers import Response as WerkzeugResponse

from .. import schedule as schedule_module
from ..config import LIST_SETTINGS, Config, ConfigError, split_list
from ..cv import MAX_UPLOAD_BYTES, CvError, extract_text, sha_of
from ..details import waiting_for_description
from ..embeddings import API_KEY_ENV, Embedder
from ..evaluation import build_report, load_labelled, score_bands
from ..fit_filters import FitResult, FitRules, evaluate
from ..matching import MATCH_MODES
from ..models import (
    FOLLOW_UP_STATES,
    PORTAL_LINKEDIN,
    PORTAL_NAUKRI,
    PORTALS,
    SORTED_STATUSES,
    STATUSES,
    TAILOR_READY,
    TAILOR_REVIEW,
    Cv,
    CvFile,
    Role,
    RoleFilter,
)
from ..naukri_run import reminder_text
from ..scoring import (
    DAILY_WINDOW,
    TRIGGER_ROLE,
    ScoreModel,
    Scorer,
    ScoringUnavailable,
    cv_chunks,
    gemini_from_env,
    plan_scoring,
)
from ..store import Store, StoreError
from ..tailor import (
    START_NEW,
    START_NO_CV,
    START_NO_DESCRIPTION,
    ClientFactory,
    default_client_factory,
    discard,
    generate,
    start_request,
    thread_launcher,
)
from ..tailor_ai import Proposal, TailorError
from ..tailor_docx import Converter, DocxError, analyse, libreoffice_pdf
from ..timeutil import (
    format_display,
    from_iso,
    has_time_of_day,
    is_day,
    now_ist,
    to_iso,
    weekday_short,
)
from .diffs import moved_items, word_diff
from .forms import (
    NEW_PHRASE_ROWS,
    parse_config_form,
    parse_role_form,
    rated_to_csv,
    roles_to_csv,
    safe_next,
)
from .scores import ScoreView, score_views

SKIPPED_PREVIEW = 10  # skipped titles shown until someone asks for more
PAGE_SIZES = (20, 50, 100)
DEFAULT_PAGE_SIZE = 20
EVAL_PORTAL_COOKIE = "jobscan_eval_portal"
PAGE_SIZE_COOKIE = "jobscan_rows"
RATING_CHOICES = ("good", "bad", "clear")
# What the Eval page can add to the Fit filters: (setting, allowed value, how to say it).
EXCLUDE_KINDS = {
    "title": ("fit_deny_keywords", re.compile(r"[a-z][a-z0-9]{2,}"), "a title deny word"),
    "industry": (
        "fit_excluded_industries",
        re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'/().-]{1,78}[A-Za-z0-9).]"),
        "an excluded industry",
    ),
}
FIT_CHOICES = ("eligible", "excluded")
SCORE_CHOICES = ("strong", "partial", "weak", "unscored")
# Sortable columns of the roles table: key -> (header, direction on the first click).
SORT_COLUMNS = {
    "title": ("Title", "asc"),
    "company": ("Company", "asc"),
    "location": ("Location", "asc"),
    "portal": ("Portal", "asc"),
    "posted": ("Posted (IST)", "desc"),
    "fetched": ("Fetched (IST)", "desc"),
    "fit": ("Fit", "asc"),
    "score": ("Score", "desc"),
    "status": ("Status", "asc"),
    "applied": ("Applied", "desc"),
}
SORT_CHOICES = tuple(SORT_COLUMNS)
SCORE_WAIT_SECONDS = 15.0  # the app handles one request at a time, so a button must not wait long
TRIGGER_LABELS = {"scan": "After a scan", "manual": "jobscan score", "role": "Score this role"}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
SAFE_SITES = frozenset({"same-origin", "none"})

# (field, label, hint) in the order they appear on the Config page.
GROUP_INFO = {
    "Scoring": (
        "Scores are saved as raw keyword and meaning values. The weights, floors and bands here "
        "apply as soon as you save and refresh, with no new run. Chunk weights, top matches, "
        "chunk size and the chunk cap change the saved values, so roles are re-scored at the "
        "next scan or jobscan score, using stored embeddings (no new API calls)."
    ),
    "Fit filters": (
        "Fit filters apply immediately. Save, then refresh the Roles tab and every role is "
        "re-checked against these values. No scan is needed, and nothing is stored or hidden. "
        "Search and pacing settings still apply from the next scan."
    ),
}

SETTING_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "Search",
        (
            (
                "match_mode",
                "Title match mode",
                "contains: phrase anywhere in the title; exact: whole title",
            ),
            (
                "stop_after_empty_pages",
                "Stop after empty pages",
                "Consecutive pages with no title match. Used by LinkedIn and Naukri",
            ),
        ),
    ),
    (
        "LinkedIn search",
        (
            ("location_name", "Location", "Search location text, for example India"),
            ("geo_id", "Geo ID", "LinkedIn geography ID (India is 102713980)"),
            (
                "time_window_seconds",
                "Posted within (seconds)",
                "86400 (24 hours) is the only value verified to work. Scheduled scans use it too. "
                "If widened, raise Fit recency days to match",
            ),
            ("max_pages_per_keyword", "Page cap per keyword", "10 results per page"),
        ),
    ),
    (
        "Pacing",
        (
            ("delay_seconds_min", "Delay between requests, min (s)", ""),
            ("delay_seconds_max", "Delay between requests, max (s)", ""),
            ("keyword_pause_min", "Pause between keywords, min (s)", ""),
            ("keyword_pause_max", "Pause between keywords, max (s)", ""),
        ),
    ),
    (
        "Throttling",
        (
            ("retry_count", "Retries per page or description", ""),
            ("retry_wait_min", "Retry wait, min (s)", ""),
            ("retry_wait_max", "Retry wait, max (s)", ""),
            (
                "abort_after_consecutive_throttles",
                "Abort after throttled responses in a row",
                "Applies to listing pages and job descriptions. Leftover roles wait for next run",
            ),
        ),
    ),
    (
        "LinkedIn job descriptions",
        (
            (
                "detail_max_per_run",
                "Descriptions to fetch per scan",
                "One extra request each. 0 turns description fetching off",
            ),
        ),
    ),
    (
        "Job descriptions",
        (
            (
                "detail_max_age_days",
                "Only for roles fetched in the last (days)",
                "Older roles without a description are left alone. Used by LinkedIn and Naukri",
            ),
        ),
    ),
    (
        "Naukri",
        (
            (
                "naukri_location",
                "Location",
                "Place name as in a Naukri address, for example india or bengaluru",
            ),
            ("naukri_max_pages", "Page cap per keyword", "20 results per page"),
            ("naukri_delay_seconds_min", "Delay between pages, min (s)", ""),
            ("naukri_delay_seconds_max", "Delay between pages, max (s)", ""),
            (
                "naukri_max_job_pages",
                "Job pages to open per run",
                "One visit per role that passes the Fit rules. 0 turns it off",
            ),
            (
                "naukri_max_age_hours",
                "Only roles posted within (hours)",
                "Older roles are ignored",
            ),
            (
                "naukri_abort_after_captchas",
                "Stop after captchas",
                "The run stops once this many captchas appear in one run",
            ),
            (
                "naukri_captcha_wait_seconds",
                "Wait for you to solve a captcha (s)",
                "How long the run waits in the browser window before trying again",
            ),
        ),
    ),
    (
        "Tailoring",
        (
            (
                "tailor_base_url",
                "AI service address",
                "Any OpenAI-compatible service. Change it to switch provider",
            ),
            ("tailor_model", "Model", "Model names change. Update here if one is retired"),
            (
                "tailor_key_env",
                "Key variable name",
                "Name of the terminal variable holding the key. The key itself is never stored",
            ),
        ),
    ),
    (
        "Fit filters",
        (
            (
                "fit_recency_days",
                "Only roles fetched in the last (days)",
                "Older roles are tagged Excluded: recency",
            ),
            (
                "fit_min_years",
                "Minimum years of experience asked",
                "Roles asking for fewer are excluded. 0 turns it off. Roles naming no years stay",
            ),
            (
                "fit_min_salary",
                "Minimum salary (INR per year)",
                "Roles whose description lists a lower salary are excluded. 0 turns it off. "
                "Roles listing no salary stay",
            ),
            (
                "fit_junior_words",
                "Junior words in the title",
                "Comma separated. Associate Director and similar senior titles are not caught",
            ),
            (
                "fit_junior_levels",
                "Junior LinkedIn levels",
                "Comma separated, matched against the Seniority level shown on the role",
            ),
            (
                "fit_excluded_companies",
                "Excluded companies",
                "Comma separated. Amazon also catches Amazon Development Centre",
            ),
            (
                "fit_skip_statuses",
                "Skip roles with these statuses",
                "Comma separated, from the status list",
            ),
            (
                "fit_deny_keywords",
                "Deny keywords in the title",
                "Comma separated. Whole words or phrases, title only",
            ),
            (
                "fit_excluded_industries",
                "Excluded industries",
                "Comma separated, as LinkedIn names them (for example Staffing and Recruiting). "
                "Needs the role's description to have been fetched",
            ),
        ),
    ),
    (
        "Scoring",
        (
            (
                "score_after_scan",
                "Score after each scan",
                "1 scores new roles at the end of a scan, 0 only on demand",
            ),
            (
                "score_weight_keyword",
                "Keyword share of the score (%)",
                "The rest is the meaning (embedding) score. 0 to 100",
            ),
            (
                "score_weight_requirement",
                "Chunk weight: requirements",
                "Matches on requirement text count this much",
            ),
            (
                "score_weight_responsibility",
                "Chunk weight: responsibilities",
                "Also used for text before any heading",
            ),
            (
                "score_weight_company",
                "Chunk weight: company text",
                "0 skips company text. Above 0 embeds it too",
            ),
            (
                "score_top_k",
                "Top CV matches per chunk",
                "1 uses only the closest CV chunk. 2 or 3 averages the closest few",
            ),
            (
                "score_chunk_words",
                "Target chunk size (words)",
                "Neighbouring bullets are packed to about this size. Smaller means more API calls",
            ),
            ("score_max_chunks", "Most chunks per role", "Requirement chunks are kept first"),
            (
                "score_kw_floor",
                "Keyword coverage worth 0",
                "Raw coverage at or below this scores 0",
            ),
            (
                "score_kw_ceiling",
                "Keyword coverage worth 100",
                "Raw coverage at or above this scores 100",
            ),
            (
                "score_sem_floor",
                "Similarity worth 0",
                "Raw cosine similarity at or below this scores 0",
            ),
            (
                "score_sem_ceiling",
                "Similarity worth 100",
                "Raw cosine similarity at or above this scores 100",
            ),
            ("score_strong_min", "Strong fit from", "Scores at or above this are Strong"),
            (
                "score_partial_min",
                "Partial fit from",
                "Scores from here up to Strong are Partial, below are Weak",
            ),
        ),
    ),
    (
        "Embeddings (Gemini)",
        (
            (
                "embed_model",
                "Model",
                "Changing it re-embeds everything. Vectors of different models are never mixed",
            ),
            (
                "embed_dims",
                "Vector size",
                "Numbers kept per vector (768 of 3072). Changing it re-embeds",
            ),
            (
                "embed_batch_size",
                "Texts per request",
                "How many texts go in one embedding request. 1 to 100",
            ),
            (
                "embed_texts_per_minute",
                "Texts per minute",
                "Google counts each text as one request. Free tier shows 100 a minute, kept below",
            ),
            (
                "embed_tokens_per_minute",
                "Tokens per minute",
                "Free tier shows 30,000; kept below it",
            ),
            (
                "embed_daily_limit",
                "Texts per 24 hours",
                "Google counts each text as one request. Free tier shows 1,000 a day",
            ),
        ),
    ),
    (
        "Retention",
        (("skipped_titles_retention_days", "Keep skipped titles (days)", ""),),
    ),
    (
        "Run spacing",
        (("min_gap_between_runs_minutes", "Minimum gap between runs (min)", "--force overrides"),),
    ),
    (
        "Scheduled scan",
        (
            (
                "schedule_time",
                "Run daily at (HH:MM)",
                "24-hour, IST. Run jobscan schedule on again after changing it",
            ),
            (
                "schedule_timeout_minutes",
                "Stop a scan after (min)",
                "A scheduled scan still running after this is stopped",
            ),
            (
                "schedule_notify_finish",
                "Notify when a scan ends",
                "1 also notifies on success. Failures always notify. 0 or 1",
            ),
        ),
    ),
)


# Which section of the Config page each group above belongs to, and what each section affects.
GROUP_SECTION = {
    "Search": "Common",
    "Job descriptions": "Common",
    "Fit filters": "Common",
    "Tailoring": "Common",
    "Scoring": "Common",
    "Embeddings (Gemini)": "Common",
    "Retention": "Common",
    "LinkedIn search": "LinkedIn",
    "Pacing": "LinkedIn",
    "Throttling": "LinkedIn",
    "LinkedIn job descriptions": "LinkedIn",
    "Run spacing": "LinkedIn",
    "Scheduled scan": "LinkedIn",
    "Naukri": "Naukri",
}
SECTION_NOTES = {
    "Common": "Used by both LinkedIn and Naukri: title phrases, matching, Fit filters, scoring.",
    "LinkedIn": "Used only by the LinkedIn scan (jobscan scan, and the daily schedule).",
    "Naukri": "Used only by the Naukri run (jobscan naukri).",
}
CONFIG_SECTIONS = tuple(
    (name, SECTION_NOTES[name], tuple(g for g in SETTING_GROUPS if GROUP_SECTION[g[0]] == name))
    for name in SECTION_NOTES
)


def _host_name(host: str) -> str:
    """Host without the port, keeping IPv6 brackets."""
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.split(":")[0]


def format_seconds(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def scans_by_day(runs: list[Any]) -> dict[str, dict[str, str]]:
    """For each day (IST) the scans started: the latest one's status, and the time they took."""
    days: dict[str, dict[str, str]] = {}
    seconds: dict[str, int] = {}
    for run in sorted(runs, key=lambda r: r.started_on):
        day = run.started_on[:10]
        entry = days.setdefault(day, {"status": "", "took": ""})
        entry["status"] = run.status
        if run.finished_on:
            seconds[day] = seconds.get(day, 0) + round(
                (from_iso(run.finished_on) - from_iso(run.started_on)).total_seconds()
            )
        entry["took"] = (
            "running" if run.status == "running" else format_seconds(seconds.get(day, 0))
        )
    return days


def label_note(role: Role) -> str:
    """Says whether the eval counts this role."""
    return "Counts in the eval." if role.fit_rating is not None else "Not in the eval."


def create_app(
    store: Store,
    *,
    secret_key: str | None = None,
    allowed_hosts: Collection[str] = LOOPBACK_HOSTS,
    make_embedder: Callable[[Config], Embedder] = gemini_from_env,
    make_tailor_client: ClientFactory = default_client_factory,
    tailor_convert: Converter = libreoffice_pdf,
) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secret_key or secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 512 * 1024  # the file plus form fields
    allowed = frozenset(h.lower() for h in allowed_hosts)
    store.fail_stale_tailor()
    launch_tailor = thread_launcher(store, make_tailor_client)

    # ---- request guards -------------------------------------------------------------------

    @app.before_request
    def guard() -> None:
        if _host_name(request.host).lower() not in allowed:
            abort(400, "Unexpected Host header")
        if request.method == "POST":
            # Browsers state where a request came from in Sec-Fetch-Site; that is the most
            # reliable signal. Origin is the fallback for browsers that do not send it.
            site = request.headers.get("Sec-Fetch-Site")
            if site is not None:
                if site not in SAFE_SITES:
                    abort(403, "Cross-site request refused")
                return
            origin = request.headers.get("Origin")
            if origin is not None and urlparse(origin).netloc != request.host:
                abort(403, "Cross-site request refused")

    @app.after_request
    def secure_headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def http_error(error: Any) -> tuple[str, int]:
        code = getattr(error, "code", 500)
        message = getattr(error, "description", "")
        return render_template("error.html", code=code, message=message), code

    # ---- template helpers -----------------------------------------------------------------

    @app.template_filter("ist")
    def ist(value: str | None) -> str:
        return format_display(value)

    @app.template_filter("weekday")
    def weekday(day: str) -> str:
        return weekday_short(day)

    @app.template_filter("duration")
    def duration(run: Any) -> str:
        if not run.finished_on:
            return "running"
        seconds = round((from_iso(run.finished_on) - from_iso(run.started_on)).total_seconds())
        return format_seconds(seconds)

    @app.template_filter("trigger_run")
    def trigger_run(run: Any) -> str:
        """What started a scoring run, naming the portal when a scan or run did."""
        if run.trigger == "scan" and run.source:
            return "After Naukri run" if run.source == "Naukri" else f"After {run.source} scan"
        return str(TRIGGER_LABELS.get(run.trigger, run.trigger))

    @app.template_filter("trigger")
    def trigger(value: str) -> str:
        return TRIGGER_LABELS.get(value, value)

    @app.template_filter("ist_date")
    def ist_date(value: str | None) -> str:
        return format_display(value, with_time=False)

    @app.template_filter("posted")
    def posted(role: Role) -> str:
        if not role.posted_on:
            return "unknown"
        if has_time_of_day(role.posted_precision):
            return format_display(role.posted_on)
        text = format_display(role.posted_on, with_time=False)
        return text if role.posted_precision == "day" else f"~{text}"

    @app.template_filter("short_day")
    def short_day(value: str) -> str:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return value
        return f"{day.day} {day.strftime('%b')}"

    @app.template_filter("safe_link")
    def safe_link(url: str) -> str:
        return url if url.startswith("https://") else ""

    @app.template_global("without")
    def without(values: Mapping[str, Any], key: str, drop: str = "") -> dict[str, Any]:
        """The filters minus one key, or minus one ticked value of it."""
        out = {k: v for k, v in values.items() if k != key}
        if drop and isinstance(values.get(key), list):
            left = [v for v in values[key] if v != drop]
            if left:
                out[key] = left
        return out

    @app.context_processor
    def inject() -> dict[str, Any]:
        return {
            "statuses": SORTED_STATUSES,
            "endpoint": request.endpoint,
            "tailor_review_count": store.count_tailor_review(),
        }

    # ---- roles ----------------------------------------------------------------------------

    def read_filter(args: Any) -> tuple[RoleFilter, dict[str, Any]]:
        """Filter from the query string. Status, fit, score, follow-up and fetched take several
        values (repeated parameters); none ticked means no filter on that one."""

        def picked(name: str, allowed: Iterable[str]) -> list[str]:
            ok = set(allowed)
            return sorted({v for v in args.getlist(name) if v in ok})

        statuses = [s for s in STATUSES if s in args.getlist("status")]
        days = sorted({v for v in args.getlist("fetched") if is_day(v)}, reverse=True)
        follow = [s for s in FOLLOW_UP_STATES if s in args.getlist("followup")]
        portals = [p for p in PORTALS if p in args.getlist("portal")]
        fit = picked("fit", FIT_CHOICES)
        score = [c for c in SCORE_CHOICES if c in args.getlist("score")]
        sort = args.get("sort", "")
        query = args.get("q", "").strip()
        dup = args.get("dup", "") == "1"
        flt = RoleFilter(
            query=query,
            statuses=tuple(statuses),
            portals=tuple(portals),
            duplicates_only=dup,
            fetched_days=tuple(days),
            follow_ups=tuple(follow),
            today=today_iso(),
        )
        shown: dict[str, Any] = {
            "q": query,
            "portal": portals,
            "status": statuses,
            "dup": "1" if dup else "",
            "fetched": days,
            "followup": follow,
            "fit": fit,
            "score": score,
            "sort": sort if sort in SORT_CHOICES else "",
        }
        if shown["sort"] and args.get("dir", "") in ("asc", "desc"):
            shown["dir"] = args["dir"]
        return flt, {k: v for k, v in shown.items() if v}

    @app.get("/")
    def index() -> WerkzeugResponse:
        return redirect(url_for("roles"))

    def current_config() -> Config:
        try:
            return Config.from_raw(store.get_config_raw())
        except ConfigError:
            return Config()

    def fit_filtered(
        rows: list[Role], fit: Iterable[str]
    ) -> tuple[list[Role], dict[str, FitResult]]:
        """Fit result for every row, and only the rows matching the ticked Fit choices."""
        rules, now = FitRules.from_config(current_config()), now_ist()
        results = {r.key: evaluate(r, rules, now) for r in rows}
        wanted = {c for c in fit if c in FIT_CHOICES}
        if wanted and len(wanted) < len(FIT_CHOICES):
            rows = [r for r in rows if (results[r.key].eligible) == ("eligible" in wanted)]
        return rows, results

    def scored_filtered(
        rows: list[Role], views: Mapping[str, ScoreView], choices: Iterable[str]
    ) -> list[Role]:
        """Rows in any of the ticked score bands (unscored means no score yet)."""
        wanted = set(choices)
        if not wanted:
            return rows
        return [
            r
            for r in rows
            if ("unscored" in wanted if r.key not in views else views[r.key].band in wanted)
        ]

    def sorted_rows(
        rows: list[Role],
        views: Mapping[str, ScoreView],
        fits: Mapping[str, FitResult],
        sort: str,
        direction: str,
    ) -> list[Role]:
        """Rows ordered by a column. Empty values go last either way; ties keep newest first."""
        if sort not in SORT_COLUMNS:
            return rows

        def value(r: Role) -> str | int | float | None:
            if sort == "title":
                return r.title.casefold() or None
            if sort == "company":
                return r.company.casefold() or None
            if sort == "location":
                return r.location.casefold() or None
            if sort == "portal":
                return r.portal.casefold()
            if sort == "posted":
                return r.posted_on or None
            if sort == "fetched":
                return r.fetched_on
            if sort == "fit":
                fit = fits[r.key]
                return f"{0 if fit.eligible else 1}{fit.label}"
            if sort == "score":
                return views[r.key].final if r.key in views else None
            if sort == "status":
                return r.status.casefold()
            return r.applied_on or None

        # Stable sorts, so rows with equal values keep their newest-first order.
        present = [r for r in rows if value(r) is not None]
        missing = [r for r in rows if value(r) is None]
        present.sort(key=lambda r: value(r), reverse=direction == "desc")  # type: ignore[arg-type,return-value]
        return present + missing

    def sort_heads(
        active: Mapping[str, Any], per: int, current: str, direction: str
    ) -> dict[str, dict[str, str]]:
        """Header link and arrow for each sortable column: a click flips the active one."""
        base = {k: v for k, v in active.items() if k not in ("sort", "dir")}
        heads: dict[str, dict[str, str]] = {}
        for key, (label, first) in SORT_COLUMNS.items():
            nxt = ("asc" if direction == "desc" else "desc") if key == current else first
            query: dict[str, Any] = {**base, "per": per, "sort": key, "dir": nxt}
            heads[key] = {
                "label": label,
                "url": url_for("roles", **query),
                "arrow": direction if key == current else "",
            }
        return heads

    def fetch_options(picked: Iterable[str]) -> list[tuple[str, str]]:
        """Last 14 days with counts, plus any older day already ticked from a link."""
        days = dict(store.recent_fetch_days(today_iso()))
        for day in picked:
            days.setdefault(day, store.count_roles(RoleFilter(fetched_days=(day,))))
        return [
            (d, f"{date.fromisoformat(d).strftime('%d %b')} ({n})")
            for d, n in sorted(days.items(), reverse=True)
        ]

    @app.get("/roles")
    def roles() -> Response | str:
        flt, active = read_filter(request.args)
        asked = request.args.get("per", "")
        remembered = request.cookies.get(PAGE_SIZE_COOKIE, "")
        per = next(
            (int(v) for v in (asked, remembered) if v.isdigit() and int(v) in PAGE_SIZES),
            DEFAULT_PAGE_SIZE,
        )
        fit, score_choice = active.get("fit", []), active.get("score", [])
        sort = active.get("sort", "")
        direction = active.get("dir") or (SORT_COLUMNS[sort][1] if sort else "desc")
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1
        config, cv = current_config(), store.get_cv()
        if fit or score_choice or sort:
            # Fit and score are worked out in Python, so filter and sort first, then cut the page.
            filtered, fits = fit_filtered(store.list_roles(flt), fit)
            views = score_views(store, config, cv, filtered)
            matching = sorted_rows(
                scored_filtered(filtered, views, score_choice), views, fits, sort, direction
            )
            total = len(matching)
            pages = max(1, math.ceil(total / per))
            page = min(max(page, 1), pages)
            rows = matching[(page - 1) * per : page * per]
        else:
            total = store.count_roles(flt)
            pages = max(1, math.ceil(total / per))
            page = min(max(page, 1), pages)
            rows, fits = fit_filtered(
                store.list_roles(flt, limit=per, offset=(page - 1) * per), fit
            )
            views = score_views(store, config, cv, rows)
        here_args: dict[str, Any] = {**active}
        if page > 1:
            here_args["page"] = page
        here = url_for("roles", **here_args)
        response = make_response(
            render_template(
                "roles.html",
                per=per,
                page_sizes=PAGE_SIZES,
                roles=rows,
                fits=fits,
                scores=views,
                has_cv=cv is not None,
                total=total,
                page=page,
                pages=pages,
                filters=active,
                advanced_open=any(k in active for k in ("fit", "score", "followup", "fetched")),
                today=today_iso(),
                status_options=[(v, v) for v in SORTED_STATUSES],
                portal_options=[(p, p) for p in PORTALS],
                naukri_reminder=reminder_text(store),
                fit_options=(("eligible", "Eligible"), ("excluded", "Excluded")),
                score_options=(
                    ("strong", "Strong fit"),
                    ("partial", "Partial fit"),
                    ("weak", "Weak fit"),
                    ("unscored", "Unscored"),
                ),
                follow_options=(
                    ("due", "Due"),
                    ("upcoming", "Upcoming"),
                    ("overdue", "Overdue"),
                    ("nodate", "No date set"),
                ),
                fetch_options=fetch_options(active.get("fetched", [])),
                heads=sort_heads(active, per, sort or "fetched", direction),
                here=here,
                last_success=store.last_successful_run(),
            )
        )
        if asked.isdigit() and int(asked) in PAGE_SIZES:
            response.set_cookie(PAGE_SIZE_COOKIE, str(per), max_age=365 * 86400, samesite="Lax")
        return response

    @app.post("/roles/status")
    def set_status() -> WerkzeugResponse | tuple[Response, int] | Response:
        ids = request.form.getlist("ids")
        status = request.form.get("status", "")
        # A background save from the roles list asks for JSON so the page does not reload.
        background = request.headers.get("X-Panel") == "1"
        target = safe_next(request.form.get("next"), url_for("roles"))
        error = ""
        count = 0
        if not ids:
            error = "Select at least one role."
        else:
            try:
                count = store.update_status(ids, status)
            except StoreError as exc:
                error = str(exc)
        if background:
            if error:
                return jsonify(error=error), 400
            applied = {}
            fits = {}
            for key in dict.fromkeys(ids):
                portal, _, job_id = key.partition("|")
                role = store.get_role(portal, job_id)
                if role is not None:
                    applied[key] = _role_values(role)["applied_on"]
                    fits[key] = fit_fragments(role)["cell"]
            return jsonify(status=status, applied_on=applied, fit=fits)
        flash(error or f"{count} role(s) set to {status}.", "error" if error else "ok")
        return redirect(target)

    def fit_fragments(role: Role) -> dict[str, str]:
        """The Fit cell and the panel's Fit line as they read now, for a background save."""
        fit = evaluate(role, FitRules.from_config(current_config()), now_ist())
        macros = app.jinja_env.get_template("_fit.html").module
        return {
            "cell": str(getattr(macros, "cell")(fit)),  # noqa: B009
            "detail": str(getattr(macros, "detail")(fit)),  # noqa: B009
        }

    def today_iso() -> str:
        return to_iso(now_ist())[:10]

    def follow_up_tag(role: Role) -> str:
        macros = app.jinja_env.get_template("_followup.html").module
        return str(getattr(macros, "tag")(role, today_iso()))  # noqa: B009

    def contact_links(role: Role) -> dict[str, str]:
        """Where the email and LinkedIn icons point once a role is saved. Empty hides one."""
        return {
            "email": f"mailto:{role.contact_email}" if role.contact_email else "",
            "linkedin": role.contact_linkedin,
        }

    def role_context(role: Role) -> dict[str, Any]:
        config, cv = current_config(), store.get_cv()
        return {
            "role": role,
            "fit": evaluate(role, FitRules.from_config(config), now_ist()),
            "score": score_views(store, config, cv, [role]).get(role.key),
            "has_cv": cv is not None,
            "label_note": label_note(role),
            "can_tailor": bool((role.description or "").strip()),
        }

    def score_one(role: Role) -> tuple[str, ScoreView | None]:
        """Score one role now, whatever its fit result. Returns a message and the new score."""
        config = current_config()
        scorer = Scorer(
            store=store,
            config=config,
            make_embedder=make_embedder,
            emit=lambda _line: None,
            max_wait=SCORE_WAIT_SECONDS,
        )
        try:
            run = scorer.run(TRIGGER_ROLE, only=[role])
        except ScoringUnavailable:
            return "Upload your CV on the CV page first.", None
        if run.scored:
            view = score_views(store, config, store.get_cv(), [role])[role.key]
            return f"Scored {view.final} ({view.band} fit).", view
        if run.no_description:
            return "This role has no description yet, so there is nothing to score.", None
        return run.note or "Could not score this role.", None

    @app.get("/roles/<portal>/<job_id>")
    def role_page(portal: str, job_id: str) -> str:
        role = store.get_role(portal, job_id)
        if role is None:
            abort(404, "Role not found")
        # ?partial=1 returns just the form, for the side panel on the roles list.
        template = "_role_form.html" if request.args.get("partial") else "role.html"
        return render_template(
            template,
            **role_context(role),
            form=_role_values(role),
            errors={},
            next=safe_next(request.args.get("next"), url_for("roles")),
        )

    @app.post("/roles/<portal>/<job_id>")
    def role_save(portal: str, job_id: str) -> str | Response | WerkzeugResponse | tuple[str, int]:
        role = store.get_role(portal, job_id)
        if role is None:
            abort(404, "Role not found")
        target = safe_next(request.form.get("next"), url_for("roles"))
        edit, errors = parse_role_form(request.form, now_ist())
        panel = request.headers.get("X-Panel") == "1"
        if edit is None:
            template = "_role_form.html" if panel else "role.html"
            return (
                render_template(
                    template,
                    **role_context(role),
                    form=dict(request.form),
                    errors=errors,
                    next=target,
                ),
                400,
            )
        store.update_details(
            portal,
            job_id,
            status=edit.status,
            applied_on=edit.applied_on,
            contact_person=edit.contact_person,
            comments=edit.comments,
            follow_up_needed=edit.follow_up_needed,
            follow_up_on=edit.follow_up_on,
            contact_email=edit.contact_email,
            contact_phone=edit.contact_phone,
            contact_linkedin=edit.contact_linkedin,
        )
        if panel:
            saved = store.get_role(portal, job_id)
            assert saved is not None
            return jsonify(
                status=saved.status,
                applied_on=_role_values(saved)["applied_on"],
                note=label_note(saved),
                fit=fit_fragments(saved),
                follow_up=follow_up_tag(saved),
                links=contact_links(saved),
            )
        flash("Saved.", "ok")
        return redirect(target)

    @app.post("/roles/<portal>/<job_id>/score")
    def role_score(portal: str, job_id: str) -> WerkzeugResponse | tuple[Response, int] | Response:
        role = store.get_role(portal, job_id)
        if role is None:
            abort(404, "Role not found")
        message, view = score_one(role)
        if request.headers.get("X-Panel") == "1":
            if view is None:
                return jsonify(message=message), 400
            return jsonify(
                message=message,
                score={
                    "final": view.final,
                    "band": view.band,
                    "detail": f"Keywords {view.keyword}, meaning {view.semantic}, "
                    f"{view.chunks} chunks, scored {format_display(view.scored_on)}",
                },
            )
        flash(message, "ok" if view else "error")
        return redirect(safe_next(request.form.get("next"), url_for("roles")))

    @app.post("/roles/<portal>/<job_id>/rating")
    def role_rating(portal: str, job_id: str) -> WerkzeugResponse | Response:
        role = store.get_role(portal, job_id)
        if role is None:
            abort(404, "Role not found")
        choice = request.form.get("rating", "")
        if choice not in RATING_CHOICES:
            abort(400, "Unknown rating")
        store.set_fit_rating(portal, job_id, None if choice == "clear" else choice)
        saved = store.get_role(portal, job_id)
        assert saved is not None
        if request.headers.get("X-Panel") == "1":
            return jsonify(rating=saved.fit_rating or "", note=label_note(saved))
        flash(label_note(saved), "ok")
        return redirect(safe_next(request.form.get("next"), url_for("roles")))

    # ---- eval -----------------------------------------------------------------------------

    @app.get("/eval")
    def eval_page() -> Response:
        config, cv = current_config(), store.get_cv()
        phrases = [p.phrase for p in store.list_phrases() if p.use_for_match]
        chosen = request.args.get("portal")
        if chosen is None:  # no choice in the address: use the last one made on this tab
            chosen = request.cookies.get(EVAL_PORTAL_COOKIE, "")
        portal = chosen if chosen in PORTALS else ""
        portals = [portal] if portal else []
        report = build_report(load_labelled(store, config, cv, portals), config, phrases)
        response = make_response(
            render_template(
                "eval.html",
                report=report,
                bands=score_bands(store, config, cv, portals),
                rated_count=len(store.list_labelled_roles(portals)),
                has_cv=cv is not None,
                portal=portal,
                portals=PORTALS,
            )
        )
        if request.args.get("portal") is not None:
            response.set_cookie(EVAL_PORTAL_COOKIE, portal, max_age=365 * 86400, samesite="Lax")
        return response

    @app.get("/eval/rated.csv")
    def eval_export() -> Response:
        """The rated roles with their scores and descriptions, for working out what to change."""
        config = current_config()
        portals = [p for p in PORTALS if p in request.args.getlist("portal")]
        items = load_labelled(store, config, store.get_cv(), portals)
        body = rated_to_csv(
            items, ScoreModel.from_config(config), FitRules.from_config(config), now_ist()
        )
        stamp = now_ist().strftime("%Y-%m-%d_%H%M")
        return Response(
            body,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="rated_roles_{stamp}.csv"'},
        )

    @app.post("/eval/exclude")
    def eval_exclude() -> WerkzeugResponse:
        """Add a suggested title word or industry to the matching Fit filter list."""
        kind = request.form.get("kind", "")
        if kind not in EXCLUDE_KINDS:
            abort(400, "Unknown kind")
        setting, pattern, noun = EXCLUDE_KINDS[kind]
        value = " ".join(request.form.get("value", "").split())
        if not pattern.fullmatch(value):
            abort(400, "Not a usable value")
        config = current_config()
        items = list(split_list(getattr(config, setting)))
        if value.casefold() not in {item.casefold() for item in items}:
            raw = config.to_raw()
            raw[setting] = ", ".join([*items, value])
            try:
                store.set_config_raw(Config.from_raw(raw).to_raw())
            except ConfigError as exc:
                flash(next(iter(exc.errors.values())), "error")
                return redirect(url_for("eval_page"))
        flash(f'"{value}" is now {noun}. Fit filters apply at once.', "ok")
        return redirect(url_for("eval_page"))

    @app.get("/export.csv")
    def export_csv() -> Response:
        flt, active = read_filter(request.args)
        rows, fits = fit_filtered(store.list_roles(flt), active.get("fit", []))
        views = score_views(store, current_config(), store.get_cv(), rows)
        sort = active.get("sort", "")
        direction = active.get("dir") or (SORT_COLUMNS[sort][1] if sort else "desc")
        rows = sorted_rows(
            scored_filtered(rows, views, active.get("score", [])), views, fits, sort, direction
        )
        body = roles_to_csv(rows, views)
        stamp = now_ist().strftime("%Y-%m-%d_%H%M")
        return Response(
            body,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="roles_{stamp}.csv"'},
        )

    # ---- CV -------------------------------------------------------------------------------

    def render_cv(error: str = "", status: int = 200) -> tuple[str, int]:
        config, cv = current_config(), store.get_cv()
        pending = plan_scoring(store, config, cv, now_ist()) if cv else None
        return (
            render_template(
                "cv.html",
                cv=cv,
                words=len(cv.text.split()) if cv else 0,
                chunks=[text for text, _ in store.get_cv_chunks()],
                word_ok=store.get_cv_file() is not None,
                pending=len(pending.selected) if pending else 0,
                key_set=bool(os.environ.get(API_KEY_ENV)),
                key_name=API_KEY_ENV,
                max_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
                error=error,
            ),
            status,
        )

    @app.get("/cv")
    def cv_page() -> tuple[str, int]:
        return render_cv()

    @app.post("/cv")
    def cv_upload() -> tuple[str, int] | WerkzeugResponse:
        upload = request.files.get("cv")
        if upload is None or not upload.filename:
            return render_cv("Choose a file to upload.", 400)
        name = os.path.basename(upload.filename.replace("\\", "/"))[:200]
        data = upload.read(MAX_UPLOAD_BYTES + 1)
        try:
            text = extract_text(name, data)
            chunks = cv_chunks(text)
        except CvError as exc:
            return render_cv(str(exc), 400)
        if not chunks:
            return render_cv("No usable text was found in this file.", 400)
        when = to_iso(now_ist())
        word_note = ""
        word_file: CvFile | None = None
        if name.lower().endswith(".docx"):
            try:
                analyse(data)
                word_file = CvFile(name, data, hashlib.sha256(data).hexdigest(), when)
            except DocxError as exc:
                word_note = f" It cannot be used for tailoring: {exc}"
        existing = store.get_cv()
        if existing is not None and existing.sha == sha_of(text):
            if word_file is not None:
                saved = store.get_cv_file()
                if saved is None or saved.sha != word_file.sha:
                    store.save_cv_file(word_file)
                    store.mark_tailor_stale()
                flash("Word file saved. Resume tailoring is now available.", "ok")
            else:
                flash("This is the same CV as before, so nothing changed." + word_note, "ok")
            return redirect(url_for("cv_page"))
        cv = Cv(filename=name, text=text, sha=sha_of(text), uploaded_on=when)
        store.save_cv(cv, chunks)
        store.mark_tailor_stale()
        if word_file is not None:
            store.save_cv_file(word_file)
        else:
            store.clear_cv_file()
        waiting = len(plan_scoring(store, current_config(), cv, now_ist()).selected)
        flash(
            f"CV saved as {len(chunks)} chunks. {waiting} roles will be scored at the next scan, "
            "or run jobscan score in a terminal to do it now." + word_note,
            "ok",
        )
        return redirect(url_for("cv_page"))

    # ---- summary --------------------------------------------------------------------------

    def scoring_context() -> dict[str, Any]:
        config, cv = current_config(), store.get_cv()
        history = store.list_scoring_runs(10)
        waiting = plan_scoring(store, config, cv, now_ist()) if cv else None
        return {
            "cv": cv,
            "score_runs": history,
            "score_latest": history[0] if history else None,
            "score_waiting": len(waiting.selected) if waiting else 0,
            "daily_used": store.texts_embedded_since(to_iso(now_ist() - DAILY_WINDOW)),
            "daily_limit": config.embed_daily_limit,
        }

    @app.get("/summary")
    def summary() -> str:
        portal = request.args.get("portal", "")
        portal = portal if portal in PORTALS else ""
        days = store.fetch_summary(portal or None)
        totals = {status: sum(d.by_status[status] for d in days) for status in STATUSES}
        return render_template(
            "summary.html",
            days=days,
            workflow=STATUSES,
            totals=totals,
            grand_total=sum(d.total for d in days),
            last_success=store.last_successful_run(portal or PORTAL_LINKEDIN),
            portal=portal,
            portals=PORTALS,
            naukri_reminder=reminder_text(store),
            naukri_latest=store.latest_run(PORTAL_NAUKRI),
            linkedin_latest=store.latest_run(PORTAL_LINKEDIN),
            naukri_waiting=waiting_for_description(store, current_config(), PORTAL_NAUKRI),
            scans=scans_by_day(store.list_runs(200, portal or None)),
            latest_run=store.latest_run(portal or PORTAL_LINKEDIN),
            elapsed=schedule_module.elapsed_text,
            schedule_line=schedule_module.describe_state(schedule_module.schedule_state()),
            **run_context(),
            desc_waiting=waiting_for_description(store, current_config()),
            desc_days=current_config().detail_max_age_days,
            **scoring_context(),
        )

    # ---- run log (shown on the Summary page) ------------------------------------------------

    @app.get("/runs")
    def runs() -> WerkzeugResponse:
        """The old Run Log address: the Summary page now holds it."""
        args: dict[str, Any] = {k: v for k, v in request.args.items() if k in ("run", "runs")}
        target = url_for("summary", **args) + ("#run-detail" if "run" in args else "#scans")
        return redirect(target)

    def run_context() -> dict[str, Any]:
        """The Scans table (last 10 runs, or all 30) and the details of the selected run."""
        shown_all = request.args.get("runs") == "all"
        asked = request.args.get("portal", "")
        portal = asked if asked in PORTALS else None
        run_rows = store.list_runs(30 if shown_all else 10, portal)
        selected = None
        raw = request.args.get("run", "")
        if raw.isdigit():
            selected = store.get_run(int(raw))
        if selected is None and run_rows:
            selected = run_rows[0]
        detail: dict[str, Any] = {}
        if selected is not None:
            depth = store.depth_stats(selected.id)
            detail = {
                "keywords": store.keyword_records(selected.id),
                "depth": depth,
                "phrases": store.phrase_matches(selected.id),
                "precision": (selected.matched / selected.cards_seen)
                if selected.cards_seen
                else None,
            }
        every = request.args.get("skipped") == "all"
        retention = current_config().skipped_titles_retention_days
        since = to_iso(now_ist() - timedelta(days=retention))
        skipped, skipped_total = store.recent_skipped(
            since, 200 if every else SKIPPED_PREVIEW, portal
        )
        return {
            "skipped": skipped,
            "skipped_total": skipped_total,
            "skipped_days": retention,
            "skipped_all": every,
            "run_rows": run_rows,
            "runs": run_rows,
            "selected": selected,
            "detail_open": raw.isdigit(),
            "runs_shown_all": shown_all,
            "more_runs": not shown_all and len(store.list_runs(11, portal)) > 10,
            **detail,
        }

    # ---- config ---------------------------------------------------------------------------

    def render_config(
        raw: Mapping[str, str],
        phrases: list[tuple[str, bool, bool]],
        errors: Mapping[str, str],
        status: int = 200,
    ) -> tuple[str, int]:
        rows = [*phrases, *[("", True, True)] * NEW_PHRASE_ROWS]
        return (
            render_template(
                "config.html",
                sections=CONFIG_SECTIONS,
                group_info=GROUP_INFO,
                list_settings=LIST_SETTINGS,
                values=raw,
                phrases=rows,
                errors=errors,
                match_modes=MATCH_MODES,
            ),
            status,
        )

    @app.get("/config")
    def config_page() -> tuple[str, int]:
        try:
            current = Config.from_raw(store.get_config_raw())
        except ConfigError:
            current = Config()
            flash("Stored settings were invalid; showing defaults. Saving replaces them.", "error")
        stored = [(p.phrase, p.use_as_keyword, p.use_for_match) for p in store.list_phrases()]
        return render_config(current.to_raw(), stored, {})

    @app.post("/config")
    def config_save() -> tuple[str, int] | WerkzeugResponse:
        form = parse_config_form(request.form)
        if form.errors:
            return render_config(form.raw, form.phrases, form.errors, 400)
        try:
            with store.transaction():
                store.set_config_raw(Config.from_raw(form.raw).to_raw())
                store.replace_phrases(form.phrases)
        except StoreError as exc:
            return render_config(form.raw, form.phrases, {"phrases": str(exc)}, 400)
        flash("Configuration saved. It applies from the next scan.", "ok")
        return redirect(url_for("config_page"))

    # ---- tailored resumes -----------------------------------------------------------------

    @app.post("/tailor/start")
    def tailor_start() -> WerkzeugResponse:
        portal, job_id = request.form.get("portal", ""), request.form.get("job_id", "")
        result = start_request(store, portal, job_id)
        if result.kind == START_NO_CV:
            flash("Upload your CV as a Word (.docx) file to tailor a resume.", "error")
            return redirect(url_for("cv_page"))
        if result.kind == START_NO_DESCRIPTION:
            flash("This role has no description yet, so there is nothing to tailor to.", "error")
            return redirect(safe_next(request.form.get("next"), url_for("roles")))
        if result.request_id is None:
            abort(404, "Role not found")
        if result.kind == START_NEW:
            launch_tailor(result.request_id)
        return redirect(url_for("cv_history", _anchor=f"r{result.request_id}"))

    @app.get("/cv/history")
    def cv_history() -> str:
        rows = store.list_tailor_requests()
        return render_template(
            "history.html",
            rows=rows,
            preparing=any(r.status == "preparing" for r in rows),
        )

    def tailor_view(request_id: int) -> tuple[Any, Proposal]:
        row = store.get_tailor_request(request_id)
        if row is None or not row.proposal:
            abort(404, "Request not found")
        return row, Proposal.from_json(row.proposal)

    @app.get("/cv/tailor/<int:request_id>")
    def tailor_page(request_id: int) -> str | WerkzeugResponse:
        row, proposal = tailor_view(request_id)
        if row.status not in (TAILOR_REVIEW, TAILOR_READY):
            return redirect(url_for("cv_history", _anchor=f"r{request_id}"))
        applied = set(json.loads(row.applied)) if row.applied else set()
        views = []
        for change in proposal.changes:
            view: dict[str, Any] = dict(change)
            if change["kind"] in ("summary", "bullet"):
                before, after = word_diff(change["before"], change["after"])
                view.update(before_html=before, after_html=after)
                view["lengths"] = (len(change["before"]), len(change["after"]))
            elif change["kind"] == "competencies":
                view["moved"] = moved_items(change["before"], change["after"])
            view["on"] = change["id"] in applied if row.status == TAILOR_READY else True
            views.append(view)
        return render_template(
            "tailor.html",
            row=row,
            proposal=proposal,
            changes=views,
            ready=row.status == TAILOR_READY,
        )

    @app.post("/cv/tailor/<int:request_id>/generate")
    def tailor_generate(request_id: int) -> WerkzeugResponse:
        selected = set(request.form.getlist("change"))
        try:
            count = generate(store, request_id, selected, tailor_convert)
        except TailorError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Resume ready with {count} change{'s' if count != 1 else ''}.", "ok")
        return redirect(url_for("tailor_page", request_id=request_id))

    @app.post("/cv/tailor/<int:request_id>/discard")
    def tailor_discard(request_id: int) -> WerkzeugResponse:
        discard(store, request_id)
        return redirect(url_for("cv_history", _anchor=f"r{request_id}"))

    @app.post("/cv/tailor/<int:request_id>/again")
    def tailor_again(request_id: int) -> WerkzeugResponse:
        row = store.get_tailor_request(request_id)
        if row is None:
            abort(404, "Request not found")
        result = start_request(store, row.portal, row.job_id)
        if result.kind == START_NO_CV:
            flash("Upload your CV as a Word (.docx) file to tailor a resume.", "error")
            return redirect(url_for("cv_page"))
        if result.kind == START_NO_DESCRIPTION:
            flash("This role has no description yet, so there is nothing to tailor to.", "error")
            return redirect(url_for("cv_history"))
        if result.kind == START_NEW and result.request_id is not None:
            launch_tailor(result.request_id)
        target = result.request_id or request_id
        return redirect(url_for("cv_history", _anchor=f"r{target}"))

    @app.get("/cv/tailor/<int:request_id>/download")
    def tailor_download(request_id: int) -> Response:
        found = store.get_tailor_output(request_id)
        if found is None:
            abort(404, "No file for this request")
        name, data = found
        return Response(
            data,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return app


def _role_values(role: Role) -> dict[str, str]:
    return {
        "status": role.status,
        "applied_on": format_display(role.applied_on, with_time=False),
        "contact_person": role.contact_person,
        "comments": role.comments,
        "follow_up_needed": "1" if role.follow_up_needed else "",
        "follow_up_on": role.follow_up_on or "",
        "contact_email": role.contact_email,
        "contact_phone": role.contact_phone,
        "contact_linkedin": role.contact_linkedin,
    }
