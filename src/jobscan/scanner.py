"""Orchestrates one scan: keywords, paging, pacing, retries, stop rules and storage."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .client import PAGE_SIZE, DetailFetcher, DetailOutcome, FetchResult, Outcome, PageFetcher
from .config import Config
from .matching import TitleMatcher, dup_key
from .models import PORTAL_LINKEDIN, KeywordRecord, NewRole, PageStat
from .store import RUN_FAILED, RUN_PARTIAL, RUN_SUCCESS, Store
from .timeutil import now_ist, posted_from_relative, to_iso

STOP_END = "end"
STOP_EMPTY_PAGES = "empty_pages"
STOP_PAGE_CAP = "page_cap"
STOP_ABORTED = "aborted"
STOP_LAYOUT = "layout_changed"
STOP_INTERRUPTED = "interrupted"

_COMPLETED_STOPS = frozenset({STOP_END, STOP_EMPTY_PAGES, STOP_PAGE_CAP})
_DETAIL_THROTTLES_BEFORE_STOP = 2


class ScanSetupError(ValueError):
    """The scan cannot start with the current configuration."""


class _AbortRun(Exception):
    """Too many consecutive throttled responses."""


class _LayoutChanged(Exception):
    """LinkedIn's page layout no longer matches what the parser expects."""


@dataclass
class RunResult:
    run_id: int
    status: str
    note: str
    keywords: list[KeywordRecord] = field(default_factory=list)


class Scanner:
    def __init__(
        self,
        *,
        store: Store,
        fetcher: PageFetcher,
        details: DetailFetcher | None = None,
        config: Config,
        emit: Callable[[str], None] = print,
        sleep: Callable[[float], None],
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._details = details
        self._config = config
        self._emit = emit
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock
        self._consecutive_throttles = 0
        self._first_request = True
        self._warnings: list[str] = []
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

        run_id = self._store.start_run(
            window_seconds=self._config.time_window_seconds, keywords_total=len(keywords)
        )
        purged = self._store.purge_skipped(
            self._config.skipped_titles_retention_days, now=self._clock()
        )
        if purged:
            self._emit(f"Removed {purged} skipped-title rows past retention")

        records: list[KeywordRecord] = []
        try:
            self._scan_keywords(run_id, keywords, matcher, records)
            self._fetch_details(records)
        except KeyboardInterrupt:
            self._store.finish_run(run_id, RUN_FAILED, "Interrupted by user")
            raise
        except Exception as exc:
            self._store.finish_run(
                run_id, RUN_FAILED, f"Unexpected error: {type(exc).__name__}: {exc}"
            )
            raise

        status, note = self._final_status(records)
        self._store.add_phrase_matches(run_id, dict(self._phrase_counts))
        self._store.finish_run(run_id, status, note)
        return RunResult(run_id, status, note, records)

    # ---- keyword loop -------------------------------------------------------------------

    def _scan_keywords(
        self, run_id: int, keywords: list[str], matcher: TitleMatcher, records: list[KeywordRecord]
    ) -> None:
        for number, keyword in enumerate(keywords, start=1):
            record = KeywordRecord(keyword)
            records.append(record)
            pages: list[PageStat] = []
            try:
                self._scan_keyword(run_id, number, len(keywords), record, matcher, pages)
            except _AbortRun:
                record.stopped_by = STOP_ABORTED
                self._emit(
                    f"ABORT: {self._config.abort_after_consecutive_throttles} throttled "
                    "responses in a row; stopping the run"
                )
            except _LayoutChanged as exc:
                record.stopped_by = STOP_LAYOUT
                self._warnings.append(str(exc))
                self._emit(f"LAYOUT CHANGED: {exc}")
            except BaseException:
                record.stopped_by = STOP_INTERRUPTED
                raise
            finally:
                self._persist_keyword(run_id, record, pages)
            if record.stopped_by in (STOP_ABORTED, STOP_LAYOUT):
                break
            self._emit(self._keyword_summary(record))

    def _persist_keyword(self, run_id: int, record: KeywordRecord, pages: list[PageStat]) -> None:
        record.completed = record.stopped_by in _COMPLETED_STOPS
        with self._store.transaction():
            self._store.record_keyword(run_id, record)
            self._store.record_pages(run_id, record.keyword, pages)

    def _scan_keyword(
        self,
        run_id: int,
        number: int,
        total: int,
        record: KeywordRecord,
        matcher: TitleMatcher,
        pages: list[PageStat],
    ) -> None:
        cfg = self._config
        empty_streak = 0
        label = f"[{number}/{total}] {record.keyword!r}"
        for page_index in range(cfg.max_pages_per_keyword):
            self._pace(first_page=page_index == 0)
            result = self._fetch_with_retries(record, page_index, label)
            if result is None:
                record.failed_pages += 1
                self._emit(f"{label} page {page_index + 1}: skipped after retries")
                continue
            if result.outcome is Outcome.LAYOUT_CHANGED:
                raise _LayoutChanged(result.detail)
            if result.outcome is Outcome.END or result.page is None:
                if result.warning:
                    self._warnings.append(f"{record.keyword!r}: {result.warning}")
                    self._emit(f"{label} WARNING: {result.warning}")
                record.stopped_by = STOP_END
                self._emit(f"{label} page {page_index + 1}: end of results ({result.detail})")
                return

            stat = self._process_page(run_id, record, matcher, result, page_index)
            pages.append(stat)
            self._emit(
                f"{label} page {page_index + 1} (start={page_index * PAGE_SIZE}): "
                f"{stat.cards} cards, {stat.matched} matched"
            )
            empty_streak = empty_streak + 1 if stat.matched == 0 else 0
            if empty_streak >= cfg.stop_after_empty_pages:
                record.stopped_by = STOP_EMPTY_PAGES
                return
        record.stopped_by = STOP_PAGE_CAP

    # ---- one page -----------------------------------------------------------------------

    def _process_page(
        self,
        run_id: int,
        record: KeywordRecord,
        matcher: TitleMatcher,
        result: FetchResult,
        page_index: int,
    ) -> PageStat:
        assert result.page is not None
        fetched = self._clock()
        fetched_iso = to_iso(fetched)
        record.pages += 1
        record.malformed += result.page.malformed
        matched = 0
        skipped: list[tuple[str, str, str, str, str]] = []
        with self._store.transaction():
            for card in result.page.cards:
                record.cards += 1
                phrase = matcher.match(card.title)
                if phrase is None:
                    skipped.append(
                        (PORTAL_LINKEDIN, card.job_id, card.title, card.company, fetched_iso)
                    )
                    continue
                matched += 1
                record.matched += 1
                self._phrase_counts[phrase] += 1
                if self._store.role_exists(PORTAL_LINKEDIN, card.job_id):
                    continue
                key = dup_key(card.company, card.title, card.location)
                duplicate_of = self._store.find_duplicate(key)
                posted, precision = posted_from_relative(fetched, card.raw_relative)
                self._store.insert_role(
                    NewRole(
                        portal=PORTAL_LINKEDIN,
                        job_id=card.job_id,
                        title=card.title,
                        company=card.company,
                        location=card.location,
                        link=card.link,
                        posted_on=to_iso(posted) if posted else None,
                        posted_precision=precision,
                        fetched_on=fetched_iso,
                        actively_hiring=card.actively_hiring,
                        matched_phrase=phrase,
                        possible_duplicate_of=duplicate_of,
                        dup_key=key,
                        raw_datetime=card.raw_datetime,
                        raw_relative=card.raw_relative,
                        run_id=run_id,
                    )
                )
                record.new_roles += 1
                if duplicate_of:
                    record.possible_dups += 1
            self._store.add_skipped(run_id, skipped)
        return PageStat(page_index, len(result.page.cards), matched)

    # ---- fetching, pacing, retries --------------------------------------------------------

    def _pace(self, *, first_page: bool) -> None:
        cfg = self._config
        if self._first_request:
            self._first_request = False
            return
        if first_page:
            self._wait(cfg.keyword_pause_min, cfg.keyword_pause_max, "next keyword")
        else:
            self._wait(cfg.delay_seconds_min, cfg.delay_seconds_max, None)

    def _wait(self, low: float, high: float, why: str | None) -> None:
        seconds = self._rng.uniform(low, high)
        if why:
            self._emit(f"Pausing {seconds:.0f}s before {why}")
        self._sleep(seconds)

    def _fetch_with_retries(
        self, record: KeywordRecord, page_index: int, label: str
    ) -> FetchResult | None:
        cfg = self._config
        attempt = 0
        while True:
            result = self._fetcher.fetch_page(record.keyword, page_index)
            if result.outcome is not Outcome.THROTTLED:
                self._consecutive_throttles = 0
                return result
            record.throttled += 1
            self._consecutive_throttles += 1
            self._emit(f"{label} page {page_index + 1}: throttled ({result.detail})")
            if self._consecutive_throttles >= cfg.abort_after_consecutive_throttles:
                raise _AbortRun
            if attempt >= cfg.retry_count:
                return None
            attempt += 1
            record.retries += 1
            self._wait(cfg.retry_wait_min, cfg.retry_wait_max, f"retry {attempt}/{cfg.retry_count}")

    # ---- job descriptions ----------------------------------------------------------------

    def _fetch_details(self, records: list[KeywordRecord]) -> None:
        """Fetch the description of recent roles that do not have one yet.

        A problem here never fails the scan: the roles are already saved, and a role that
        could not be fetched is simply tried again on a later scan.
        """
        cfg = self._config
        if self._details is None or cfg.detail_max_per_run <= 0:
            return
        if any(r.stopped_by in (STOP_ABORTED, STOP_LAYOUT) for r in records):
            self._emit("Descriptions: skipped because the search was cut short")
            return
        since = to_iso(self._clock() - timedelta(days=cfg.detail_max_age_days))
        pending = self._store.pending_details(since, cfg.detail_max_per_run)
        if not pending:
            return
        self._emit(f"Descriptions: fetching {len(pending)} role(s)")
        saved = failed = throttled_in_row = 0
        failed_ids: set[str] = set()
        stopped = False
        for portal, job_id in pending:
            self._wait(cfg.delay_seconds_min, cfg.delay_seconds_max, None)
            result = self._details.fetch_detail(job_id)
            if result.outcome is DetailOutcome.THROTTLED:
                throttled_in_row += 1
                self._emit(f"  description {job_id}: throttled ({result.note})")
                if throttled_in_row >= _DETAIL_THROTTLES_BEFORE_STOP:
                    stopped = True
                    break
                self._wait(cfg.retry_wait_min, cfg.retry_wait_max, "next description")
                continue
            throttled_in_row = 0
            if result.outcome is DetailOutcome.OK and result.detail is not None:
                self._store.save_detail(portal, job_id, result.detail, when=to_iso(self._clock()))
                saved += 1
            else:
                self._store.record_detail_failure(portal, job_id)
                failed += 1
                failed_ids.add(job_id)
                self._emit(f"  description {job_id}: none ({result.note})")
        waiting = self._store.pending_details(since, 1_000_000)
        left = sum(1 for _, job_id in waiting if job_id not in failed_ids)
        self._emit(f"Descriptions: {saved} saved, {failed} without one, {left} left for later")
        if stopped:
            self._warnings.append(
                f"Descriptions stopped after repeated throttling ({left} left for the next scan)"
            )

    # ---- results ------------------------------------------------------------------------

    @staticmethod
    def _keyword_summary(record: KeywordRecord) -> str:
        return (
            f"  {record.keyword!r}: {record.pages} pages, {record.cards} cards, "
            f"{record.matched} matched, {record.new_roles} new, {record.possible_dups} possible "
            f"duplicates, {record.throttled} throttled, stopped by {record.stopped_by}"
        )

    def _final_status(self, records: list[KeywordRecord]) -> tuple[str, str]:
        notes = list(self._warnings)
        if any(r.stopped_by == STOP_LAYOUT for r in records):
            return RUN_FAILED, "; ".join(["LinkedIn page layout changed", *notes])
        done = [r for r in records if r.completed]
        aborted = any(r.stopped_by == STOP_ABORTED for r in records)
        if aborted:
            notes.insert(0, "Aborted after repeated throttling")
        if not done:
            return RUN_FAILED, "; ".join(notes or ["No keyword completed"])
        failed_pages = sum(r.failed_pages for r in records)
        if failed_pages:
            notes.insert(0, f"{failed_pages} page(s) skipped after retries")
        if aborted or failed_pages or len(done) < len(records):
            return RUN_PARTIAL, "; ".join(notes)
        return RUN_SUCCESS, "; ".join(notes)
