"""Parsing and validation of submitted forms, plus small output-safety helpers."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import date, datetime
from urllib.parse import urlparse

from ..config import Config, ConfigError
from ..models import DEFAULT_STATUS, STATUSES, Role
from ..timeutil import IST, format_display, has_time_of_day, to_iso

MAX_CONTACT = 200
MAX_COMMENTS = 5000
MAX_PHRASE = 100
NEW_PHRASE_ROWS = 3
_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class RoleEdit:
    status: str
    applied_on: str | None
    contact_person: str
    comments: str


def resolve_applied_on(status: str, applied_on: str | None, today: datetime) -> str | None:
    """Applied with no date gets today (IST); Not Applied never keeps a date."""
    if status == DEFAULT_STATUS:
        return None
    if status == "Applied" and applied_on is None:
        return to_iso(today)
    return applied_on


def parse_role_form(
    form: Mapping[str, str], today: datetime
) -> tuple[RoleEdit | None, dict[str, str]]:
    errors: dict[str, str] = {}
    status = form.get("status", "")
    if status not in STATUSES:
        errors["status"] = "Choose one of the listed statuses."

    applied_on: str | None = None
    applied_raw = form.get("applied_on", "").strip()
    if applied_raw:
        try:
            day = date.fromisoformat(applied_raw)
        except ValueError:
            errors["applied_on"] = "Use the format YYYY-MM-DD."
        else:
            if day > today.astimezone(IST).date():
                errors["applied_on"] = "Applied On cannot be in the future."
            else:
                applied_on = to_iso(datetime(day.year, day.month, day.day, tzinfo=IST))

    contact = form.get("contact_person", "").strip()
    if len(contact) > MAX_CONTACT:
        errors["contact_person"] = f"At most {MAX_CONTACT} characters."
    comments = form.get("comments", "").replace("\r\n", "\n").strip()
    if len(comments) > MAX_COMMENTS:
        errors["comments"] = f"At most {MAX_COMMENTS} characters."

    if errors:
        return None, errors
    return (
        RoleEdit(status, resolve_applied_on(status, applied_on, today), contact, comments),
        {},
    )


@dataclass(frozen=True)
class ConfigForm:
    raw: dict[str, str]
    phrases: list[tuple[str, bool, bool]]
    errors: dict[str, str]


def parse_config_form(form: Mapping[str, str]) -> ConfigForm:
    errors: dict[str, str] = {}
    raw = {f.name: form.get(f.name, "").strip() for f in fields(Config)}
    try:
        Config.from_raw(raw)
    except ConfigError as exc:
        errors.update(exc.errors)

    try:
        count = int(form.get("phrase_count", "0"))
    except ValueError:
        count = 0
    phrases: list[tuple[str, bool, bool]] = []
    seen: set[str] = set()
    for i in range(max(0, min(count, 500))):
        text = " ".join(form.get(f"phrase_{i}", "").split())
        if not text or form.get(f"remove_{i}"):
            continue
        if len(text) > MAX_PHRASE:
            errors["phrases"] = f"A phrase can have at most {MAX_PHRASE} characters."
            continue
        if text.casefold() in seen:
            errors["phrases"] = f"Duplicate phrase: {text}"
            continue
        seen.add(text.casefold())
        phrases.append((text, bool(form.get(f"keyword_{i}")), bool(form.get(f"match_{i}"))))
    if "phrases" not in errors:
        if not any(keyword for _, keyword, _ in phrases):
            errors["phrases"] = "Switch at least one phrase on as a search keyword."
        elif not any(match for _, _, match in phrases):
            errors["phrases"] = "Switch at least one phrase on for title matching."
    return ConfigForm(raw, phrases, errors)


def safe_next(value: str | None, default: str) -> str:
    """Only same-site relative paths are allowed as a redirect target."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return default
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or "\n" in value or "\r" in value:
        return default
    return value


def csv_cell(value: object) -> str:
    """Neutralise spreadsheet formula injection by prefixing risky cells with an apostrophe."""
    text = "" if value is None else str(value)
    if text.startswith(_CSV_DANGEROUS_PREFIXES):
        return "'" + text
    return text


CSV_HEADER = (
    "Job ID",
    "Title",
    "Company",
    "Location",
    "Link",
    "Portal",
    "Posted On (IST)",
    "Fetched On (IST)",
    "Actively Hiring",
    "Status",
    "Applied On (IST)",
    "Contact Person",
    "Comments",
    "Matched Phrase",
    "Possible Duplicate Of",
)


def roles_to_csv(roles: Iterable[Role]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for role in roles:
        posted = format_display(role.posted_on, with_time=has_time_of_day(role.posted_precision))
        writer.writerow(
            [
                csv_cell(v)
                for v in (
                    role.job_id,
                    role.title,
                    role.company,
                    role.location,
                    role.link,
                    role.portal,
                    posted,
                    format_display(role.fetched_on),
                    "Yes" if role.actively_hiring else "",
                    role.status,
                    format_display(role.applied_on, with_time=False),
                    role.contact_person,
                    role.comments,
                    role.matched_phrase,
                    role.possible_duplicate_of or "",
                )
            ]
        )
    return buffer.getvalue()
