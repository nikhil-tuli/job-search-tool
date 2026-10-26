"""Naukri's own search and job data: what its pages receive, turned into plain values.

Naukri has no public API. Its website calls `jobapi/v3/search` and `jobapi/v4/job/<id>`, and a
plain request to them is refused with "recaptcha required". So this module never makes a
request. It only reads the JSON that a real browser window has already received (see
naukri_browser.py) and checks that it has the expected shape.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from .fit_filters import rupee_text
from .models import JobDetail
from .parser import _description_text
from .timeutil import IST, to_iso

NAUKRI_BASE = "https://www.naukri.com"
PRECISION = "minute"  # Naukri gives a full timestamp for when a role was posted
MAX_PLAUSIBLE_YEARS = 40
_NOT_WORDS = re.compile(r"[^a-z0-9]+")
_CLOSED_WORDS = re.compile(
    r"\b(expired|no longer (?:available|accepting)|job (?:is )?closed)\b", re.I
)


class NaukriShapeError(ValueError):
    """The data is not the shape Naukri's pages returned when this was written."""


@dataclass(frozen=True)
class NaukriCard:
    """One role on a search results page."""

    job_id: str
    title: str
    company: str
    location: str
    link: str
    posted_on: str | None  # IST timestamp, or None when Naukri gave none
    raw_posted: str  # Naukri's own value, kept for reference
    raw_relative: str  # for example "3 Days Ago"
    min_years: int | None
    max_years: int | None
    salary_min: int | None  # rupees a year, only when Naukri shows a salary
    salary_max: int | None
    skills: str
    snippet: str  # the short description the list carries (plain text)


@dataclass(frozen=True)
class NaukriJob:
    """What a job's own page adds."""

    detail: JobDetail | None  # None when the page has no description
    posted_on: str | None
    closed: bool


def _text(value: object) -> str:
    return " ".join(str(value).split()) if isinstance(value, str | int | float) else ""


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def posted_iso(value: object) -> str | None:
    """IST timestamp from Naukri's posted value: epoch milliseconds, or "YYYY-MM-DD HH:MM:SS"."""
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        try:
            return to_iso(datetime.fromtimestamp(value / 1000, timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            return to_iso(datetime.fromisoformat(value.strip()).replace(tzinfo=IST))
        except ValueError:
            return None
    return None


def html_to_text(markup: object) -> str:
    """Plain text of an HTML description, keeping line breaks and bullet points."""
    if not isinstance(markup, str) or not markup.strip():
        return ""
    return _description_text(BeautifulSoup(markup, "html.parser"))


def _location(item: dict[str, Any]) -> str:
    for holder in item.get("placeholders") or []:
        if isinstance(holder, dict) and holder.get("type") == "location":
            return _text(holder.get("label"))
    return ""


def _salary(item: dict[str, Any]) -> tuple[int | None, int | None]:
    detail = item.get("salaryDetail")
    if not isinstance(detail, dict) or detail.get("hideSalary"):
        return None, None
    low, high = _int(detail.get("minimumSalary")), _int(detail.get("maximumSalary"))
    if not low or low <= 0:
        return None, None
    return low, high if high and high >= low else low


def parse_search(payload: object) -> list[NaukriCard]:
    """The roles on one search results page. Raises NaukriShapeError if it is not one."""
    if not isinstance(payload, dict) or not isinstance(payload.get("jobDetails"), list):
        raise NaukriShapeError("search response has no jobDetails list")
    cards: list[NaukriCard] = []
    for item in payload["jobDetails"]:
        if not isinstance(item, dict):
            continue
        job_id, title = _text(item.get("jobId")), _text(item.get("title"))
        if not job_id or not title:
            continue
        low, high = _salary(item)
        url = _text(item.get("jdURL"))
        cards.append(
            NaukriCard(
                job_id=job_id,
                title=title,
                company=_text(item.get("companyName")),
                location=_location(item),
                link=(NAUKRI_BASE + url) if url.startswith("/") else url,
                posted_on=posted_iso(item.get("createdDate")),
                raw_posted=_text(item.get("createdDate")),
                raw_relative=_text(item.get("footerPlaceholderLabel")),
                min_years=_years(item.get("minimumExperience")),
                max_years=_years(item.get("maximumExperience")),
                salary_min=low,
                salary_max=high,
                skills=_text(item.get("tagsAndSkills")),
                snippet=html_to_text(item.get("jobDescription")),
            )
        )
    return cards


def _years(value: object) -> int | None:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())  # search lists give "8", job pages give 8
    number = _int(value)
    return number if number is not None and 0 <= number <= MAX_PLAUSIBLE_YEARS else None


def facts_header(details: dict[str, Any]) -> str:
    """Experience and salary from a job page as plain sentences, put ahead of the description.

    The Fit rules read years and salary out of the description text, and Naukri keeps both
    outside it, so this carries them in. A line is only added when Naukri gave the figure.
    """
    lines: list[str] = []
    low_years, high_years = (
        _years(details.get("minimumExperience")),
        _years(details.get("maximumExperience")),
    )
    if low_years is not None:
        if high_years is not None and high_years > low_years:
            lines.append(f"Experience required: {low_years} to {high_years} years.")
        else:
            lines.append(f"Experience required: {low_years} years.")
    low, high = _salary(details)
    if low:
        first = rupee_text(low)
        last = rupee_text(high) if high else first
        lines.append(
            f"Salary: {first} to {last} a year." if last != first else f"Salary: {first} a year."
        )
    return "\n".join(lines)


def job_closed(payload: object, http_status: int = 200) -> bool:
    """True when a job page says the role is gone: HTTP 404 or 410, or an "expired" notice.

    Naukri's exact closed-job reply has not been seen yet, so this only trusts those clear
    signals. When unsure it says False and the role stays as it is.
    """
    if http_status in (404, 410):
        return True
    if not isinstance(payload, dict):
        return False
    message = payload.get("message")
    if isinstance(message, str) and _CLOSED_WORDS.search(message):
        return True
    details = payload.get("jobDetails")
    if isinstance(details, dict):
        for key in ("expired", "isExpired", "jobExpired", "closed", "isClosed"):
            if details.get(key) is True:
                return True
    return False


def parse_job(payload: object) -> NaukriJob:
    """What a job page's data carries: the description (with experience and salary first)."""
    if job_closed(payload):
        return NaukriJob(None, None, True)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobDetails"), dict):
        raise NaukriShapeError("job response has no jobDetails")
    details: dict[str, Any] = payload["jobDetails"]
    body = html_to_text(details.get("description"))
    if not body:
        return NaukriJob(None, posted_iso(details.get("createdDate")), False)
    header = facts_header(details)
    text = f"{header}\n\n{body}" if header else body
    detail = JobDetail(
        description=text,
        seniority=None,
        employment_type=_text(details.get("employmentType")) or None,
        industries=_text(details.get("industry")) or None,
        job_function=_text(details.get("functionalArea")) or None,
    )
    return NaukriJob(detail, posted_iso(details.get("createdDate")), False)


def keyword_slug(text: str) -> str:
    """ "Technical Program Manager" becomes "technical-program-manager"."""
    return _NOT_WORDS.sub("-", text.lower()).strip("-")


def job_age_days(hours: int | float) -> int:
    """Naukri's freshness filter counts whole days: 24 hours is 1, 25 to 48 is 2, and so on."""
    return max(1, math.ceil(hours / 24))


def search_url(keyword: str, location: str, page_index: int, age_days: int | None = None) -> str:
    """Address of a search results page, the same form Naukri's own links use.

    Page 1 is `<keyword>-jobs-in-<place>`, page 2 adds `-2`, and so on. `sort=f` is Naukri's
    "Freshness" order (newest first) and `jobAge` its "posted in the last N days" filter.
    """
    base = f"{NAUKRI_BASE}/{keyword_slug(keyword)}-jobs-in-{keyword_slug(location)}"
    page = f"-{page_index + 1}" if page_index > 0 else ""
    age = f"&jobAge={age_days}" if age_days else ""
    return f"{base}{page}?sort=f{age}&k={quote(keyword)}&l={quote(keyword_slug(location))}"
