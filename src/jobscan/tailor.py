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
from pathlib import Path

from . import db
from .config import Config, ConfigError
from .models import TAILOR_DISCARDED, TAILOR_REVIEW, TAILOR_STALE, CvFile
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
    verify_same_shape,
)

ClientFactory = Callable[[Config], TailorClient]
Launcher = Callable[[int], None]

START_NEW = "started"
START_EXISTING = "existing"
START_NO_CV = "no_cv"
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


def start_request(store: Store, portal: str, job_id: str) -> StartResult:
    """Create a request for a role, unless one is already active or it cannot be made."""
    role = store.get_role(portal, job_id)
    if role is None:
        return StartResult(START_NO_ROLE)
    active = store.active_tailor_request(portal, job_id)
    if active is not None:
        return StartResult(START_EXISTING, active.id)
    cv_file = store.get_cv_file()
    if cv_file is None:
        return StartResult(START_NO_CV)
    if not (role.description or "").strip():
        return StartResult(START_NO_DESCRIPTION)
    request_id = store.create_tailor_request(
        portal=portal,
        job_id=job_id,
        company=role.company,
        title=role.title,
        description=role.description or "",
        cv_filename=cv_file.filename,
        cv_sha=cv_file.sha,
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
    if request is None or request.status != TAILOR_REVIEW:
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
