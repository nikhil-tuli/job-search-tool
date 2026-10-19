from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jobscan import db
from jobscan.models import (
    DEFAULT_PHRASES,
    STATUSES,
    JobDetail,
    KeywordRecord,
    NewRole,
    PageStat,
    RoleFilter,
)
from jobscan.store import (
    MAX_DETAIL_ATTEMPTS,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_SUCCESS,
    Store,
    StoreError,
)
from jobscan.timeutil import IST, now_ist, to_iso


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


def make_role(job_id: str = "1", **overrides: object) -> NewRole:
    fields: dict[str, object] = {
        "portal": "LinkedIn",
        "job_id": job_id,
        "title": "Technical Program Manager",
        "company": "Acme",
        "location": "Pune, India",
        "link": f"https://www.linkedin.com/jobs/view/{job_id}",
        "posted_on": "2026-10-14T10:00:00+05:30",
        "posted_precision": "hour",
        "fetched_on": "2026-10-15T09:00:00+05:30",
        "actively_hiring": False,
        "matched_phrase": "Technical Program Manager",
        "possible_duplicate_of": None,
        "dup_key": "acme|technical program manager|pune",
        "raw_datetime": "2026-10-14",
        "raw_relative": "22 hours ago",
        "run_id": 1,
    }
    fields.update(overrides)
    return NewRole(**fields)  # type: ignore[arg-type]


@pytest.fixture
def run_id(store: Store) -> int:
    return store.start_run(window_seconds=86400, keywords_total=2)


# ---- db / migrations --------------------------------------------------------------------


def test_migrate_creates_schema_and_seeds_phrases(store: Store) -> None:
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert [p.phrase for p in store.list_phrases()] == list(DEFAULT_PHRASES)
    assert all(p.use_as_keyword and p.use_for_match for p in store.list_phrases())


def test_migrate_is_idempotent(store: Store) -> None:
    db.migrate(store.conn)
    assert len(store.list_phrases()) == len(DEFAULT_PHRASES)


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "x.db")
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(db.SchemaTooNewError):
        db.migrate(conn)
    conn.close()


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half (x INTEGER)")
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "MIGRATIONS", (broken,))
    monkeypatch.setattr(db, "SCHEMA_VERSION", 1)
    conn = db.connect(tmp_path / "y.db")
    with pytest.raises(RuntimeError):
        db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'half'").fetchone() is None
    conn.close()


def test_file_database_uses_wal_and_creates_parent_dirs(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "nested" / "dir" / "a.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_status_check_constraint_matches_models(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE roles SET status = 'Bogus'")
    for status in STATUSES:
        store.conn.execute("UPDATE roles SET status = ?", (status,))


# ---- config / phrases --------------------------------------------------------------------


def test_config_roundtrip_and_update(store: Store) -> None:
    assert store.get_config_raw() == {}
    store.set_config_raw({"a": "1", "b": "2"})
    store.set_config_raw({"b": "3"})
    assert store.get_config_raw() == {"a": "1", "b": "3"}


def test_replace_phrases_trims_and_orders(store: Store) -> None:
    store.replace_phrases([("  Delivery   Manager ", True, True), ("TPM", False, True)])
    phrases = store.list_phrases()
    assert [p.phrase for p in phrases] == ["Delivery Manager", "TPM"]
    assert [p.use_as_keyword for p in phrases] == [True, False]


@pytest.mark.parametrize(
    "bad",
    [[("", True, True)], [("  ", True, True)], [("TPM", True, True), ("tpm", True, True)]],
)
def test_replace_phrases_rejects_bad_input_and_keeps_old(
    store: Store, bad: list[tuple[str, bool, bool]]
) -> None:
    with pytest.raises(StoreError):
        store.replace_phrases(bad)
    assert [p.phrase for p in store.list_phrases()] == list(DEFAULT_PHRASES)


# ---- roles -------------------------------------------------------------------------------


def test_insert_and_get_role(store: Store, run_id: int) -> None:
    assert store.insert_role(make_role("1", run_id=run_id, actively_hiring=True))
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.status == "Not Applied"
    assert role.applied_on is None
    assert role.actively_hiring is True
    assert role.first_run_id == run_id
    assert role.key == "LinkedIn|1"


def test_insert_same_key_is_ignored_and_keeps_user_edits(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_status(["LinkedIn|1"], "Interview Ongoing")
    assert store.insert_role(make_role("1", title="Changed", run_id=run_id)) is False
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.title, role.status) == ("Technical Program Manager", "Interview Ongoing")


def test_same_job_id_on_another_portal_is_a_different_role(store: Store, run_id: int) -> None:
    assert store.insert_role(make_role("1", run_id=run_id))
    assert store.insert_role(make_role("1", portal="Naukri", run_id=run_id))
    assert store.count_roles() == 2


def test_role_exists(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    assert store.role_exists("LinkedIn", "1")
    assert not store.role_exists("LinkedIn", "2")


def test_find_duplicate_returns_earliest(store: Store, run_id: int) -> None:
    store.insert_role(make_role("2", fetched_on="2026-10-15T10:00:00+05:30", run_id=run_id))
    store.insert_role(make_role("1", fetched_on="2026-10-15T09:00:00+05:30", run_id=run_id))
    assert store.find_duplicate("acme|technical program manager|pune") == "LinkedIn|1"
    assert (
        store.find_duplicate("acme|technical program manager|pune", exclude_job_id="1")
        == "LinkedIn|2"
    )
    assert store.find_duplicate("other") is None
    assert store.find_duplicate(None) is None


def seed(store: Store, run_id: int) -> None:
    store.insert_role(
        make_role(
            "1",
            title="Technical Program Manager",
            run_id=run_id,
            fetched_on="2026-10-15T09:00:00+05:30",
        )
    )
    store.insert_role(
        make_role(
            "2",
            title="Delivery Manager",
            company="Globex",
            run_id=run_id,
            fetched_on="2026-10-15T10:00:00+05:30",
            possible_duplicate_of="LinkedIn|1",
        )
    )
    store.insert_role(
        make_role(
            "3",
            title="TPM",
            company="100%_Co",
            run_id=run_id,
            fetched_on="2026-10-14T10:00:00+05:30",
        )
    )
    store.update_status(["LinkedIn|3"], "Applied")
    store.update_status(["LinkedIn|1"], "Not relevant")


def test_list_roles_order_is_newest_fetched_first(store: Store, run_id: int) -> None:
    seed(store, run_id)
    assert [r.job_id for r in store.list_roles()] == ["2", "1", "3"]


def test_filters(store: Store, run_id: int) -> None:
    seed(store, run_id)

    def ids(**kw: object) -> list[str]:
        return [r.job_id for r in store.list_roles(RoleFilter(**kw))]  # type: ignore[arg-type]

    assert ids(query="delivery") == ["2"]
    assert ids(query="GLOBEX") == ["2"]
    assert ids(query="pune") == ["2", "1", "3"]
    assert ids(status="Applied") == ["3"]
    assert ids(status="Not relevant") == ["1"]
    assert ids(duplicates_only=True) == ["2"]
    assert ids(query="manager", status="Not Applied", duplicates_only=True) == ["2"]
    assert ids(status="Not Applied") == ["2"]


def test_like_wildcards_in_query_are_literal(store: Store, run_id: int) -> None:
    seed(store, run_id)
    assert [r.job_id for r in store.list_roles(RoleFilter(query="100%_"))] == ["3"]
    assert store.list_roles(RoleFilter(query="%")) == [store.get_role("LinkedIn", "3")]
    assert store.list_roles(RoleFilter(query="_")) == [store.get_role("LinkedIn", "3")]


def test_filter_search_covers_comments_and_contact(store: Store, run_id: int) -> None:
    seed(store, run_id)
    store.update_details(
        "LinkedIn",
        "1",
        status="Not Applied",
        applied_on=None,
        contact_person="Priya Nair",
        comments="referral pending",
    )
    assert [r.job_id for r in store.list_roles(RoleFilter(query="priya"))] == ["1"]
    assert [r.job_id for r in store.list_roles(RoleFilter(query="referral"))] == ["1"]


def test_invalid_status_filter_raises(store: Store) -> None:
    with pytest.raises(StoreError):
        store.list_roles(RoleFilter(status="Bogus"))


def test_pagination(store: Store, run_id: int) -> None:
    for i in range(1, 8):
        store.insert_role(
            make_role(str(i), run_id=run_id, fetched_on=f"2026-10-15T09:0{i}:00+05:30")
        )
    page1 = store.list_roles(limit=3, offset=0)
    page2 = store.list_roles(limit=3, offset=3)
    page3 = store.list_roles(limit=3, offset=6)
    assert [r.job_id for r in page1 + page2 + page3] == ["7", "6", "5", "4", "3", "2", "1"]
    assert store.count_roles() == 7


# ---- status rules ------------------------------------------------------------------------

TODAY = datetime(2026, 10, 15, 9, 30, tzinfo=IST)


def test_applied_sets_applied_on_when_empty(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_status(["LinkedIn|1"], "Applied", today=TODAY)
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.applied_on == to_iso(TODAY)


def test_applied_keeps_existing_applied_on(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_status(["LinkedIn|1"], "Applied", today=TODAY - timedelta(days=3))
    store.update_status(["LinkedIn|1"], "Applied", today=TODAY)
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.applied_on == to_iso(TODAY - timedelta(days=3))


def test_not_applied_clears_applied_on(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_status(["LinkedIn|1"], "Applied", today=TODAY)
    store.update_status(["LinkedIn|1"], "Not Applied")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.status, role.applied_on) == ("Not Applied", None)


def test_other_statuses_leave_applied_on(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_status(["LinkedIn|1"], "Applied", today=TODAY)
    store.update_status(["LinkedIn|1"], "Interview cleared")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.status, role.applied_on) == ("Interview cleared", to_iso(TODAY))


def test_other_status_does_not_invent_applied_on(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_status(["LinkedIn|1"], "Initial screening rejected")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.applied_on is None


def test_bulk_update_and_duplicate_keys(store: Store, run_id: int) -> None:
    for i in "123":
        store.insert_role(make_role(i, run_id=run_id))
    assert store.update_status(["LinkedIn|1", "LinkedIn|2", "LinkedIn|1"], "Applied") == 2
    assert [r.status for r in store.list_roles()].count("Applied") == 2


def test_bulk_update_is_atomic_when_one_key_is_unknown(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    with pytest.raises(StoreError, match="unknown role"):
        store.update_status(["LinkedIn|1", "LinkedIn|999"], "Applied")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.status, role.applied_on) == ("Not Applied", None)


@pytest.mark.parametrize(
    ("keys", "status"),
    [([], "Applied"), (["LinkedIn|1"], "Bogus"), (["nobar"], "Applied"), (["|1"], "Applied")],
)
def test_invalid_status_updates_raise(
    store: Store, run_id: int, keys: list[str], status: str
) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    with pytest.raises(StoreError):
        store.update_status(keys, status)


def test_job_id_containing_pipe_splits_on_first_pipe_only(store: Store, run_id: int) -> None:
    store.insert_role(make_role("a|b", run_id=run_id))
    store.update_status(["LinkedIn|a|b"], "Applied")
    role = store.get_role("LinkedIn", "a|b")
    assert role is not None
    assert role.status == "Applied"


def test_update_details(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_details(
        "LinkedIn",
        "1",
        status="Applied",
        applied_on="2026-10-10T00:00:00+05:30",
        contact_person="  Priya ",
        comments=" call Monday ",
    )
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.contact_person, role.comments) == ("Priya", "call Monday")
    assert role.applied_on == "2026-10-10T00:00:00+05:30"


def test_update_details_rejects_bad_input(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    base = {"applied_on": None, "contact_person": "", "comments": ""}
    with pytest.raises(StoreError):
        store.update_details("LinkedIn", "1", status="Bogus", **base)  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="unknown role"):
        store.update_details("LinkedIn", "9", status="Applied", **base)  # type: ignore[arg-type]


# ---- transactions ------------------------------------------------------------------------


def _insert_then_fail(store: Store, run_id: int, job_id: str, *, nested: bool) -> None:
    with store.transaction():
        if nested:
            with store.transaction():
                store.insert_role(make_role(job_id, run_id=run_id))
        else:
            store.insert_role(make_role(job_id, run_id=run_id))
        raise RuntimeError("x")


@pytest.mark.parametrize("nested", [False, True])
def test_transaction_rolls_back_on_error(store: Store, run_id: int, nested: bool) -> None:
    with pytest.raises(RuntimeError):
        _insert_then_fail(store, run_id, "1", nested=nested)
    assert store.count_roles() == 0
    with store.transaction(), store.transaction():
        store.insert_role(make_role("2", run_id=run_id))
    assert store.count_roles() == 1


# ---- runs --------------------------------------------------------------------------------


def test_run_lifecycle_and_summary_totals(store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=2)
    running = store.get_run(run)
    assert running is not None
    assert (running.status, running.finished_on) == ("running", None)
    store.record_keyword(
        run,
        KeywordRecord(
            "TPM",
            pages=3,
            cards=30,
            matched=10,
            new_roles=4,
            possible_dups=1,
            throttled=2,
            retries=2,
            failed_pages=1,
            malformed=0,
            completed=True,
            stopped_by="end",
        ),
    )
    store.record_keyword(
        run,
        KeywordRecord(
            "Delivery Manager",
            pages=2,
            cards=20,
            matched=5,
            new_roles=5,
            malformed=1,
            completed=False,
            stopped_by="aborted",
        ),
    )
    store.finish_run(run, RUN_PARTIAL, "note")
    summary = store.get_run(run)
    assert summary is not None
    assert (summary.status, summary.note) == (RUN_PARTIAL, "note")
    assert summary.finished_on is not None
    assert (summary.pages, summary.cards_seen, summary.matched) == (5, 50, 15)
    assert (summary.new_roles, summary.possible_dups) == (9, 1)
    assert (summary.throttled, summary.retries, summary.failed_pages) == (2, 2, 1)
    assert (summary.malformed_cards, summary.keywords_done, summary.keywords_total) == (1, 1, 2)
    assert [k.keyword for k in store.keyword_records(run)] == ["TPM", "Delivery Manager"]


def test_run_without_keywords_has_zero_totals(store: Store, run_id: int) -> None:
    summary = store.get_run(run_id)
    assert summary is not None
    assert (summary.pages, summary.cards_seen, summary.keywords_done) == (0, 0, 0)


def test_finish_run_validates(store: Store, run_id: int) -> None:
    with pytest.raises(StoreError):
        store.finish_run(run_id, "running")
    with pytest.raises(StoreError):
        store.finish_run(run_id, "weird")
    with pytest.raises(StoreError, match="unknown run"):
        store.finish_run(9999, RUN_SUCCESS)


def test_last_successful_run_ignores_other_statuses(store: Store) -> None:
    assert store.last_successful_run() is None
    ok = store.start_run(window_seconds=86400, keywords_total=1)
    store.finish_run(ok, RUN_SUCCESS)
    bad = store.start_run(window_seconds=86400, keywords_total=1)
    store.finish_run(bad, RUN_FAILED)
    part = store.start_run(window_seconds=86400, keywords_total=1)
    store.finish_run(part, RUN_PARTIAL)
    store.start_run(window_seconds=86400, keywords_total=1)
    last = store.last_successful_run()
    assert last is not None
    assert last.id == ok
    latest = store.latest_run()
    assert latest is not None
    assert latest.status == "running"
    assert [r.id for r in store.list_runs()] == [4, 3, 2, 1]
    assert [r.id for r in store.list_runs(2)] == [4, 3]


def test_fail_stale_runs(store: Store) -> None:
    a = store.start_run(window_seconds=1, keywords_total=1)
    b = store.start_run(window_seconds=1, keywords_total=1)
    store.finish_run(b, RUN_SUCCESS)
    assert store.fail_stale_runs() == 1
    run = store.get_run(a)
    assert run is not None
    assert run.status == RUN_FAILED
    assert "Interrupted" in run.note
    assert store.fail_stale_runs() == 0


def test_depth_stats_aggregate_across_keywords(store: Store, run_id: int) -> None:
    store.record_pages(run_id, "A", [PageStat(0, 10, 6), PageStat(1, 10, 2)])
    store.record_pages(run_id, "B", [PageStat(0, 10, 4)])
    stats = store.depth_stats(run_id)
    assert [(s.page_index, s.cards, s.matched) for s in stats] == [(0, 20, 10), (1, 10, 2)]
    assert stats[0].match_rate == 0.5
    assert stats[1].match_rate == pytest.approx(0.2)


def test_depth_stat_with_no_cards_has_zero_rate(store: Store, run_id: int) -> None:
    store.record_pages(run_id, "A", [PageStat(0, 0, 0)])
    assert store.depth_stats(run_id)[0].match_rate == 0.0


def test_phrase_matches_accumulate(store: Store, run_id: int) -> None:
    store.add_phrase_matches(run_id, {"TPM": 2, "Delivery Manager": 5, "Unused": 0})
    store.add_phrase_matches(run_id, {"TPM": 4})
    assert store.phrase_matches(run_id) == [("TPM", 6), ("Delivery Manager", 5)]


def test_deleting_a_run_cascades_but_not_roles(store: Store, run_id: int) -> None:
    store.record_pages(run_id, "A", [PageStat(0, 1, 1)])
    store.add_skipped(run_id, [("LinkedIn", "9", "Chef", "X", "2026-10-15T09:00:00+05:30")])
    store.conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    assert store.depth_stats(run_id) == []
    assert store.list_skipped() == []


# ---- skipped titles ----------------------------------------------------------------------


def test_skipped_titles_dedupe_within_run_and_purge_by_age(store: Store, run_id: int) -> None:
    now = datetime(2026, 10, 15, 12, 0, tzinfo=IST)
    old = to_iso(now - timedelta(days=31))
    recent = to_iso(now - timedelta(days=2))
    store.add_skipped(
        run_id,
        [
            ("LinkedIn", "1", "Chef", "X", old),
            ("LinkedIn", "1", "Chef", "X", old),
            ("LinkedIn", "2", "Barista", "Y", recent),
        ],
    )
    assert len(store.list_skipped(run_id)) == 2
    assert store.purge_skipped(30, now=now) == 1
    assert [s.job_id for s in store.list_skipped()] == ["2"]


def test_purge_uses_real_clock_by_default(store: Store, run_id: int) -> None:
    store.add_skipped(run_id, [("LinkedIn", "1", "Chef", "X", to_iso(now_ist()))])
    assert store.purge_skipped(30) == 0


def test_role_must_reference_an_existing_run(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_role(make_role("1", run_id=999))


def test_incomplete_migration_sql_is_rejected() -> None:
    conn = db.connect(":memory:")
    with pytest.raises(ValueError, match="incomplete"):
        db._execute_script(conn, "SELECT 1")
    conn.close()


def test_not_relevant_status_leaves_applied_on_alone(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_status(["LinkedIn|1"], "Applied", today=TODAY)
    store.update_status(["LinkedIn|1"], "Not relevant")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.status, role.applied_on) == ("Not relevant", to_iso(TODAY))


# ---- migration 2: Relevant column becomes a status --------------------------------------------


def test_migration_2_converts_relevant_marks(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "old.db")
    conn.execute("BEGIN IMMEDIATE")
    db.MIGRATIONS[0](conn)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("COMMIT")

    def add(job_id: str, status: str, relevant: str | None) -> None:
        conn.execute(
            "INSERT INTO roles (portal, job_id, title, company, location, link, posted_precision, "
            "fetched_on, matched_phrase, status, relevant, contact_person, comments) "
            "VALUES ('LinkedIn', ?, 'TPM', 'Acme', 'Pune', 'https://x', 'hour', "
            "'2026-10-15T09:00:00+05:30', 'TPM', ?, ?, 'Priya', 'keep me')",
            (job_id, status, relevant),
        )

    add("1", "Not Applied", "N")  # becomes Not relevant
    add("2", "Applied", "N")  # already moved on: status kept
    add("3", "Not Applied", "Y")  # Y mark is dropped
    add("4", "Not Applied", None)
    db.migrate(conn)

    store = Store(conn)
    statuses = {r.job_id: r.status for r in store.list_roles()}
    assert statuses == {
        "1": "Not relevant",
        "2": "Applied",
        "3": "Not Applied",
        "4": "Not Applied",
    }
    kept = store.get_role("LinkedIn", "1")
    assert kept is not None
    assert (kept.contact_person, kept.comments) == ("Priya", "keep me")
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(roles)")]
    assert "relevant" not in columns
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(roles)")}
    assert {"roles_dup_key", "roles_fetched_on", "roles_status"} <= indexes
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_status_constraint_accepts_not_relevant(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.conn.execute("UPDATE roles SET status = 'Not relevant'")


def test_closed_status_leaves_applied_on_alone(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_status(["LinkedIn|1"], "Applied", today=TODAY)
    store.update_status(["LinkedIn|1"], "Closed")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.status, role.applied_on) == ("Closed", to_iso(TODAY))
    assert store.count_roles(RoleFilter(status="Closed")) == 1


def test_migration_3_adds_closed_and_keeps_every_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "v2.db")
    for version in (1, 2):
        conn.execute("BEGIN IMMEDIATE")
        db.MIGRATIONS[version - 1](conn)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    for job_id, status in (("1", "Applied"), ("2", "Not relevant"), ("3", "Not Applied")):
        conn.execute(
            "INSERT INTO roles (portal, job_id, title, company, location, link, posted_precision, "
            "fetched_on, matched_phrase, status, applied_on, contact_person, comments) "
            "VALUES ('LinkedIn', ?, 'TPM', 'Acme', 'Pune', 'https://x', 'hour', "
            "'2026-10-15T09:00:00+05:30', 'TPM', ?, '2026-10-10T00:00:00+05:30', 'Priya', 'note')",
            (job_id, status),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE roles SET status = 'Closed'")

    db.migrate(conn)

    store = Store(conn)
    assert {r.job_id: r.status for r in store.list_roles()} == {
        "1": "Applied",
        "2": "Not relevant",
        "3": "Not Applied",
    }
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.applied_on, role.contact_person, role.comments) == (
        "2026-10-10T00:00:00+05:30",
        "Priya",
        "note",
    )
    store.update_status(["LinkedIn|1"], "Closed")
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(roles)")}
    assert {"roles_dup_key", "roles_fetched_on", "roles_status"} <= indexes
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_skipped_status_leaves_applied_on_alone(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.update_status(["LinkedIn|1"], "Skipped")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.status, role.applied_on) == ("Skipped", None)
    assert store.count_roles(RoleFilter(status="Skipped")) == 1


def test_migration_4_adds_skipped_and_keeps_every_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "v3.db")
    for version in (1, 2, 3):
        conn.execute("BEGIN IMMEDIATE")
        db.MIGRATIONS[version - 1](conn)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    for job_id, status in (("1", "Applied"), ("2", "Closed"), ("3", "Not relevant")):
        conn.execute(
            "INSERT INTO roles (portal, job_id, title, company, location, link, posted_precision, "
            "fetched_on, matched_phrase, status, applied_on, contact_person, comments) "
            "VALUES ('LinkedIn', ?, 'TPM', 'Acme', 'Pune', 'https://x', 'hour', "
            "'2026-10-15T09:00:00+05:30', 'TPM', ?, '2026-10-10T00:00:00+05:30', 'Priya', 'note')",
            (job_id, status),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE roles SET status = 'Skipped'")

    db.migrate(conn)

    store = Store(conn)
    assert {r.job_id: r.status for r in store.list_roles()} == {
        "1": "Applied",
        "2": "Closed",
        "3": "Not relevant",
    }
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.applied_on, role.contact_person, role.comments) == (
        "2026-10-10T00:00:00+05:30",
        "Priya",
        "note",
    )
    store.update_status(["LinkedIn|1"], "Skipped")
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(roles)")}
    assert {"roles_dup_key", "roles_fetched_on", "roles_status"} <= indexes
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_migration_5_adds_new_phrases_once_and_keeps_edits(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "v4.db")
    for version in (1, 2, 3, 4):
        conn.execute("BEGIN IMMEDIATE")
        db.MIGRATIONS[version - 1](conn)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    before = [r["phrase"] for r in conn.execute("SELECT phrase FROM phrases ORDER BY position")]
    assert len(before) == 8
    conn.execute("DELETE FROM phrases WHERE phrase = 'TPM'")
    # A phrase the user already added by hand must not be duplicated or changed.
    conn.execute(
        "INSERT INTO phrases (phrase, use_as_keyword, use_for_match, position) "
        "VALUES ('it project manager', 0, 1, 20)"
    )

    db.migrate(conn)

    rows = conn.execute("SELECT * FROM phrases ORDER BY position").fetchall()
    names = [r["phrase"] for r in rows]
    assert "TPM" not in names
    assert names.count("it project manager") == 1
    assert "IT Project Manager" not in names
    for phrase in (
        "Senior Project Manager",
        "Software Program Manager",
        "Technical Project/Program Manager",
        "Technical Program Management",
    ):
        assert phrase in names
    mine = next(r for r in rows if r["phrase"] == "it project manager")
    assert (mine["use_as_keyword"], mine["use_for_match"]) == (0, 1)
    assert all(r["use_as_keyword"] == 1 for r in rows if r["phrase"] != "it project manager")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_fresh_database_has_all_default_phrases_in_order(store: Store) -> None:
    assert [p.phrase for p in store.list_phrases()] == list(DEFAULT_PHRASES)


# ---- fetch summary and fetch-date filter -----------------------------------------------------


def _fetched(store: Store, run_id: int, job_id: str, day: str, status: str = "Not Applied") -> None:
    store.insert_role(make_role(job_id, run_id=run_id, fetched_on=f"{day}T09:00:00+05:30"))
    if status != "Not Applied":
        store.update_status([f"LinkedIn|{job_id}"], status)


def test_fetch_summary_is_empty_without_roles(store: Store) -> None:
    assert store.fetch_summary() == []


def test_fetch_summary_counts_by_day_and_status_and_fills_gaps(store: Store, run_id: int) -> None:
    _fetched(store, run_id, "1", "2026-10-15")
    _fetched(store, run_id, "2", "2026-10-15", "Applied")
    _fetched(store, run_id, "3", "2026-10-15", "Not relevant")
    _fetched(store, run_id, "4", "2026-10-18", "Skipped")

    days = store.fetch_summary()

    assert [d.day for d in days] == ["2026-10-18", "2026-10-17", "2026-10-16", "2026-10-15"]
    assert [d.total for d in days] == [1, 0, 0, 3]
    oct15 = days[-1].by_status
    assert (oct15["Not Applied"], oct15["Applied"], oct15["Not relevant"]) == (1, 1, 1)
    assert days[0].by_status["Skipped"] == 1


def test_fetch_summary_covers_every_status_and_adds_up(store: Store, run_id: int) -> None:
    for number, status in enumerate(STATUSES, start=1):
        _fetched(store, run_id, str(number), "2026-10-15", status)
    (day,) = store.fetch_summary()
    assert tuple(day.by_status) == STATUSES  # workflow order, every status present
    assert all(count == 1 for count in day.by_status.values())
    assert day.total == sum(day.by_status.values()) == len(STATUSES)
    assert day.total == store.count_roles(RoleFilter(fetched_on="2026-10-15"))


def test_fetch_summary_groups_by_ist_day(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id, fetched_on="2026-10-15T00:30:00+05:30"))
    store.insert_role(make_role("2", run_id=run_id, fetched_on="2026-10-15T23:59:00+05:30"))
    store.insert_role(make_role("3", run_id=run_id, fetched_on="2026-10-16T00:01:00+05:30"))
    assert [(d.day, d.total) for d in store.fetch_summary()] == [
        ("2026-10-16", 1),
        ("2026-10-15", 2),
    ]


def test_fetched_on_filter_combines_with_status(store: Store, run_id: int) -> None:
    _fetched(store, run_id, "1", "2026-10-15", "Applied")
    _fetched(store, run_id, "2", "2026-10-15")
    _fetched(store, run_id, "3", "2026-10-16", "Applied")
    day = RoleFilter(fetched_on="2026-10-15")
    assert {r.job_id for r in store.list_roles(day)} == {"1", "2"}
    both = RoleFilter(fetched_on="2026-10-15", status="Applied")
    assert [r.job_id for r in store.list_roles(both)] == ["1"]
    assert store.count_roles(both) == 1


@pytest.mark.parametrize("bad", ["2026-13-01", "15-10-2026", "2026-1-5", "x", "2026-10-15 "])
def test_fetched_on_filter_rejects_malformed_dates(store: Store, bad: str) -> None:
    with pytest.raises(StoreError):
        store.count_roles(RoleFilter(fetched_on=bad))


# ---- job descriptions ----------------------------------------------------------------------


def test_migration_6_adds_description_columns_and_keeps_every_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "v5.db")
    for version in (1, 2, 3, 4, 5):
        conn.execute("BEGIN IMMEDIATE")
        db.MIGRATIONS[version - 1](conn)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    conn.execute(
        "INSERT INTO roles (portal, job_id, title, company, location, link, posted_precision, "
        "fetched_on, matched_phrase, status, contact_person, comments) "
        "VALUES ('LinkedIn', '1', 'TPM', 'Acme', 'Pune', 'https://x', 'hour', "
        "'2026-10-15T09:00:00+05:30', 'TPM', 'Applied', 'Priya', 'note')"
    )

    db.migrate(conn)

    store = Store(conn)
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.status, role.contact_person, role.comments) == ("Applied", "Priya", "note")
    assert role.description is None
    assert store.pending_details("2000-01-01", 5) == [("LinkedIn", "1")]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_save_detail_stores_text_and_criteria(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    detail = JobDetail("Lead programs.", seniority="Director", industries="Software")
    store.save_detail("LinkedIn", "1", detail, when="2026-10-16T10:00:00+05:30")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.description == "Lead programs."
    assert (role.seniority, role.industries, role.employment_type) == ("Director", "Software", None)
    assert store.pending_details("2000-01-01", 5) == []


def test_pending_details_filters_orders_and_limits(store: Store, run_id: int) -> None:
    for job_id, day in (("1", "2026-10-10"), ("2", "2026-10-14"), ("3", "2026-10-15")):
        store.insert_role(make_role(job_id, run_id=run_id, fetched_on=f"{day}T09:00:00+05:30"))
    store.save_detail("LinkedIn", "3", JobDetail("done"), when="2026-10-15T10:00:00+05:30")
    assert store.pending_details("2026-10-12T00:00:00+05:30", 10) == [("LinkedIn", "2")]
    assert store.pending_details("2026-10-01T00:00:00+05:30", 10) == [
        ("LinkedIn", "2"),
        ("LinkedIn", "1"),
    ]
    assert store.pending_details("2026-10-01T00:00:00+05:30", 1) == [("LinkedIn", "2")]
    assert store.pending_details("2026-10-01T00:00:00+05:30", 0) == []


def test_a_role_is_given_up_after_the_attempt_limit(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    for attempt in range(MAX_DETAIL_ATTEMPTS):
        assert store.pending_details("2000-01-01", 5) == [("LinkedIn", "1")], attempt
        store.record_detail_failure("LinkedIn", "1")
    assert store.pending_details("2000-01-01", 5) == []


def test_search_matches_the_description(store: Store, run_id: int) -> None:
    store.insert_role(make_role("1", run_id=run_id))
    store.insert_role(make_role("2", run_id=run_id))
    store.save_detail(
        "LinkedIn", "2", JobDetail("Experience with payment gateways and 100% uptime"), when="x"
    )
    assert {r.job_id for r in store.list_roles(RoleFilter(query="payment gateways"))} == {"2"}
    assert {r.job_id for r in store.list_roles(RoleFilter(query="100%"))} == {"2"}
    assert store.count_roles(RoleFilter(query="kubernetes")) == 0


def test_scoring_tables_exist(store: Store) -> None:
    names = {
        r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"cv", "cv_chunks", "embeddings", "scoring_runs", "role_scores"} <= names


def test_upgrade_from_version_6_adds_scoring_and_keeps_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest, migrations = db.SCHEMA_VERSION, db.MIGRATIONS
    conn = db.connect(tmp_path / "old.db")
    with monkeypatch.context() as old:
        old.setattr(db, "SCHEMA_VERSION", 6)
        old.setattr(db, "MIGRATIONS", migrations[:6])
        db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'cv'").fetchone() is None
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'role_scores'").fetchone()
    conn.close()
