from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest

from jobscan import db
from jobscan.config import Config
from jobscan.details import fetch_descriptions
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


def fetch(
    store: Store, details: ScriptedDetails, ids: list[str], config: Config | None = None
) -> tuple:  # type: ignore[type-arg]
    lines: list[str] = []
    waits: list[tuple[float, float, str | None]] = []
    result = fetch_descriptions(
        store=store,
        fetcher=details,
        pending=[("LinkedIn", j) for j in ids],
        config=config or Config(),
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


def test_a_throttled_description_is_retried_and_not_counted(store: Store) -> None:
    details = ScriptedDetails({"1": [detail_throttled(), detail_ok("one")]})
    result, _, waits = fetch(store, details, ["1"])
    assert (result.saved, result.failed, result.stopped) == (1, 0, False)
    assert details.calls == ["1", "1"]
    assert any(w[2] == "retry 1/3" for w in waits)
    count = store.conn.execute("SELECT detail_attempts FROM roles WHERE job_id = '1'").fetchone()[0]
    assert count == 0


def test_a_role_throttled_past_the_retry_limit_is_left_for_later(store: Store) -> None:
    script = {"1": [detail_throttled()] * 4, "2": [detail_ok("two")]}
    details = ScriptedDetails(script)
    config = Config(retry_count=3, abort_after_consecutive_throttles=10)
    result, _, _ = fetch(store, details, ["1", "2"], config=config)
    assert details.calls == ["1", "1", "1", "1", "2"]  # first try plus 3 retries
    assert (result.saved, result.failed, result.stopped) == (1, 0, False)
    assert role(store, "1").description is None
    count = store.conn.execute("SELECT detail_attempts FROM roles WHERE job_id = '1'").fetchone()[0]
    assert count == 0


def test_the_batch_stops_after_the_set_number_of_throttles_in_a_row(store: Store) -> None:
    script = {str(i): [detail_throttled()] * 9 for i in range(1, 5)}
    details = ScriptedDetails(script)
    config = Config(retry_count=3, abort_after_consecutive_throttles=5)
    result, _, _ = fetch(store, details, ["1", "2", "3", "4"], config=config)
    assert result.stopped
    assert len(details.calls) == 5
    assert result.saved == 0


def test_a_success_resets_the_throttle_count(store: Store) -> None:
    script = {"1": [detail_throttled()] * 3, "3": [detail_throttled()] * 3}
    config = Config(retry_count=3, abort_after_consecutive_throttles=5)
    result, _, _ = fetch(store, ScriptedDetails(script), ["1", "2", "3", "4"], config=config)
    assert not result.stopped  # 3 + 3 throttles, but the success on role 2 reset the count
    assert result.saved == 4


def test_nothing_pending_is_a_no_op(store: Store) -> None:
    details = ScriptedDetails()
    result, lines, waits = fetch(store, details, [])
    assert (result.saved, result.failed) == (0, 0)
    assert details.calls == []
    assert lines == []
    assert waits == []


def test_the_retry_limit_matches_the_store() -> None:
    assert MAX_DETAIL_ATTEMPTS == 3
