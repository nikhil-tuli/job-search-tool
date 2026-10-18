from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from jobscan import db
from jobscan.config import Config
from jobscan.models import RoleFilter
from jobscan.scanner import Scanner, ScanSetupError
from jobscan.store import RUN_FAILED, RUN_PARTIAL, RUN_SUCCESS, Store
from jobscan.timeutil import IST, to_iso
from tests.fakes import DM, TPM, ScriptedFetcher, card, end, layout, ok, throttled

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=IST)


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


@pytest.fixture
def cfg() -> Config:
    return Config()


class Harness:
    def __init__(self, store: Store, cfg: Config, script: dict) -> None:  # type: ignore[type-arg]
        self.store = store
        self.fetcher = ScriptedFetcher(script)
        self.sleeps: list[float] = []
        self.lines: list[str] = []
        self.scanner = Scanner(
            store=store,
            fetcher=self.fetcher,
            config=cfg,
            emit=self.lines.append,
            sleep=self.sleeps.append,
            rng=random.Random(7),
            clock=lambda: NOW,
        )

    def run(self):  # type: ignore[no-untyped-def]
        return self.scanner.run()


def one_keyword(store: Store) -> None:
    store.replace_phrases([(TPM, True, True), (DM, False, True), ("TPM", False, True)])


def two_keywords(store: Store) -> None:
    store.replace_phrases([(TPM, True, True), (DM, True, True)])


# ---- happy path and stop rules ------------------------------------------------------------


def test_happy_path_inserts_matches_and_skips_the_rest(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(
        store,
        cfg,
        {
            (TPM, 0): [ok(card("1"), card("2", "Chef"), card("3", DM, company="Globex"))],
            (TPM, 1): [end()],
        },
    )
    result = h.run()
    assert result.status == RUN_SUCCESS
    assert result.note == ""
    assert {r.job_id for r in store.list_roles()} == {"1", "3"}
    rec = result.keywords[0]
    assert (rec.pages, rec.cards, rec.matched, rec.new_roles) == (1, 3, 2, 2)
    assert rec.stopped_by == "end"
    assert rec.completed
    skipped = store.list_skipped(result.run_id)
    assert [(s.job_id, s.title) for s in skipped] == [("2", "Chef")]
    last = store.last_successful_run()
    assert last is not None
    assert last.id == result.run_id
    assert (last.cards_seen, last.matched, last.new_roles) == (3, 2, 2)


def test_stored_role_fields_and_ist_times(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(store, cfg, {(TPM, 0): [ok(card("1", relative="22 hours ago"))], (TPM, 1): [end()]})
    h.run()
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.fetched_on == to_iso(NOW) == "2026-10-15T09:00:00+05:30"
    assert role.posted_on == "2026-10-14T11:00:00+05:30"
    assert role.posted_precision == "hour"
    assert role.matched_phrase == TPM
    assert role.raw_relative == "22 hours ago"
    assert role.link == "https://www.linkedin.com/jobs/view/1"
    assert role.status == "Not Applied"
    assert role.first_run_id == 1


def test_unknown_relative_age_stores_no_posted_time(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(store, cfg, {(TPM, 0): [ok(card("1", relative=None))], (TPM, 1): [end()]})
    h.run()
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.posted_on, role.posted_precision) == (None, "unknown")


def test_page_cap_stops_the_keyword(store: Store, cfg: Config) -> None:
    one_keyword(store)
    cfg = replace(cfg, max_pages_per_keyword=3)
    script = {(TPM, i): [ok(card(str(i)))] for i in range(3)}
    h = Harness(store, cfg, script)
    result = h.run()
    assert h.fetcher.calls == [(TPM, 0), (TPM, 1), (TPM, 2)]
    assert result.keywords[0].stopped_by == "page_cap"
    assert result.status == RUN_SUCCESS


def test_two_empty_pages_in_a_row_stop_the_keyword(store: Store, cfg: Config) -> None:
    one_keyword(store)
    chef = card("9", "Chef")
    script = {
        (TPM, 0): [ok(card("1"))],
        (TPM, 1): [ok(chef)],
        (TPM, 2): [ok(card("10", "Cook"))],
    }
    h = Harness(store, cfg, script)
    result = h.run()
    assert result.keywords[0].stopped_by == "empty_pages"
    assert h.fetcher.calls == [(TPM, 0), (TPM, 1), (TPM, 2)]


def test_a_matching_page_resets_the_empty_streak(store: Store, cfg: Config) -> None:
    one_keyword(store)
    cfg = replace(cfg, max_pages_per_keyword=4)
    script = {
        (TPM, 0): [ok(card("1", "Chef"))],
        (TPM, 1): [ok(card("2"))],
        (TPM, 2): [ok(card("3", "Chef"))],
        (TPM, 3): [end()],
    }
    h = Harness(store, cfg, script)
    result = h.run()
    assert result.keywords[0].stopped_by == "end"
    assert len(h.fetcher.calls) == 4


def test_end_on_first_page_is_a_successful_empty_keyword(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(store, cfg, {(TPM, 0): [end()]})
    result = h.run()
    assert result.status == RUN_SUCCESS
    assert result.keywords[0].pages == 0


def test_page_without_cards_and_no_end_signal_warns(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(store, cfg, {(TPM, 0): [end(warning="page with no cards")]})
    result = h.run()
    assert result.status == RUN_SUCCESS
    assert "page with no cards" in result.note
    assert any("WARNING" in line for line in h.lines)


def test_only_keyword_phrases_are_searched(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(store, cfg, {(TPM, 0): [end()]})
    h.run()
    assert {k for k, _ in h.fetcher.calls} == {TPM}


def test_match_only_phrase_still_keeps_roles(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(store, cfg, {(TPM, 0): [ok(card("1", "Senior TPM, Infra"))], (TPM, 1): [end()]})
    h.run()
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.matched_phrase == "TPM"


# ---- dedupe ---------------------------------------------------------------------------------


def test_same_role_under_two_keywords_is_stored_once(store: Store, cfg: Config) -> None:
    two_keywords(store)
    same = card("1", "Senior Technical Program Manager")
    script = {(TPM, 0): [ok(same)], (TPM, 1): [end()], (DM, 0): [ok(same)], (DM, 1): [end()]}
    h = Harness(store, cfg, script)
    result = h.run()
    assert store.count_roles() == 1
    first, second = result.keywords
    assert (first.matched, first.new_roles) == (1, 1)
    assert (second.matched, second.new_roles) == (1, 0)


def test_rerun_adds_nothing_and_keeps_user_edits(store: Store, cfg: Config) -> None:
    one_keyword(store)
    script = {(TPM, 0): [ok(card("1"))], (TPM, 1): [end()]}
    Harness(store, cfg, script).run()
    store.update_status(["LinkedIn|1"], "Applied", today=NOW)
    again = Harness(store, cfg, {(TPM, 0): [ok(card("1"))], (TPM, 1): [end()]}).run()
    assert again.keywords[0].new_roles == 0
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.status == "Applied"
    assert role.first_run_id == 1


def test_repost_with_new_id_is_flagged_as_possible_duplicate(store: Store, cfg: Config) -> None:
    one_keyword(store)
    script = {
        (TPM, 0): [
            ok(
                card(
                    "1",
                    "Sr. Technical Program Manager",
                    company="Acme Pvt Ltd",
                    location="Bangalore, Karnataka, India",
                ),
                card(
                    "2",
                    "Senior Technical Program Manager",
                    company="ACME",
                    location="Bengaluru, India",
                ),
                card(
                    "3", "Senior Technical Program Manager", company="Acme", location="Pune, India"
                ),
            )
        ],
        (TPM, 1): [end()],
    }
    h = Harness(store, cfg, script)
    result = h.run()
    first = store.get_role("LinkedIn", "1")
    second = store.get_role("LinkedIn", "2")
    third = store.get_role("LinkedIn", "3")
    assert first is not None
    assert second is not None
    assert third is not None
    assert first.possible_duplicate_of is None
    assert second.possible_duplicate_of == "LinkedIn|1"
    assert third.possible_duplicate_of is None
    assert result.keywords[0].possible_dups == 1
    assert [r.job_id for r in store.list_roles(RoleFilter(duplicates_only=True))] == ["2"]


# ---- skipped titles ---------------------------------------------------------------------------


def test_old_skipped_titles_are_purged_at_run_start(store: Store, cfg: Config) -> None:
    one_keyword(store)
    old_run = store.start_run(window_seconds=1, keywords_total=1)
    old = to_iso(NOW - timedelta(days=cfg.skipped_titles_retention_days + 1))
    store.add_skipped(old_run, [("LinkedIn", "old", "Chef", "X", old)])
    h = Harness(store, cfg, {(TPM, 0): [end()]})
    h.run()
    assert store.list_skipped() == []
    assert any("Removed 1 skipped" in line for line in h.lines)


def test_same_skipped_title_under_two_keywords_is_stored_once(store: Store, cfg: Config) -> None:
    two_keywords(store)
    chef = card("5", "Chef")
    script = {(TPM, 0): [ok(chef)], (TPM, 1): [end()], (DM, 0): [ok(chef)], (DM, 1): [end()]}
    result = Harness(store, cfg, script).run()
    assert len(store.list_skipped(result.run_id)) == 1


# ---- statistics ---------------------------------------------------------------------------------


def test_depth_and_phrase_statistics(store: Store, cfg: Config) -> None:
    one_keyword(store)
    script = {
        (TPM, 0): [ok(card("1"), card("2", "Chef"), card("3", "Senior TPM"))],
        (TPM, 1): [ok(card("4", "Cook"), card("5", TPM, company="B"))],
        (TPM, 2): [end()],
    }
    result = Harness(store, cfg, script).run()
    depth = {d.page_index: (d.cards, d.matched) for d in store.depth_stats(result.run_id)}
    assert depth == {0: (3, 2), 1: (2, 1)}
    assert dict(store.phrase_matches(result.run_id)) == {TPM: 2, "TPM": 1}


def test_malformed_cards_are_counted(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(store, cfg, {(TPM, 0): [ok(card("1"), malformed=2)], (TPM, 1): [end()]})
    result = h.run()
    assert result.keywords[0].malformed == 2
    summary = store.get_run(result.run_id)
    assert summary is not None
    assert summary.malformed_cards == 2


# ---- pacing -----------------------------------------------------------------------------------


def test_pacing_between_pages_and_keywords(store: Store, cfg: Config) -> None:
    two_keywords(store)
    script = {(TPM, 0): [ok(card("1"))], (TPM, 1): [end()], (DM, 0): [end()]}
    h = Harness(store, cfg, script)
    h.run()
    assert len(h.sleeps) == 2  # none before the very first request
    assert cfg.delay_seconds_min <= h.sleeps[0] <= cfg.delay_seconds_max
    assert cfg.keyword_pause_min <= h.sleeps[1] <= cfg.keyword_pause_max


def test_no_wait_before_the_first_request(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(store, cfg, {(TPM, 0): [end()]})
    h.run()
    assert h.sleeps == []


# ---- throttling, retries, abort ---------------------------------------------------------------


def test_throttled_page_is_retried_with_long_waits(store: Store, cfg: Config) -> None:
    one_keyword(store)
    script = {(TPM, 0): [throttled(), throttled(), ok(card("1"))], (TPM, 1): [end()]}
    h = Harness(store, cfg, script)
    result = h.run()
    rec = result.keywords[0]
    assert (rec.throttled, rec.retries, rec.failed_pages) == (2, 2, 0)
    assert result.status == RUN_SUCCESS
    assert len(h.sleeps) == 3
    assert all(cfg.retry_wait_min <= s <= cfg.retry_wait_max for s in h.sleeps[:2])
    assert cfg.delay_seconds_min <= h.sleeps[2] <= cfg.delay_seconds_max
    assert store.count_roles() == 1


def test_page_failing_all_retries_is_skipped_and_run_is_partial(store: Store, cfg: Config) -> None:
    one_keyword(store)
    script = {
        (TPM, 0): [throttled()] * (cfg.retry_count + 1),
        (TPM, 1): [ok(card("2"))],
        (TPM, 2): [end()],
    }
    h = Harness(store, cfg, script)
    result = h.run()
    rec = result.keywords[0]
    assert rec.failed_pages == 1
    assert rec.throttled == cfg.retry_count + 1
    assert rec.retries == cfg.retry_count
    assert rec.completed
    assert result.status == RUN_PARTIAL
    assert "skipped after retries" in result.note
    assert store.count_roles() == 1


def test_a_failed_page_does_not_count_as_an_empty_page(store: Store, cfg: Config) -> None:
    one_keyword(store)
    script = {
        (TPM, 0): [ok(card("1", "Chef"))],
        (TPM, 1): [throttled()] * (cfg.retry_count + 1),
        (TPM, 2): [ok(card("2"))],
        (TPM, 3): [end()],
    }
    h = Harness(store, cfg, script)
    result = h.run()
    assert result.keywords[0].stopped_by == "end"
    assert store.count_roles() == 1


def test_abort_after_consecutive_throttles_with_nothing_done_is_failed(
    store: Store, cfg: Config
) -> None:
    two_keywords(store)
    script = {
        (TPM, 0): [throttled()] * (cfg.retry_count + 1),
        (TPM, 1): [throttled()],
    }
    h = Harness(store, cfg, script)
    result = h.run()
    assert result.status == RUN_FAILED
    assert result.keywords[0].stopped_by == "aborted"
    assert len(result.keywords) == 1
    assert (TPM, 1) in h.fetcher.calls
    assert not any(k == DM for k, _ in h.fetcher.calls)
    assert "Aborted after repeated throttling" in result.note
    assert any(line.startswith("ABORT") for line in h.lines)


def test_abort_after_one_keyword_finished_is_partial(store: Store, cfg: Config) -> None:
    two_keywords(store)
    script = {
        (TPM, 0): [ok(card("1"))],
        (TPM, 1): [end()],
        # page 0 uses up its retries (4 throttles); the next response is the 5th in a row
        (DM, 0): [throttled()] * (cfg.retry_count + 1),
        (DM, 1): [throttled()],
    }
    h = Harness(store, cfg, script)
    result = h.run()
    assert result.status == RUN_PARTIAL
    assert [k.stopped_by for k in result.keywords] == ["end", "aborted"]
    assert store.count_roles() == 1
    summary = store.get_run(result.run_id)
    assert summary is not None
    assert (summary.keywords_done, summary.keywords_total) == (1, 2)


def test_a_success_resets_the_consecutive_throttle_counter(store: Store, cfg: Config) -> None:
    one_keyword(store)
    cfg = replace(cfg, max_pages_per_keyword=4)
    script = {
        (TPM, 0): [throttled(), throttled(), throttled(), ok(card("1"))],
        (TPM, 1): [throttled(), throttled(), throttled(), ok(card("2"))],
        (TPM, 2): [end()],
    }
    result = Harness(store, cfg, script).run()
    assert result.keywords[0].throttled == 6
    assert result.status == RUN_SUCCESS


# ---- fatal conditions -------------------------------------------------------------------------


def test_layout_change_fails_the_run_and_stops(store: Store, cfg: Config) -> None:
    two_keywords(store)
    script = {(TPM, 0): [ok(card("1"))], (TPM, 1): [layout()]}
    h = Harness(store, cfg, script)
    result = h.run()
    assert result.status == RUN_FAILED
    assert "layout changed" in result.note
    assert result.keywords[0].stopped_by == "layout_changed"
    assert not any(k == DM for k, _ in h.fetcher.calls)
    assert store.count_roles() == 1  # what was already stored is kept
    assert store.last_successful_run() is None


def test_keyboard_interrupt_marks_run_failed_and_reraises(store: Store, cfg: Config) -> None:
    one_keyword(store)
    script = {(TPM, 0): [ok(card("1"))], (TPM, 1): [KeyboardInterrupt()]}
    h = Harness(store, cfg, script)
    with pytest.raises(KeyboardInterrupt):
        h.run()
    run = store.latest_run()
    assert run is not None
    assert (run.status, run.note) == (RUN_FAILED, "Interrupted by user")
    assert run.finished_on is not None
    assert store.count_roles() == 1
    assert store.keyword_records(run.id)[0].stopped_by == "interrupted"
    assert store.last_successful_run() is None


def test_unexpected_error_marks_run_failed_and_reraises(store: Store, cfg: Config) -> None:
    one_keyword(store)
    h = Harness(store, cfg, {(TPM, 0): [RuntimeError("boom")]})
    with pytest.raises(RuntimeError, match="boom"):
        h.run()
    run = store.latest_run()
    assert run is not None
    assert run.status == RUN_FAILED
    assert "RuntimeError: boom" in run.note


def test_page_is_stored_atomically(
    store: Store, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_keyword(store)
    original = store.add_skipped

    def broken(run_id: int, rows: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "add_skipped", broken)
    h = Harness(store, cfg, {(TPM, 0): [ok(card("1"), card("2", "Chef"))]})
    with pytest.raises(RuntimeError):
        h.run()
    assert store.count_roles() == 0  # the whole page rolled back
    monkeypatch.setattr(store, "add_skipped", original)


# ---- setup errors -----------------------------------------------------------------------------


def test_no_keyword_phrases_refuses_to_start(store: Store, cfg: Config) -> None:
    store.replace_phrases([(TPM, False, True)])
    with pytest.raises(ScanSetupError, match="keyword"):
        Harness(store, cfg, {}).run()
    assert store.latest_run() is None


def test_no_match_phrases_refuses_to_start(store: Store, cfg: Config) -> None:
    store.replace_phrases([(TPM, True, False)])
    with pytest.raises(ScanSetupError, match="matching"):
        Harness(store, cfg, {}).run()
    assert store.latest_run() is None
