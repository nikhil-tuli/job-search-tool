"""One Naukri run: search pages, keep fresh matching roles, then read job pages.

The run decides what to do with the data. Getting the data (a real browser window, with the
captcha left to the person) is the job of a NaukriSource, so this logic can be tested without
a browser.
"""

from __future__ import annotations

import enum
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from .config import Config
from .fit_filters import FitRules, evaluate
from .matching import TitleMatcher, dup_key
from .models import PORTAL_NAUKRI, KeywordRecord, NewRole, PageStat
from .naukri import (
    PRECISION,
    NaukriCard,
    NaukriShapeError,
    parse_job,
    parse_search,
)
from .scanner import (
    _COMPLETED_STOPS,
    STOP_ABORTED,
    STOP_EMPTY_PAGES,
    STOP_END,
    STOP_INTERRUPTED,
    STOP_LAYOUT,
    STOP_PAGE_CAP,
    RunResult,
    ScanSetupError,
)
from .store import RUN_FAILED, RUN_PARTIAL, RUN_SUCCESS, Store
from .timeutil import from_iso, now_ist, to_iso

FULL_SNIPPET_CHARS = 1_500  # a list description this long is taken as the whole text
PAGE_SIZE = 20


REMINDER_AFTER_DAYS = 2


def reminder_text(store: Store, now: datetime | None = None) -> str | None:
    """A line telling you the last Naukri run is old, or None.

    Nothing is shown until a Naukri run has happened, and nothing while the last one is recent.
    """
    latest = store.latest_run(PORTAL_NAUKRI)
    if latest is None:
        return None
    age = (now or now_ist()) - from_iso(latest.started_on)
    if age <= timedelta(days=REMINDER_AFTER_DAYS):
        return None
    days = age.days
    return f"Naukri last run: {days} day{'s' if days != 1 else ''} ago. Run jobscan naukri."


class Reply(enum.Enum):
    OK = "ok"
    CAPTCHA = "captcha"  # Naukri asked for a captcha
    GONE = "gone"  # the page or job does not exist any more
    ERROR = "error"  # anything else: no data came back


@dataclass(frozen=True)
class SourceReply:
    kind: Reply
    payload: object = None
    note: str = ""


class NaukriSource(Protocol):
    """Where Naukri's data comes from. The real one is a visible browser window."""

    def search(self, keyword: str, page_index: int) -> SourceReply: ...

    def job(self, job_id: str, link: str) -> SourceReply: ...

    def wait_for_captcha(self, seconds: int) -> None:
        """Hold while the person solves a captcha in the window, for up to `seconds`."""
        ...


class _StopRun(Exception):
    """Too many captchas in one run."""


@dataclass
class _Totals:
    captchas: int = 0
    jobs_read: int = 0
    jobs_closed: int = 0
    jobs_failed: int = 0
    warnings: list[str] = field(default_factory=list)


class NaukriRunner:
    def __init__(
        self,
        *,
        store: Store,
        source: NaukriSource,
        config: Config,
        emit: Callable[[str], None] = print,
        sleep: Callable[[float], None],
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._store = store
        self._source = source
        self._config = config
        self._emit = emit
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock
        self._totals = _Totals()
        self._first_request = True
        self._phrase_counts: Counter[str] = Counter()

    # ---- public -------------------------------------------------------------------------

    def run(self) -> RunResult:
        phrases = self._store.list_phrases()
        keywords = [p.phrase for p in phrases if p.use_as_keyword]
        match_phrases = [p.phrase for p in phrases if p.use_for_match]
        if not keywords:
            raise ScanSetupError("no title phrase is switched on as a search keyword")
        if not match_phrases:
            raise ScanSetupError("no title phrase is switched on for title matching")
        matcher = TitleMatcher(match_phrases, self._config.match_mode)
        window = timedelta(hours=self._config.naukri_max_age_hours)
        run_id = self._store.start_run(
            window_seconds=int(window.total_seconds()),
            keywords_total=len(keywords),
            portal=PORTAL_NAUKRI,
        )
        self._store.purge_skipped(self._config.skipped_titles_retention_days, now=self._clock())
        records: list[KeywordRecord] = []
        try:
            self._search_all(run_id, keywords, matcher, window, records)
            if not any(r.stopped_by in (STOP_ABORTED, STOP_LAYOUT) for r in records):
                self._read_jobs()
            else:
                self._emit("Job pages: skipped because the search was cut short")
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

    def read_pending(self) -> tuple[int, int, int]:
        """Only the job-page step: read Naukri roles still without a description.

        Returns (read, closed, without a description). The roles come from the same list and
        the same Fit gate as at the end of a Naukri run.
        """
        self._read_jobs()
        t = self._totals
        return t.jobs_read, t.jobs_closed, t.jobs_failed

    # ---- searching ----------------------------------------------------------------------

    def _search_all(
        self,
        run_id: int,
        keywords: list[str],
        matcher: TitleMatcher,
        window: timedelta,
        records: list[KeywordRecord],
    ) -> None:
        for number, keyword in enumerate(keywords, start=1):
            record = KeywordRecord(keyword)
            records.append(record)
            pages: list[PageStat] = []
            try:
                self._search_keyword(
                    run_id,
                    f"[{number}/{len(keywords)}] {keyword!r}",
                    record,
                    matcher,
                    window,
                    pages,
                )
            except _StopRun:
                record.stopped_by = STOP_ABORTED
                self._emit(
                    f"ABORT: {self._totals.captchas} captchas in this run; stopping. "
                    "Try again later."
                )
            except NaukriShapeError as exc:
                record.stopped_by = STOP_LAYOUT
                self._totals.warnings.append(str(exc))
                self._emit(f"LAYOUT CHANGED: {exc}")
            except BaseException:
                record.stopped_by = STOP_INTERRUPTED
                raise
            finally:
                record.completed = record.stopped_by in _COMPLETED_STOPS
                with self._store.transaction():
                    self._store.record_keyword(run_id, record)
                    self._store.record_pages(run_id, record.keyword, pages)
            if record.stopped_by in (STOP_ABORTED, STOP_LAYOUT):
                break
            self._emit(
                f"  {record.keyword!r}: {record.pages} pages, {record.cards} cards, "
                f"{record.matched} matched, {record.new_roles} new, "
                f"{record.possible_dups} possible duplicates, stopped by {record.stopped_by}"
            )

    def _search_keyword(
        self,
        run_id: int,
        label: str,
        record: KeywordRecord,
        matcher: TitleMatcher,
        window: timedelta,
        pages: list[PageStat],
    ) -> None:
        cfg = self._config
        empty_streak = 0
        previous_ids: set[str] = set()
        for page_index in range(cfg.naukri_max_pages):
            self._pace()
            reply = self._ask(self._search_call(record.keyword, page_index), label)
            if reply.kind is not Reply.OK:
                record.failed_pages += 1
                self._emit(
                    f"{label} page {page_index + 1}: no data ({reply.note or reply.kind.value})"
                )
                if reply.kind is Reply.GONE:
                    record.stopped_by = STOP_END
                    return
                continue
            cards = parse_search(reply.payload)
            if not cards:
                record.stopped_by = STOP_END
                self._emit(f"{label} page {page_index + 1}: end of results")
                return
            ids = {c.job_id for c in cards}
            if page_index > 0 and ids <= previous_ids:
                # The same roles as the page before: paging is not working, so stop here
                # rather than count them again.
                record.failed_pages += 1
                record.stopped_by = STOP_END
                self._emit(
                    f"{label} page {page_index + 1}: same roles as the page before, stopping"
                )
                return
            previous_ids = ids
            stat = self._process_page(run_id, record, matcher, window, cards, page_index)
            pages.append(stat)
            self._emit(f"{label} page {page_index + 1}: {stat.cards} cards, {stat.matched} matched")
            empty_streak = empty_streak + 1 if stat.matched == 0 else 0
            if empty_streak >= cfg.stop_after_empty_pages:
                record.stopped_by = STOP_EMPTY_PAGES
                return
            if len(cards) < PAGE_SIZE:
                record.stopped_by = STOP_END
                return
        record.stopped_by = STOP_PAGE_CAP

    def _process_page(
        self,
        run_id: int,
        record: KeywordRecord,
        matcher: TitleMatcher,
        window: timedelta,
        cards: list[NaukriCard],
        page_index: int,
    ) -> PageStat:
        fetched = self._clock()
        fetched_iso = to_iso(fetched)
        record.pages += 1
        matched = 0
        skipped: list[tuple[str, str, str, str, str]] = []
        with self._store.transaction():
            for card in cards:
                record.cards += 1
                phrase = matcher.match(card.title)
                if phrase is None:
                    skipped.append(
                        (PORTAL_NAUKRI, card.job_id, card.title, card.company, fetched_iso)
                    )
                    continue
                if card.posted_on is None or fetched - from_iso(card.posted_on) > window:
                    continue  # a title match, but not posted inside the run window
                matched += 1
                record.matched += 1
                self._phrase_counts[phrase] += 1
                if self._store.role_exists(PORTAL_NAUKRI, card.job_id):
                    continue
                key = dup_key(card.company, card.title, card.location)
                duplicate_of = self._store.find_duplicate(key)
                self._store.insert_role(
                    NewRole(
                        portal=PORTAL_NAUKRI,
                        job_id=card.job_id,
                        title=card.title,
                        company=card.company,
                        location=card.location,
                        link=card.link,
                        posted_on=card.posted_on,
                        posted_precision=PRECISION,
                        fetched_on=fetched_iso,
                        actively_hiring=False,
                        matched_phrase=phrase,
                        possible_duplicate_of=duplicate_of,
                        dup_key=key,
                        raw_datetime=card.raw_posted or None,
                        raw_relative=card.raw_relative or None,
                        run_id=run_id,
                    )
                )
                record.new_roles += 1
                if duplicate_of:
                    record.possible_dups += 1
            self._store.add_skipped(run_id, skipped)
        return PageStat(page_index, len(cards), matched)

    # ---- job pages ----------------------------------------------------------------------

    def _read_jobs(self) -> None:
        """Read the page of each recent role that has no description, if it passes Fit.

        A problem here never fails the run: the roles are already saved, and a role that could
        not be read is tried again on a later run.
        """
        cfg = self._config
        if cfg.naukri_max_job_pages <= 0:
            return
        now = self._clock()
        since = to_iso(now - timedelta(days=cfg.detail_max_age_days))
        waiting = self._store.pending_details(since, 1_000_000, PORTAL_NAUKRI)
        rules = FitRules.from_config(cfg)
        todo: list[tuple[str, str]] = []
        for portal, job_id in waiting:
            role = self._store.get_role(portal, job_id)
            if role is not None and evaluate(role, rules, now).eligible:
                todo.append((role.job_id, role.link))
        todo = todo[: cfg.naukri_max_job_pages]
        if not todo:
            return
        self._emit(f"Job pages: reading {len(todo)} role(s)")
        try:
            for job_id, link in todo:
                self._pace()
                self._read_job(job_id, link)
        except _StopRun:
            self._emit(f"ABORT: {self._totals.captchas} captchas in this run; stopping.")
            self._totals.warnings.append("Job pages stopped after repeated captchas")
        t = self._totals
        self._emit(
            f"Job pages: {t.jobs_read} read, {t.jobs_closed} closed, {t.jobs_failed} without a "
            "description"
        )

    def _search_call(self, keyword: str, page_index: int) -> Callable[[], SourceReply]:
        return lambda: self._source.search(keyword, page_index)

    def _read_job(self, job_id: str, link: str) -> None:
        t = self._totals
        reply = self._ask(lambda: self._source.job(job_id, link), f"job {job_id}")
        if reply.kind is Reply.GONE:
            if self._store.close_if_untouched(PORTAL_NAUKRI, job_id):
                t.jobs_closed += 1
                self._emit(f"  job {job_id}: closed, status set to Closed")
            return
        try:
            job = parse_job(reply.payload) if reply.kind is Reply.OK else None
        except NaukriShapeError as exc:
            self._emit(f"  job {job_id}: unexpected data ({exc})")
            job = None
        if job is not None and job.closed:
            if self._store.close_if_untouched(PORTAL_NAUKRI, job_id):
                t.jobs_closed += 1
                self._emit(f"  job {job_id}: closed, status set to Closed")
            return
        if job is None or job.detail is None:
            self._store.record_detail_failure(PORTAL_NAUKRI, job_id)
            t.jobs_failed += 1
            self._emit(f"  job {job_id}: no description ({reply.note or reply.kind.value})")
            return
        self._store.save_detail(PORTAL_NAUKRI, job_id, job.detail, when=to_iso(self._clock()))
        t.jobs_read += 1

    # ---- shared -------------------------------------------------------------------------

    def _ask(self, call: Callable[[], SourceReply], label: str) -> SourceReply:
        """Make a request. On a captcha, wait for the person, then try once more."""
        cfg = self._config
        reply = call()
        while reply.kind is Reply.CAPTCHA:
            self._totals.captchas += 1
            if self._totals.captchas >= cfg.naukri_abort_after_captchas:
                raise _StopRun
            self._emit(
                f"{label}: Naukri asked for a captcha. Solve it in the browser window; "
                f"waiting up to {cfg.naukri_captcha_wait_seconds}s."
            )
            self._source.wait_for_captcha(cfg.naukri_captcha_wait_seconds)
            reply = call()
        return reply

    def _pace(self) -> None:
        if self._first_request:
            self._first_request = False
            return
        cfg = self._config
        self._sleep(self._rng.uniform(cfg.naukri_delay_seconds_min, cfg.naukri_delay_seconds_max))

    def _final(self, records: list[KeywordRecord]) -> tuple[str, str]:
        notes = list(self._totals.warnings)
        if any(r.stopped_by == STOP_LAYOUT for r in records):
            return RUN_FAILED, "; ".join(["Naukri data layout changed", *notes])
        done = [r for r in records if r.completed]
        aborted = any(r.stopped_by == STOP_ABORTED for r in records)
        if aborted:
            notes.insert(0, "Stopped after repeated captchas")
        if not done:
            return RUN_FAILED, "; ".join(notes or ["No keyword completed"])
        failed_pages = sum(r.failed_pages for r in records)
        if failed_pages:
            notes.insert(0, f"{failed_pages} page(s) gave no data")
        if aborted or failed_pages or len(done) < len(records):
            return RUN_PARTIAL, "; ".join(notes)
        return RUN_SUCCESS, "; ".join(notes)
