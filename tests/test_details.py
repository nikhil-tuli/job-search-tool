from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest

from jobscan import db
from jobscan.config import Config
from jobscan.details import THROTTLES_BEFORE_STOP, fetch_descriptions
from jobscan.models import NewRole
from jobscan.store import MAX_DETAIL_ATTEMPTS, Store
from jobscan.timeutil import IST
from tests.fakes import ScriptedDetails, detail_none, detail_ok, detail_throttled

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=IST)


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    store = Store(conn)
    run = store.start_run(window_seconds=86400, keywords_total=1)
    for job_id in ("1", "2", "3", "4"):
        store.insert_role(
            NewRole(
                portal="LinkedIn",
                job_id=job_id,
                title=f"Role {job_id}",
                company="Acme",
                location="Pune",
                link="https://www.linkedin.com/jobs/view/" + job_id,
                posted_on=None,
                posted_precision="day",
                fetched_on="2026-10-20T10:00:00+05:30",
                actively_hiring=False,
                matched_phrase="TPM",
                possible_duplicate_of=None,
                dup_key=None,
                raw_datetime=None,
                raw_relative=None,
                run_id=run,
            )
        )
    yield store
    conn.close()


def fetch(store: Store, details: ScriptedDetails, ids: list[str]) -> tuple:  # type: ignore[type-arg]
    lines: list[str] = []
    waits: list[tuple[float, float, str | None]] = []
    result = fetch_descriptions(
        store=store,
        fetcher=details,
        pending=[("LinkedIn", j) for j in ids],
        config=Config(),
        emit=lines.append,
        wait=lambda low, high, why: waits.append((low, high, why)),
        clock=lambda: NOW,
    )
    return result, lines, waits


def role(store: Store, job_id: str):  # type: ignore[no-untyped-def]
    found = store.get_role("LinkedIn", job_id)
    assert found is not None
    return found


def test_saves_descriptions_and_waits_before_each_request(store: Store) -> None:
    result, _, waits = fetch(store, ScriptedDetails(), ["1", "2"])
    assert (result.saved, result.failed, result.stopped) == (2, 0, False)
    assert role(store, "1").description == "Description of 1"
    assert [w[2] for w in waits] == [None, None]
    assert waits[0][:2] == (Config().delay_seconds_min, Config().delay_seconds_max)


def test_a_missing_posting_counts_against_the_role(store: Store) -> None:
    result, lines, _ = fetch(store, ScriptedDetails({"1": [detail_none("HTTP 404")]}), ["1", "2"])
    assert (result.saved, result.failed) == (1, 1)
    assert result.failed_ids == {"1"}
    assert role(store, "1").description is None
    assert any("1: none (HTTP 404)" in line for line in lines)
    count = store.conn.execute("SELECT detail_attempts FROM roles WHERE job_id = '1'").fetchone()[0]
    assert count == 1


def test_one_throttle_is_waited_out_and_not_counted(store: Store) -> None:
    details = ScriptedDetails({"1": [detail_throttled()], "2": [detail_ok("two")]})
    result, _, waits = fetch(store, details, ["1", "2"])
    assert (result.saved, result.failed, result.stopped) == (1, 0, False)
    assert any(w[2] == "next description" for w in waits)
    count = store.conn.execute("SELECT detail_attempts FROM roles WHERE job_id = '1'").fetchone()[0]
    assert count == 0


def test_repeated_throttling_stops_the_batch(store: Store) -> None:
    script = {str(i): [detail_throttled()] for i in range(1, 5)}
    details = ScriptedDetails(script)
    result, _, _ = fetch(store, details, ["1", "2", "3", "4"])
    assert result.stopped
    assert len(details.calls) == THROTTLES_BEFORE_STOP
    assert result.saved == 0


def test_a_success_resets_the_throttle_count(store: Store) -> None:
    script = {"1": [detail_throttled()], "3": [detail_throttled()]}
    result, _, _ = fetch(store, ScriptedDetails(script), ["1", "2", "3", "4"])
    assert not result.stopped
    assert result.saved == 2


def test_nothing_pending_is_a_no_op(store: Store) -> None:
    details = ScriptedDetails()
    result, lines, waits = fetch(store, details, [])
    assert (result.saved, result.failed) == (0, 0)
    assert details.calls == []
    assert lines == []
    assert waits == []


def test_the_retry_limit_matches_the_store() -> None:
    assert MAX_DETAIL_ATTEMPTS == 3
