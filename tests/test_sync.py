from __future__ import annotations

import imaplib
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from flask.testing import FlaskClient

from jobscan import db
from jobscan.config import Config, ConfigError
from jobscan.mailbox import ImapSource, Mail, MailError, parse_mail
from jobscan.models import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SYNC_APPLIED,
    SYNC_REVIEW,
)
from jobscan.store import Store
from jobscan.sync import (
    accept_update,
    discard_update,
    is_due,
    run_sync,
    sync_once,
    sync_problem,
    target_status,
)
from jobscan.sync_rules import (
    STAGE_CLOSED,
    STAGE_INTERVIEW,
    STAGE_OFFER,
    STAGE_RECEIVED,
    STAGE_REJECTED,
    STAGE_UNCLEAR,
    candidates,
    detect_stage,
    pick_role,
)
from jobscan.timeutil import now_ist, to_iso
from jobscan.web import create_app
from tests.test_web import add_role

NOW = now_ist()


def mail(
    subject: str = "Hello",
    body: str = "",
    sender: str = "hr@acme.com",
    *,
    message_id: str = "m1",
    invite: bool = False,
    when: datetime | None = None,
) -> Mail:
    return Mail(message_id, sender, subject, to_iso(when or NOW), body, invite)


class FakeSource:
    def __init__(self, *mails: Mail) -> None:
        self.mails = list(mails)
        self.since: list[datetime] = []

    def fetch(self, since: datetime, progress: object = None) -> list[Mail]:
        self.since.append(since)
        return list(self.mails)


class FakeAI:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.sent.append(user)
        return self.reply


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


@pytest.fixture
def run_id(store: Store) -> int:
    return store.start_run(window_seconds=86400, keywords_total=1)


def applied(store: Store, run_id: int, job_id: str, status: str = "Applied", **fields: str) -> None:
    add_role(store, run_id, job_id, **fields)
    store.update_status([f"LinkedIn|{job_id}"], status)


def status_of(store: Store, job_id: str) -> str:
    role = store.get_role("LinkedIn", job_id)
    assert role is not None
    return role.status


def sync(store: Store, *mails: Mail, config: Config | None = None, ai: FakeAI | None = None):
    return run_sync(store, config or Config(), FakeSource(*mails), lambda c: ai, NOW)  # type: ignore[arg-type,return-value]


# ---- reading the stage -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "body", "stage"),
    [
        ("Interview scheduled with Acme", "", STAGE_INTERVIEW),
        ("Update", "We would like to schedule an interview", STAGE_INTERVIEW),
        ("Update", "Unfortunately we will not be moving forward with your profile", STAGE_REJECTED),
        (
            "Thank you for your interest in Quince",
            "we\u2019ve decided not to move forward with your application for the role",
            STAGE_REJECTED,
        ),
        ("Update", "We are unable to move forward with your candidacy", STAGE_REJECTED),
        ("Update", "We won't be moving forward at this time", STAGE_REJECTED),
        ("Update", "The position has been closed", STAGE_CLOSED),
        ("Offer", "We are pleased to offer you the role", STAGE_OFFER),
        ("Thanks", "Thank you for applying to Acme", STAGE_RECEIVED),
        ("Update", "We will not be moving forward. Schedule an interview? no", STAGE_UNCLEAR),
        ("Weekly newsletter", "jobs you may like", None),
    ],
)
def test_stage_from_wording(subject: str, body: str, stage: str | None) -> None:
    assert detect_stage(mail(subject, body)) == stage


def test_a_calendar_invite_means_interview() -> None:
    assert detect_stage(mail("Invitation: chat", invite=True)) == STAGE_INTERVIEW


@pytest.mark.parametrize(
    ("stage", "current", "target"),
    [
        (STAGE_INTERVIEW, "Applied", "Interview Ongoing"),
        (STAGE_INTERVIEW, "Interview Ongoing", None),
        (STAGE_REJECTED, "Applied", "Initial screening rejected"),
        (STAGE_REJECTED, "Interview Ongoing", "Interview phase rejected"),
        (STAGE_OFFER, "Interview Ongoing", "Interview cleared"),
        (STAGE_CLOSED, "Applied", "Closed"),
        (STAGE_INTERVIEW, "Closed", None),
        (STAGE_REJECTED, "Not Applied", None),
    ],
)
def test_targets_never_move_backwards(stage: str, current: str, target: str | None) -> None:
    assert target_status(stage, current) == target


# ---- which role, and how sure -------------------------------------------------------------------


def test_one_role_matched_by_the_sender_domain_is_high(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme Pvt Ltd")
    roles = store.list_roles()
    found = candidates(mail("Next steps", sender="hr@acme.com"), roles)
    assert [c.signal for c in found] == ["domain"]
    assert pick_role(mail(), found, decided_by_ai=False, ai_sure=True).confidence == CONFIDENCE_HIGH


def test_company_only_in_the_body_is_medium(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    m = mail("Next steps", "About your application to Acme", sender="no-reply@greenhouse.io")
    found = candidates(m, store.list_roles())
    assert found[0].signal == "body"
    assert pick_role(m, found, decided_by_ai=False, ai_sure=True).confidence == CONFIDENCE_MEDIUM


def test_several_roles_without_a_title_clue_is_low(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme", title="Chef")
    applied(store, run_id, "2", company="Acme", title="Pilot")
    m = mail("Next steps")
    found = candidates(m, store.list_roles())
    assert len(found) == 2
    assert pick_role(m, found, decided_by_ai=False, ai_sure=True).confidence == CONFIDENCE_LOW


def test_a_unique_title_among_several_is_medium(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme", title="Technical Program Manager")
    applied(store, run_id, "2", company="Acme", title="Pilot")
    m = mail("Technical Program Manager interview")
    pick = pick_role(m, candidates(m, store.list_roles()), decided_by_ai=False, ai_sure=True)
    assert pick.role.job_id == "1"
    assert pick.confidence == CONFIDENCE_MEDIUM


def test_ai_decisions_are_never_high() -> None:
    from jobscan.sync_rules import _after_ai

    assert _after_ai(CONFIDENCE_HIGH, True, True) == CONFIDENCE_MEDIUM
    assert _after_ai(CONFIDENCE_HIGH, True, False) == CONFIDENCE_LOW
    assert _after_ai(CONFIDENCE_HIGH, False, True) == CONFIDENCE_HIGH


# ---- a sync ---------------------------------------------------------------------------------


def test_a_clear_invite_is_applied_automatically(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    result = sync(store, mail("Interview scheduled", "Round 1 on Monday"))
    assert (result.applied, result.review, result.ignored) == (1, 0, 0)
    assert status_of(store, "1") == "Interview Ongoing"
    row = store.list_sync_updates()[0]
    assert (row.state, row.previous_status, row.new_status) == (
        SYNC_APPLIED,
        "Applied",
        "Interview Ongoing",
    )


def test_a_rejection_waits_for_review_and_changes_nothing(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    result = sync(store, mail("Update", "We will not be moving forward"))
    assert (result.applied, result.review) == (0, 1)
    assert status_of(store, "1") == "Applied"
    assert store.count_sync_review() == 1
    assert store.list_sync_updates()[0].new_status == "Initial screening rejected"


def test_an_invite_matched_only_by_body_waits_for_review(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    sync(store, mail("Invitation", "schedule an interview for Acme", sender="x@greenhouse.io"))
    assert status_of(store, "1") == "Applied"
    assert store.list_sync_updates()[0].confidence == CONFIDENCE_MEDIUM


def test_mail_about_other_companies_or_stages_is_ignored(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    result = sync(
        store,
        mail("Interview", "schedule an interview", sender="hr@other.com", message_id="a"),
        mail("Newsletter", "jobs for you", message_id="b"),
        mail("Thanks", "Thank you for applying to Acme", message_id="c"),
    )
    assert (result.checked, result.ignored, store.list_sync_updates()) == (3, 3, [])


def test_roles_not_applied_to_are_not_candidates(store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", company="Acme")  # still Not Applied
    assert sync(store, mail("Interview", "schedule an interview")).ignored == 1


def test_the_same_mail_is_only_processed_once(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    m = mail("Update", "We will not be moving forward")
    sync(store, m)
    again = sync(store, m)
    assert again.checked == 0
    assert len(store.list_sync_updates()) == 1


def test_a_second_mail_for_the_same_change_is_not_listed_twice(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    sync(
        store,
        mail("Update", "We will not be moving forward", message_id="a"),
        mail("Update 2", "We regret to inform you", message_id="b"),
    )
    assert len(store.list_sync_updates()) == 1


def test_unclear_mail_goes_to_the_ai_and_is_never_high(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    ai = FakeAI('{"stage": "interview", "sure": true}')
    sync(store, mail("Update", "not moving forward but schedule an interview"), ai=ai)
    row = store.list_sync_updates()[0]
    assert row.new_status == "Interview Ongoing"
    assert row.state == SYNC_REVIEW
    assert row.confidence == CONFIDENCE_MEDIUM
    assert "Subject: Update" in ai.sent[0]
    assert "Snippet:" in ai.sent[0]
    assert status_of(store, "1") == "Applied"


def test_unclear_mail_with_the_ai_off_is_left_for_you(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    off = Config.from_raw({"sync_use_ai": "0"})
    ai = FakeAI('{"stage": "interview", "sure": true}')
    sync(store, mail("Update", "not moving forward but schedule an interview"), config=off, ai=ai)
    row = store.list_sync_updates()[0]
    assert (row.new_status, row.confidence, row.state) == ("", CONFIDENCE_LOW, SYNC_REVIEW)
    assert ai.sent == []


def test_an_unreadable_ai_reply_is_left_for_you(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    sync(store, mail("Update", "not moving forward but schedule an interview"), ai=FakeAI("hm"))
    assert store.list_sync_updates()[0].new_status == ""


def test_first_sync_reads_the_configured_days_then_resumes(store: Store, run_id: int) -> None:
    source = FakeSource()
    config = Config.from_raw({"sync_email": "a@gmail.com", "sync_first_days": "10"})
    assert sync_once(store, config, lambda c: source, now=NOW) is not None
    assert abs((NOW - source.since[0]) - timedelta(days=10)) < timedelta(seconds=1)
    sync_once(store, config, lambda c: source, now=NOW + timedelta(hours=5))
    # later syncs start two days before the last good one, so nothing sent late is missed
    assert abs(source.since[1] - (NOW - timedelta(days=2))) < timedelta(seconds=1)


# ---- acting on rows ---------------------------------------------------------------------------


def review_row(store: Store, run_id: int) -> int:
    applied(store, run_id, "1", company="Acme")
    sync(store, mail("Update", "We will not be moving forward"))
    return store.list_sync_updates()[0].id


def test_accept_applies_the_status(store: Store, run_id: int) -> None:
    update_id = review_row(store, run_id)
    accept_update(store, update_id)
    assert status_of(store, "1") == "Initial screening rejected"
    assert store.list_sync_updates()[0].state == "accepted"
    assert store.count_sync_review() == 0


def test_accept_does_not_overwrite_a_status_you_changed(store: Store, run_id: int) -> None:
    update_id = review_row(store, run_id)
    store.update_status(["LinkedIn|1"], "Interview Ongoing")
    accept_update(store, update_id)
    assert status_of(store, "1") == "Interview Ongoing"
    assert store.list_sync_updates()[0].state == "discarded"


def test_discarding_a_proposal_changes_nothing(store: Store, run_id: int) -> None:
    update_id = review_row(store, run_id)
    discard_update(store, update_id)
    assert status_of(store, "1") == "Applied"
    assert store.list_sync_updates()[0].state == "discarded"


def test_undo_restores_the_previous_status(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    sync(store, mail("Interview scheduled", "round 1"))
    update_id = store.list_sync_updates()[0].id
    discard_update(store, update_id)
    assert status_of(store, "1") == "Applied"


def test_undo_leaves_a_status_you_changed_yourself(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    sync(store, mail("Interview scheduled", "round 1"))
    store.update_status(["LinkedIn|1"], "Interview cleared")
    discard_update(store, store.list_sync_updates()[0].id)
    assert status_of(store, "1") == "Interview cleared"


def test_an_unclear_row_cannot_be_accepted(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    sync(store, mail("Update", "not moving forward but schedule an interview"))
    row = store.list_sync_updates()[0]
    assert "nothing to apply" in accept_update(store, row.id)


# ---- bookkeeping -----------------------------------------------------------------------------


def test_a_failed_sync_keeps_the_last_good_time_and_shows_the_reason(store: Store) -> None:
    class Broken:
        def fetch(self, since: datetime, progress: object = None) -> list[Mail]:
            raise MailError("Gmail refused the sign-in.")

    config = Config.from_raw({"sync_email": "a@gmail.com"})
    sync_once(store, config, lambda c: FakeSource(), now=NOW)
    sync_once(store, config, lambda c: Broken(), now=NOW + timedelta(hours=5))  # type: ignore[arg-type,return-value]
    state = store.get_sync_state()
    assert state.error == "Gmail refused the sign-in."
    assert state.last_synced_on == to_iso(NOW)
    assert state.running_since == ""


def test_only_one_sync_runs_at_a_time(store: Store) -> None:
    assert store.begin_sync(now=to_iso(NOW), stale_before=to_iso(NOW - timedelta(minutes=30)))
    assert not store.begin_sync(now=to_iso(NOW), stale_before=to_iso(NOW - timedelta(minutes=30)))
    assert sync_once(store, Config(), lambda c: FakeSource(), now=NOW) is None


def test_scheduling_follows_the_interval() -> None:
    config = Config.from_raw({"sync_email": "a@gmail.com", "sync_every_hours": "4"})
    assert is_due("", config, NOW)
    assert not is_due(to_iso(NOW - timedelta(hours=3)), config, NOW)
    assert is_due(to_iso(NOW - timedelta(hours=4)), config, NOW)
    assert not is_due("", Config(), NOW)


def test_problems_are_explained() -> None:
    assert "Not connected" in sync_problem(Config(), {})
    config = Config.from_raw({"sync_email": "a@gmail.com"})
    assert "JOBSYNC_APP_PASSWORD" in sync_problem(config, {})
    assert sync_problem(config, {"JOBSYNC_APP_PASSWORD": "x"}) == ""


def test_settings_are_validated() -> None:
    with pytest.raises(ConfigError):
        Config.from_raw({"sync_email": "not an email"})
    with pytest.raises(ConfigError):
        Config.from_raw({"sync_every_hours": "0"})
    assert Config.from_raw({"sync_email": ""}).sync_email == ""


# ---- reading Gmail ---------------------------------------------------------------------------

RAW = (
    b"From: Careers <Careers@Acme.com>\r\nSubject: Interview scheduled\r\n"
    b"Date: Mon, 26 Oct 2026 10:00:00 +0530\r\nMessage-ID: <abc@acme>\r\n"
    b"Content-Type: text/plain\r\n\r\nSee you on Monday."
)


def test_a_message_is_parsed() -> None:
    parsed = parse_mail(RAW)
    assert parsed is not None
    assert (parsed.sender, parsed.subject, parsed.message_id) == (
        "careers@acme.com",
        "Interview scheduled",
        "<abc@acme>",
    )
    assert "See you" in parsed.body
    assert parsed.date.startswith("2026-10-26T10:00")


def test_a_calendar_part_is_an_invite() -> None:
    raw = (
        b"From: a@acme.com\r\nSubject: Invitation: Intro\r\n"
        b"Date: Mon, 26 Oct 2026 10:00:00 +0530\r\nMessage-ID: <i@acme>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\n"
        b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nSUMMARY:Intro call\r\nEND:VCALENDAR\r\n"
    )
    parsed = parse_mail(raw)
    assert parsed is not None
    assert parsed.invite
    assert parsed.invite_summary == "Intro call"


def test_a_message_without_a_date_is_skipped() -> None:
    assert parse_mail(b"From: a@b.com\r\nSubject: x\r\n\r\nbody") is None


class FakeImap:
    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, ...]] = []

    def login(self, user: str, password: str) -> None:
        if self.refuse:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, box: str, readonly: bool = False):
        self.calls.append(("select", box, str(readonly)))
        return "OK", [b"1"]

    def search(self, charset: object, *criteria: str):
        self.calls.append(("search", *criteria))
        return "OK", [b"1"]

    def fetch(self, number: bytes, what: str):
        self.calls.append(("fetch", what))
        return "OK", [(b"1 (BODY[] {10}", RAW), b")"]

    def logout(self) -> None:
        self.calls.append(("logout",))


def test_gmail_is_read_without_marking_anything() -> None:
    fake = FakeImap()
    source = ImapSource("a@gmail.com", "pw", connect=lambda host, port: fake)  # type: ignore[arg-type,return-value]
    mails = source.fetch(datetime(2026, 10, 1, tzinfo=NOW.tzinfo))
    assert [m.subject for m in mails] == ["Interview scheduled"]
    assert ("select", "INBOX", "True") in fake.calls
    assert ("search", "SINCE", "01-Oct-2026") in fake.calls
    assert ("fetch", "(BODY.PEEK[])") in fake.calls


def test_a_refused_sign_in_is_explained() -> None:
    source = ImapSource("a@gmail.com", "pw", connect=lambda h, p: FakeImap(refuse=True))  # type: ignore[arg-type,return-value]
    with pytest.raises(MailError, match="refused the sign-in"):
        source.fetch(NOW)


def test_an_unreachable_gmail_is_explained() -> None:
    def down(host: str, port: int) -> imaplib.IMAP4:
        raise OSError("no route")

    with pytest.raises(MailError, match="could not be reached"):
        ImapSource("a@gmail.com", "pw", connect=down).fetch(NOW)


# ---- the web tab -------------------------------------------------------------------------------


@pytest.fixture
def client(store: Store) -> FlaskClient:
    store.set_config_raw(Config.from_raw({"sync_email": "a@gmail.com"}).to_raw())
    source = FakeSource(mail("Update", "We will not be moving forward"))
    return create_app(
        store,
        secret_key="t",
        make_mail_source=lambda c: source,
        make_tailor_client=lambda c: None,  # type: ignore[arg-type,return-value]
    ).test_client()


def test_the_tab_before_setup_explains_what_to_do(store: Store) -> None:
    page = create_app(store, secret_key="t").test_client().get("/sync").get_data(as_text=True)
    assert "Not connected yet" in page
    assert "disabled" in page


def test_sync_now_then_review_then_roles_follow(
    client: FlaskClient, store: Store, run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSYNC_APP_PASSWORD", "pw")
    applied(store, run_id, "1", company="Acme")
    client.post("/sync/run")
    page = client.get("/sync").get_data(as_text=True)
    assert "Review needed" in page
    assert "Initial screening rejected" in page
    assert 'class="badge"' in client.get("/roles").get_data(as_text=True)
    row = store.list_sync_updates()[0]
    client.post(f"/sync/{row.id}/accept")
    assert status_of(store, "1") == "Initial screening rejected"
    assert "Accepted" in client.get("/sync?state=accepted").get_data(as_text=True)
    panel = client.get("/roles/LinkedIn/1?partial=1").get_data(as_text=True)
    assert "Status changed automatically" in panel
    assert "(Email Sync)" in panel


def test_sync_now_without_a_password_explains(client: FlaskClient) -> None:
    page = client.post("/sync/run", follow_redirects=True).get_data(as_text=True)
    assert "app password is missing" in page


def test_filters_and_discard(
    client: FlaskClient, store: Store, run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSYNC_APP_PASSWORD", "pw")
    applied(store, run_id, "1", company="Acme")
    client.post("/sync/run")
    row = store.list_sync_updates()[0]
    client.post(f"/sync/{row.id}/discard")
    assert "Acme" not in client.get("/sync?state=review").get_data(as_text=True)
    assert "Acme" in client.get("/sync?state=discarded").get_data(as_text=True)
    assert "Acme" not in client.get("/sync?portal=Naukri").get_data(as_text=True)


def test_the_reference_email_links_to_that_message_in_gmail(
    client: FlaskClient, store: Store, run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSYNC_APP_PASSWORD", "pw")
    applied(store, run_id, "1", company="Acme")
    client.post("/sync/run")
    page = client.get("/sync").get_data(as_text=True)
    assert "https://mail.google.com/mail/?authuser=a%40gmail.com#search/rfc822msgid:m1" in page
    assert 'target="_blank"' in page


# ---- progress ---------------------------------------------------------------------------------


class ManyImap(FakeImap):
    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    def search(self, charset: object, *criteria: str):
        return "OK", [b" ".join(str(n).encode() for n in range(1, self.count + 1))]


def test_reading_reports_progress_and_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobscan.mailbox as mailbox

    monkeypatch.setattr(mailbox, "MAX_MESSAGES", 25)
    seen: list[tuple[int, int, int]] = []
    source = ImapSource("a@gmail.com", "pw", connect=lambda h, p: ManyImap(30))  # type: ignore[arg-type,return-value]
    source.fetch(NOW, lambda done, total, available: seen.append((done, total, available)))
    assert seen[0] == (0, 25, 30)
    assert seen[-1] == (25, 25, 30)
    assert (10, 25, 30) in seen
    assert (20, 25, 30) in seen


def test_the_cap_is_1500() -> None:
    from jobscan.mailbox import MAX_MESSAGES

    assert MAX_MESSAGES == 1_500


def test_sync_progress_is_saved_while_running_and_cleared_after(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Acme")
    steps: list[tuple[str, int, int]] = []
    original = store.set_sync_progress

    def spy(phase: str, done: int, total: int, available: int = 0) -> None:
        steps.append((phase, done, total))
        original(phase, done, total, available)

    store.set_sync_progress = spy  # type: ignore[method-assign]
    config = Config.from_raw({"sync_email": "a@gmail.com"})
    mails = [mail("Update", "We will not be moving forward", message_id="a")]
    sync_once(store, config, lambda c: FakeSource(*mails), now=NOW)
    assert ("reading", 0, 0) in steps
    assert ("checking", 0, 1) in steps
    state = store.get_sync_state()
    assert (state.phase, state.done, state.total) == ("", 0, 0)


def test_the_tab_shows_the_counter_while_running(store: Store) -> None:
    store.set_config_raw(Config.from_raw({"sync_email": "a@gmail.com"}).to_raw())
    store.begin_sync(now=to_iso(NOW), stale_before="0")
    store.set_sync_progress("reading", 240, 1500, 2100)
    page = create_app(store, secret_key="t").test_client().get("/sync").get_data(as_text=True)
    assert "Reading mail:</strong> 240 of 1500" in page
    assert "cap reached: the newest 1500 of 2100" in page
    store.set_sync_progress("checking", 30, 90)
    page = create_app(store, secret_key="t").test_client().get("/sync").get_data(as_text=True)
    assert "Checking new emails:</strong> 30 of 90" in page


QUINCE = (
    "Thank you for your interest in Quince",
    "Unfortunately after careful review, we\u2019ve decided not to move forward with your "
    "application for the Principal Technical Program Manager role at this time.",
)


def test_quince_style_rejection_is_picked_up(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Quince", title="Principal Technical Program Manager")
    result = sync(store, mail(*QUINCE, sender="no-reply@onequince.com"))
    assert (result.review, result.applied) == (1, 0)
    assert store.list_sync_updates()[0].new_status == "Initial screening rejected"


def test_reread_looks_again_at_mail_already_read(store: Store, run_id: int) -> None:
    applied(store, run_id, "1", company="Quince", title="Principal Technical Program Manager")
    quiet = mail("Weekly news", "nothing here", sender="no-reply@onequince.com", message_id="q1")
    assert sync(store, quiet).review == 0  # read, and remembered as seen
    assert store.seen_messages(["q1"]) == {"q1"}
    source = FakeSource(mail(*QUINCE, sender="no-reply@onequince.com", message_id="q1"))
    assert run_sync(store, Config(), source, lambda c: None, NOW).review == 0  # type: ignore[arg-type,return-value]
    again = run_sync(store, Config(), source, lambda c: None, NOW, reread_days=30)  # type: ignore[arg-type,return-value]
    assert again.review == 1
    assert source.since[-1] <= NOW - timedelta(days=29)
    twice = run_sync(store, Config(), source, lambda c: None, NOW, reread_days=30)  # type: ignore[arg-type,return-value]
    assert twice.review == 0  # the same email never gets a second row
    assert len(store.list_sync_updates()) == 1


def test_reread_button_starts_a_sync(store: Store, client: FlaskClient) -> None:
    reply = client.post("/sync/reread")
    assert reply.status_code == 302
    assert "Re-read last 30 days" in client.get("/sync").get_data(as_text=True)
