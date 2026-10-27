"""Tailored resumes: start a request, prepare proposed changes, apply the ones you tick.

The AI is called once, when changes are prepared. Ticking changes and making the Word file
afterwards use no AI at all.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import db
from .config import Config, ConfigError
from .cover_ai import cv_text, propose_cover, template_text
from .cover_docx import (
    MAX_BULLETS,
    LetterBullet,
    LetterPlan,
    analyse_letter,
    build_letter,
    check_text,
    format_date,
    too_long,
    verify_letter,
)
from .models import (
    TAILOR_COVER,
    TAILOR_DISCARDED,
    TAILOR_RESUME,
    TAILOR_REVIEW,
    TAILOR_STALE,
    CvFile,
)
from .store import Store
from .tailor_ai import Proposal, TailorClient, TailorError, make_client, propose
from .tailor_docx import (
    Converter,
    DocxError,
    EditPlan,
    analyse,
    apply_plan,
    libreoffice_pdf,
    longer_in_pages,
    numbers_in,
    verify_same_shape,
)
from .timeutil import now_ist

COVER_STALE_NOTE = "Your cover letter template was replaced. Start again."
ClientFactory = Callable[[Config], TailorClient]
Launcher = Callable[[int], None]

START_NEW = "started"
START_EXISTING = "existing"
START_NO_CV = "no_cv"
START_NO_TEMPLATE = "no_template"
START_NO_DESCRIPTION = "no_description"
START_NO_ROLE = "no_role"


@dataclass(frozen=True)
class StartResult:
    kind: str
    request_id: int | None = None


def default_client_factory(config: Config) -> TailorClient:
    return make_client(
        config.tailor_base_url, config.tailor_model, config.tailor_key_env, dict(os.environ)
    )


def start_request(store: Store, portal: str, job_id: str, kind: str = TAILOR_RESUME) -> StartResult:
    """Create a request for a role, unless one is already active or it cannot be made."""
    role = store.get_role(portal, job_id)
    if role is None:
        return StartResult(START_NO_ROLE)
    active = store.active_tailor_request(portal, job_id, kind)
    if active is not None:
        return StartResult(START_EXISTING, active.id)
    cv_file = store.get_cv_file()
    if cv_file is None:
        return StartResult(START_NO_CV)
    template = store.get_cover_template() if kind == TAILOR_COVER else None
    if kind == TAILOR_COVER and template is None:
        return StartResult(START_NO_TEMPLATE)
    if not (role.description or "").strip():
        return StartResult(START_NO_DESCRIPTION)
    request_id = store.create_tailor_request(
        portal=portal,
        job_id=job_id,
        company=role.company,
        title=role.title,
        description=role.description or "",
        cv_filename=template.filename if template else cv_file.filename,
        cv_sha=cv_file.sha,
        kind=kind,
        template_sha=template.sha if template else "",
    )
    return StartResult(START_NEW, request_id)


def run_request(
    store: Store, request_id: int, config: Config, make_client_for: ClientFactory
) -> None:
    """Prepare the proposed changes. Any failure is recorded with a reason; nothing is raised."""
    try:
        request = store.get_tailor_request(request_id)
        if request is None:
            return
        cv_file = store.get_cv_file()
        if cv_file is None or cv_file.sha != store.get_tailor_cv_sha(request_id):
            store.set_tailor_failed(request_id, "Your CV was replaced. Start again.")
            return
        structure = analyse(cv_file.data)
        if request.kind == TAILOR_COVER:
            template = store.get_cover_template()
            if template is None or template.sha != store.get_tailor_template_sha(request_id):
                store.set_tailor_failed(request_id, COVER_STALE_NOTE)
                return
            client = make_client_for(config)
            found = propose_cover(
                client,
                structure,
                analyse_letter(template.data),
                request.title,
                request.company,
                request.description,
            )
            store.set_tailor_proposal(request_id, found.to_json(), len(found.changes))
            return
        client = make_client_for(config)
        proposal = propose(client, structure, request.title, request.company, request.description)
        store.set_tailor_proposal(request_id, proposal.to_json(), len(proposal.changes))
    except (TailorError, DocxError) as exc:
        store.set_tailor_failed(request_id, str(exc))
    except Exception as exc:  # never leave a request stuck as "preparing"
        store.set_tailor_failed(request_id, f"Unexpected error: {type(exc).__name__}. Try again.")


def thread_launcher(
    store: Store, make_client_for: ClientFactory = default_client_factory
) -> Launcher:
    """Run preparation in the background so the web app stays usable.

    A file database gets its own connection in the worker thread. An in-memory database
    cannot be shared, so there the work runs straight away.
    """
    path = store.db_path()

    def launch(request_id: int) -> None:
        if path is None:
            _work(store, request_id, make_client_for)
            return

        def target() -> None:
            conn = db.connect(Path(path))
            try:
                _work(Store(conn), request_id, make_client_for)
            finally:
                conn.close()

        threading.Thread(target=target, daemon=True).start()

    return launch


def _work(store: Store, request_id: int, make_client_for: ClientFactory) -> None:
    try:
        config = Config.from_raw(store.get_config_raw())
    except ConfigError:
        config = Config()
    run_request(store, request_id, config, make_client_for)


# ---- applying ---------------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def output_name(cv_filename: str, company: str, title: str) -> str:
    stem = _slug(Path(cv_filename).stem) or "CV"
    parts = [stem, _slug(company), _slug(title)]
    return "_".join(p for p in parts if p)[:120] + ".docx"


def build_plan(proposal: Proposal, selected: Collection[str]) -> tuple[EditPlan, int]:
    """The edit plan for the ticked changes, and how many were applied."""
    summary: str | None = None
    competencies: tuple[str, ...] | None = None
    rewrites: dict[str, str] = {}
    orders: dict[str, tuple[str, ...]] = {}
    count = 0
    for change in proposal.changes:
        if change["id"] not in selected:
            continue
        count += 1
        kind = change["kind"]
        if kind == "summary":
            summary = change["after"]
        elif kind == "competencies":
            competencies = tuple(change["after"])
        elif kind == "bullet":
            rewrites[change["target"]] = change["after"]
        elif kind == "order":
            orders[change["target"]] = tuple(change["after_ids"])
    return EditPlan(summary, competencies, rewrites, orders), count


def generate(
    store: Store,
    request_id: int,
    selected: Collection[str],
    convert: Converter = libreoffice_pdf,
) -> int:
    """Make the Word file from the ticked changes. Raises TailorError and changes nothing."""
    request = store.get_tailor_request(request_id)
    if request is None or request.kind != TAILOR_RESUME or request.status != TAILOR_REVIEW:
        raise TailorError("This request is not waiting for review.")
    cv_file: CvFile | None = store.get_cv_file()
    if cv_file is None or cv_file.sha != store.get_tailor_cv_sha(request_id):
        store.set_tailor_status(request_id, TAILOR_STALE, "Your CV was replaced. Start again.")
        raise TailorError("Your CV was replaced. Start again.")
    plan, count = build_plan(Proposal.from_json(request.proposal), selected)
    if count == 0:
        raise TailorError("Tick at least one change.")
    try:
        edited = apply_plan(cv_file.data, plan)
    except DocxError as exc:
        raise TailorError(str(exc)) from exc
    problem = verify_same_shape(cv_file.data, edited) or longer_in_pages(
        cv_file.data, edited, convert
    )
    if problem:
        raise TailorError(f"Not generated: {problem}. Untick some changes and try again.")
    store.set_tailor_ready(
        request_id,
        json.dumps(sorted(selected)),
        count,
        output_name(request.cv_filename, request.company, request.title),
        edited,
    )
    return count


def discard(store: Store, request_id: int) -> bool:
    request = store.get_tailor_request(request_id)
    if request is None or request.status != TAILOR_REVIEW:
        return False
    store.set_tailor_status(request_id, TAILOR_DISCARDED, "You rejected the proposed changes.")
    return True


# ---- cover letters ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverEdit:
    """One part of the letter as it stands on the review screen."""

    label: str
    text: str
    on: bool = True


def cover_output_name(template_filename: str, company: str, title: str) -> str:
    return output_name(template_filename, company, title)


def generate_cover(
    store: Store,
    request_id: int,
    edits: dict[str, CoverEdit],
    convert: Converter = libreoffice_pdf,
    today: date | None = None,
) -> int:
    """Write the letter from the edited parts. Raises TailorError and changes nothing.

    No AI is used. Returns the number of bullets in the letter.
    """
    request = store.get_tailor_request(request_id)
    if request is None or request.kind != TAILOR_COVER or request.status != TAILOR_REVIEW:
        raise TailorError("This request is not waiting for review.")
    cv_file = store.get_cv_file()
    template = store.get_cover_template()
    if cv_file is None or cv_file.sha != store.get_tailor_cv_sha(request_id):
        store.set_tailor_status(request_id, TAILOR_STALE, "Your CV was replaced. Start again.")
        raise TailorError("Your CV was replaced. Start again.")
    if template is None or template.sha != store.get_tailor_template_sha(request_id):
        store.set_tailor_status(request_id, TAILOR_STALE, COVER_STALE_NOTE)
        raise TailorError(COVER_STALE_NOTE)
    try:
        structure = analyse(cv_file.data)
        layout = analyse_letter(template.data)
    except DocxError as exc:
        raise TailorError(str(exc)) from exc
    allowed = numbers_in(cv_text(structure) + "\n" + template_text(layout))
    proposal = Proposal.from_json(request.proposal)

    parts: dict[str, str] = {}
    bullets: list[LetterBullet] = []
    chosen: list[str] = []
    for change in proposal.changes:
        edit = edits.get(change["id"])
        if edit is None:
            edit = CoverEdit(change.get("after_label", ""), change["after"])
        kind = change["kind"]
        if kind == "bullet" and not edit.on:
            continue
        label = edit.label.strip().rstrip(":").strip()
        text = edit.text.strip()
        key = {"lead_in": "lead", "bullet": "text"}.get(kind, kind)
        name = {"opening": "The opening", "lead_in": "The lead-in line", "closing": "The closing"}
        what = name.get(kind, f"Bullet {len(bullets) + 1}")
        if kind == "bullet":
            problem = check_text("label", label, allowed)
            if problem:
                raise TailorError(f"{what} label was not accepted: {problem}.")
        problem = check_text(key, text, allowed)
        if problem:
            raise TailorError(f"{what} was not accepted: {problem}.")
        chosen.append(change["id"])
        if kind == "bullet":
            bullets.append(LetterBullet(label, text))
        else:
            parts[kind] = text
    if not bullets:
        raise TailorError("Keep at least one bullet.")
    if len(bullets) > MAX_BULLETS:
        raise TailorError(f"Use at most {MAX_BULLETS} bullets.")
    plan = LetterPlan(
        opening=parts["opening"],
        lead_in=parts.get("lead_in", ""),
        bullets=tuple(bullets),
        closing=parts["closing"],
        date_text=format_date(today or now_ist().date()),
        company_text=request.company,
    )
    try:
        edited = build_letter(template.data, plan)
    except DocxError as exc:
        raise TailorError(str(exc)) from exc
    problem = verify_letter(template.data, edited) or too_long(plan, edited, convert)
    if problem:
        raise TailorError(f"Not generated: {problem}. Shorten the text or untick bullets.")
    store.set_tailor_ready(
        request_id,
        json.dumps(
            {"ids": sorted(chosen), "parts": parts, "bullets": [b.__dict__ for b in bullets]}
        ),
        len(bullets),
        cover_output_name(request.cv_filename, request.company, request.title),
        edited,
    )
    return len(bullets)
