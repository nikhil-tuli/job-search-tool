"""Naukri: reading its data, the run logic with a stand-in source, and the command."""

from __future__ import annotations

import io
import json
import random
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jobscan import db
from jobscan.cli import main
from jobscan.config import Config, ConfigError
from jobscan.fit_filters import listed_salary, required_years
from jobscan.matching import TitleMatcher
from jobscan.models import PORTAL_LINKEDIN, PORTAL_NAUKRI, NewRole, RoleFilter
from jobscan.naukri import (
    NaukriShapeError,
    facts_header,
    job_age_days,
    job_closed,
    parse_job,
    parse_search,
    posted_iso,
    search_url,
)
from jobscan.naukri_run import (
    NaukriRunner,
    Reply,
    SourceReply,
    reminder_text,
)
from jobscan.scanner import ScanSetupError
from jobscan.store import RUN_FAILED, RUN_PARTIAL, RUN_SUCCESS, Store
from jobscan.timeutil import now_ist, to_iso

FIXTURES = Path(__file__).parent / "fixtures" / "naukri"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


def fresh_page(count: int | None = None, *, stale: set[str] = frozenset()) -> dict[str, Any]:  # type: ignore[assignment]
    """The logged-out search page with every role posted a few hours ago (except `stale`)."""
    page = load("search_logged_out.json")
    now_ms = int((now_ist() - timedelta(hours=3)).timestamp() * 1000)
    for item in page["jobDetails"]:
        item["createdDate"] = (now_ms - 90 * 86_400_000) if item["jobId"] in stale else now_ms
    if count is not None:
        page["jobDetails"] = page["jobDetails"][:count]
    return page


class FakeSource:
    """Pages and job replies are scripted; every request is recorded."""

    def __init__(
        self,
        pages: list[object],
        jobs: dict[str, SourceReply] | None = None,
        captchas: int = 0,
    ) -> None:
        self.pages = pages
        self.jobs = jobs or {}
        self.captchas = captchas
        self.searches: list[tuple[str, int]] = []
        self.job_calls: list[str] = []
        self.waits = 0

    def search(self, keyword: str, page_index: int) -> SourceReply:
        self.searches.append((keyword, page_index))
        if self.captchas > 0:
            self.captchas -= 1
            return SourceReply(Reply.CAPTCHA, note="HTTP 406")
        if page_index >= len(self.pages):
            return SourceReply(Reply.OK, {"jobDetails": []})
        return SourceReply(Reply.OK, self.pages[page_index])

    def job(self, job_id: str, link: str) -> SourceReply:
        self.job_calls.append(job_id)
        if self.captchas > 0:
            self.captchas -= 1
            return SourceReply(Reply.CAPTCHA, note="HTTP 406")
        return self.jobs.get(job_id) or SourceReply(Reply.OK, load("job_logged_out.json"))

    def wait_for_captcha(self, seconds: int) -> None:
        self.waits += 1


def runner(store: Store, source: FakeSource, **config: Any) -> tuple[NaukriRunner, list[str]]:
    lines: list[str] = []
    defaults = {"stop_after_empty_pages": 20, "naukri_max_pages": 3}
    cfg = Config(**{**defaults, **config})
    return (
        NaukriRunner(
            store=store,
            source=source,  # type: ignore[arg-type]
            config=cfg,
            emit=lines.append,
            sleep=lambda _s: None,
            rng=random.Random(1),
        ),
        lines,
    )


def matching_ids(page: dict[str, Any], store: Store) -> list[str]:
    phrases = [p.phrase for p in store.list_phrases() if p.use_for_match]
    matcher = TitleMatcher(phrases, "contains")
    return [i["jobId"] for i in page["jobDetails"] if matcher.match(i["title"])]


# ---- reading the data -------------------------------------------------------------------


def test_search_page_is_read_into_cards() -> None:
    cards = parse_search(load("search_logged_out.json"))
    assert len(cards) == 20
    first = cards[1]
    assert first.job_id == "051026504407"
    assert first.title == "Sr Manager - TPM"
    assert first.company == "Schneider Electric"
    assert first.location == "Vadodara"
    assert first.link.startswith("https://www.naukri.com/job-listings-")
    assert first.posted_on is not None
    assert first.posted_on.endswith("+05:30")
    assert (first.min_years, first.max_years) == (7, 11)
    assert first.salary_min is None  # "Not disclosed"
    assert "<" not in first.snippet


def test_salary_is_read_when_naukri_shows_one() -> None:
    cards = parse_search(load("search_logged_in.json"))
    shown = [c for c in cards if c.salary_min]
    assert len(shown) == 4
    assert shown[0].salary_min == 2_250_000
    assert shown[0].salary_max == 3_750_000


@pytest.mark.parametrize("payload", [{}, [], "x", {"jobDetails": "x"}, None])
def test_a_page_of_the_wrong_shape_is_refused(payload: object) -> None:
    with pytest.raises(NaukriShapeError):
        parse_search(payload)


def test_items_without_an_id_or_title_are_skipped() -> None:
    page = {"jobDetails": [{"jobId": "", "title": "x"}, {"jobId": "1", "title": ""}, "junk"]}
    assert parse_search(page) == []


def test_posted_time_reads_both_forms() -> None:
    assert posted_iso(1_791_807_259_000) == "2026-10-12T17:44:19+05:30"
    assert posted_iso("2026-10-05 18:50:11") == "2026-10-05T18:50:11+05:30"
    assert posted_iso(None) is None
    assert posted_iso("soon") is None
    assert posted_iso(True) is None


def test_job_page_gives_description_with_experience_first() -> None:
    job = parse_job(load("job_logged_out.json"))
    assert not job.closed
    assert job.detail is not None
    assert job.detail.description.startswith("Experience required: 7 to 11 years.")
    assert "Salary" not in job.detail.description
    assert "<" not in job.detail.description
    assert "- Lead cross-functional improvement projects" in job.detail.description
    assert job.detail.employment_type == "Full Time, Permanent"
    assert job.detail.industries == "Electrical Equipment"
    assert job.detail.job_function == "Production, Manufacturing & Engineering"
    assert job.posted_on == "2026-10-05T18:50:11+05:30"


def test_the_fit_rules_can_read_what_the_header_carries() -> None:
    details = {
        "minimumExperience": 7,
        "maximumExperience": 11,
        "salaryDetail": {
            "hideSalary": False,
            "minimumSalary": 4_400_000,
            "maximumSalary": 7_000_000,
        },
    }
    header = facts_header(details)
    assert required_years(header) == 7  # the lower end, as for any "7 to 11 years"
    assert listed_salary(header) == 4_400_000


def test_job_page_without_a_description_has_no_detail() -> None:
    payload = {"jobDetails": {"description": "  ", "createdDate": "2026-10-05 18:50:11"}}
    job = parse_job(payload)
    assert job.detail is None
    assert not job.closed


def test_job_page_of_the_wrong_shape_is_refused() -> None:
    with pytest.raises(NaukriShapeError):
        parse_job({"message": "hello"})


@pytest.mark.parametrize(
    ("payload", "status", "closed"),
    [
        ({}, 404, True),
        ({}, 410, True),
        ({"message": "This job has expired"}, 200, True),
        ({"jobDetails": {"expired": True}}, 200, True),
        ({"jobDetails": {"expired": False}}, 200, False),
        ({"message": "Please provide the valid App Id"}, 400, False),
        (None, 200, False),
    ],
)
def test_closed_job_detection_trusts_only_clear_signals(
    payload: object, status: int, closed: bool
) -> None:
    assert job_closed(payload, status) is closed


def test_search_addresses() -> None:
    assert search_url("Technical Program Manager", "india", 0).startswith(
        "https://www.naukri.com/technical-program-manager-jobs-in-india?sort=f&k=Technical%20Program"
    )
    assert "-jobs-in-india-3?" in search_url("TPM", "India", 2)


def test_search_is_newest_first_and_limited_to_recent_days() -> None:
    url = search_url("tpm", "india", 0, 1)
    assert url == "https://www.naukri.com/tpm-jobs-in-india?sort=f&jobAge=1&k=tpm&l=india"
    assert [job_age_days(h) for h in (1, 24, 25, 48, 72)] == [1, 1, 2, 2, 3]


# ---- the run ----------------------------------------------------------------------------


def test_run_saves_fresh_matching_roles_and_skips_the_rest(store: Store) -> None:
    page = fresh_page()
    source = FakeSource([page])
    run, _lines = runner(store, source)
    result = run.run()
    wanted = matching_ids(page, store)
    assert result.status == RUN_SUCCESS
    assert store.count_roles(RoleFilter(portals=(PORTAL_NAUKRI,))) == len(wanted)
    assert store.count_roles(RoleFilter(portals=(PORTAL_LINKEDIN,))) == 0
    role = store.get_role(PORTAL_NAUKRI, "051026504407")
    assert role is not None
    assert role.posted_precision == "minute"
    assert role.link.startswith("https://www.naukri.com/")
    summary = store.get_run(result.run_id)
    assert summary is not None
    assert summary.portal == PORTAL_NAUKRI
    assert summary.new_roles == len(wanted)
    skipped, total = store.recent_skipped("2000-01-01T00:00:00+05:30", 100, PORTAL_NAUKRI)
    assert total == 20 - len(wanted)
    assert {s.portal for s in skipped} == {PORTAL_NAUKRI}


def test_roles_posted_outside_the_window_are_ignored(store: Store) -> None:
    page = fresh_page(stale={"051026504407"})
    run, _ = runner(store, FakeSource([page]))
    run.run()
    assert store.get_role(PORTAL_NAUKRI, "051026504407") is None


def test_a_second_run_adds_nothing_twice(store: Store) -> None:
    page = fresh_page()
    runner(store, FakeSource([page]))[0].run()
    first = store.count_roles()
    runner(store, FakeSource([page]))[0].run()
    assert store.count_roles() == first


def test_paging_stops_at_the_cap_and_at_the_end(store: Store) -> None:
    pages = [fresh_page(), fresh_page(), fresh_page(), fresh_page()]
    source = FakeSource(pages)
    runner(store, source, naukri_max_pages=2)[0].run()
    keyword = source.searches[0][0]
    assert [i for k, i in source.searches if k == keyword] == [0, 1]
    short = FakeSource([fresh_page(5)])
    runner(store, short, naukri_max_pages=5)[0].run()
    keyword = short.searches[0][0]
    assert [i for k, i in short.searches if k == keyword] == [0]


def test_a_same_company_title_and_city_on_linkedin_is_flagged(store: Store) -> None:
    run_id = store.start_run(window_seconds=86_400, keywords_total=1)
    store.insert_role(
        NewRole(
            portal=PORTAL_LINKEDIN,
            job_id="999",
            title="Technical Program Manager (TPM)",
            company="Mindteck",
            location="Bengaluru",
            link="https://www.linkedin.com/jobs/view/999",
            posted_on=None,
            posted_precision="unknown",
            fetched_on=to_iso(now_ist()),
            actively_hiring=False,
            matched_phrase="Technical Program Manager",
            possible_duplicate_of=None,
            dup_key="mindteck|technical program manager (tpm)|bengaluru",
            raw_datetime=None,
            raw_relative=None,
            run_id=run_id,
        )
    )
    from jobscan.matching import dup_key

    store._conn.execute(
        "UPDATE roles SET dup_key = ? WHERE job_id = '999'",
        (dup_key("Mindteck", "Technical Program Manager (TPM)", "Bengaluru"),),
    )
    runner(store, FakeSource([fresh_page()]))[0].run()
    role = store.get_role(PORTAL_NAUKRI, "121026503769")
    assert role is not None
    assert role.possible_duplicate_of == "LinkedIn|999"


def test_job_pages_fill_in_descriptions(store: Store) -> None:
    page = fresh_page()
    source = FakeSource([page])
    runner(store, source)[0].run()
    role = store.get_role(PORTAL_NAUKRI, "051026504407")
    assert role is not None
    assert role.description is not None
    assert role.description.startswith("Experience required")
    assert role.industries == "Electrical Equipment"
    assert source.job_calls  # at least one page was opened


def test_roles_failing_fit_are_not_opened(store: Store) -> None:
    page = fresh_page()
    source = FakeSource([page])
    runner(store, source, fit_excluded_companies="Schneider Electric")[0].run()
    assert "051026504407" not in source.job_calls
    assert "121026503769" in source.job_calls


def test_the_cap_on_job_pages_holds(store: Store) -> None:
    source = FakeSource([fresh_page()])
    runner(store, source, naukri_max_job_pages=2)[0].run()
    assert len(source.job_calls) == 2


def test_job_pages_can_be_switched_off(store: Store) -> None:
    source = FakeSource([fresh_page()])
    runner(store, source, naukri_max_job_pages=0)[0].run()
    assert source.job_calls == []


def test_a_gone_job_is_closed_unless_you_already_moved_it(store: Store) -> None:
    page = fresh_page()
    gone = SourceReply(Reply.GONE, note="HTTP 404")
    source = FakeSource([page], jobs={"051026504407": gone, "121026503769": gone})
    store.start_run(window_seconds=1, keywords_total=1)
    # Run once without job pages so the roles exist, then mark one as applied by hand.
    runner(store, source, naukri_max_job_pages=0)[0].run()
    store.update_status([f"{PORTAL_NAUKRI}|121026503769"], "Applied")
    runner(store, source)[0].run()
    closed = store.get_role(PORTAL_NAUKRI, "051026504407")
    applied = store.get_role(PORTAL_NAUKRI, "121026503769")
    assert closed is not None
    assert closed.status == "Closed"
    assert applied is not None
    assert applied.status == "Applied"


def test_an_expired_notice_in_the_data_closes_the_role(store: Store) -> None:
    expired = SourceReply(Reply.OK, {"message": "This job has expired"})
    source = FakeSource([fresh_page()], jobs={"051026504407": expired})
    runner(store, source)[0].run()
    role = store.get_role(PORTAL_NAUKRI, "051026504407")
    assert role is not None
    assert role.status == "Closed"


def test_a_missing_description_is_counted_and_retried_later(store: Store) -> None:
    empty = SourceReply(Reply.OK, {"jobDetails": {"description": ""}})
    source = FakeSource([fresh_page()], jobs={"051026504407": empty})
    runner(store, source)[0].run()
    role = store.get_role(PORTAL_NAUKRI, "051026504407")
    assert role is not None
    assert role.description is None
    row = store._conn.execute(
        "SELECT detail_attempts FROM roles WHERE job_id = '051026504407'"
    ).fetchone()
    assert row[0] == 1


def test_a_captcha_waits_for_you_then_carries_on(store: Store) -> None:
    source = FakeSource([fresh_page(5)], captchas=1)
    run, lines = runner(store, source)
    result = run.run()
    assert source.waits == 1
    assert result.status == RUN_SUCCESS
    assert any("captcha" in line.lower() for line in lines)


def test_repeated_captchas_stop_the_run(store: Store) -> None:
    source = FakeSource([fresh_page()], captchas=10)
    result = runner(store, source, naukri_abort_after_captchas=2)[0].run()
    assert result.status in (RUN_PARTIAL, RUN_FAILED)
    assert "captcha" in result.note.lower()
    assert source.job_calls == []
    assert source.waits == 1


def test_a_changed_data_layout_fails_the_run_and_saves_nothing(store: Store) -> None:
    source = FakeSource([{"somethingNew": []}])
    result = runner(store, source)[0].run()
    assert result.status == RUN_FAILED
    assert "layout" in result.note.lower()
    assert store.count_roles() == 0
    assert source.job_calls == []


def test_the_run_needs_a_keyword(store: Store) -> None:
    for phrase in store.list_phrases():
        store._conn.execute("UPDATE phrases SET use_as_keyword = 0 WHERE id = ?", (phrase.id,))
    with pytest.raises(ScanSetupError):
        runner(store, FakeSource([]))[0].run()


# ---- per-portal behaviour in the store ---------------------------------------------------


def test_each_portal_has_its_own_runs_and_pending_descriptions(store: Store) -> None:
    runner(store, FakeSource([fresh_page()]), naukri_max_job_pages=0)[0].run()
    since = "2000-01-01T00:00:00+05:30"
    assert store.pending_details(since, 100) == []  # LinkedIn's list is untouched
    assert store.pending_details(since, 100, PORTAL_NAUKRI)
    assert store.last_successful_run() is None
    assert store.last_successful_run(PORTAL_NAUKRI) is not None
    assert store.latest_run() is None
    assert [r.portal for r in store.list_runs()] == [PORTAL_NAUKRI]


def test_fetch_summary_can_be_limited_to_one_portal(store: Store) -> None:
    runner(store, FakeSource([fresh_page()]), naukri_max_job_pages=0)[0].run()
    assert store.fetch_summary(PORTAL_NAUKRI)
    assert all(day.total == 0 for day in store.fetch_summary(PORTAL_LINKEDIN))


def test_scoring_run_remembers_which_portal_triggered_it(store: Store) -> None:
    run_id = store.start_scoring_run("scan", when=to_iso(now_ist()), source=PORTAL_NAUKRI)
    row = store._conn.execute("SELECT source FROM scoring_runs WHERE id = ?", (run_id,)).fetchone()
    assert row[0] == PORTAL_NAUKRI


# ---- reminder and command -----------------------------------------------------------------


def test_reminder_shows_only_after_a_naukri_run_gets_old(store: Store) -> None:
    now = now_ist()
    assert reminder_text(store, now) is None  # never run: no nagging
    run_id = store.start_run(window_seconds=1, keywords_total=1, portal=PORTAL_NAUKRI)
    store.finish_run(run_id, RUN_SUCCESS)
    assert reminder_text(store, now + timedelta(days=1)) is None
    assert reminder_text(store, now + timedelta(days=2) - timedelta(minutes=1)) is None
    text = reminder_text(store, now + timedelta(days=3, minutes=1))
    assert text == "Naukri last run: 3 days ago. Run jobscan naukri."


class _Window:
    def __init__(self, source: FakeSource) -> None:
        self.source = source

    def __enter__(self) -> FakeSource:
        return self.source

    def __exit__(self, *exc: object) -> None:
        return None


def test_the_naukri_command_runs_and_reports(tmp_path: Path) -> None:
    out = io.StringIO()
    source = FakeSource([fresh_page()])
    code = main(
        ["--db", str(tmp_path / "j.db"), "naukri"],
        out=out,
        open_source=lambda _cfg, _emit: _Window(source),
        sleep=lambda _s: None,
        make_embedder=lambda _c: (_ for _ in ()).throw(RuntimeError("no key")),
    )
    text = out.getvalue()
    assert code == 0
    assert "private browser window" in text
    assert "Run #1 success" in text


def test_status_shows_the_naukri_run(tmp_path: Path) -> None:
    db_file = tmp_path / "j.db"
    main(
        ["--db", str(db_file), "naukri"],
        out=io.StringIO(),
        open_source=lambda _cfg, _emit: _Window(FakeSource([fresh_page()])),
        sleep=lambda _s: None,
        make_embedder=lambda _c: (_ for _ in ()).throw(RuntimeError("no key")),
    )
    out = io.StringIO()
    main(["--db", str(db_file), "status"], out=out)
    assert "Naukri latest run: #1 success" in out.getvalue()


def test_naukri_settings_are_validated() -> None:
    cfg = Config.from_raw({"naukri_location": "  Bengaluru  "})
    assert cfg.naukri_location == "bengaluru"
    for bad in ({"naukri_location": "x/y"}, {"naukri_max_pages": "0"}):
        with pytest.raises(ConfigError):
            Config.from_raw(bad)
    with pytest.raises(ConfigError):
        Config.from_raw({"naukri_delay_seconds_min": "20", "naukri_delay_seconds_max": "10"})


def test_a_page_repeating_the_one_before_stops_that_keyword(store: Store) -> None:
    source = FakeSource([fresh_page(), fresh_page(), fresh_page()])
    run, lines = runner(store, source, naukri_max_pages=3)
    result = run.run()
    keyword = source.searches[0][0]
    assert [i for k, i in source.searches if k == keyword] == [0, 1]
    assert any("same roles as the page before" in line for line in lines)
    assert result.status == "partial"


def test_a_later_page_copies_the_page_one_request() -> None:
    from jobscan.naukri_browser import _page_url

    url = "https://www.naukri.com/jobapi/v3/search?keyword=tpm&pageNo=1&k=tpm&noOfResults=20"
    assert "pageNo=3" in _page_url(url, 3)
    assert "keyword=tpm" in _page_url(url, 3)
    assert "pageNo=2" in _page_url("https://www.naukri.com/jobapi/v3/search?k=tpm", 2)


def test_naukri_details_reads_only_job_pages_for_roles_still_missing_them(tmp_path: Path) -> None:
    db_file = tmp_path / "j.db"
    no_embed = lambda _c: (_ for _ in ()).throw(RuntimeError("no key"))  # noqa: E731
    first = FakeSource([fresh_page()])
    first.job = lambda job_id, link: SourceReply(Reply.ERROR, note="HTTP 500")  # type: ignore[method-assign]
    main(
        ["--db", str(db_file), "naukri"],
        out=io.StringIO(),
        open_source=lambda _cfg, _emit: _Window(first),
        sleep=lambda _s: None,
        make_embedder=no_embed,
    )
    out = io.StringIO()
    second = FakeSource([])
    code = main(
        ["--db", str(db_file), "naukri-details"],
        out=out,
        open_source=lambda _cfg, _emit: _Window(second),
        sleep=lambda _s: None,
        make_embedder=no_embed,
    )
    text = out.getvalue()
    assert code == 0
    assert second.searches == []  # no searching, only job pages
    assert second.job_calls
    assert "lack a description" in text
    assert "Job pages:" in text


def test_naukri_details_with_nothing_missing_does_not_open_a_window(tmp_path: Path) -> None:
    def refuse(_cfg: Config, _emit: object) -> object:
        raise AssertionError("no window should open")

    out = io.StringIO()
    code = main(
        ["--db", str(tmp_path / "j.db"), "naukri-details"],
        out=out,
        open_source=refuse,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    assert code == 0
    assert "nothing to fetch" in out.getvalue()
