"""The real source of Naukri data: a visible Chrome window that loads pages like a person.

Each run opens a fresh private session (like an incognito window): no sign-in, no saved
cookies, nothing kept afterwards. The window loads Naukri's own pages and this code reads the
JSON those pages receive. It never solves or avoids a captcha. When Naukri asks for one, the
run waits while the person solves it in the window.

Needs the optional package: pip install playwright. It drives the Chrome
already on the computer, so no separate browser download is needed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .naukri import NAUKRI_BASE, job_age_days, keyword_slug, search_url
from .naukri_run import NaukriSource, Reply, SourceReply

SEARCH_PATH = "/jobapi/v3/search"
JOB_PATH = "/jobapi/v4/job/"
APP_ID = "1950"  # the value Naukri's own pages send in the appid and systemid headers
LOAD_TIMEOUT_MS = 25_000
CAPTCHA_POLL_SECONDS = 3
# Headers the page's own request carries that a script cannot or should not set again.
_SKIP_HEADERS = frozenset(
    {
        "host",
        "cookie",
        "content-length",
        "connection",
        "referer",
        "origin",
        "user-agent",
        "accept-encoding",
    }
)


def _page_url(request_url: str, page_number: int) -> str:
    """The same search request as the page made, asking for another results page."""
    parts = urlsplit(request_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["pageNo"] = str(page_number)
    return urlunsplit(parts._replace(query=urlencode(query)))


class BrowserUnavailable(RuntimeError):
    """The browser package or Chrome is missing."""


def _is_captcha(status: int, body: str) -> bool:
    return status == 406 or "recaptcha" in body.lower()


class BrowserSource:
    """Use as a context manager: the window opens on entry and closes on exit."""

    def __init__(
        self,
        location: str,
        emit: Callable[[str], None] = print,
        headless: bool = False,
        max_age_hours: int = 24,
    ) -> None:
        self._location = location
        self._age_days = job_age_days(max_age_hours)
        self._emit = emit
        self._headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._ok_since_captcha = 0
        # The search request the page itself made for page 1 of each keyword, reused for the
        # later pages so they are asked for exactly the way the site asks.
        self._templates: dict[str, tuple[str, dict[str, str]]] = {}
        self._last_request: tuple[str, dict[str, str]] | None = None

    # ---- lifecycle ----------------------------------------------------------------------

    def __enter__(self) -> BrowserSource:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "The browser package is missing. Install it with: pip install playwright"
            ) from exc
        self._playwright = sync_playwright().start()
        try:
            try:
                self._browser = self._playwright.chromium.launch(
                    channel="chrome", headless=self._headless
                )
            except Exception:
                self._emit("Chrome was not found; using the bundled Chromium instead.")
                self._browser = self._playwright.chromium.launch(headless=self._headless)
        except Exception as exc:
            self._playwright.stop()
            raise BrowserUnavailable(f"Could not open a browser window: {exc}") from exc
        # A new context is a clean private session: nothing is carried over between runs.
        self._context = self._browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 850},
        )
        self._page = self._context.new_page()
        return self

    def __exit__(self, *exc_info: object) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # closing a window that is already gone is fine
                pass
        if self._playwright is not None:
            self._playwright.stop()

    # ---- the NaukriSource calls ---------------------------------------------------------

    def search(self, keyword: str, page_index: int) -> SourceReply:
        if page_index == 0:
            self._last_request = None
            reply = self._load(search_url(keyword, self._location, 0, self._age_days), SEARCH_PATH)
            if self._last_request is not None and reply.kind is Reply.OK:
                self._templates[keyword] = self._last_request
            if reply.kind is Reply.OK or reply.kind is Reply.CAPTCHA:
                return reply
            return self._fetch_in_page(self._constructed_path(keyword, 0))
        # Later pages: the address of page N does not load different results, so ask the
        # search call for that page directly, copying the request the page made for page 1.
        template = self._templates.get(keyword)
        if template is not None:
            return self._fetch_in_page(_page_url(template[0], page_index + 1), template[1])
        return self._fetch_in_page(self._constructed_path(keyword, page_index))

    def _constructed_path(self, keyword: str, page_index: int) -> str:
        slug = keyword_slug(self._location)
        word = quote(keyword)
        return (
            f"{SEARCH_PATH}?noOfResults=20&urlType=search_by_key_loc&searchType=adv"
            f"&location={slug}&keyword={word}&pageNo={page_index + 1}&k={word}&l={slug}"
            f"&sort=f&jobAge={self._age_days}"
            f"&seoKey={keyword_slug(keyword)}-jobs-in-{slug}&src=jobsearchDesk&latLong="
        )

    def job(self, job_id: str, link: str) -> SourceReply:
        reply = self._load(link, f"{JOB_PATH}{job_id}")
        if reply.kind is not Reply.ERROR:
            return reply
        path = (
            f"{JOB_PATH}{job_id}?microsite=y&brandedConsultantJd=true"
            "&amboxConsultantData=true&src=jobsearchDesk"
        )
        return self._fetch_in_page(path)

    def wait_for_captcha(self, seconds: int) -> None:
        """Give the person time to solve it: until the page loads data again, or time runs out."""
        before = self._ok_since_captcha
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._page.wait_for_timeout(CAPTCHA_POLL_SECONDS * 1000)
            if self._ok_since_captcha > before:
                return

    # ---- helpers ------------------------------------------------------------------------

    def _load(self, url: str, path_part: str) -> SourceReply:
        """Open a page and return the data call it makes (the one whose address has path_part)."""
        try:
            with self._page.expect_response(
                lambda r: path_part in r.url, timeout=LOAD_TIMEOUT_MS
            ) as waiting:
                self._page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
            response = waiting.value
        except Exception as exc:  # timeout or navigation failure
            return SourceReply(Reply.ERROR, note=f"page gave no data ({type(exc).__name__})")
        try:
            body = response.text()
        except Exception:
            return SourceReply(Reply.ERROR, note="could not read the response")
        try:
            self._last_request = (response.request.url, dict(response.request.headers))
        except Exception:  # the request details are a convenience, not required
            self._last_request = None
        return self._classify(response.status, body)

    def _fetch_in_page(self, path: str, copied: dict[str, str] | None = None) -> SourceReply:
        headers = {
            "accept": "application/json",
            "appid": APP_ID,
            "systemid": APP_ID,
            "content-type": "application/json",
        }
        for name, value in (copied or {}).items():
            low = name.lower()
            if low in _SKIP_HEADERS or low.startswith((":", "sec-")):
                continue
            headers[low] = value
        script = """async ([path, headers]) => {
            const r = await fetch(path, {headers: headers, credentials: 'include'});
            return [r.status, await r.text()];
        }"""
        try:
            status, body = self._page.evaluate(script, [path, headers])
        except Exception as exc:
            return SourceReply(Reply.ERROR, note=f"in-page request failed ({type(exc).__name__})")
        return self._classify(int(status), str(body))

    def _classify(self, status: int, body: str) -> SourceReply:
        if _is_captcha(status, body):
            return SourceReply(Reply.CAPTCHA, note=f"HTTP {status}")
        if status in (404, 410):
            return SourceReply(Reply.GONE, note=f"HTTP {status}")
        if status != 200:
            return SourceReply(Reply.ERROR, note=f"HTTP {status}")
        try:
            payload = json.loads(body)
        except ValueError:
            return SourceReply(Reply.ERROR, note="response was not JSON")
        self._ok_since_captcha += 1
        return SourceReply(Reply.OK, payload=payload)


def open_source(
    location: str, emit: Callable[[str], None], max_age_hours: int = 24
) -> BrowserSource:  # pragma: no cover - needs a real browser
    return BrowserSource(location, emit, max_age_hours=max_age_hours)


__all__ = ["NAUKRI_BASE", "BrowserSource", "BrowserUnavailable", "NaukriSource", "open_source"]
