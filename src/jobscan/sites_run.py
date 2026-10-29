"""One Company sites run: read each company's job list, keep matching roles, save them.

The run decides what to keep. Getting the lists is the job of the readers in sites_readers, so
this logic is tested with fake readers and no network.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Config, split_list
from .matching import TitleMatcher, dup_key
from .models import (
    PORTAL_SITES,
    SITE_FAILED,
    SITE_OK,
    SITE_UNSUPPORTED,
    JobDetail,
    KeywordRecord,
    NewRole,
    SitesCompany,
)
from .scanner import STOP_ABORTED, STOP_END, STOP_INTERRUPTED, RunResult, ScanSetupError
from .sites_readers import (
    Detected,
    Http,
    HttpClient,
    Reader,
    SiteError,
    SiteJob,
    board_answers,
    detect,
    detect_from_page,
    make_reader,
    valid_board,
    with_detail,
)
from .store import RUN_FAILED, RUN_PARTIAL, RUN_SUCCESS, Store
from .timeutil import from_iso, now_ist, to_iso

STOP_ERROR = "error"
DESCRIBE_FAILURES_BEFORE_GIVING_UP = 3

ReaderFactory = Callable[[SitesCompany, Http], Reader]


def default_reader_factory(company: SitesCompany, http: Http) -> Reader:
    return make_reader(company.system, company.board, http)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "company"


def location_ok(location: str, words: tuple[str, ...]) -> bool:
    """True when the location holds one of the words. No words means every location."""
    if not words:
        return True
    text = location.casefold()
    return any(word.casefold() in text for word in words)


def too_old(posted_on: str | None, now: datetime, days: int) -> bool:
    return posted_on is not None and now - from_iso(posted_on) > timedelta(days=days)


@dataclass(frozen=True)
class SiteCheck:
    ok: bool
    total: int = 0
    matched: int = 0
    note: str = ""


def redetect(company: SitesCompany, http: Http, keywords: list[str]) -> Detected | None:
    """A better board for a company whose saved one failed, found again on its own career page.

    A direct hiring-system link is taken as given, so only company-page links are looked at again.
    """
    try:
        if detect(company.url).system:
            return None
    except SiteError:
        return None
    found = detect_from_page(company.url, http, board_answers(http, keywords))
    if found.system and (found.system, found.board) != (company.system, company.board):
        return found
    return None


def try_company(
    company: SitesCompany,
    config: Config,
    phrases: list[str],
    match_phrases: list[str],
    http: Http,
    factory: ReaderFactory = default_reader_factory,
) -> SiteCheck:
    """Read one company's list without saving roles: how many jobs, how many look relevant."""
    if not company.system:
        return SiteCheck(False, note="Not supported yet.")
    try:
        jobs = factory(company, http).list_jobs(phrases, config.sites_max_roles_per_company)
    except SiteError as exc:
        return SiteCheck(False, note=str(exc))
    matcher = TitleMatcher(match_phrases, config.match_mode)
    words = split_list(config.sites_location_words)
    now = now_ist()
    matched = sum(
        1
        for job in jobs
        if matcher.match(job.title)
        and not too_old(job.posted_on, now, config.sites_max_age_days)
        and (not job.location or location_ok(job.location, words))
    )
    return SiteCheck(True, len(jobs), matched)


class SitesRunner:
    def __init__(
        self,
        *,
        store: Store,
        config: Config,
        http: Http | None = None,
        reader_factory: ReaderFactory = default_reader_factory,
        emit: Callable[[str], None] = print,
        sleep: Callable[[float], None],
        clock: Callable[[], datetime] = now_ist,
        only: str | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._http: Http = http or HttpClient()
        self._factory = reader_factory
        self._emit = emit
        self._sleep = sleep
        self._clock = clock
        self._only = only
        self._phrase_counts: Counter[str] = Counter()
        self._first_request = True
        self.unsupported: list[SitesCompany] = []
        self.failures: list[tuple[str, str]] = []

    def run(self) -> RunResult:
        phrases = self._store.list_phrases()
        keywords = [p.phrase for p in phrases if p.use_as_keyword]
        match_phrases = [p.phrase for p in phrases if p.use_for_match]
        if not match_phrases:
            raise ScanSetupError("no title phrase is switched on for title matching")
        sites = [s for s in self._store.list_sites() if s.enabled]
        if self._only:
            sites = [s for s in sites if s.name.casefold() == self._only.casefold()]
            if not sites:
                raise ScanSetupError(f"no switched-on company is named {self._only!r}")
        if not sites:
            raise ScanSetupError("no company is switched on. Add companies on the Config page")
        for bad in [s for s in sites if s.system and not valid_board(s.system, s.board)]:
            self._store.set_site_system(bad.id, "", "")  # a placeholder, not a real board
        sites = [self._store.get_site(s.id) or s for s in sites]
        for site in [s for s in sites if not s.system]:
            found = detect_from_page(  # its own page may link to the system
                site.url, self._http, board_answers(self._http, keywords)
            )
            if found.system:
                self._store.set_site_system(site.id, found.system, found.board)
                self._emit(f"{site.name}: found its hiring system ({found.system})")
        sites = [s for s in self._store.list_sites() if s.id in {x.id for x in sites}]
        self.unsupported = [s for s in sites if not s.system]
        for site in self.unsupported:
            self._store.record_site_result(site.id, SITE_UNSUPPORTED, "Not supported yet.", 0)
        todo = [s for s in sites if s.system]
        if not todo:
            raise ScanSetupError("none of the switched-on companies uses a supported system yet")
        matcher = TitleMatcher(match_phrases, self._config.match_mode)
        run_id = self._store.start_run(
            window_seconds=self._config.sites_max_age_days * 86_400,
            keywords_total=len(todo),
            portal=PORTAL_SITES,
        )
        records: list[KeywordRecord] = []
        try:
            for number, site in enumerate(todo, start=1):
                record = KeywordRecord(site.name)
                records.append(record)
                self._company(run_id, number, len(todo), site, record, keywords, matcher)
        except KeyboardInterrupt:
            self._store.finish_run(run_id, RUN_FAILED, "Interrupted by user")
            raise
        except Exception as exc:
            self._store.finish_run(
                run_id, RUN_FAILED, f"Unexpected error: {type(exc).__name__}: {exc}"
            )
            raise
        status, note = self._final(records)
        self._store.add_phrase_matches(run_id, dict(self._phrase_counts))
        self._store.finish_run(run_id, status, note)
        return RunResult(run_id, status, note, records)

    # ---- one company ---------------------------------------------------------------------

    def _company(
        self,
        run_id: int,
        number: int,
        total: int,
        site: SitesCompany,
        record: KeywordRecord,
        keywords: list[str],
        matcher: TitleMatcher,
    ) -> None:
        label = f"[{number}/{total}] {site.name}"
        try:
            reader = self._factory(site, self._http)
            self._pace()
            limit = self._config.sites_max_roles_per_company
            try:
                jobs = reader.list_jobs(keywords, limit)
            except SiteError:
                better = redetect(site, self._http, keywords)
                if better is None:
                    raise
                self._store.set_site_system(site.id, better.system, better.board)
                self._emit(f"{label}: tried another board from its page ({better.system})")
                site = self._store.get_site(site.id) or site
                reader = self._factory(site, self._http)
                jobs = reader.list_jobs(keywords, limit)
            record.cards = len(jobs)
            with self._store.transaction():
                self._keep(run_id, site, reader, jobs, matcher, record)
        except SiteError as exc:
            record.failed_pages = 1
            record.stopped_by = STOP_ERROR
            self.failures.append((site.name, str(exc)))
            self._store.record_site_result(site.id, SITE_FAILED, str(exc), 0)
            self._emit(f"{label}: FAILED: {exc}")
        except KeyboardInterrupt:
            record.stopped_by = STOP_INTERRUPTED
            raise
        else:
            record.stopped_by = STOP_END
            record.completed = True
            self._store.record_site_result(site.id, SITE_OK, "", record.matched)
            self._emit(
                f"{label}: {record.cards} jobs, {record.matched} matched, "
                f"{record.new_roles} new, {record.possible_dups} possible duplicates"
            )
        finally:
            self._store.record_keyword(run_id, record)

    def _keep(
        self,
        run_id: int,
        site: SitesCompany,
        reader: Reader,
        jobs: list[SiteJob],
        matcher: TitleMatcher,
        record: KeywordRecord,
    ) -> None:
        cfg = self._config
        words = split_list(cfg.sites_location_words)
        now = self._clock()
        fetched_iso = to_iso(now)
        prefix = slug(site.name)
        describe_failures = 0
        considered = 0
        for job in jobs:
            phrase = matcher.match(job.title)
            if phrase is None:
                continue
            if too_old(job.posted_on, now, cfg.sites_max_age_days):
                continue
            considered += 1
            if considered > cfg.sites_max_roles_per_company:
                break
            role_id = f"{prefix}:{job.job_id}"
            exists = self._store.role_exists(PORTAL_SITES, role_id)
            wants_description = not exists and cfg.sites_fetch_descriptions and not job.description
            if (
                not exists
                and (job.needs_detail or wants_description)
                and (describe_failures < DESCRIBE_FAILURES_BEFORE_GIVING_UP)
            ):
                try:
                    self._pace()
                    job = with_detail(job, reader.describe(job))
                except SiteError:
                    describe_failures += 1
            if too_old(job.posted_on, now, cfg.sites_max_age_days):
                continue
            if not job.location and exists:
                continue  # its place cannot be checked again without another request
            if not location_ok(job.location, words) or not job.location:
                continue
            record.matched += 1
            self._phrase_counts[phrase] += 1
            if exists:
                continue
            key = dup_key(site.name, job.title, job.location)
            duplicate_of = self._store.find_duplicate(key)
            self._store.insert_role(
                NewRole(
                    portal=PORTAL_SITES,
                    job_id=role_id,
                    title=job.title,
                    company=site.name,
                    location=job.location,
                    link=job.link,
                    posted_on=job.posted_on,
                    posted_precision="day" if job.posted_on else "unknown",
                    fetched_on=fetched_iso,
                    actively_hiring=False,
                    matched_phrase=phrase,
                    possible_duplicate_of=duplicate_of,
                    dup_key=key,
                    raw_datetime=None,
                    raw_relative=None,
                    run_id=run_id,
                )
            )
            if job.description:
                self._store.save_detail(
                    PORTAL_SITES, role_id, JobDetail(job.description), when=fetched_iso
                )
            record.new_roles += 1
            if duplicate_of:
                record.possible_dups += 1

    def _pace(self) -> None:
        if self._first_request:
            self._first_request = False
            return
        if self._config.sites_delay_seconds > 0:
            self._sleep(self._config.sites_delay_seconds)

    def _final(self, records: list[KeywordRecord]) -> tuple[str, str]:
        done = [r for r in records if r.completed]
        notes = [f"{name}: {why}" for name, why in self.failures]
        if not done:
            return RUN_FAILED, "; ".join(notes or ["No company completed"])
        if len(done) < len(records):
            return RUN_PARTIAL, "; ".join(notes)
        return RUN_SUCCESS, ""


__all__ = ["STOP_ABORTED", "SiteCheck", "SitesRunner", "try_company"]
