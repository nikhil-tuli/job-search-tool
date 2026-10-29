"""Email Sync: read recent mail, work out which roles it is about, update their status.

Only moves into Interview Ongoing are applied without asking, and only when the match and the
wording are both clear. Everything else waits in the Email Sync tab. A role is never moved
backwards. See docs/REQUIREMENTS.md, Round 11.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .config import Config, ConfigError
from .mailbox import ImapSource, Mail, MailError, MailSource
from .models import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    SYNC_ACCEPTED,
    SYNC_APPLIED,
    SYNC_DISCARDED,
    SYNC_REVIEW,
    Role,
    RoleFilter,
)
from .store import Store, StoreError
from .sync_rules import (
    STAGE_CLOSED,
    STAGE_INTERVIEW,
    STAGE_OFFER,
    STAGE_RECEIVED,
    STAGE_REJECTED,
    STAGE_UNCLEAR,
    STAGES,
    candidates,
    detect_stage,
    pick_role,
)
from .tailor import ClientFactory, default_client_factory
from .tailor_ai import TailorError
from .timeutil import from_iso, now_ist, to_iso

OPEN_STATUSES = ("Applied", "Interview Ongoing")
STUCK_AFTER = timedelta(minutes=30)
SNIPPET_CHARS = 400
CHECK_EVERY = 5

AI_PROMPT = """You read one email about a job application. Reply with a single JSON object and \
nothing else: {"stage": "interview" | "rejected" | "offer" | "closed" | "none", "sure": true or \
false}.
interview = an invitation to an interview or a call, or the next round. rejected = the candidate \
is not moving forward. offer = a job offer. closed = the role is closed, filled or on hold. \
none = anything else. Set "sure" to false when the email is vague."""


class SyncSetupError(Exception):
    """Sync cannot start. The message says what to do."""


@dataclass
class SyncResult:
    checked: int = 0
    ignored: int = 0
    review: int = 0
    applied: int = 0
    notes: list[str] = field(default_factory=list)


MailSourceFactory = Callable[[Config], MailSource]


def sync_problem(config: Config, environ: Mapping[str, str] | None = None) -> str:
    """Why sync cannot run yet, or an empty string."""
    env = os.environ if environ is None else environ
    if not config.sync_email:
        return (
            "Not connected yet. Add your Gmail address under Config, Email Sync, and keep "
            f"a Gmail app password in the terminal variable {config.sync_password_env}."
        )
    if not env.get(config.sync_password_env, "").strip():
        return (
            f"The app password is missing. Set {config.sync_password_env} in the terminal, then "
            "start jobscan serve from that terminal."
        )
    return ""


def default_source_factory(config: Config) -> MailSource:
    problem = sync_problem(config)
    if problem:
        raise SyncSetupError(problem)
    return ImapSource(config.sync_email, os.environ[config.sync_password_env].strip())


# ---- what an email means -------------------------------------------------------------------


def target_status(stage: str, current: str) -> str | None:
    """The status a stage leads to from the current one, or None when nothing should change."""
    if current not in OPEN_STATUSES:
        return None
    if stage == STAGE_INTERVIEW:
        return "Interview Ongoing" if current == "Applied" else None
    if stage == STAGE_REJECTED:
        return "Initial screening rejected" if current == "Applied" else "Interview phase rejected"
    if stage == STAGE_OFFER:
        return "Interview cleared"
    if stage == STAGE_CLOSED:
        return "Closed"
    return None


def ask_ai(client: object, mail: Mail) -> tuple[str | None, bool]:
    """(stage or None, sure). Only the sender, subject and a short snippet are sent."""
    complete = getattr(client, "complete")  # noqa: B009
    message = f"From: {mail.sender}\nSubject: {mail.subject}\nSnippet: {mail.body[:SNIPPET_CHARS]}"
    try:
        reply = complete(AI_PROMPT, message)
        start, end = reply.find("{"), reply.rfind("}")
        data = json.loads(reply[start : end + 1])
        stage = data.get("stage")
        return (stage if stage in STAGES else None), bool(data.get("sure"))
    except (TailorError, ValueError, TypeError, AttributeError):
        return None, False


@dataclass
class _Decision:
    mail: Mail
    stage: str  # a stage, or "" when the email could not be understood
    role: Role
    confidence: str


def _decide(
    mail: Mail,
    roles: list[Role],
    use_ai: bool,
    get_client: Callable[[], object | None],
) -> _Decision | None:
    stage = detect_stage(mail)
    if stage is None or stage == STAGE_RECEIVED:
        return None
    found = candidates(mail, roles)
    if not found:
        return None
    decided_by_ai, sure = False, True
    if stage == STAGE_UNCLEAR:
        client = get_client() if use_ai else None
        answer: str | None = None
        if client is not None:
            decided_by_ai = True
            answer, sure = ask_ai(client, mail)
        if answer is None:
            match = pick_role(mail, found, decided_by_ai=False, ai_sure=True)
            return _Decision(mail, "", match.role, CONFIDENCE_LOW)
        stage = answer
    match = pick_role(mail, found, decided_by_ai=decided_by_ai, ai_sure=sure)
    return _Decision(mail, stage, match.role, match.confidence)


# ---- one sync --------------------------------------------------------------------------------


def run_sync(
    store: Store,
    config: Config,
    source: MailSource,
    make_client: ClientFactory = default_client_factory,
    now: datetime | None = None,
    reread_days: int = 0,
) -> SyncResult:
    moment = now or now_ist()
    state = store.get_sync_state()
    if reread_days:
        since = moment - timedelta(days=reread_days)
        store.forget_seen_since(to_iso(since))  # mail already read is looked at again
    elif state.last_synced_on:
        since = from_iso(state.last_synced_on) - timedelta(days=2)
    else:
        since = moment - timedelta(days=config.sync_first_days)
    store.set_sync_progress("reading", 0, 0)

    def reading(done: int, total: int, available: int) -> None:
        store.set_sync_progress("reading", done, total, available)

    mails = source.fetch(since, reading)
    seen = store.seen_messages([m.message_id for m in mails])
    fresh = sorted((m for m in mails if m.message_id not in seen), key=lambda m: m.date)
    result = SyncResult(checked=len(fresh))
    roles = store.list_roles(RoleFilter(statuses=OPEN_STATUSES))

    client_box: list[object | None] = []

    def get_client() -> object | None:
        if not client_box:
            try:
                client_box.append(make_client(config))
            except TailorError as exc:
                client_box.append(None)
                result.notes.append(f"The AI was not used: {exc}")
        return client_box[0]

    decisions: list[_Decision | None] = []
    for index, mail in enumerate(fresh, 1):
        if index == 1 or index % CHECK_EVERY == 0:
            store.set_sync_progress("checking", index - 1, len(fresh))
        decisions.append(_decide(mail, roles, bool(config.sync_use_ai), get_client))

    when = to_iso(moment)
    with store.transaction():
        for decision in decisions:
            if decision is None or not _apply_decision(store, decision, when, result):
                result.ignored += 1
        store.mark_messages_seen([m.message_id for m in fresh], when=when)
    return result


def _apply_decision(store: Store, decision: _Decision, when: str, result: SyncResult) -> bool:
    """Record one decision. False when there was nothing to record."""
    role = store.get_role(decision.role.portal, decision.role.job_id)
    if role is None or role.status not in OPEN_STATUSES:
        return False
    new_status = target_status(decision.stage, role.status) if decision.stage else ""
    if new_status is None:
        return False
    if new_status and store.has_pending_sync(role.portal, role.job_id, new_status):
        return False
    automatic = new_status == "Interview Ongoing" and decision.confidence == CONFIDENCE_HIGH
    note = "" if decision.stage else "The email could not be understood. Please check it."
    update_id = store.create_sync_update(
        portal=role.portal,
        job_id=role.job_id,
        company=role.company,
        title=role.title,
        previous_status=role.status,
        new_status=new_status,
        subject=decision.mail.subject,
        sender=decision.mail.sender,
        email_date=decision.mail.date,
        message_id=decision.mail.message_id,
        confidence=decision.confidence if decision.stage else CONFIDENCE_LOW,
        state=SYNC_APPLIED if automatic else SYNC_REVIEW,
        when=when,
        note=note,
    )
    if update_id is None:
        return False
    if automatic:
        store.update_status([role.key], new_status)
        result.applied += 1
    else:
        result.review += 1
    return True


def sync_once(
    store: Store,
    config: Config,
    make_source: MailSourceFactory = default_source_factory,
    make_client: ClientFactory = default_client_factory,
    now: datetime | None = None,
    reread_days: int = 0,
) -> SyncResult | None:
    """Run one sync with the guard and the bookkeeping. None when it did not run.

    `reread_days` looks at all mail of the last days again, including mail read before.
    """
    moment = now or now_ist()
    if not store.begin_sync(now=to_iso(moment), stale_before=to_iso(moment - STUCK_AFTER)):
        return None
    result = SyncResult()
    error = ""
    try:
        result = run_sync(store, config, make_source(config), make_client, moment, reread_days)
    except (MailError, SyncSetupError) as exc:
        error = str(exc)
    except StoreError as exc:
        error = f"Could not save: {exc}"
    except Exception as exc:  # never leave the sync marked as running
        error = f"Unexpected error: {type(exc).__name__}. Try again."
    store.finish_sync(
        now=to_iso(now_ist() if now is None else moment),
        error=error,
        checked=result.checked,
        ignored=result.ignored,
    )
    return result


# ---- acting on rows --------------------------------------------------------------------------


def accept_update(store: Store, update_id: int, now: datetime | None = None) -> str:
    """Apply a proposed change. Returns a short message for the page."""
    row = store.get_sync_update(update_id)
    when = to_iso(now or now_ist())
    if row is None or row.state != SYNC_REVIEW:
        return "This row is not waiting for review."
    if not row.new_status:
        return "There is nothing to apply. Discard the row once you have checked the email."
    role = store.get_role(row.portal, row.job_id)
    if role is None or role.status != row.previous_status:
        store.set_sync_state_of(
            update_id,
            SYNC_DISCARDED,
            when=when,
            note="The status changed since this was proposed, so it was left as it is.",
        )
        return "The role's status changed since, so nothing was applied."
    with store.transaction():
        store.update_status([role.key], row.new_status)
        store.set_sync_state_of(update_id, SYNC_ACCEPTED, when=when)
    return f"Status set to {row.new_status}."


def discard_update(store: Store, update_id: int, now: datetime | None = None) -> str:
    """Discard a proposal, or undo an automatic change (restoring the previous status)."""
    row = store.get_sync_update(update_id)
    when = to_iso(now or now_ist())
    if row is None or row.state not in (SYNC_REVIEW, SYNC_APPLIED):
        return "This row cannot be discarded."
    if row.state == SYNC_REVIEW:
        store.set_sync_state_of(update_id, SYNC_DISCARDED, when=when)
        return "Discarded. Nothing was changed."
    role = store.get_role(row.portal, row.job_id)
    with store.transaction():
        if role is not None and role.status == row.new_status:
            store.update_status([role.key], row.previous_status)
            store.set_sync_state_of(update_id, SYNC_DISCARDED, when=when)
            return f"Status restored to {row.previous_status}."
        store.set_sync_state_of(
            update_id,
            SYNC_DISCARDED,
            when=when,
            note="You had changed the status yourself, so it was left as it is.",
        )
    return "Marked discarded. The status was left as you set it."


# ---- running in the background ---------------------------------------------------------------


def is_due(state_last: str, config: Config, now: datetime) -> bool:
    """Whether the scheduled sync should run now."""
    if not config.sync_email:
        return False
    if not state_last:
        return True
    return now - from_iso(state_last) >= timedelta(hours=config.sync_every_hours)


def start_in_background(
    store: Store,
    make_source: MailSourceFactory = default_source_factory,
    make_client: ClientFactory = default_client_factory,
    reread_days: int = 0,
) -> None:
    """Run one sync without blocking the page. An in-memory database runs it straight away."""
    path = store.db_path()
    config = _config_of(store)
    if path is None:
        sync_once(store, config, make_source, make_client, reread_days=reread_days)
        return

    def target() -> None:
        conn = db.connect(Path(path))
        try:
            own = Store(conn)
            sync_once(own, _config_of(own), make_source, make_client, reread_days=reread_days)
        finally:
            conn.close()

    threading.Thread(target=target, daemon=True).start()


def _config_of(store: Store) -> Config:
    try:
        return Config.from_raw(store.get_config_raw())
    except ConfigError:
        return Config()


def run_scheduler(
    db_path: Path,
    make_source: MailSourceFactory = default_source_factory,
    make_client: ClientFactory = default_client_factory,
    *,
    poll_seconds: float = 60,
    stop: threading.Event | None = None,
) -> None:
    """Check every minute whether a sync is due. Meant to run in a daemon thread."""
    done = stop or threading.Event()
    while not done.is_set():
        try:
            conn = db.connect(db_path)
            try:
                store = Store(conn)
                config = _config_of(store)
                if is_due(store.get_sync_state().last_synced_on, config, now_ist()) and not (
                    sync_problem(config)
                ):
                    sync_once(store, config, make_source, make_client)
            finally:
                conn.close()
        except Exception:  # the scheduler must never die
            pass
        done.wait(poll_seconds)


def start_scheduler(db_path: Path) -> threading.Event:
    stop = threading.Event()
    threading.Thread(
        target=run_scheduler, args=(db_path,), kwargs={"stop": stop}, daemon=True
    ).start()
    return stop


__all__ = [
    "CONFIDENCE_LOW",
    "SYNC_ACCEPTED",
    "SyncResult",
    "SyncSetupError",
    "accept_update",
    "discard_update",
    "is_due",
    "run_scheduler",
    "run_sync",
    "start_in_background",
    "start_scheduler",
    "sync_once",
    "sync_problem",
]
