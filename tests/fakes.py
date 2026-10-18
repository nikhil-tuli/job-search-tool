"""Scripted stand-ins for the LinkedIn client, shared by scanner and CLI tests."""

from __future__ import annotations

from jobscan.client import FetchResult, Outcome
from jobscan.models import Card
from jobscan.parser import ParsedPage

TPM = "Technical Program Manager"
DM = "Delivery Manager"


def card(
    job_id: str,
    title: str = TPM,
    company: str = "Acme",
    location: str = "Pune, India",
    relative: str | None = "5 hours ago",
) -> Card:
    return Card(
        job_id=job_id,
        title=title,
        company=company,
        location=location,
        link=f"https://www.linkedin.com/jobs/view/{job_id}",
        actively_hiring=False,
        raw_datetime="2026-10-15",
        raw_relative=relative,
    )


def ok(*cards: Card, malformed: int = 0) -> FetchResult:
    return FetchResult(Outcome.OK, page=ParsedPage(list(cards), malformed))


def end(warning: str = "") -> FetchResult:
    return FetchResult(Outcome.END, detail="HTTP 400", warning=warning)


def throttled() -> FetchResult:
    return FetchResult(Outcome.THROTTLED, detail="HTTP 429")


def layout() -> FetchResult:
    return FetchResult(Outcome.LAYOUT_CHANGED, detail="no cards parsed")


class ScriptedFetcher:
    """Replies from a per-(keyword, page) queue; an unscripted request is a test failure."""

    def __init__(self, script: dict[tuple[str, int], list[FetchResult | BaseException]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, int]] = []

    def fetch_page(self, keyword: str, page_index: int) -> FetchResult:
        self.calls.append((keyword, page_index))
        queue = self.script.get((keyword, page_index))
        assert queue, f"unscripted request: {keyword!r} page {page_index}"
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
