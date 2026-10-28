"""Read the public job lists of company career pages.

Most companies run their career page on a hiring system (Greenhouse, Lever, Ashby, Workday,
Jobvite, Zoho Recruit). Each has a public job list. A reader turns one into plain SiteJob rows;
what to keep (title, location, age) is decided by the run, not here.

Every failure is a SiteError with a reason in plain words. Nothing here writes to the database.
"""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

from .timeutil import IST, now_ist, to_iso

REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "Mozilla/5.0 (compatible; jobscan personal job tracker)"
WORKDAY_PAGE = 20
MAX_WORKDAY_PAGES = 10

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
WORKDAY = "workday"
JOBVITE = "jobvite"
ZOHO = "zoho"
SYSTEM_LABELS = {
    GREENHOUSE: "Greenhouse",
    LEVER: "Lever",
    ASHBY: "Ashby",
    WORKDAY: "Workday",
    JOBVITE: "Jobvite",
    ZOHO: "Zoho Recruit",
    "": "Unknown",
}


class SiteError(Exception):
    """Why a company's job list could not be read, in plain words."""


@dataclass(frozen=True)
class SiteJob:
    job_id: str  # the company system's own number
    title: str
    location: str
    link: str
    posted_on: str | None = None  # ISO (IST) when the list gives a date
    description: str | None = None
    needs_detail: bool = False  # the job's own page holds the date or the location


@dataclass(frozen=True)
class Detail:
    description: str | None = None
    location: str | None = None
    posted_on: str | None = None


def with_detail(job: SiteJob, detail: Detail) -> SiteJob:
    return replace(
        job,
        description=detail.description or job.description,
        location=detail.location if detail.location else job.location,
        posted_on=detail.posted_on or job.posted_on,
        needs_detail=False,
    )


# ---- recognising the system -------------------------------------------------------------------


@dataclass(frozen=True)
class Detected:
    system: str  # "" when not recognised
    board: str


_LOCALE = re.compile(r"[a-z]{2}-[A-Z]{2}")


def detect(url: str) -> Detected:
    """Work out the hiring system and the board name from a career link."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SiteError("Enter the full link, starting with https://")
    host = parsed.hostname.lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host.endswith("greenhouse.io") and parts:
        return Detected(GREENHOUSE, parts[0])
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return Detected(LEVER, f"{host}|{parts[0]}")
    if host == "jobs.ashbyhq.com" and parts:
        return Detected(ASHBY, parts[0])
    if host.endswith(".myworkdayjobs.com"):
        sites = [p for p in parts if not _LOCALE.fullmatch(p)]
        if sites:
            return Detected(WORKDAY, f"{host}|{host.split('.')[0]}|{sites[0]}")
    if host == "jobs.jobvite.com" and parts:
        return Detected(JOBVITE, parts[0])
    if "zohorecruit." in host:
        return Detected(ZOHO, host)
    return Detected("", "")


_SYSTEM_LINK = re.compile(
    r"https?://[\w.-]*(?:myworkdayjobs\.com|greenhouse\.io|jobs\.lever\.co|jobs\.eu\.lever\.co|"
    r"jobs\.ashbyhq\.com|jobs\.jobvite\.com|zohorecruit\.\w+)[^\s\"'<>)\\]*",
    re.IGNORECASE,
)


def detect_from_page(url: str, http: Http) -> Detected:
    """Read a company's own career page and look for a link to its hiring system.

    For companies whose career page is on their own site but whose jobs sit on a hiring system.
    Returns Detected("", "") when none is found or the page cannot be read.
    """
    try:
        page = http.get_text(url)
    except SiteError:
        return Detected("", "")
    for match in _SYSTEM_LINK.finditer(html.unescape(page)):
        try:
            found = detect(match.group(0).rstrip(".,;"))
        except SiteError:
            continue
        if found.system:
            return found
    return Detected("", "")


# ---- talking to a company site ----------------------------------------------------------------


class Http(Protocol):
    def get_json(self, url: str) -> Any: ...

    def get_text(self, url: str) -> str: ...

    def post_json(self, url: str, body: dict[str, Any]) -> Any: ...


class HttpClient:
    """The real thing: requests, with failures turned into plain reasons."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

    def _check(self, response: requests.Response) -> requests.Response:
        status = response.status_code
        if status == 404:
            raise SiteError("Link not found (404). Check the career link.")
        if status in (401, 403, 429):
            raise SiteError(f"Blocked or too many requests (HTTP {status}). Retried next run.")
        if status >= 500:
            raise SiteError(f"The company site had a problem (HTTP {status}). Retried next run.")
        if status != 200:
            raise SiteError(f"The company site refused the request (HTTP {status}).")
        return response

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        try:
            response = self._session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        except requests.Timeout:
            raise SiteError("The company site took too long to answer.") from None
        except requests.RequestException:
            raise SiteError("The company site could not be reached. Check your internet.") from None
        return self._check(response)

    def get_json(self, url: str) -> Any:
        try:
            return self._send("GET", url).json()
        except ValueError:
            raise SiteError("The job list was not in the expected form.") from None

    def get_text(self, url: str) -> str:
        return self._send("GET", url).text

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        try:
            return self._send("POST", url, json=body).json()
        except ValueError:
            raise SiteError("The job list was not in the expected form.") from None


# ---- helpers ----------------------------------------------------------------------------------


def html_to_text(markup: str) -> str:
    soup = BeautifulSoup(html.unescape(markup), "html.parser")
    for item in soup.find_all("li"):
        item.insert_before("\n- ")
    for tag in soup.find_all(["br", "p", "div", "h1", "h2", "h3", "h4", "ul"]):
        tag.insert_after("\n")
    lines = [" ".join(line.split()) for line in soup.get_text().splitlines()]
    return "\n".join(line for line in lines if line)


def _date_iso(value: object) -> str | None:
    """An ISO date or timestamp (or a millisecond epoch) as an IST timestamp, else None."""
    try:
        if isinstance(value, int | float) and value > 10**11:
            return to_iso(datetime.fromtimestamp(value / 1000, IST))
        if isinstance(value, str) and value.strip():
            text = value.strip().replace("Z", "+00:00")
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=IST)
            return to_iso(moment)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _items(data: Any, key: str | None = None) -> list[dict[str, Any]]:
    found = data.get(key) if key and isinstance(data, dict) else data
    if not isinstance(found, list):
        raise SiteError("The job list was not in the expected form.")
    return [item for item in found if isinstance(item, dict)]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


# ---- readers ----------------------------------------------------------------------------------


class Reader(Protocol):
    def list_jobs(self, keywords: list[str], limit: int) -> list[SiteJob]: ...

    def describe(self, job: SiteJob) -> Detail: ...


class GreenhouseReader:
    def __init__(self, board: str, http: Http) -> None:
        self._board, self._http = board, http

    def list_jobs(self, keywords: list[str], limit: int) -> list[SiteJob]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{quote(self._board)}/jobs?content=true"
        jobs = []
        for item in _items(self._http.get_json(url), "jobs"):
            location = item.get("location")
            jobs.append(
                SiteJob(
                    job_id=str(item.get("id", "")),
                    title=_text(item.get("title")),
                    location=_text(location.get("name")) if isinstance(location, dict) else "",
                    link=_text(item.get("absolute_url")),
                    posted_on=_date_iso(item.get("first_published")),
                    description=html_to_text(item["content"])
                    if isinstance(item.get("content"), str)
                    else None,
                )
            )
        return [j for j in jobs if j.job_id and j.title]

    def describe(self, job: SiteJob) -> Detail:
        return Detail(job.description)


class LeverReader:
    def __init__(self, board: str, http: Http) -> None:
        self._host, _, self._site = board.partition("|")
        self._http = http

    def list_jobs(self, keywords: list[str], limit: int) -> list[SiteJob]:
        api = "api.eu.lever.co" if "eu" in self._host else "api.lever.co"
        url = f"https://{api}/v0/postings/{quote(self._site)}?mode=json"
        jobs = []
        for item in _items(self._http.get_json(url)):
            categories = item.get("categories")
            cats = categories if isinstance(categories, dict) else {}
            places = [_text(cats.get("location"))]
            places += [p for p in item.get("allLocations") or [] if isinstance(p, str)]
            parts = [_text(item.get("descriptionPlain"))]
            for block in item.get("lists") or []:
                if isinstance(block, dict):
                    parts.append(_text(block.get("text")))
                    parts.append(html_to_text(_text(block.get("content"))))
            parts.append(_text(item.get("additionalPlain")))
            jobs.append(
                SiteJob(
                    job_id=_text(item.get("id")),
                    title=_text(item.get("text")),
                    location="; ".join(dict.fromkeys(p for p in places if p)),
                    link=_text(item.get("hostedUrl")),
                    posted_on=_date_iso(item.get("createdAt")),
                    description="\n".join(p for p in parts if p) or None,
                )
            )
        return [j for j in jobs if j.job_id and j.title]

    def describe(self, job: SiteJob) -> Detail:
        return Detail(job.description)


class AshbyReader:
    def __init__(self, board: str, http: Http) -> None:
        self._board, self._http = board, http

    def list_jobs(self, keywords: list[str], limit: int) -> list[SiteJob]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(self._board)}"
        jobs = []
        for item in _items(self._http.get_json(url), "jobs"):
            if item.get("isListed") is False:
                continue
            places = [_text(item.get("location"))]
            for other in item.get("secondaryLocations") or []:
                if isinstance(other, dict):
                    places.append(_text(other.get("location")))
            description = _text(item.get("descriptionPlain")) or html_to_text(
                _text(item.get("descriptionHtml"))
            )
            jobs.append(
                SiteJob(
                    job_id=_text(item.get("id")),
                    title=_text(item.get("title")),
                    location="; ".join(dict.fromkeys(p for p in places if p)),
                    link=_text(item.get("jobUrl")),
                    posted_on=_date_iso(item.get("publishedAt")),
                    description=description or None,
                )
            )
        return [j for j in jobs if j.job_id and j.title]

    def describe(self, job: SiteJob) -> Detail:
        return Detail(job.description)


_AGO = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.IGNORECASE)
_MANY_PLACES = re.compile(r"\d+\s+locations?", re.IGNORECASE)


class WorkdayReader:
    def __init__(self, board: str, http: Http) -> None:
        self._host, self._tenant, self._site = board.split("|")
        self._http = http

    def _api(self) -> str:
        return f"https://{self._host}/wday/cxs/{self._tenant}/{self._site}"

    def list_jobs(self, keywords: list[str], limit: int) -> list[SiteJob]:
        seen: dict[str, SiteJob] = {}
        pages = min(MAX_WORKDAY_PAGES, max(1, math.ceil(limit / WORKDAY_PAGE)))
        for keyword in keywords or [""]:
            for page in range(pages):
                data = self._http.post_json(
                    f"{self._api()}/jobs",
                    {
                        "appliedFacets": {},
                        "limit": WORKDAY_PAGE,
                        "offset": page * WORKDAY_PAGE,
                        "searchText": keyword,
                    },
                )
                postings = _items(data, "jobPostings")
                for item in postings:
                    job = self._job(item)
                    if job is not None:
                        seen.setdefault(job.job_id, job)
                if len(postings) < WORKDAY_PAGE:
                    break
        return list(seen.values())

    def _job(self, item: dict[str, Any]) -> SiteJob | None:
        path = _text(item.get("externalPath"))
        title = _text(item.get("title"))
        if not path or not title:
            return None
        place = _text(item.get("locationsText"))
        posted, needs = None, False
        match = _AGO.search(_text(item.get("postedOn")))
        if match is None:
            needs = True
        elif match.group(1).lower().endswith("+ days ago") or "+" in match.group(1):
            needs = True  # "30+ days ago": the job's page has the real date
        else:
            days = {"today": 0, "yesterday": 1}.get(match.group(1).lower())
            days = int(match.group(2)) if days is None else days
            posted = to_iso((now_ist() - timedelta(days=days)).replace(hour=0, minute=0, second=0))
        many = bool(_MANY_PLACES.fullmatch(place)) or not place
        return SiteJob(
            job_id=path.rsplit("/", 1)[-1],
            title=title,
            location="" if many else place,
            link=f"https://{self._host}/{self._site}{path}",
            posted_on=posted,
            needs_detail=needs or many,
        )

    def describe(self, job: SiteJob) -> Detail:
        path = job.link.split(f"/{self._site}", 1)[-1]
        data = self._http.get_json(f"{self._api()}{path}")
        info = data.get("jobPostingInfo") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            raise SiteError("The job page was not in the expected form.")
        places = [_text(info.get("location"))]
        places += [p for p in info.get("additionalLocations") or [] if isinstance(p, str)]
        return Detail(
            description=html_to_text(_text(info.get("jobDescription"))) or None,
            location="; ".join(dict.fromkeys(p for p in places if p)) or None,
            posted_on=_date_iso(info.get("startDate")),
        )


class JobviteReader:
    def __init__(self, board: str, http: Http) -> None:
        self._board, self._http = board, http

    def list_jobs(self, keywords: list[str], limit: int) -> list[SiteJob]:
        page = self._http.get_text(f"https://jobs.jobvite.com/{quote(self._board)}/jobs")
        soup = BeautifulSoup(page, "html.parser")
        jobs: dict[str, SiteJob] = {}
        for link in soup.select('a[href*="/job/"]'):
            href = str(link.get("href", ""))
            match = re.search(r"/job/([^/?#]+)", href)
            title = " ".join(link.get_text().split())
            if not match or not title:
                continue
            row = link.find_parent(["tr", "li"])
            place = row.select_one(".jv-job-list-location") if row else None
            jobs.setdefault(
                match.group(1),
                SiteJob(
                    job_id=match.group(1),
                    title=title,
                    location=" ".join(place.get_text().split()) if place else "",
                    link=f"https://jobs.jobvite.com{href}" if href.startswith("/") else href,
                    needs_detail=place is None,
                ),
            )
        if not jobs and "jv-" not in page:
            raise SiteError(
                "The job list could not be read (the page may load its jobs with scripts)."
            )
        return list(jobs.values())

    def describe(self, job: SiteJob) -> Detail:
        soup = BeautifulSoup(self._http.get_text(job.link), "html.parser")
        body = soup.select_one(".jv-job-detail-description")
        place = soup.select_one(".jv-job-detail-meta")
        text = html_to_text(str(body)) if body else ""
        return Detail(text or None, " ".join(place.get_text().split()) if place else None)


class ZohoReader:
    def __init__(self, board: str, http: Http) -> None:
        self._host, self._http = board, http

    def list_jobs(self, keywords: list[str], limit: int) -> list[SiteJob]:
        items: list[dict[str, Any]] | None = None
        try:
            items = _items(
                self._http.get_json(f"https://{self._host}/recruit/v2/publishedjobs"), "data"
            )
        except SiteError:
            items = None
        if items is None:
            items = self._from_page()
        jobs = []
        for item in items:
            job_id = _text(item.get("id") or item.get("Id"))
            title = _text(
                item.get("Posting_Title") or item.get("Job_Opening_Name") or item.get("title")
            )
            place = ", ".join(
                p
                for p in (
                    _text(item.get("City")),
                    _text(item.get("State")),
                    _text(item.get("Country")),
                )
                if p
            )
            if not job_id or not title:
                continue
            jobs.append(
                SiteJob(
                    job_id=job_id,
                    title=title,
                    location=place or _text(item.get("location")),
                    link=f"https://{self._host}/jobs/Careers/{job_id}",
                    posted_on=_date_iso(item.get("Date_Opened") or item.get("Created_Time")),
                    description=html_to_text(_text(item.get("Job_Description"))) or None,
                )
            )
        return jobs

    def _from_page(self) -> list[dict[str, Any]]:
        page = self._http.get_text(f"https://{self._host}/jobs/Careers")
        field = BeautifulSoup(page, "html.parser").find("input", id="jobs")
        if field is not None:
            try:
                data = json.loads(html.unescape(str(field.get("value", ""))))
            except ValueError:
                data = None
            if isinstance(data, list):
                return [i for i in data if isinstance(i, dict)]
        raise SiteError("The job list could not be read (the page may load its jobs with scripts).")

    def describe(self, job: SiteJob) -> Detail:
        return Detail(job.description)


READERS: dict[str, type] = {
    GREENHOUSE: GreenhouseReader,
    LEVER: LeverReader,
    ASHBY: AshbyReader,
    WORKDAY: WorkdayReader,
    JOBVITE: JobviteReader,
    ZOHO: ZohoReader,
}


def make_reader(system: str, board: str, http: Http) -> Reader:
    kind = READERS.get(system)
    if kind is None:
        raise SiteError("Not supported yet.")
    reader: Reader = kind(board, http)
    return reader
