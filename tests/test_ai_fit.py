from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest

from jobscan import db
from jobscan.ai_fit import (
    BAD,
    GOOD,
    PARTIAL,
    AiFitError,
    AiScorer,
    AiScoringUnavailable,
    band_of,
    inputs_fingerprint,
    parse_reply,
    reason_of,
    redact_cv,
    trim_description,
)
from jobscan.config import Config
from jobscan.models import Role
from jobscan.store import Store
from jobscan.tailor_ai import TailorError
from tests.test_scoring import CV_TEXT, NOW, add_role, upload_cv

LONG_JD = (
    "Responsibilities\n\n"
    + "\n".join(
        f"- Own delivery item {i} with engineering teams and vendors across the platform"
        for i in range(14)
    )
    + "\n\nQualifications\n\n- Experience with payment gateways and agile delivery\n"
)


def reply(items: list[tuple[str, str, str]], *, ok: bool = True, domain: str = "match") -> str:
    return json.dumps(
        {
            "must_haves": [{"item": i, "status": s, "evidence": e} for i, s, e in items],
            "role_type_ok": ok,
            "role_type_note": "" if ok else "warehouse operations",
            "domain": domain,
        }
    )


GOOD_REPLY = reply([("payment gateways", "Met", "Led payment gateway migration")])


class FakeClient:
    def __init__(self, replies: list[str | TailorError]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(
        self, system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 0
    ) -> str:
        self.calls.append((system, user))
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, TailorError):
            raise item
        return item


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


def scorer(store: Store, client: FakeClient, config: Config | None = None) -> AiScorer:
    return AiScorer(
        store=store,
        config=config or Config(),
        make_client=lambda _c: client,
        emit=lambda _line: None,
        sleep=lambda _s: None,
        clock=lambda: NOW,
    )


def test_band_rules_cover_every_case() -> None:
    met = ("a", "Met", "x")
    part = ("b", "Partly", "")
    miss = ("c", "Missing", "")
    assert band_of([met], True, "match", 2) == GOOD
    assert band_of([met, part], True, "close", 2) == GOOD
    assert band_of([met, part, part], True, "match", 2) == PARTIAL
    assert band_of([met, miss], True, "match", 2) == PARTIAL
    assert band_of([met, miss, miss], True, "match", 2) == PARTIAL
    assert band_of([met, miss, miss, miss], True, "match", 2) == BAD
    assert band_of([met, miss], True, "match", 0) == BAD
    assert band_of([met], False, "match", 2) == BAD
    assert band_of([met], True, "different", 2) == PARTIAL
    assert band_of([miss, miss, miss], True, "different", 2) == BAD
    assert band_of([], True, "match", 2) == GOOD  # no stated must-haves: role type and domain
    assert band_of([], True, "different", 2) == PARTIAL


def test_reason_is_built_from_the_labels() -> None:
    checks = [("SAP", "Missing", ""), ("Kafka", "Partly", ""), ("AWS", "Met", "x")]
    assert reason_of(checks, True, "", "different") == (
        "Missing: SAP. Partly: Kafka. Different domain."
    )
    assert reason_of([("AWS", "Met", "x")], True, "", "match") == (
        "All must-haves met; role type and domain fit."
    )
    assert reason_of([], False, "warehouse operations", "match") == (
        "Wrong role type: warehouse operations"
    )
    assert reason_of([], True, "", "match").startswith("No stated must-haves")


def test_parse_reply_checks_and_downgrades_unsupported_met() -> None:
    verdict = parse_reply(
        "```json\n"
        + reply([("A", "Met", ""), ("B", "met", "line"), ("C", "Nope", ""), ("", "Met", "x")])
        + "\n```"
    )
    assert verdict.checks == (("A", "Partly", ""), ("B", "Met", "line"))
    assert verdict.role_type_ok
    assert verdict.domain == "match"


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"must_haves": 3}',
        '{"must_haves": [], "domain": "match"}',
        '{"must_haves": [], "role_type_ok": true, "domain": "far"}',
    ],
)
def test_parse_reply_rejects_unusable_replies(text: str) -> None:
    with pytest.raises(AiFitError):
        parse_reply(text)


def test_redact_cv_removes_contact_details() -> None:
    text = (
        "Nikhil Tuli\nnikhil@example.com | +91 98456 42255 | linkedin.com/in/nikhil\n"
        "Technical Program Manager\n- Led migration"
    )
    out = redact_cv(text)
    assert "Nikhil" not in out
    assert "@" not in out
    assert "98456" not in out
    assert "linkedin" not in out
    assert "Technical Program Manager" in out
    assert "Led migration" in out


def test_trim_leaves_a_short_description_alone() -> None:
    assert trim_description("short text here", 100) == ("short text here", False)


def test_trim_drops_company_text_first_and_keeps_requirements() -> None:
    jd = (
        "About us\n\n"
        + " ".join(["company"] * 300)
        + "\n\nResponsibilities\n\n- Run delivery for the platform\n\n"
        "Requirements\n\n- Must know payment gateways\n"
    )
    text, trimmed = trim_description(jd, 60)
    assert trimmed
    assert "company company" not in text
    assert "Must know payment gateways" in text
    assert "Run delivery" in text


def test_trim_never_cuts_requirements() -> None:
    jd = "Requirements\n\n" + "\n".join(f"- Need skill {i} in depth" for i in range(40))
    text, trimmed = trim_description(jd, 50)
    assert trimmed
    assert "Need skill 39" in text


def test_run_needs_a_cv(store: Store) -> None:
    with pytest.raises(AiScoringUnavailable):
        scorer(store, FakeClient([GOOD_REPLY])).run()


def test_run_scores_eligible_roles_and_saves_fit(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    client = FakeClient([GOOD_REPLY])
    result = scorer(store, client).run()
    assert (result.scored, result.failed, result.waiting) == (1, 0, 0)
    row = store.get_ai_fits()["LinkedIn|1"]
    assert row.reason == "All must-haves met; role type and domain fit."
    assert "Nikhil" not in client.calls[0][1]
    assert "Technical Program Manager" in client.calls[0][1]
    assert "LinkedIn|1" in store.get_fits()  # Fit is kept once a role is scored
    assert store.ai_calls_since("2026-01-01") == 1


def test_second_run_skips_current_and_redoes_stale(store: Store) -> None:
    cv = upload_cv(store)
    role = add_role(store, "1", LONG_JD)
    client = FakeClient([GOOD_REPLY])
    scorer(store, client).run()
    scorer(store, client).run()
    assert len(client.calls) == 1
    changed = replace(Config(), ai_role_focus="Something else entirely")
    assert inputs_fingerprint(changed, cv, role) != inputs_fingerprint(Config(), cv, role)
    scorer(store, client, changed).run()
    assert len(client.calls) == 2


def test_band_setting_needs_no_new_call(store: Store) -> None:
    cv = upload_cv(store)
    role = add_role(store, "1", LONG_JD)
    assert inputs_fingerprint(replace(Config(), ai_max_missing=0), cv, role) == (
        inputs_fingerprint(Config(), cv, role)
    )


def test_short_or_missing_description_is_not_scored(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", "Too short to judge")
    add_role(store, "2", None)
    result = scorer(store, FakeClient([GOOD_REPLY])).run()
    assert result.scored == 0
    assert result.no_description == 2


def test_excluded_roles_are_not_sent(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD, company="Amazon")
    client = FakeClient([GOOD_REPLY])
    result = scorer(store, client).run()
    assert result.excluded == 1
    assert not client.calls


def test_unreadable_reply_is_retried_once_then_failed(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    client = FakeClient(["garbage"])
    result = scorer(store, client).run()
    assert result.failed == 1
    assert result.scored == 0
    assert len(client.calls) == 2
    assert "could not be read" in store.last_ai_errors()["LinkedIn|1"]


def test_service_error_stops_the_run_and_keeps_the_rest_waiting(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    add_role(store, "2", LONG_JD)
    client = FakeClient([TailorError("The AI service is busy (rate limit).")])
    result = scorer(store, client).run()
    assert result.scored == 0
    assert result.waiting == 2
    assert "busy" in result.stopped
    assert len(client.calls) == 1


def test_missing_key_stops_before_any_call(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)

    def no_key(_config: Config) -> FakeClient:
        raise TailorError("The AI key is missing.")

    result = AiScorer(
        store=store, config=Config(), make_client=no_key, emit=lambda _l: None, clock=lambda: NOW
    ).run()
    assert result.waiting == 1
    assert "key is missing" in result.stopped


def test_daily_cap_leaves_the_rest_for_tomorrow(store: Store) -> None:
    upload_cv(store)
    for n in range(3):
        add_role(store, str(n), LONG_JD + f"\n- Extra duty {n}")
    config = replace(Config(), ai_daily_cap=2)
    result = scorer(store, FakeClient([GOOD_REPLY]), config).run()
    assert result.scored == 2
    assert result.waiting == 1
    assert "daily cap" in result.stopped
    again = scorer(store, FakeClient([GOOD_REPLY]), config).run()
    assert again.scored == 0
    assert again.waiting == 1


def test_calls_older_than_a_day_do_not_count(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    old = (NOW - timedelta(hours=25)).isoformat()
    store.log_ai_call("LinkedIn", "x", when=old, ok=True)
    config = replace(Config(), ai_daily_cap=1)
    assert scorer(store, FakeClient([GOOD_REPLY]), config).run().scored == 1


def test_calls_are_paced(store: Store) -> None:
    upload_cv(store)
    for n in range(2):
        add_role(store, str(n), LONG_JD + f"\n- Extra duty {n}")
    waits: list[float] = []
    AiScorer(
        store=store,
        config=replace(Config(), ai_calls_per_minute=30),
        make_client=lambda _c: FakeClient([GOOD_REPLY]),
        emit=lambda _l: None,
        sleep=waits.append,
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
    ).run()
    assert waits == [2.0]


def test_only_scores_exactly_the_given_roles(store: Store) -> None:
    upload_cv(store, CV_TEXT)
    role: Role = add_role(store, "1", LONG_JD, company="Amazon")  # excluded by rule, still scored
    result = scorer(store, FakeClient([GOOD_REPLY])).run(only=[role])
    assert result.scored == 1
