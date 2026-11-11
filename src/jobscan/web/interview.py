"""Interview Prep pages and JSON endpoints. Registered by create_app."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.wrappers import Response as WerkzeugResponse

from ..ai_fit import Completer
from ..config import Config
from ..interview import questions as qs
from ..interview import rules as rl
from ..interview import service as sv
from ..interview import store as st
from ..interview import views as vw
from ..interview.stt import MAX_AUDIO_BYTES, Transcriber
from ..models import RoleFilter
from ..store import Store
from ..timeutil import now_ist, to_iso

ROLE_SEARCH_LIMIT = 20
KIND_LABELS = {
    qs.COMPANY_ROLE: "Company role",
    qs.GENERAL_ROLE: "General role",
    qs.MY_QUESTIONS: "My questions",
}
STATUS_CLASS = {
    st.IN_PROGRESS: "st-acc",
    st.PREPARING: "st-wait",
    st.READY: "st-ready",
    st.INCOMPLETE: "st-review",
    st.FAILED: "st-fail",
}


def _role_key(value: str) -> tuple[str, str]:
    portal, _, job_id = value.partition("|")
    return portal, job_id


def register(
    app: Flask,
    store: Store,
    *,
    make_ai: Callable[[Config], Completer],
    make_transcriber: Callable[[Config], Transcriber],
    now: Callable[[], str] | None = None,
    launch: sv.Launcher | None = None,
    pacer: sv.Pacer | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    clock = now or (lambda: to_iso(now_ist()))
    run_job = launch or sv.thread_runner(store)
    env = sv.Env(make_ai=make_ai, pacer=pacer or sv.Pacer(), sleep=sleep, now=clock)
    # Why question writing failed, by interview id. The interview is deleted when it fails,
    # so this is the only place the reason survives. Memory only; it holds no interview content.
    failures: dict[int, str] = {}
    store.fail_stale_interviews(clock())

    def config() -> Config:
        return sv.load_config(store)

    # ---- helpers ----------------------------------------------------------------------------

    def start_background_questions(interview_id: int, cfg: Config) -> None:
        def job(s: Store) -> None:
            message = sv.write_questions(s, env, cfg, interview_id)
            if message:
                failures[interview_id] = message

        run_job(job)

    def start_background_feedback(interview_id: int, cfg: Config) -> None:
        def job(s: Store) -> None:
            sv.build_feedback(s, env, cfg, interview_id)

        run_job(job)

    def begin(
        kind: str,
        *,
        role_key: str = "",
        general_title: str = "",
        own: list[str] | None = None,
        read_aloud: bool = True,
    ) -> int:
        """Create an interview and start writing its questions. Raises InterviewError."""
        cfg = config()
        role = None
        if kind == qs.COMPANY_ROLE:
            portal, job_id = _role_key(role_key)
            role = store.get_role(portal, job_id)
        interview_id = sv.create_interview(
            store,
            cfg,
            store.get_cv(),
            kind=kind,
            now=clock(),
            role=role,
            general_title=general_title,
            own=own,
            read_aloud=read_aloud,
        )
        if kind != qs.MY_QUESTIONS:
            start_background_questions(interview_id, cfg)
        return interview_id

    def get_row(interview_id: int) -> st.InterviewRow:
        row = store.get_interview(interview_id)
        if row is None:
            abort(404, "Interview not found")
        return row

    def json_error(message: str, code: int = 400) -> tuple[Response, int]:
        return jsonify(error=message), code

    def tab_context(tab: str) -> dict[str, Any]:
        return {"tab": tab, "running": store.running_interview()}

    # ---- start ------------------------------------------------------------------------------

    @app.get("/interview")
    def interview_start() -> str:
        cfg = config()
        cv = store.get_cv()
        selected = None
        if request.args.get("role"):
            portal, job_id = _role_key(request.args["role"])
            selected = store.get_role(portal, job_id)
        kind = request.args.get("kind", "")
        if selected is not None:
            kind = qs.COMPANY_ROLE
        return render_template(
            "interview_start.html",
            cv=cv if cv is not None and cv.text.strip() else None,
            selected=selected,
            kind=kind if kind in qs.INTERVIEW_TYPES else "",
            general_title=request.args.get("title", ""),
            roles=cfg.interview_role_list,
            read_aloud=cfg.interview_read_aloud == 1,
            max_questions=qs.MAX_OWN_QUESTIONS,
            own_min=qs.MIN_OWN_CHARS,
            own_max=qs.MAX_OWN_CHARS,
            **tab_context("online"),
        )

    @app.get("/interview/roles")
    def interview_roles() -> Response:
        query = request.args.get("q", "").strip()
        roles = store.list_roles(RoleFilter(query=query), limit=ROLE_SEARCH_LIMIT)
        return jsonify(
            roles=[
                {"key": f"{r.portal}|{r.job_id}", "title": r.title, "company": r.company}
                for r in roles
            ]
        )

    @app.post("/interview")
    def interview_create() -> WerkzeugResponse:
        kind = request.form.get("kind", "")
        own = request.form.getlist("q")
        try:
            interview_id = begin(
                kind,
                role_key=request.form.get("role", ""),
                general_title=request.form.get("general_title", ""),
                own=own,
                read_aloud=request.form.get("read_aloud") == "1",
            )
        except sv.InterviewError as exc:
            flash(str(exc), "error")
            return redirect(url_for("interview_start", kind=kind or None))
        return redirect(url_for("interview_run", interview_id=interview_id))

    @app.post("/interview/<int:interview_id>/again")
    def interview_again(interview_id: int) -> WerkzeugResponse:
        old = get_row(interview_id)
        own = (
            [q.text for q in store.get_questions(interview_id)]
            if old.kind == qs.MY_QUESTIONS
            else None
        )
        try:
            new_id = begin(
                old.kind,
                role_key=f"{old.role_portal}|{old.role_job_id}",
                general_title=old.general_title,
                own=own,
                read_aloud=old.read_aloud,
            )
        except sv.InterviewError as exc:
            flash(str(exc), "error")
            return redirect(url_for("interview_page", interview_id=interview_id))
        return redirect(url_for("interview_run", interview_id=new_id))

    # ---- running ----------------------------------------------------------------------------

    @app.get("/interview/<int:interview_id>/status")
    def interview_status(interview_id: int) -> Response:
        row = store.get_interview(interview_id)
        if row is None:
            reason = failures.get(interview_id)
            if reason:
                return jsonify(state="error", error=reason)
            return jsonify(state="gone", error="This interview no longer exists.")
        if row.status == st.IN_PROGRESS:
            return jsonify(state="writing" if row.questions == 0 else "ready")
        return jsonify(state=row.status.lower(), stage=row.stage, error=row.error)

    @app.get("/interview/<int:interview_id>/run")
    def interview_run(interview_id: int) -> str | WerkzeugResponse:
        row = store.get_interview(interview_id)
        if row is None:
            reason = failures.pop(interview_id, "")
            if reason:
                flash(reason, "error")
                return redirect(url_for("interview_start"))
            abort(404, "Interview not found")
        if row.status != st.IN_PROGRESS:
            return redirect(url_for("interview_page", interview_id=interview_id))
        cfg = config()
        questions = store.get_questions(interview_id)
        return render_template(
            "interview_run.html",
            row=row,
            writing=not questions,
            settings={
                "id": interview_id,
                "questions": [{"idx": q.idx, "text": q.text} for q in questions],
                "readAloud": row.read_aloud,
                "amber": cfg.interview_amber_s,
                "over": cfg.interview_over_s,
                "stop": cfg.interview_stop_s,
                "maxBytes": MAX_AUDIO_BYTES,
                "urls": {
                    "status": url_for("interview_status", interview_id=interview_id),
                    "answer": url_for("interview_answer", interview_id=interview_id, idx=0),
                    "skip": url_for("interview_skip", interview_id=interview_id, idx=0),
                    "notScored": url_for("interview_not_scored", interview_id=interview_id, idx=0),
                    "finish": url_for("interview_finish", interview_id=interview_id),
                    "abandon": url_for("interview_abandon", interview_id=interview_id),
                    "start": url_for("interview_start"),
                },
            },
            **tab_context("online"),
        )

    @app.post("/interview/<int:interview_id>/answer/<int:idx>")
    def interview_answer(interview_id: int, idx: int) -> Any:
        request.max_content_length = MAX_AUDIO_BYTES + 1024 * 1024
        upload = request.files.get("audio")
        if upload is None:
            return json_error("No recording was sent.")
        try:
            duration = float(request.form.get("duration", "0"))
        except ValueError:
            duration = 0.0
        try:
            outcome = sv.submit_answer(
                store,
                make_transcriber(config()),
                interview_id,
                idx,
                audio=upload.read(),
                filename=upload.filename or "answer.webm",
                content_type=upload.mimetype or "",
                duration_s=duration,
                now=clock(),
            )
        except sv.InterviewError as exc:
            return json_error(str(exc))
        except Exception as exc:  # a missing key is a plain message, not a crash page
            return json_error(str(exc) or "The recording could not be sent.", 503)
        return jsonify(
            status=outcome.status,
            words=outcome.words,
            retry_after=outcome.retry_after,
            error=outcome.error,
        )

    @app.post("/interview/<int:interview_id>/skip/<int:idx>")
    def interview_skip(interview_id: int, idx: int) -> Any:
        try:
            sv.skip_answer(store, interview_id, idx, now=clock())
        except sv.InterviewError as exc:
            return json_error(str(exc))
        return jsonify(status=st.SKIPPED)

    @app.post("/interview/<int:interview_id>/not-scored/<int:idx>")
    def interview_not_scored(interview_id: int, idx: int) -> Any:
        try:
            sv.mark_not_scored(
                store, interview_id, idx, now=clock(), note=request.form.get("note", "")
            )
        except sv.InterviewError as exc:
            return json_error(str(exc))
        return jsonify(status=st.NOT_SCORED)

    @app.post("/interview/<int:interview_id>/finish")
    def interview_finish(interview_id: int) -> Any:
        try:
            result = sv.finish_interview(store, interview_id, now=clock())
        except sv.InterviewError as exc:
            return json_error(str(exc))
        if result == sv.DISCARDED:
            return jsonify(discarded=True, next=url_for("interview_history"))
        start_background_feedback(interview_id, config())
        return jsonify(discarded=False, next=url_for("interview_page", interview_id=interview_id))

    @app.post("/interview/<int:interview_id>/abandon")
    def interview_abandon(interview_id: int) -> tuple[str, int]:
        sv.abandon_interview(store, interview_id, now=clock())
        return "", 204

    # ---- feedback ---------------------------------------------------------------------------

    def feedback_blocks(
        row: st.InterviewRow, fb: st.FeedbackRow
    ) -> tuple[list[dict[str, Any]], dict[str, float]]:
        questions = store.get_questions(row.id)
        answers = {a.idx: a for a in store.get_answers(row.id)}
        by_idx = {int(a["idx"]): a for a in fb.answers}  # type: ignore[call-overload]
        thumbs = store.get_thumbs(row.id)
        blocks: list[dict[str, Any]] = []
        totals: dict[str, list[int]] = {}
        for q in questions:
            answer = answers.get(q.idx)
            block: dict[str, Any] = {
                "idx": q.idx,
                "text": q.text,
                "kind": q.kind,
                "answer": answer,
                "thumb": thumbs.get(q.idx, 0),
                "feedback": None,
                "length": vw.minutes_seconds(answer.duration_s)
                if answer and answer.duration_s
                else "",
            }
            data = by_idx.get(q.idx)
            if data is not None:
                for rating in data["ratings"]:  # type: ignore[attr-defined]
                    totals.setdefault(rating["key"], []).append(int(rating["score"]))
                block["feedback"] = data
                block["band"] = vw.answer_band(int(data["score"]))  # type: ignore[call-overload]
                block["band_class"] = vw.band_class(block["band"])
            blocks.append(block)
        averages = {k: sum(v) / len(v) for k, v in totals.items()}
        return blocks, averages

    def feedback_page(row: st.InterviewRow) -> str:
        fb = store.get_feedback(row.id)
        if fb is None:
            abort(404, "Feedback not found")
        cards, spoken_score = vw.spoken_cards(fb.spoken)
        previous = store.previous_interview(row)
        blocks, averages = feedback_blocks(row, fb)
        weakest = min(averages, key=lambda k: averages[k]) if averages else ""
        answer_change = vw.change_text(fb.answer_score, previous.answer_score if previous else None)
        spoken_change = vw.change_text(spoken_score, previous.spoken_score if previous else None)
        worst = next(
            (
                c
                for c in sorted(cards, key=lambda c: rl.RATING_ORDER[c.rating])
                if c.rating != rl.GOOD
            ),
            None,
        )
        return render_template(
            "interview_feedback.html",
            row=row,
            fb=fb,
            cards=cards,
            spoken_score=spoken_score,
            spoken_band=rl.band_of(spoken_score) if spoken_score is not None else "",
            spoken_band_class=vw.band_class(rl.band_of(spoken_score))
            if spoken_score is not None
            else "",
            spoken_change=spoken_change,
            spoken_change_class=vw.change_class(spoken_change),
            answer_band=rl.band_of(fb.answer_score) if fb.answer_score is not None else "",
            answer_band_class=vw.band_class(rl.band_of(fb.answer_score))
            if fb.answer_score is not None
            else "",
            answer_change=answer_change,
            answer_change_class=vw.change_class(answer_change),
            worst=worst,
            weakest=vw.aspect_label(weakest) if weakest else "",
            weakest_score=round(averages[weakest]) if weakest else 0,
            blocks=blocks,
            examples=vw.EXAMPLES,
            aspect_label=vw.aspect_label,
            spoken_tip=vw.SPOKEN_LABEL_TIP,
            answer_tip=vw.ANSWER_LABEL_TIP,
            kind_label=KIND_LABELS[row.kind],
            role_exists=bool(row.role_portal and store.get_role(row.role_portal, row.role_job_id)),
            min_words=rl.MIN_SPOKEN_WORDS,
            **tab_context("history"),
        )

    @app.get("/interview/<int:interview_id>")
    def interview_page(interview_id: int) -> str | WerkzeugResponse:
        row = get_row(interview_id)
        if row.status == st.IN_PROGRESS:
            return redirect(url_for("interview_run", interview_id=interview_id))
        if row.status == st.READY:
            return feedback_page(row)
        return render_template(
            "interview_wait.html",
            row=row,
            kind_label=KIND_LABELS[row.kind],
            status_url=url_for("interview_status", interview_id=interview_id),
            **tab_context("history"),
        )

    @app.post("/interview/<int:interview_id>/retry")
    def interview_retry(interview_id: int) -> WerkzeugResponse:
        try:
            result = sv.retry_feedback(store, interview_id, now=clock())
        except sv.InterviewError as exc:
            flash(str(exc), "error")
            return redirect(url_for("interview_history"))
        if result == sv.DISCARDED:
            flash("Nothing to score. The interview was not saved.", "error")
            return redirect(url_for("interview_history"))
        start_background_feedback(interview_id, config())
        return redirect(url_for("interview_page", interview_id=interview_id))

    @app.post("/interview/<int:interview_id>/thumb")
    def interview_thumb(interview_id: int) -> WerkzeugResponse:
        get_row(interview_id)
        try:
            idx = int(request.form.get("idx", ""))
            value = int(request.form.get("value", ""))
        except ValueError:
            abort(400, "Invalid thumb")
        if value not in (-1, 0, 1):
            abort(400, "Invalid thumb")
        store.set_thumb(interview_id, idx, value)
        return redirect(url_for("interview_page", interview_id=interview_id, _anchor=f"q{idx}"))

    @app.post("/interview/<int:interview_id>/delete")
    def interview_delete(interview_id: int) -> WerkzeugResponse:
        row = get_row(interview_id)
        if row.status in (st.PREPARING, st.IN_PROGRESS):
            flash("Wait until this interview has finished before deleting it.", "error")
        else:
            store.delete_interview(interview_id)
            flash("Interview deleted.", "ok")
        return redirect(url_for("interview_history"))

    # ---- history ----------------------------------------------------------------------------

    @app.get("/interview/history")
    def interview_history() -> str:
        cutoff = to_iso(now_ist() - timedelta(minutes=st.STALE_AFTER_MINUTES))
        store.expire_stale_interviews(cutoff, clock())
        kind = request.args.get("type", "")
        kind = kind if kind in qs.INTERVIEW_TYPES else ""
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        rows, total = store.list_interviews(kind, page=page)
        items = []
        for r in rows:
            item: dict[str, Any] = {"row": r, "status_class": STATUS_CLASS[r.status]}
            if r.kind == qs.COMPANY_ROLE:
                item["role_exists"] = store.get_role(r.role_portal, r.role_job_id) is not None
            elif r.kind == qs.MY_QUESTIONS:
                item["questions"] = [q.text for q in store.get_questions(r.id)]
            items.append(item)
        pages = max(1, -(-total // st.PAGE_SIZE))
        return render_template(
            "interview_history.html",
            items=items,
            kind=kind,
            kinds=KIND_LABELS,
            page=page,
            pages=pages,
            total=total,
            preparing=any(r.status == st.PREPARING for r in rows),
            **tab_context("history"),
        )
