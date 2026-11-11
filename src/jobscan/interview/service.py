"""Running an interview: start it, take answers, build feedback.

Everything here works on the Store and takes its providers (speech service, AI service, clock,
sleep) as arguments, so tests run with fakes. Nothing about the audio is kept or logged. Nothing
the user said or the model wrote goes into the logs.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import db
from ..ai_fit import Completer, redact_cv, trim_description
from ..config import Config, ConfigError
from ..models import Cv, Role
from ..store import Store
from ..tailor_ai import TailorClient, TailorError
from . import feedback as fb
from . import metrics as mx
from . import questions as qs
from . import store as st
from .rules import MIN_ANSWER_WORDS
from .stt import ALLOWED_AUDIO_TYPES, TranscribeError, Transcriber, make_transcriber

WHERE = "Config, Interview Prep"
RATE_LIMIT_WAIT_SECONDS = 30.0
QUESTIONS_TEMPERATURE = 0.5
QUESTIONS_MAX_TOKENS = 1_500
FEEDBACK_TEMPERATURE = 0.2
MAX_ANSWER_SECONDS = 900  # never trust a longer length from the browser

DISCARDED = "discarded"
PREPARING_NOW = "preparing"

Sleep = Callable[[float], None]
Now = Callable[[], str]


class InterviewError(Exception):
    """A problem the user can read: nothing is wrong with the app."""


# ---- providers --------------------------------------------------------------------------------


def interview_ai_from_env(config: Config, environ: Mapping[str, str] | None = None) -> Completer:
    env = os.environ if environ is None else environ
    base_url, model, key_env = config.interview_ai
    key = env.get(key_env, "").strip()
    if not key:
        raise TailorError(
            f"The AI key is missing. Set {key_env} in the terminal, then start jobscan "
            "from that terminal."
        )
    return TailorClient(base_url, model, key, where=WHERE)


def transcriber_from_env(config: Config, environ: Mapping[str, str] | None = None) -> Transcriber:
    env = os.environ if environ is None else environ
    return make_transcriber(
        config.interview_stt_base_url,
        config.interview_stt_model,
        config.interview_stt_key_env,
        dict(env),
    )


class Pacer:
    """Keeps interview AI calls at least 60 / calls-a-minute seconds apart."""

    def __init__(
        self,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self, calls_per_minute: int) -> None:
        with self._lock:
            gap = 60.0 / max(1, calls_per_minute)
            if self._last is not None:
                remaining = gap - (self._monotonic() - self._last)
                if remaining > 0:
                    self._sleep(remaining)
            self._last = self._monotonic()


@dataclass
class Env:
    """What a job needs besides the Store: the providers and the clocks."""

    make_ai: Callable[[Config], Completer]
    pacer: Pacer
    sleep: Sleep
    now: Now


# ---- starting ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleMaterial:
    lines: list[str]
    must_haves: list[tuple[str, str]]
    note: str


def role_material(
    store: Store, config: Config, portal: str, job_id: str, company: str, title: str
) -> RoleMaterial:
    """What the model is told about a role: title, company, description, must-haves."""
    role: Role | None = store.get_role(portal, job_id)
    title = role.title if role else title
    company = role.company if role else company
    lines = [f"Title: {title}", f"Company: {company}"]
    note = ""
    description = (role.description or "").strip() if role else ""
    if description:
        trimmed, _ = trim_description(description, config.ai_max_words)
        lines += ["Description:", trimmed]
    else:
        note = "This role has no description. Questions use the title and company only."
    fit = store.get_ai_fits().get(f"{portal}|{job_id}")
    must_haves = [(item, status) for item, status, _ in fit.checks] if fit else []
    return RoleMaterial(lines, must_haves, note)


def create_interview(
    store: Store,
    config: Config,
    cv: Cv | None,
    *,
    kind: str,
    now: str,
    role: Role | None = None,
    general_title: str = "",
    own: list[str] | None = None,
    read_aloud: bool = True,
) -> int:
    """Create the interview. For own questions it has its questions at once; for the other
    kinds the questions are written next (write_questions)."""
    if kind not in qs.INTERVIEW_TYPES:
        raise InterviewError("Choose a way to start.")
    if cv is None or not cv.text.strip():
        raise InterviewError("Upload your CV first.")
    if store.running_interview() is not None:
        raise InterviewError("Finish or end your current interview first.")
    base = {
        "kind": kind,
        "now": now,
        "cv_name": cv.filename,
        "cv_uploaded_on": cv.uploaded_on,
        "read_aloud": read_aloud,
    }
    if kind == qs.MY_QUESTIONS:
        questions, error = qs.clean_own_questions(own or [])
        if error:
            raise InterviewError(error)
        return store.create_interview(questions=qs.own_questions(questions), **base)  # type: ignore[arg-type]
    if kind == qs.COMPANY_ROLE:
        if role is None:
            raise InterviewError("Role not found. Pick another.")
        material = role_material(store, config, role.portal, role.job_id, role.company, role.title)
        return store.create_interview(
            questions=[],
            role_portal=role.portal,
            role_job_id=role.job_id,
            role_company=role.company,
            role_title=role.title,
            note=material.note,
            question_model=config.interview_ai[1],
            **base,  # type: ignore[arg-type]
        )
    title = general_title.strip()
    allowed = {r.casefold() for r in config.interview_role_list}
    if title.casefold() not in allowed:
        raise InterviewError("Choose a role from the list.")
    shown = next(r for r in config.interview_role_list if r.casefold() == title.casefold())
    return store.create_interview(
        questions=[],
        general_title=shown,
        question_model=config.interview_ai[1],
        **base,  # type: ignore[arg-type]
    )


def _ask(
    store: Store,
    env: Env,
    config: Config,
    interview_id: int,
    system: str,
    user: str,
    *,
    temperature: float,
    max_tokens: int,
) -> str:
    """One model call, paced. A rate limit is waited out once. Counts go to the interview."""
    client = env.make_ai(config)
    for attempt in (1, 2):
        env.pacer.wait(config.ai_calls_per_minute)
        try:
            text = client.complete(system, user, temperature=temperature, max_tokens=max_tokens)
        except TailorError as exc:
            store.add_stats(interview_id, ai_calls=1, ai_errors=1)
            if exc.status == 429 and attempt == 1:
                store.add_stats(interview_id, ai_retries=1)
                env.sleep(RATE_LIMIT_WAIT_SECONDS)
                continue
            raise
        info = getattr(client, "last_call", None)
        store.add_stats(
            interview_id,
            ai_calls=1,
            ai_ms=getattr(info, "ms", 0) or 0,
            tokens_in=getattr(info, "tokens_in", 0) or 0,
            tokens_out=getattr(info, "tokens_out", 0) or 0,
        )
        return text
    raise AssertionError("unreachable")  # pragma: no cover


def write_questions(store: Store, env: Env, config: Config, interview_id: int) -> str:
    """Write the questions for an interview that has none. Returns "" or a message.

    On a failure the interview is removed: nothing is saved (FR-C6).
    """
    row = store.get_interview(interview_id)
    cv = store.get_cv()
    if row is None or cv is None:
        return "Upload your CV first."
    try:
        if row.kind == qs.COMPANY_ROLE:
            material = role_material(
                store, config, row.role_portal, row.role_job_id, row.role_company, row.role_title
            )
            role_lines, must_haves = material.lines, material.must_haves
            compare = row.compare_key
        else:
            role_lines = [
                f"Title: {row.general_title}",
                "No specific company. Ask what is typical for this job title.",
            ]
            must_haves = []
            compare = row.compare_key
        recent = store.recent_questions(row.kind, compare)
        user = qs.build_user_message(
            cv_text=redact_cv(cv.text),
            role_lines=role_lines,
            must_haves=must_haves,
            recent=recent,
        )
        questions: list[qs.Question] = []
        for attempt in (1, 2):
            reply = _ask(
                store, env, config, interview_id, qs.SYSTEM_PROMPT, user,
                temperature=QUESTIONS_TEMPERATURE, max_tokens=QUESTIONS_MAX_TOKENS,
            )  # fmt: skip
            try:
                questions = qs.parse_questions(reply)
                break
            except fb.FeedbackError:
                store.add_stats(interview_id, ai_retries=1)
                if attempt == 2:
                    raise
        store.set_questions(interview_id, questions)
        return ""
    except TailorError as exc:
        message = str(exc)
    except fb.FeedbackError:
        message = "Could not write questions. Try again."
    store.delete_interview(interview_id)
    return message


# ---- answers ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AnswerOutcome:
    status: str  # an answer status, or "" when the answer was not taken
    words: int = 0
    retry_after: float | None = None  # set when the speech service asked us to wait
    error: str = ""


def _running(store: Store, interview_id: int) -> st.InterviewRow:
    row = store.get_interview(interview_id)
    if row is None:
        raise InterviewError("This interview was not found.")
    if row.status != st.IN_PROGRESS:
        raise InterviewError("This interview is not running.")
    return row


def _check_idx(store: Store, interview_id: int, idx: int) -> None:
    if idx not in {q.idx for q in store.get_questions(interview_id)}:
        raise InterviewError("There is no such question.")


def submit_answer(
    store: Store,
    transcriber: Transcriber,
    interview_id: int,
    idx: int,
    *,
    audio: bytes,
    filename: str,
    content_type: str,
    duration_s: float,
    now: str,
) -> AnswerOutcome:
    """Transcribe one answer, measure it and save the text. The audio is dropped at the end."""
    _running(store, interview_id)
    _check_idx(store, interview_id, idx)
    if not audio:
        raise InterviewError("The recording is empty.")
    if not content_type.startswith(ALLOWED_AUDIO_TYPES):
        raise InterviewError("That file is not a recording.")
    duration = min(max(0.0, duration_s), float(MAX_ANSWER_SECONDS))
    try:
        result = transcriber.transcribe(audio, filename, content_type)
    except TranscribeError as exc:
        store.add_stats(interview_id, stt_calls=1, stt_errors=1)
        if exc.status == 429:
            return AnswerOutcome("", retry_after=exc.retry_after or RATE_LIMIT_WAIT_SECONDS)
        return AnswerOutcome("", error=str(exc))
    finally:
        del audio  # the only copy of the recording on this side
    store.add_stats(interview_id, stt_calls=1, stt_ms=result.ms)
    times = result.words or result.sentences
    measure = mx.measure_answer(
        result.text,
        times,
        duration_s=duration or (result.duration_s or 0.0),
        exact=bool(result.words),
    )
    if measure.words == 0:
        status = st.NO_SPEECH
    elif measure.words < MIN_ANSWER_WORDS:
        status = st.TOO_SHORT
    else:
        status = st.ANSWERED
    store.save_answer(
        interview_id,
        idx,
        status=status,
        now=now,
        transcript=result.text,
        duration_s=measure.duration_s,
        word_count=measure.words,
        measure=measure.to_dict(),
    )
    return AnswerOutcome(status, words=measure.words)


def skip_answer(store: Store, interview_id: int, idx: int, *, now: str) -> None:
    _running(store, interview_id)
    _check_idx(store, interview_id, idx)
    store.save_answer(interview_id, idx, status=st.SKIPPED, now=now)


def mark_not_scored(store: Store, interview_id: int, idx: int, *, now: str, note: str) -> None:
    """The recording could not be turned into text. Nothing else about it is kept."""
    _running(store, interview_id)
    _check_idx(store, interview_id, idx)
    store.save_answer(interview_id, idx, status=st.NOT_SCORED, now=now, note=note[:200])


# ---- ending -----------------------------------------------------------------------------------


def _scorable(store: Store, interview_id: int) -> list[st.AnswerRow]:
    return [a for a in store.get_answers(interview_id) if a.status == st.ANSWERED]


def finish_interview(store: Store, interview_id: int, *, now: str) -> str:
    """End a running interview. DISCARDED when nothing can be scored, else PREPARING_NOW."""
    _running(store, interview_id)
    return _start_feedback(store, interview_id, now)


def retry_feedback(store: Store, interview_id: int, *, now: str) -> str:
    """Get feedback on an Incomplete interview, or try a Failed one again."""
    row = store.get_interview(interview_id)
    if row is None:
        raise InterviewError("This interview was not found.")
    if row.status not in (st.INCOMPLETE, st.FAILED):
        raise InterviewError("This interview cannot be retried now.")
    return _start_feedback(store, interview_id, now)


def _start_feedback(store: Store, interview_id: int, now: str) -> str:
    if not _scorable(store, interview_id):
        store.delete_interview(interview_id)
        return DISCARDED
    store.set_interview_status(interview_id, st.PREPARING, now=now, stage="scoring")
    return PREPARING_NOW


def abandon_interview(store: Store, interview_id: int, *, now: str) -> None:
    """The page was closed. With no answer made there is nothing to keep."""
    row = store.get_interview(interview_id)
    if row is None or row.status != st.IN_PROGRESS:
        return
    if not _scorable(store, interview_id):
        store.delete_interview(interview_id)
    else:
        store.set_interview_status(interview_id, st.INCOMPLETE, now=now)


# ---- feedback ---------------------------------------------------------------------------------


def build_feedback(store: Store, env: Env, config: Config, interview_id: int) -> str:
    """Make the feedback for a Preparing interview. Returns "" or the reason it failed."""
    row = store.get_interview(interview_id)
    if row is None or row.status != st.PREPARING:
        return "This interview is not waiting for feedback."
    cv = store.get_cv()
    if cv is None or not cv.text.strip():
        return _fail(store, env, interview_id, "Upload your CV first, then try again.")
    questions = {q.idx: q for q in store.get_questions(interview_id)}
    answers = [a for a in store.get_answers(interview_id) if a.status == st.ANSWERED]
    if not answers:
        return _fail(store, env, interview_id, "There is no answer to score.")
    measures = [mx.AnswerMeasure.from_dict(a.measure) for a in answers]
    spoken = mx.spoken_result(
        measures,
        reading_aloud=row.read_aloud,
        question_words=[len(questions[a.idx].text.split()) for a in answers],
    )
    sent = [fb.AnswerText(a.idx, questions[a.idx].text, a.transcript) for a in answers]
    cv_text = redact_cv(cv.text)
    if row.kind == qs.COMPANY_ROLE:
        role_lines = role_material(
            store, config, row.role_portal, row.role_job_id, row.role_company, row.role_title
        ).lines
    elif row.kind == qs.GENERAL_ROLE:
        role_lines = [f"Title: {row.general_title}", "No specific company."]
    else:
        role_lines = ["These are the candidate's own practice questions. No specific role."]
    user = fb.build_user_message(cv_text=cv_text, role_lines=role_lines, answers=sent)
    store.set_interview_status(interview_id, st.PREPARING, now=env.now(), stage="writing")
    store.set_interview_models(interview_id, config.interview_ai[1])
    result: fb.FeedbackResult | None = None
    try:
        for attempt in (1, 2):
            reply = _ask(
                store, env, config, interview_id, fb.SYSTEM_PROMPT, user,
                temperature=FEEDBACK_TEMPERATURE, max_tokens=config.interview_reply_cap,
            )  # fmt: skip
            try:
                result = fb.parse_feedback(reply, sent, cv_text)
                break
            except fb.FeedbackError:
                store.add_stats(interview_id, ai_retries=1)
                if attempt == 2:
                    raise
    except TailorError as exc:
        return _fail(store, env, interview_id, str(exc))
    except fb.FeedbackError as exc:
        # The parser's reason names the problem only. It never carries any answer text.
        return _fail(
            store, env, interview_id, f"The AI reply could not be used ({exc}) Try again."
        )
    assert result is not None
    store.save_feedback(
        interview_id,
        spoken={
            "enough": spoken.enough,
            "items": [i.to_dict() for i in spoken.items],
            "raw": spoken.raw,
        },
        answers=[a.to_dict() for a in result.answers],
        spoken_score=spoken.score,
        answer_score=result.score,
        tips=fb.combine_tips(spoken.worst_tip(), result.top_tips),
        missing=result.missing,
        now=env.now(),
    )
    store.set_interview_status(interview_id, st.READY, now=env.now())
    return ""


def _fail(store: Store, env: Env, interview_id: int, message: str) -> str:
    store.set_interview_status(interview_id, st.FAILED, now=env.now(), error=message)
    return message


# ---- background -------------------------------------------------------------------------------

Job = Callable[[Store], None]
Launcher = Callable[[Job], None]


def thread_runner(store: Store) -> Launcher:
    """Run a job in the background so the web app stays usable.

    A file database gets its own connection in the worker thread. An in-memory database cannot
    be shared, so there the job runs straight away.
    """
    path = store.db_path()

    def launch(job: Job) -> None:
        if path is None:
            job(store)
            return

        def target() -> None:
            conn = db.connect(Path(path))
            try:
                job(Store(conn))
            finally:
                conn.close()

        threading.Thread(target=target, daemon=True).start()

    return launch


def load_config(store: Store) -> Config:
    try:
        return Config.from_raw(store.get_config_raw())
    except ConfigError:
        return Config()
