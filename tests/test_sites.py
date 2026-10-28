"""Company sites: recognising the hiring system, the readers, the run, the command and the pages."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from flask.testing import FlaskClient

from jobscan import cli, db
from jobscan.cli import EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, main
from jobscan.config import Config
from jobscan.models import SitesCompany
from jobscan.sites_readers import (
    AshbyReader,
    Detail,
    GreenhouseReader,
    HttpClient,
    JobviteReader,
    LeverReader,
    SiteError,
    SiteJob,
    WorkdayReader,
    ZohoReader,
    detect,
    html_to_text,
    make_reader,
    with_detail,
)
from jobscan.sites_run import SitesRunner, location_ok, slug, try_company
from jobscan.store import Store
from jobscan.timeutil import IST, to_iso
from jobscan.web import create_app

NOW = datetime(2026, 10, 28, 8, 0, tzinfo=IST)
FRESH = to_iso(NOW - timedelta(days=3))
OLD = to_iso(NOW - timedelta(days=90))
TPM = "Technical Program Manager"


# ---- recognising the hiring system ------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "system", "board"),
    [
        ("https://jobs.lever.co/mythic-ai.com", "lever", "jobs.lever.co|mythic-ai.com"),
        ("https://job-boards.greenhouse.io/doordashusa", "greenhouse", "doordashusa"),
        ("https://boards.greenhouse.io/acme/jobs/12", "greenhouse", "acme"),
        ("https://jobs.ashbyhq.com/acme", "ashby", "acme"),
        (
            "https://arcticwolf.wd1.myworkdayjobs.com/External",
            "workday",
            "arcticwolf.wd1.myworkdayjobs.com|arcticwolf|External",
        ),
        (
            "https://target.wd5.myworkdayjobs.com/en-US/targetcareers/jobs",
            "workday",
            "target.wd5.myworkdayjobs.com|target|targetcareers",
        ),
        ("https://jobs.jobvite.com/imagine-learning", "jobvite", "imagine-learning"),
        (
            "https://tazapay.zohorecruit.in/jobs/Careers",
            "zoho",
            "tazapay.zohorecruit.in",
        ),
        ("https://careersatdoordash.com/job-search/", "", ""),
        ("https://www.syneoshealth.com/clinical-corporate-careers/search/jobs/", "", ""),
    ],
)
def test_detect(url: str, system: str, board: str) -> None:
    found = detect(url)
    assert (found.system, found.board) == (system, board)


def test_detect_needs_a_full_link() -> None:
    with pytest.raises(SiteError):
        detect("jobs.lever.co/acme")


# ---- readers (fake network) -------------------------------------------------------------------


class FakeHttp:
    def __init__(self, gets: dict[str, Any] | None = None, posts: Any = None) -> None:
        self.gets = gets or {}
        self.posts = posts
        self.calls: list[str] = []

    def _get(self, url: str) -> Any:
        self.calls.append(url)
        if url not in self.gets:
            raise SiteError("missing")
        value = self.gets[url]
        if isinstance(value, Exception):
            raise value
        return value

    def get_json(self, url: str) -> Any:
        return self._get(url)

    def get_text(self, url: str) -> str:
        return str(self._get(url))

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        self.calls.append(f"POST {url} {body['offset']} {body['searchText']}")
        return self.posts(url, body)


def test_greenhouse_reader() -> None:
    http = FakeHttp(
        {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": {
                "jobs": [
                    {
                        "id": 7,
                        "title": TPM,
                        "location": {"name": "Hyderabad, India"},
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
                        "first_published": "2026-10-20T10:00:00-04:00",
                        "content": "&lt;p&gt;Lead &lt;b&gt;delivery&lt;/b&gt;&lt;/p&gt;",
                    },
                    {"id": 8, "title": "", "location": {}},
                ]
            }
        }
    )
    (job,) = GreenhouseReader("acme", http).list_jobs([], 100)
    assert (job.job_id, job.location, job.description) == ("7", "Hyderabad, India", "Lead delivery")
    assert job.posted_on is not None
    assert job.posted_on.endswith("+05:30")


def test_lever_reader() -> None:
    http = FakeHttp(
        {
            "https://api.lever.co/v0/postings/mythic-ai.com?mode=json": [
                {
                    "id": "abc",
                    "text": "Program Manager",
                    "categories": {"location": "Bangalore"},
                    "allLocations": ["Bangalore", "Remote"],
                    "hostedUrl": "https://jobs.lever.co/mythic-ai.com/abc",
                    "createdAt": 1_760_000_000_000,
                    "descriptionPlain": "About the role",
                    "lists": [{"text": "You will", "content": "<li>ship</li>"}],
                }
            ]
        }
    )
    (job,) = LeverReader("jobs.lever.co|mythic-ai.com", http).list_jobs([], 10)
    assert job.location == "Bangalore; Remote"
    assert "About the role" in (job.description or "")
    assert "- ship" in (job.description or "")
    assert job.posted_on is not None


def test_ashby_reader_skips_unlisted() -> None:
    http = FakeHttp(
        {
            "https://api.ashbyhq.com/posting-api/job-board/acme": {
                "jobs": [
                    {
                        "id": "1",
                        "title": TPM,
                        "location": "Bengaluru",
                        "jobUrl": "u1",
                        "publishedAt": "2026-10-25T00:00:00.000+00:00",
                        "descriptionPlain": "D",
                    },
                    {"id": "2", "title": "Hidden", "isListed": False},
                ]
            }
        }
    )
    jobs = AshbyReader("acme", http).list_jobs([], 10)
    assert [j.job_id for j in jobs] == ["1"]


def workday_posts(url: str, body: dict[str, Any]) -> dict[str, Any]:
    offset = body["offset"]
    rows = [
        {
            "title": TPM,
            "externalPath": f"/job/Bengaluru/TPM_R{offset + i}",
            "locationsText": "Bengaluru",
            "postedOn": "Posted 3 Days Ago",
        }
        for i in range(20 if offset == 0 else 3)
    ]
    rows.append(
        {
            "title": "Old",
            "externalPath": "/job/x/Old_R9",
            "locationsText": "2 Locations",
            "postedOn": "Posted 30+ Days Ago",
        }
    )
    return {"total": 24, "jobPostings": rows[: 20 if offset == 0 else 4]}


def test_workday_reader_pages_and_reads_detail() -> None:
    host = "t.wd1.myworkdayjobs.com"
    http = FakeHttp(
        {
            f"https://{host}/wday/cxs/t/Ext/job/Bengaluru/TPM_R0": {
                "jobPostingInfo": {
                    "jobDescription": "<p>Desc</p>",
                    "location": "Bengaluru",
                    "additionalLocations": ["Pune"],
                    "startDate": "2026-10-10",
                }
            },
            f"https://{host}/wday/cxs/t/Ext/job/x/Old_R9": {
                "jobPostingInfo": {
                    "jobDescription": "d",
                    "location": "Pune",
                    "startDate": "2025-01-01",
                }
            },
        },
        workday_posts,
    )
    reader = WorkdayReader(f"{host}|t|Ext", http)
    jobs = reader.list_jobs([TPM], 100)
    assert len(jobs) == 24  # 20 on page one, 3 + the old one on page two
    first = jobs[0]
    assert first.job_id == "TPM_R0"
    assert first.posted_on is not None
    assert first.link == f"https://{host}/Ext/job/Bengaluru/TPM_R0"
    old = next(j for j in jobs if j.job_id == "Old_R9")
    assert old.needs_detail
    assert old.posted_on is None
    assert old.location == ""
    detail = reader.describe(first)
    assert detail.location == "Bengaluru; Pune"
    assert detail.posted_on is not None
    assert detail.posted_on.startswith("2026-10-10")
    assert with_detail(old, reader.describe(old)).location == "Pune"


JOBVITE_PAGE = (
    '<table class="jv-job-list"><tr><td class="jv-job-list-name">'
    '<a href="/imagine/job/o123">Program Manager</a></td>'
    '<td class="jv-job-list-location">Bengaluru, India</td></tr></table>'
)


def test_jobvite_reader_and_unreadable_page() -> None:
    http = FakeHttp(
        {
            "https://jobs.jobvite.com/imagine/jobs": JOBVITE_PAGE,
            "https://jobs.jobvite.com/imagine/job/o123": (
                '<div class="jv-job-detail-description"><p>Do things</p></div>'
            ),
            "https://jobs.jobvite.com/blank/jobs": "<html><body>Loading</body></html>",
        }
    )
    reader = JobviteReader("imagine", http)
    (job,) = reader.list_jobs([], 10)
    assert (job.job_id, job.location) == ("o123", "Bengaluru, India")
    assert reader.describe(job).description == "Do things"
    with pytest.raises(SiteError, match="scripts"):
        JobviteReader("blank", http).list_jobs([], 10)


def test_zoho_reader_json_then_page_then_failure() -> None:
    host = "t.zohorecruit.in"
    row = {
        "id": "9",
        "Posting_Title": TPM,
        "City": "Bengaluru",
        "Country": "India",
        "Job_Description": "<p>D</p>",
        "Date_Opened": "2026-10-20",
    }
    via_json = FakeHttp({f"https://{host}/recruit/v2/publishedjobs": {"data": [row]}})
    (job,) = ZohoReader(host, via_json).list_jobs([], 10)
    assert (job.location, job.description) == ("Bengaluru, India", "D")
    assert job.link == f"https://{host}/jobs/Careers/9"
    page = (
        '<input type="hidden" id="jobs" value="' + json.dumps([row]).replace('"', "&quot;") + '">'
    )
    via_page = FakeHttp(
        {
            f"https://{host}/recruit/v2/publishedjobs": SiteError("no"),
            f"https://{host}/jobs/Careers": page,
        }
    )
    assert len(ZohoReader(host, via_page).list_jobs([], 10)) == 1
    nothing = FakeHttp(
        {
            f"https://{host}/recruit/v2/publishedjobs": SiteError("no"),
            f"https://{host}/jobs/Careers": "<p/>",
        }
    )
    with pytest.raises(SiteError):
        ZohoReader(host, nothing).list_jobs([], 10)


def test_make_reader_rejects_unknown_system() -> None:
    with pytest.raises(SiteError, match="Not supported"):
        make_reader("", "", FakeHttp())


def test_html_to_text_keeps_bullets() -> None:
    assert html_to_text("<p>One</p><ul><li>a</li><li>b</li></ul>") == "One\n- a\n- b"


class FakeResponse:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self._payload = payload
        self.text = "text"

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, outcome: FakeResponse | Exception) -> None:
        self.outcome = outcome
        self.headers: dict[str, str] = {}

    def request(self, *args: Any, **kwargs: Any) -> FakeResponse:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    ("outcome", "fragment"),
    [
        (FakeResponse(404), "Link not found"),
        (FakeResponse(403), "Blocked"),
        (FakeResponse(429), "too many"),
        (FakeResponse(503), "problem"),
        (FakeResponse(418), "refused"),
        (FakeResponse(200), "expected form"),
        (requests.Timeout(), "too long"),
        (requests.ConnectionError(), "reached"),
    ],
)
def test_http_failures_have_plain_reasons(outcome: FakeResponse | Exception, fragment: str) -> None:
    client = HttpClient(FakeSession(outcome))  # type: ignore[arg-type]
    with pytest.raises(SiteError, match=fragment):
        client.get_json("https://x.example/y")


def test_http_client_returns_json() -> None:
    assert HttpClient(FakeSession(FakeResponse(200, {"a": 1}))).get_json("https://x") == {"a": 1}  # type: ignore[arg-type]


# ---- the run ----------------------------------------------------------------------------------


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    s = Store(conn)
    s.replace_phrases([(TPM, True, True)])
    yield s
    conn.close()


class FakeReader:
    def __init__(
        self, jobs: list[SiteJob] | Exception, detail: Detail | Exception | None = None
    ) -> None:
        self.jobs, self.detail = jobs, detail or Detail()
        self.described: list[str] = []

    def list_jobs(self, keywords: list[str], limit: int) -> list[SiteJob]:
        if isinstance(self.jobs, Exception):
            raise self.jobs
        return self.jobs

    def describe(self, job: SiteJob) -> Detail:
        self.described.append(job.job_id)
        if isinstance(self.detail, Exception):
            raise self.detail
        return self.detail


def job(job_id: str, title: str = TPM, location: str = "Bengaluru", **more: Any) -> SiteJob:
    fields: dict[str, Any] = {
        "job_id": job_id,
        "title": title,
        "location": location,
        "link": f"https://x/{job_id}",
        "posted_on": FRESH,
        "description": "D",
    }
    fields.update(more)
    return SiteJob(**fields)


def add_company(store: Store, name: str, system: str = "lever", board: str = "b") -> SitesCompany:
    store.upsert_site(name, f"https://jobs.lever.co/{name}", system, board, "")
    found = next(c for c in store.list_sites() if c.name == name)
    return found


def runner(store: Store, readers: dict[str, FakeReader], **kw: Any) -> SitesRunner:
    return SitesRunner(
        store=store,
        config=kw.pop("config", Config()),
        reader_factory=lambda company, http: readers[company.name],  # type: ignore[return-value]
        emit=lambda line: None,
        sleep=lambda s: None,
        clock=lambda: NOW,
        **kw,
    )


def test_run_keeps_matching_roles_only(store: Store) -> None:
    add_company(store, "Mythic")
    reader = FakeReader(
        [
            job("1"),
            job("2", title="Sales Executive"),
            job("3", location="Austin, TX"),
            job("4", posted_on=OLD),
            job("5", location="Remote, Bengaluru"),
        ]
    )
    result = runner(store, {"Mythic": reader}).run()
    assert result.status == "success"
    ids = sorted(r.job_id for r in store.list_roles(limit=50))
    assert ids == ["mythic:1", "mythic:5"]
    role = store.get_role("Company sites", "mythic:1")
    assert role is not None
    assert (role.company, role.portal, role.description) == ("Mythic", "Company sites", "D")
    summary = store.get_run(result.run_id)
    assert summary is not None
    assert (summary.cards_seen, summary.matched, summary.new_roles) == (5, 2, 2)
    assert summary.portal == "Company sites"
    site = store.list_sites()[0]
    assert (site.last_status, site.last_found) == ("ok", 2)


def test_second_run_adds_nothing_new_and_counts_matches(store: Store) -> None:
    add_company(store, "Mythic")
    readers = {"Mythic": FakeReader([job("1")])}
    runner(store, readers).run()
    second = runner(store, readers).run()
    summary = store.get_run(second.run_id)
    assert summary is not None
    assert (summary.matched, summary.new_roles) == (1, 0)


def test_failed_company_makes_a_partial_run_with_reason(store: Store) -> None:
    add_company(store, "Good")
    add_company(store, "Bad")
    run = runner(
        store,
        {"Good": FakeReader([job("1")]), "Bad": FakeReader(SiteError("Link not found (404)."))},
    )
    result = run.run()
    assert result.status == "partial"
    assert "Bad: Link not found" in result.note
    assert run.failures == [("Bad", "Link not found (404).")]
    bad = next(c for c in store.list_sites() if c.name == "Bad")
    assert (bad.last_status, bad.last_note) == ("failed", "Link not found (404).")


def test_all_failed_is_a_failed_run(store: Store) -> None:
    add_company(store, "Bad")
    assert runner(store, {"Bad": FakeReader(SiteError("x"))}).run().status == "failed"


def test_unsupported_and_paused_companies_are_not_fetched(store: Store) -> None:
    add_company(store, "Good")
    add_company(store, "Custom", system="", board="")
    paused = add_company(store, "Paused")
    store.set_site_enabled(paused.id, False)
    run = runner(store, {"Good": FakeReader([job("1")])})
    result = run.run()
    assert result.status == "success"
    assert [s.name for s in run.unsupported] == ["Custom"]
    custom = next(c for c in store.list_sites() if c.name == "Custom")
    assert custom.last_status == "unsupported"
    summary = store.get_run(result.run_id)
    assert summary is not None
    assert summary.keywords_total == 1


def test_setup_problems(store: Store) -> None:
    from jobscan.scanner import ScanSetupError

    with pytest.raises(ScanSetupError, match="no company"):
        runner(store, {}).run()
    add_company(store, "Custom", system="", board="")
    with pytest.raises(ScanSetupError, match="supported system"):
        runner(store, {}).run()
    add_company(store, "Good")
    with pytest.raises(ScanSetupError, match="named"):
        runner(store, {}, only="Nobody").run()
    store.replace_phrases([(TPM, True, False)])
    with pytest.raises(ScanSetupError, match="title matching"):
        runner(store, {}).run()


def test_only_runs_one_company(store: Store) -> None:
    add_company(store, "A")
    add_company(store, "B")
    readers = {"A": FakeReader([job("1")]), "B": FakeReader([job("2")])}
    runner(store, readers, only="b").run()
    assert [r.company for r in store.list_roles(limit=10)] == ["B"]


def test_detail_supplies_missing_date_and_place(store: Store) -> None:
    add_company(store, "W")
    pending = job("1", location="", posted_on=None, description=None, needs_detail=True)
    reader = FakeReader([pending], Detail("Full text", "Pune", FRESH))
    runner(store, {"W": reader}).run()
    role = store.get_role("Company sites", "w:1")
    assert role is not None
    assert (role.location, role.description) == ("Pune", "Full text")
    assert reader.described == ["1"]


def test_detail_shows_role_is_too_old_or_elsewhere(store: Store) -> None:
    add_company(store, "W")
    reader = FakeReader(
        [job("1", location="", posted_on=None, description=None, needs_detail=True)],
        Detail("t", "Pune", OLD),
    )
    runner(store, {"W": reader}).run()
    assert store.list_roles(limit=10) == []


def test_description_failure_does_not_fail_the_company(store: Store) -> None:
    add_company(store, "W")
    reader = FakeReader([job("1", description=None), job("2", description=None)], SiteError("x"))
    result = runner(store, {"W": reader}).run()
    assert result.status == "success"
    assert len(store.list_roles(limit=10)) == 2


def test_descriptions_can_be_switched_off(store: Store) -> None:
    add_company(store, "W")
    reader = FakeReader([job("1", description=None)])
    runner(store, {"W": reader}, config=Config(sites_fetch_descriptions=0)).run()
    assert reader.described == []


def test_cap_per_company(store: Store) -> None:
    add_company(store, "W")
    reader = FakeReader([job(str(i)) for i in range(10)])
    runner(store, {"W": reader}, config=Config(sites_max_roles_per_company=3)).run()
    assert len(store.list_roles(limit=50)) == 3


def test_same_role_on_linkedin_is_flagged_as_possible_duplicate(store: Store) -> None:
    from tests.test_web import add_role

    run = store.start_run(window_seconds=86400, keywords_total=1)
    add_role(store, run, "1", company="Mythic", title=TPM, location="Bengaluru", dup_key=None)
    from jobscan.matching import dup_key

    store._conn.execute(
        "UPDATE roles SET dup_key = ? WHERE job_id = '1'", (dup_key("Mythic", TPM, "Bengaluru"),)
    )
    add_company(store, "Mythic")
    result = runner(store, {"Mythic": FakeReader([job("9")])}).run()
    summary = store.get_run(result.run_id)
    assert summary is not None
    assert summary.possible_dups == 1


def test_helpers() -> None:
    assert slug("Arctic Wolf!") == "arctic-wolf"
    assert location_ok("Hyderabad, Telangana", ("hyderabad",))
    assert not location_ok("Austin", ("India",))
    assert location_ok("Anywhere", ())


def test_try_company_counts_without_saving(store: Store) -> None:
    company = add_company(store, "W")
    reader = FakeReader([job("1"), job("2", title="Sales"), job("3", location="Austin")])
    check = try_company(company, Config(), [TPM], [TPM], None, lambda c, h: reader)  # type: ignore[arg-type]
    assert (check.ok, check.total, check.matched) == (True, 3, 1)
    assert store.list_roles(limit=10) == []
    failed = try_company(
        company, Config(), [TPM], [TPM], None, lambda c, h: FakeReader(SiteError("no"))
    )  # type: ignore[arg-type]
    assert (failed.ok, failed.note) == (False, "no")
    custom = SitesCompany(1, "C", "u", "", "", "", True, "", "", "", 0)
    assert (
        try_company(custom, Config(), [], [], None, lambda c, h: reader).note
        == "Not supported yet."
    )  # type: ignore[arg-type]


# ---- the command ------------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "jobscan.db"
    s = cli.open_store(path)
    s.replace_phrases([(TPM, True, True)])
    s.close()
    return path


def sites_cli(db_path: Path, readers: dict[str, FakeReader], *args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(
        ["--db", str(db_path), "sites", *args],
        out=out,
        sleep=lambda s: None,
        sites_reader_factory=lambda company, http: readers[company.name],  # type: ignore[arg-type,return-value]
    )
    return code, out.getvalue()


def test_command_exit_codes_and_output(db_path: Path) -> None:
    s = cli.open_store(db_path)
    add_company(s, "Good")
    add_company(s, "Custom", system="", board="")
    s.close()
    code, text = sites_cli(
        db_path,
        {
            "Good": FakeReader(
                [job(datetime.now(IST).strftime("%H%M%S"), posted_on=to_iso(datetime.now(IST)))]
            )
        },
    )
    assert code == EXIT_OK
    assert "Good" in text
    assert "Custom: skipped, not supported yet" in text
    assert "1 new" in text or "new" in text
    s = cli.open_store(db_path)
    add_company(s, "Bad")
    s.close()
    code, text = sites_cli(
        db_path, {"Good": FakeReader([]), "Bad": FakeReader(SiteError("Link not found (404)."))}
    )
    assert code == EXIT_PARTIAL
    assert "FAILED: Link not found" in text
    assert "1 company site(s) need attention" in text
    code, _ = sites_cli(db_path, {"Bad": FakeReader(SiteError("x"))}, "--company", "Bad")
    assert code == EXIT_FAILED


def test_command_without_companies(db_path: Path) -> None:
    code, text = sites_cli(db_path, {})
    assert code == EXIT_USAGE
    assert "Cannot start" in text


PAGES = {
    "https://arcticwolf.com/company/hiring/": (
        '<a href="https://arcticwolf.wd1.myworkdayjobs.com/External">Open jobs</a>'
    ),
    "https://www.imaginelearning.com/about/careers/india/": (
        '<a href="https://jobs.jobvite.com/imagine-learning">Explore</a>'
    ),
}


def test_detect_from_page_finds_the_hiring_system_link() -> None:
    from jobscan.sites_readers import detect_from_page

    http = FakeHttp(PAGES)
    found = detect_from_page("https://arcticwolf.com/company/hiring/", http)
    assert (found.system, found.board.split("|")[-1]) == ("workday", "External")
    assert (
        detect_from_page("https://www.imaginelearning.com/about/careers/india/", http).system
        == "jobvite"
    )
    assert detect_from_page("https://example.com/none", http).system == ""


# ---- the pages --------------------------------------------------------------------------------


@pytest.fixture
def client(store: Store) -> FlaskClient:
    app = create_app(
        store,
        secret_key="t",
        sites_http=FakeHttp(PAGES),
        sites_reader_factory=lambda company, http: FakeReader([job("1"), job("2")]),  # type: ignore[arg-type,return-value]
    )
    return app.test_client()


def page(client: FlaskClient, url: str = "/config") -> str:
    return client.get(url).get_data(as_text=True)


def test_config_shows_ten_companies_then_all(store: Store, client: FlaskClient) -> None:
    for number in range(12):
        store.upsert_site(
            f"Firm{number:02d}",
            f"https://jobs.lever.co/f{number}",
            "lever",
            "x|y",
            "GCC" if number < 2 else "",
        )
    first = page(client)
    assert first.count("<strong>Firm") == 10
    assert "Show all 12 companies" in first
    assert page(client, "/config?all=1").count("<strong>Firm") == 12
    assert page(client, "/config?q=firm11").count("<strong>Firm") == 1
    assert page(client, "/config?tag=GCC").count("<strong>Firm") == 2
    assert "Company sites only" in first


def test_attention_is_pinned_first_and_filterable(store: Store, client: FlaskClient) -> None:
    for name in ("Alpha", "Zeta"):
        store.upsert_site(name, f"https://jobs.lever.co/{name}", "lever", "x|y", "")
    zeta = next(c for c in store.list_sites() if c.name == "Zeta")
    store.record_site_result(zeta.id, "failed", "Link not found (404).", 0)
    text = page(client)
    assert text.index("Zeta") < text.index("Alpha")
    assert "1 company needs attention" in text
    assert "Failed: Link not found (404)." in text
    only = page(client, "/config?co=attention")
    assert "Zeta" in only
    assert "<strong>Alpha" not in only
    store.upsert_site("Custom", "https://example.com/careers", "", "", "")
    assert "Not supported yet" in page(client)


def test_add_update_many_and_remove(store: Store, client: FlaskClient) -> None:
    done = client.post(
        "/config/sites/add",
        data={"name": "Mythic", "url": "https://jobs.lever.co/mythic-ai.com", "tag": "Product"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Mythic added (Lever)" in done
    again = client.post(
        "/config/sites/add",
        data={"name": "mythic", "url": "https://jobs.lever.co/other", "tag": ""},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "updated" in again
    assert len(store.list_sites()) == 1
    bad = client.post("/config/sites/add", data={"name": "X", "url": "nope"}, follow_redirects=True)
    assert "full link" in bad.get_data(as_text=True)
    many = client.post(
        "/config/sites/add-many",
        data={
            "lines": "\n".join(
                [
                    "A, https://jobs.lever.co/a, GCC",
                    "B Corp, https://jobs.ashbyhq.com/b",
                    "broken line",
                    "DoorDash, https://careersatdoordash.com/job-search/",
                ]
            )
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "3 companies added or updated" in many
    assert "Skipped" in many
    custom = next(c for c in store.list_sites() if c.name == "DoorDash")
    assert custom.system == ""
    target = next(c for c in store.list_sites() if c.name == "A")
    assert target.tag == "GCC"
    client.post("/config/sites/act", data={"act": f"toggle:{target.id}"})
    assert next(c for c in store.list_sites() if c.name == "A").enabled is False
    client.post("/config/sites/act", data={"act": f"remove:{target.id}"})
    assert all(c.name != "A" for c in store.list_sites())
    missing = client.post("/config/sites/act", data={"act": "remove:999"}, follow_redirects=True)
    assert "no longer exists" in missing.get_data(as_text=True)


def test_test_button_reports_and_records(store: Store, client: FlaskClient) -> None:
    store.upsert_site("Mythic", "https://jobs.lever.co/m", "lever", "x|y", "")
    site = store.list_sites()[0]
    text = client.post(
        "/config/sites/act", data={"act": f"test:{site.id}"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "2 jobs found, 2 look relevant" in text
    assert store.list_sites()[0].last_found == 2
    store.upsert_site("Custom", "https://example.com", "", "", "")
    custom = next(c for c in store.list_sites() if c.name == "Custom")
    note = client.post(
        "/config/sites/act", data={"act": f"test:{custom.id}"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "Not supported yet" in note


def test_settings_are_on_the_page_and_validated(client: FlaskClient) -> None:
    text = page(client)
    for key in ("sites_location_words", "sites_max_age_days", "sites_daily", "sites_delay_seconds"):
        assert f'name="{key}"' in text
    assert Config().sites_max_age_days == 60


def test_roles_summary_and_panel(store: Store, client: FlaskClient) -> None:
    store.upsert_site("Mythic", "https://jobs.lever.co/m", "lever", "x|y", "")
    store.upsert_site("Arctic Wolf", "https://a.wd1.myworkdayjobs.com/Ext", "workday", "a|b|c", "")
    wolf = next(c for c in store.list_sites() if c.name == "Arctic Wolf")
    store.record_site_result(wolf.id, "failed", "Link not found (404).", 0)
    runner(
        store,
        {
            "Mythic": FakeReader([job("1", description="Text")]),
            "Arctic Wolf": FakeReader(SiteError("x")),
        },
    ).run()
    roles = page(client, "/roles?portal=Company+sites")
    assert "Mythic" in roles
    assert "Company sites" in roles
    summary = page(client, "/summary")
    assert "Needs attention (1)" in summary
    assert "Company sites #" in summary
    assert "Company sites" in page(client, "/summary?portal=Company+sites")
    assert "Company sites" in page(client, "/eval")
    detail = page(client, "/roles/Company%20sites/mythic:1")
    assert "Open on Mythic careers" in detail


# ---- the daily schedule -----------------------------------------------------------------------


def scheduled(store: Store, tmp_path: Path) -> list[list[str]]:
    from jobscan import schedule

    calls: list[list[str]] = []

    def spawner(command: Any, env: Any, timeout: float) -> int:
        calls.append(list(command))
        return 0

    schedule.run_scheduled(
        store,
        tmp_path / "jobscan.db",
        io.StringIO(),
        env={"GEMINI_API_KEY": "k"},
        spawner=spawner,
        notifier=lambda title, message: None,
        python="/usr/bin/python3",
    )
    return calls


def test_schedule_runs_company_sites_after_linkedin(store: Store, tmp_path: Path) -> None:
    add_company(store, "Mythic")
    calls = scheduled(store, tmp_path)
    assert [c[-1] for c in calls] == ["scan", "sites"]


def test_schedule_skips_sites_when_off_empty_or_done(store: Store, tmp_path: Path) -> None:
    assert [c[-1] for c in scheduled(store, tmp_path)] == ["scan"]  # no companies yet
    add_company(store, "Mythic")
    raw = Config(sites_daily=0).to_raw()
    store.set_config_raw(raw)
    assert [c[-1] for c in scheduled(store, tmp_path)] == ["scan"]
    store.set_config_raw(Config().to_raw())
    run = runner(store, {"Mythic": FakeReader([job("1")])}).run()
    assert store.get_run(run.run_id) is not None
    assert "sites" not in [c[-1] for c in scheduled(store, tmp_path)]


def test_adding_a_company_page_link_finds_its_system(store: Store, client: FlaskClient) -> None:
    text = client.post(
        "/config/sites/add",
        data={"name": "Arctic Wolf", "url": "https://arcticwolf.com/company/hiring/", "tag": ""},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Arctic Wolf added (Workday)" in text
    assert store.list_sites()[0].system == "workday"


def test_test_button_finds_the_system_for_an_existing_unrecognised_row(
    store: Store, client: FlaskClient
) -> None:
    store.upsert_site(
        "Imagine Learning", "https://www.imaginelearning.com/about/careers/india/", "", "", ""
    )
    site = store.list_sites()[0]
    text = client.post(
        "/config/sites/act", data={"act": f"test:{site.id}"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "found its hiring system (Jobvite)" in text
    assert store.list_sites()[0].system == "jobvite"


def test_run_finds_the_system_of_an_unrecognised_company(store: Store) -> None:
    store.upsert_site("Arctic Wolf", "https://arcticwolf.com/company/hiring/", "", "", "")
    run = SitesRunner(
        store=store,
        config=Config(),
        http=FakeHttp(PAGES),  # type: ignore[arg-type]
        reader_factory=lambda company, http: FakeReader([job("1")]),  # type: ignore[arg-type,return-value]
        emit=lambda line: None,
        sleep=lambda s: None,
        clock=lambda: NOW,
    )
    assert run.run().status == "success"
    assert run.unsupported == []
    assert store.list_sites()[0].system == "workday"
