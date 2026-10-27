from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest

from jobscan import db
from jobscan.config import Config
from jobscan.fit_book import FitBook
from jobscan.fit_filters import FitRules
from jobscan.models import Cv, RoleScore
from jobscan.scoring import inputs_fingerprint, plan_scoring
from jobscan.store import Store
from jobscan.timeutil import now_ist, to_iso
from tests.test_web import add_role


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


def fresh() -> str:
    return to_iso(now_ist())


def rules(**raw: str) -> FitRules:
    return FitRules.from_config(Config.from_raw(raw))


def score(store: Store, job_id: str) -> None:
    store.save_score(
        RoleScore("LinkedIn", job_id, 0.5, 0.5, 3, "x", fresh(), None),
    )


def test_unscored_roles_are_checked_live(store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    add_role(store, run, "1", fetched_on=fresh(), company="Intuit")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert not FitBook(store, rules(), now_ist()).get(role).eligible
    assert FitBook(store, rules(fit_excluded_companies=""), now_ist()).get(role).eligible


def test_scored_role_without_a_snapshot_gets_one_ignoring_age(store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    old = to_iso(now_ist() - timedelta(days=30))
    add_role(store, run, "1", fetched_on=old)
    score(store, "1")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert FitBook(store, rules(), now_ist()).get(role).eligible
    # now saved: a later rule change does not alter it
    assert "LinkedIn|1" in store.get_fits()
    assert FitBook(store, rules(fit_excluded_companies="Acme"), now_ist()).get(role).eligible


def test_saved_fit_ignores_status_age_and_rule_edits(store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    add_role(store, run, "1", fetched_on=fresh())
    score(store, "1")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    FitBook(store, rules(), now_ist()).get(role)  # saved as Eligible
    store.update_status(["LinkedIn|1"], "Closed")
    later = now_ist() + timedelta(days=40)
    changed = store.get_role("LinkedIn", "1")
    assert changed is not None
    book = FitBook(
        store, rules(fit_excluded_companies="Acme", fit_deny_keywords="Technical"), later
    )
    assert book.get(changed).eligible


def test_plan_keeps_an_excluded_role_excluded_until_its_inputs_change(store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    add_role(store, run, "1", fetched_on=fresh(), company="Intuit")
    cv = Cv("cv.txt", "text", "sha", fresh())
    store.save_cv(cv, [("chunk", "h")])
    config = Config()
    plan = plan_scoring(store, config, cv, now_ist())
    assert plan.excluded_total == 1
    assert len(plan.new_fits) == 1
    role, fit, inputs = plan.new_fits[0]
    store.save_fits([(role.portal, role.job_id, fit, inputs)], when=fresh())
    # the company is no longer excluded, but the role was already considered
    freed = Config.from_raw({"fit_excluded_companies": ""})
    again = plan_scoring(store, freed, cv, now_ist())
    assert again.excluded_total == 1
    assert again.new_fits == []
    # a new CV means scoring inputs changed, so Fit is worked out again
    other = Cv("cv2.txt", "new text", "sha2", fresh())
    assert inputs_fingerprint(freed, other, role) != inputs
    third = plan_scoring(store, freed, other, now_ist())
    assert third.excluded_total == 0
    assert len(third.selected) == 0
    assert len(third.new_fits) == 1
    assert third.new_fits[0][1].eligible
