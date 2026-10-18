"""Parse LinkedIn guest search result pages (HTML) into cards."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import Tag

from .models import Card

JOB_URN_RE = re.compile(r"jobPosting:(\d+)")
JOB_LINK_TEMPLATE = "https://www.linkedin.com/jobs/view/{job_id}"


@dataclass(frozen=True)
class ParsedPage:
    cards: list[Card]
    malformed: int  # card containers that lacked a job ID or a title


def has_job_urns(html: str) -> bool:
    """True if the page mentions any job posting ID, even if no card could be parsed."""
    return JOB_URN_RE.search(html) is not None


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def parse_page(html: str) -> ParsedPage:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[Card] = []
    malformed = 0
    for container in soup.select("div.base-search-card"):
        urn_match = JOB_URN_RE.search(str(container.get("data-entity-urn") or ""))
        title = _text(container.select_one("h3.base-search-card__title"))
        if urn_match is None or not title:
            malformed += 1
            continue
        job_id = urn_match.group(1)
        time_tag = container.select_one("time")
        raw_datetime = str(time_tag.get("datetime") or "") if time_tag else ""
        benefits = _text(container.select_one(".job-posting-benefits__text")).lower()
        cards.append(
            Card(
                job_id=job_id,
                title=title,
                company=_text(container.select_one("h4.base-search-card__subtitle")),
                location=_text(container.select_one("span.job-search-card__location")),
                link=JOB_LINK_TEMPLATE.format(job_id=job_id),
                actively_hiring="actively hiring" in benefits,
                raw_datetime=raw_datetime or None,
                raw_relative=_text(time_tag) or None,
            )
        )
    return ParsedPage(cards=cards, malformed=malformed)
