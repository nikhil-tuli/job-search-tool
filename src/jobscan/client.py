"""HTTP client for LinkedIn's logged-out guest job search, one request per call.

Retrying, pacing and stop rules belong to the scanner. This module only fetches a page and
classifies what came back.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Protocol

import requests

from .config import Config
from .parser import ParsedPage, has_job_urns, parse_page

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PAGE_SIZE = 10
STUB_MIN_BYTES = 20_000
_META_REFRESH_RE = re.compile(r"<meta[^>]+http-equiv=[\"']?refresh", re.IGNORECASE)
_THROTTLE_STATUSES = frozenset({403, 429, 999})

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class Outcome(enum.Enum):
    OK = "ok"  # page with at least one card
    END = "end"  # no more results for this search
    THROTTLED = "throttled"  # transient refusal or failure; retry later
    LAYOUT_CHANGED = "layout_changed"  # job IDs present but no card could be parsed


@dataclass(frozen=True)
class FetchResult:
    outcome: Outcome
    page: ParsedPage | None = None
    detail: str = ""
    warning: str = ""


class PageFetcher(Protocol):
    def fetch_page(self, keyword: str, page_index: int) -> FetchResult: ...


def classify_response(status: int, body: str) -> FetchResult:
    """Turn an HTTP status and body into one of the four outcomes."""
    if status in _THROTTLE_STATUSES or status >= 500:
        return FetchResult(Outcome.THROTTLED, detail=f"HTTP {status}")
    if status == 400:
        return FetchResult(Outcome.END, detail="HTTP 400")
    if status != 200:
        return FetchResult(Outcome.THROTTLED, detail=f"unexpected HTTP {status}")
    if not body.strip():
        return FetchResult(Outcome.END, detail="empty body")

    page = parse_page(body)
    if page.cards:
        return FetchResult(Outcome.OK, page=page)
    if has_job_urns(body) or page.malformed:
        return FetchResult(
            Outcome.LAYOUT_CHANGED,
            detail="page lists job postings but no card could be parsed",
        )
    if _META_REFRESH_RE.search(body) or len(body.encode()) >= STUB_MIN_BYTES:
        return FetchResult(Outcome.THROTTLED, detail="stub page instead of results")
    return FetchResult(
        Outcome.END,
        detail="no cards",
        warning=f"page with no cards and no end signal ({len(body)} bytes); treated as end",
    )


def build_params(config: Config, keyword: str, page_index: int) -> dict[str, str | int]:
    return {
        "keywords": keyword,
        "location": config.location_name,
        "geoId": config.geo_id,
        "f_TPR": f"r{config.time_window_seconds}",
        "start": page_index * PAGE_SIZE,
    }


class LinkedInClient:
    def __init__(
        self,
        config: Config,
        session: requests.Session | None = None,
        *,
        base_url: str = SEARCH_URL,
        timeout: float = 20.0,
    ) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._base_url = base_url
        self._timeout = timeout
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
            }
        )

    def fetch_page(self, keyword: str, page_index: int) -> FetchResult:
        try:
            response = self._session.get(
                self._base_url,
                params=build_params(self._config, keyword, page_index),
                timeout=self._timeout,
                allow_redirects=False,
            )
            body = response.text
        except requests.RequestException as exc:
            return FetchResult(Outcome.THROTTLED, detail=f"network error: {type(exc).__name__}")
        return classify_response(response.status_code, body)

    def close(self) -> None:
        self._session.close()
