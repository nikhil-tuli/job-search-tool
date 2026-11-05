"""Survey of the company pages the app cannot read yet: which hiring system each one uses.

Reads each page once, changes nothing in the database and writes a CSV. The result shows which
readers are worth building and how many pages carry job data in the page itself.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import SitesCompany
from .sites_readers import Http, SiteError, html_to_text

# Hiring systems the app does not read yet, found by the address their pages and scripts use.
SIGNATURES: tuple[tuple[str, str], ...] = (
    ("SmartRecruiters", r"smartrecruiters\.com"),
    ("Workable", r"workable\.com"),
    ("Oracle Recruiting", r"oraclecloud\.com|/hcmUI/CandidateExperience"),
    ("SuccessFactors", r"successfactors\.(?:com|eu)|/careers?/job/|jobs\.sap\.com"),
    ("Phenom", r"phenompeople\.com|cdn\.phenom\.com"),
    ("Eightfold", r"eightfold\.ai"),
    ("iCIMS", r"icims\.com"),
    ("Taleo", r"taleo\.net"),
    ("BambooHR", r"bamboohr\.com"),
    ("Recruitee", r"recruitee\.com"),
    ("Darwinbox", r"darwinbox\.(?:in|com)"),
    ("Keka", r"keka(?:hire)?\.com"),
    ("Freshteam", r"freshteam\.com"),
    ("Breezy", r"breezy\.hr"),
    ("Personio", r"personio\.(?:de|com)"),
    ("Teamtailor", r"teamtailor\.com"),
    ("Pinpoint", r"pinpointhq\.com"),
    ("Rippling", r"ats\.rippling\.com"),
    ("Dayforce", r"dayforcehcm\.com"),
    ("UKG", r"ultipro\.com|recruiting\.ukg\.com"),
    ("JazzHR", r"applytojob\.com"),
)
_SIGNATURES = tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in SIGNATURES)
_JSON_LD = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

COLUMNS = (
    "Company",
    "Career link",
    "Tag",
    "Page",
    "Systems found",
    "Likely system",
    "JobPosting data",
    "Visible text (chars)",
)


@dataclass(frozen=True)
class PageSurvey:
    systems: tuple[str, ...]  # most mentioned first
    job_postings: int  # schema.org JobPosting entries in the page
    text_chars: int


@dataclass
class SurveyRow:
    company: SitesCompany
    page: str  # "Read", or why not, in plain words
    survey: PageSurvey | None = None


@dataclass
class SurveyResult:
    rows: list[SurveyRow] = field(default_factory=list)

    def summary(self) -> list[str]:
        read = [r for r in self.rows if r.survey is not None]
        systems = Counter(r.survey.systems[0] for r in read if r.survey and r.survey.systems)
        none_found = sum(1 for r in read if r.survey and not r.survey.systems)
        with_data = sum(1 for r in read if r.survey and r.survey.job_postings)
        failed = Counter(r.page for r in self.rows if r.survey is None)
        lines = [f"Surveyed {len(self.rows)} companies: {len(read)} pages read."]
        if systems:
            lines.append(
                "Likely system: "
                + ", ".join(f"{name} {n}" for name, n in systems.most_common())
                + f", none recognised {none_found}"
            )
        else:
            lines.append(f"No known hiring system recognised on {none_found} pages.")
        lines.append(f"Pages with JobPosting data in the page: {with_data}")
        if failed:
            lines.append(
                "Pages not read: " + ", ".join(f"{why} {n}" for why, n in failed.most_common())
            )
        return lines


def _count_job_postings(node: Any) -> int:
    if isinstance(node, list):
        return sum(_count_job_postings(item) for item in node)
    if not isinstance(node, dict):
        return 0
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    own = 1 if "JobPosting" in kinds else 0
    return own + sum(_count_job_postings(v) for v in node.values() if isinstance(v, (dict, list)))


def survey_page(html: str) -> PageSurvey:
    found: Counter[str] = Counter()
    for name, pattern in _SIGNATURES:
        hits = len(pattern.findall(html))
        if hits:
            found[name] = hits
    postings = 0
    for block in _JSON_LD.findall(html):
        try:
            postings += _count_job_postings(json.loads(block.strip()))
        except ValueError:
            continue
    return PageSurvey(
        systems=tuple(name for name, _ in found.most_common()),
        job_postings=postings,
        text_chars=len(html_to_text(html)),
    )


def run_survey(
    companies: list[SitesCompany],
    http: Http,
    *,
    sleep: Callable[[float], None],
    delay: float,
    emit: Callable[[str], None],
) -> SurveyResult:
    """Read each company's page once. A page that cannot be read is noted, not fatal."""
    result = SurveyResult()
    try:
        for number, company in enumerate(companies, start=1):
            if number > 1:
                sleep(delay)
            label = f"[{number}/{len(companies)}] {company.name}"
            try:
                html = http.get_text(company.url)
            except SiteError as exc:
                result.rows.append(SurveyRow(company, _why(str(exc))))
                emit(f"{label}: page not read ({_why(str(exc))})")
                continue
            page = survey_page(html)
            result.rows.append(SurveyRow(company, "Read", page))
            emit(
                f"{label}: {page.systems[0] if page.systems else 'no known system'}"
                + (f", {page.job_postings} JobPosting" if page.job_postings else "")
            )
    except KeyboardInterrupt:
        emit("Interrupted. The companies done so far are kept in the CSV.")
    return result


def _why(message: str) -> str:
    text = message.lower()
    if "404" in text:
        return "not found"
    if "blocked" in text or "too many" in text:
        return "blocked"
    if "too long" in text:
        return "timed out"
    if "reached" in text:
        return "unreachable"
    return "other"


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def write_csv(result: SurveyResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(COLUMNS)
        for row in result.rows:
            survey = row.survey
            writer.writerow(
                [
                    _cell(v)
                    for v in (
                        row.company.name,
                        row.company.url,
                        row.company.tag,
                        row.page,
                        "; ".join(survey.systems) if survey else "",
                        survey.systems[0] if survey and survey.systems else "",
                        survey.job_postings if survey else "",
                        survey.text_chars if survey else "",
                    )
                ]
            )
