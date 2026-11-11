"""Interview Prep service and storage: starting, answers, ending, feedback, history rules."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from jobscan import db
from jobscan.config import Config
from jobscan.interview import feedback as fb
from jobscan.interview import service as sv
from jobscan.interview import store as st
from jobscan.interview.metrics import TimedWord
from jobscan.interview.questions import COMPANY_ROLE, GENERAL_ROLE, MY_QUESTIONS
from jobscan.interview.rules import ANSWER_ASPECT_KEYS
from jobscan.interview.stt import TranscribeError, Transcription
from jobscan.models import Cv
from jobscan.store import Store
from jobscan.tailor_ai import TailorError
from tests.test_scoring import CV_TEXT, add_role, upload_cv

T0 = "2026-11-11T17:00:00+05:30"
T1 = "2026-11-11T17:10:00+05:30"
LONG = (
    "I led the lender integration for six weeks. We moved fourteen lenders onto one API. "
    "I wrote the plan and I ran the daily call. I think it went well, you know."
)  # 32 words


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


class FakeAi:
    def __init__(self, *replies: str | TailorError) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, int]] = []
        self.last_call = None

    def complete(
        self, system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 0
    ) -> str:
        self.calls.append((system, user, max_tokens))
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, TailorError):
            raise item
        return item


class FakeStt:
    def __init__(self, *results: Transcription | TranscribeError) -> None:
        self.results = list(results)
        self.calls = 0

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcription:
        self.calls += 1
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, TranscribeError):
            raise item
        return item


def spoken(text: str = LONG) -> Transcription:
    words = text.split()
    times = tuple(TimedWord(2.0 + i * 0.4, 2.3 + i * 0.4) for i in range(len(words)))
    return Transcription(text, times, (), 20.0, ms=900)


def questions_reply(n: int = 5) -> str:
    return json.dumps(
        {"questions": [{"slot": i, "kind": "x", "text": f"Question {i}?"} for i in range(1, n + 1)]}
    )


def feedback_reply(*idx: int) -> str:
    rating = {"score": 80, "reason": "clear", "quote": "I led the lender integration"}
    return json.dumps(
        {
            "answers": [
                {
                    "q": i,
                    "ratings": {k: dict(rating) for k in ANSWER_ASPECT_KEYS},
                    "verdict": "Good.",
                    "worked": ["Named the work."],
                    "next": [{"title": "Add a result", "why": "w", "fix": "f"}],
                    "improved": "I led it for 99 weeks.",
                }
                for i in idx
            ],
            "top_tips": ["Add one number."],
        }
    )


def make_env(ai: FakeAi, sleeps: list[float] | None = None) -> sv.Env:
    log = sleeps if sleeps is not None else []
    return sv.Env(
        make_ai=lambda _config: ai,
        pacer=sv.Pacer(monotonic=lambda: 0.0, sleep=log.append),
        sleep=log.append,
        now=lambda: T1,
    )


def config() -> Config:
    return Config()


def started(store: Store, *, own: list[str] | None = None, kind: str = MY_QUESTIONS) -> int:
    cv = upload_cv(store)
    return sv.create_interview(
        store, config(), cv, kind=kind, now=T0, own=own or ["Why this role?", "Tell me more."]
    )


def answer(store: Store, interview_id: int, idx: int, text: str = LONG) -> sv.AnswerOutcome:
    return sv.submit_answer(
        store,
        FakeStt(spoken(text)),
        interview_id,
        idx,
        audio=b"a",
        filename="a.webm",
        content_type="audio/webm",
        duration_s=20.0,
        now=T0,
    )


# ---- starting -------------------------------------------------------------------------------


def test_own_questions_start_with_their_questions(store: Store) -> None:
    interview_id = started(store, own=["First one?", " first one? ", "Second one?"])
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.kind == MY_QUESTIONS
    assert row.status == st.IN_PROGRESS
    assert [q.text for q in store.get_questions(interview_id)] == ["First one?", "Second one?"]
    assert row.cv_name == "cv.pdf"


def test_no_cv_blocks_start(store: Store) -> None:
    with pytest.raises(sv.InterviewError, match="Upload your CV"):
        sv.create_interview(store, config(), None, kind=MY_QUESTIONS, now=T0, own=["Why?? ok"])
    empty = Cv("c.pdf", "  ", "sha", T0)
    with pytest.raises(sv.InterviewError, match="Upload your CV"):
        sv.create_interview(store, config(), empty, kind=MY_QUESTIONS, now=T0, own=["Why?? ok"])


def test_only_one_interview_runs_at_a_time(store: Store) -> None:
    started(store)
    with pytest.raises(sv.InterviewError, match="Finish or end"):
        started(store)


def test_own_question_errors_are_shown(store: Store) -> None:
    cv = upload_cv(store)
    with pytest.raises(sv.InterviewError, match="at least one"):
        sv.create_interview(store, config(), cv, kind=MY_QUESTIONS, now=T0, own=["", " "])


def test_general_role_must_be_on_the_allowed_list(store: Store) -> None:
    cv = upload_cv(store)
    with pytest.raises(sv.InterviewError, match="from the list"):
        sv.create_interview(store, config(), cv, kind=GENERAL_ROLE, now=T0, general_title="Wizard")
    interview_id = sv.create_interview(
        store, config(), cv, kind=GENERAL_ROLE, now=T0, general_title="product manager"
    )
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.general_title == "Product Manager"
    assert store.get_questions(interview_id) == []  # written next


def test_company_role_keeps_a_snapshot_and_notes_a_missing_description(store: Store) -> None:
    cv = upload_cv(store)
    role = add_role(store, "100", description=None)
    interview_id = sv.create_interview(store, config(), cv, kind=COMPANY_ROLE, now=T0, role=role)
    row = store.get_interview(interview_id)
    assert row is not None
    assert (row.role_portal, row.role_job_id, row.role_company) == ("LinkedIn", "100", "Acme")
    assert "no description" in row.note
    store.delete_interview(interview_id)
    with pytest.raises(sv.InterviewError, match="Role not found"):
        sv.create_interview(store, config(), cv, kind=COMPANY_ROLE, now=T0, role=None)


# ---- writing questions ----------------------------------------------------------------------


def test_write_questions_saves_them(store: Store) -> None:
    cv = upload_cv(store)
    role = add_role(store, "100")
    interview_id = sv.create_interview(store, config(), cv, kind=COMPANY_ROLE, now=T0, role=role)
    ai = FakeAi(questions_reply())
    assert sv.write_questions(store, make_env(ai), config(), interview_id) == ""
    assert [q.text for q in store.get_questions(interview_id)] == [
        f"Question {i}?" for i in range(1, 6)
    ]
    _system, user, _ = ai.calls[0]
    assert "Title: Technical Program Manager" in user
    assert "payment gateway" in user
    assert "CANDIDATE CV" in user
    assert store.get_interview(interview_id).stats["ai_calls"] == 1


def test_cv_contact_details_are_removed_before_sending(store: Store) -> None:
    text = (
        "Nikhil Tuli\nnikhil@example.com | +91 98456 42255\nTechnical Program Manager\n- Led things"
    )
    cv = upload_cv(store, text)
    interview_id = sv.create_interview(
        store, config(), cv, kind=GENERAL_ROLE, now=T0, general_title="Product Manager"
    )
    ai = FakeAi(questions_reply())
    sv.write_questions(store, make_env(ai), config(), interview_id)
    user = ai.calls[0][1]
    assert "example.com" not in user
    assert "98456" not in user
    assert "Led things" in user


def test_an_invalid_reply_is_retried_once(store: Store) -> None:
    interview_id = started(store, kind=GENERAL_ROLE, own=None) if False else None
    cv = upload_cv(store)
    interview_id = sv.create_interview(
        store, config(), cv, kind=GENERAL_ROLE, now=T0, general_title="Product Manager"
    )
    ai = FakeAi("not json", questions_reply())
    assert sv.write_questions(store, make_env(ai), config(), interview_id) == ""
    assert len(ai.calls) == 2


def test_two_invalid_replies_remove_the_interview(store: Store) -> None:
    cv = upload_cv(store)
    interview_id = sv.create_interview(
        store, config(), cv, kind=GENERAL_ROLE, now=T0, general_title="Product Manager"
    )
    message = sv.write_questions(store, make_env(FakeAi("nope")), config(), interview_id)
    assert "Could not write questions" in message
    assert store.get_interview(interview_id) is None


def test_a_rate_limit_is_waited_out_once(store: Store) -> None:
    cv = upload_cv(store)
    interview_id = sv.create_interview(
        store, config(), cv, kind=GENERAL_ROLE, now=T0, general_title="Product Manager"
    )
    sleeps: list[float] = []
    ai = FakeAi(TailorError("busy", status=429), questions_reply())
    assert sv.write_questions(store, make_env(ai, sleeps), config(), interview_id) == ""
    assert sv.RATE_LIMIT_WAIT_SECONDS in sleeps
    assert store.get_interview(interview_id).stats["ai_retries"] == 1  # type: ignore[union-attr]


def test_a_service_error_removes_the_interview_and_says_why(store: Store) -> None:
    cv = upload_cv(store)
    interview_id = sv.create_interview(
        store, config(), cv, kind=GENERAL_ROLE, now=T0, general_title="Product Manager"
    )
    message = sv.write_questions(
        store, make_env(FakeAi(TailorError("The AI service had a problem (HTTP 500)."))),
        config(), interview_id,
    )  # fmt: skip
    assert "HTTP 500" in message
    assert store.get_interview(interview_id) is None


def test_recent_questions_are_sent_so_they_are_not_repeated(store: Store) -> None:
    cv = upload_cv(store)
    first = sv.create_interview(
        store, config(), cv, kind=GENERAL_ROLE, now=T0, general_title="Product Manager"
    )
    sv.write_questions(store, make_env(FakeAi(questions_reply())), config(), first)
    store.set_interview_status(first, st.READY, now=T0)
    second = sv.create_interview(
        store, config(), cv, kind=GENERAL_ROLE, now=T1, general_title="Product Manager"
    )
    ai = FakeAi(questions_reply())
    sv.write_questions(store, make_env(ai), config(), second)
    assert "ALREADY ASKED" in ai.calls[0][1]
    assert "- Question 1?" in ai.calls[0][1]


# ---- answers --------------------------------------------------------------------------------


def test_an_answer_is_transcribed_measured_and_saved(store: Store) -> None:
    interview_id = started(store)
    outcome = answer(store, interview_id, 1)
    assert outcome.status == st.ANSWERED
    assert outcome.words == 32
    saved = store.get_answers(interview_id)[0]
    assert saved.transcript == LONG
    assert saved.duration_s == 20.0
    assert saved.measure["fillers"] == 1
    assert saved.measure["hedges"] == 1
    stats = store.get_interview(interview_id).stats  # type: ignore[union-attr]
    assert stats["stt_calls"] == 1
    assert stats["stt_ms"] == 900


def test_short_and_silent_answers_are_marked(store: Store) -> None:
    interview_id = started(store)
    assert answer(store, interview_id, 1, "Yes I did it").status == st.TOO_SHORT
    assert answer(store, interview_id, 2, "").status == st.NO_SPEECH


def test_a_rate_limit_asks_the_browser_to_wait_and_saves_nothing(store: Store) -> None:
    interview_id = started(store)
    outcome = sv.submit_answer(
        store, FakeStt(TranscribeError("busy", status=429, retry_after=7)), interview_id, 1,
        audio=b"a", filename="a.webm", content_type="audio/webm", duration_s=5, now=T0,
    )  # fmt: skip
    assert outcome.status == ""
    assert outcome.retry_after == 7
    assert store.get_answers(interview_id) == []


def test_a_speech_error_is_returned_not_raised(store: Store) -> None:
    interview_id = started(store)
    outcome = sv.submit_answer(
        store, FakeStt(TranscribeError("The speech service had a problem (HTTP 500).")),
        interview_id, 1, audio=b"a", filename="a.webm", content_type="audio/webm",
        duration_s=5, now=T0,
    )  # fmt: skip
    assert outcome.status == ""
    assert "HTTP 500" in outcome.error


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"idx": 9}, "no such question"),
        ({"audio": b""}, "empty"),
        ({"content_type": "text/html"}, "not a recording"),
    ],
)
def test_bad_uploads_are_refused(store: Store, kwargs: dict[str, object], message: str) -> None:
    interview_id = started(store)
    args: dict[str, object] = {
        "idx": 1, "audio": b"a", "filename": "a.webm", "content_type": "audio/webm",
        "duration_s": 5.0, "now": T0,
    }  # fmt: skip
    args.update(kwargs)
    idx = args.pop("idx")
    with pytest.raises(sv.InterviewError, match=message):
        sv.submit_answer(store, FakeStt(spoken()), interview_id, idx, **args)  # type: ignore[arg-type]


def test_answers_are_refused_when_the_interview_is_not_running(store: Store) -> None:
    interview_id = started(store)
    store.set_interview_status(interview_id, st.READY, now=T0)
    with pytest.raises(sv.InterviewError, match="not running"):
        answer(store, interview_id, 1)
    with pytest.raises(sv.InterviewError, match="not found"):
        answer(store, 999, 1)


def test_skip_and_not_scored(store: Store) -> None:
    interview_id = started(store)
    sv.skip_answer(store, interview_id, 1, now=T0)
    sv.mark_not_scored(store, interview_id, 2, now=T0, note="Could not transcribe")
    got = {a.idx: (a.status, a.note) for a in store.get_answers(interview_id)}
    assert got == {1: (st.SKIPPED, ""), 2: (st.NOT_SCORED, "Could not transcribe")}


def test_a_recording_is_never_stored(store: Store) -> None:
    interview_id = started(store)
    answer(store, interview_id, 1)
    dump = "\n".join(store.conn.iterdump())
    assert "webm" not in dump
    assert "audio" not in dump.lower().replace("interview_answer", "")


# ---- ending ---------------------------------------------------------------------------------


def test_finish_with_nothing_to_score_discards_the_interview(store: Store) -> None:
    interview_id = started(store)
    answer(store, interview_id, 1, "Yes")  # too short
    sv.skip_answer(store, interview_id, 2, now=T0)
    assert sv.finish_interview(store, interview_id, now=T1) == sv.DISCARDED
    assert store.get_interview(interview_id) is None


def test_finish_with_an_answer_starts_feedback(store: Store) -> None:
    interview_id = started(store)
    answer(store, interview_id, 1)
    assert sv.finish_interview(store, interview_id, now=T1) == sv.PREPARING_NOW
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.PREPARING
    assert row.stage == "scoring"


def test_leaving_the_page_keeps_work_as_incomplete_or_drops_an_empty_one(store: Store) -> None:
    keep = started(store)
    answer(store, keep, 1)
    sv.abandon_interview(store, keep, now=T1)
    assert store.get_interview(keep).status == st.INCOMPLETE  # type: ignore[union-attr]
    drop = started(store)
    sv.abandon_interview(store, drop, now=T1)
    assert store.get_interview(drop) is None


def test_abandon_ignores_interviews_that_are_not_running(store: Store) -> None:
    interview_id = started(store)
    answer(store, interview_id, 1)
    store.set_interview_status(interview_id, st.READY, now=T0)
    sv.abandon_interview(store, interview_id, now=T1)
    assert store.get_interview(interview_id).status == st.READY  # type: ignore[union-attr]


def test_a_quiet_interview_becomes_incomplete_after_thirty_minutes(store: Store) -> None:
    interview_id = started(store)
    assert store.expire_stale_interviews("2026-11-11T16:00:00+05:30", T1) == 0
    assert store.expire_stale_interviews("2026-11-11T17:30:00+05:30", T1) == 1
    assert store.get_interview(interview_id).status == st.INCOMPLETE  # type: ignore[union-attr]


def test_a_preparing_interview_fails_at_start_up(store: Store) -> None:
    interview_id = started(store)
    answer(store, interview_id, 1)
    sv.finish_interview(store, interview_id, now=T1)
    assert store.fail_stale_interviews(T1) == 1
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.FAILED
    assert "closed" in row.error


# ---- feedback -------------------------------------------------------------------------------


def preparing(store: Store, texts: dict[int, str] | None = None) -> int:
    interview_id = started(store, own=["Question one here?", "Question two here?"])
    for idx, text in (texts or {1: LONG}).items():
        answer(store, interview_id, idx, text)
    assert sv.finish_interview(store, interview_id, now=T1) == sv.PREPARING_NOW
    return interview_id


def test_feedback_is_built_saved_and_the_interview_is_ready(store: Store) -> None:
    interview_id = preparing(store, {1: LONG, 2: LONG})
    ai = FakeAi(feedback_reply(1, 2))
    assert sv.build_feedback(store, make_env(ai), config(), interview_id) == ""
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.READY
    assert row.answer_score == 80
    assert row.spoken_score is not None
    saved = store.get_feedback(interview_id)
    assert saved is not None
    assert saved.missing == []
    assert "Add one number." in saved.tips
    assert [a["idx"] for a in saved.answers] == [1, 2]
    assert "99" not in saved.answers[0]["improved"]  # type: ignore[operator]
    assert ai.calls[0][2] == config().interview_reply_cap
    assert "[Answer 1] " + LONG in ai.calls[0][1]
    assert row.feedback_model == config().interview_ai[1]


def test_spoken_values_come_from_the_transcript_not_the_model(store: Store) -> None:
    interview_id = preparing(store, {1: LONG + " " + LONG})  # 64 words, enough to rate
    sv.build_feedback(store, make_env(FakeAi(feedback_reply(1))), config(), interview_id)
    saved = store.get_feedback(interview_id)
    assert saved is not None
    items = {i["key"]: i for i in saved.spoken["items"]}  # type: ignore[attr-defined]
    assert items["fillers"]["value"] == pytest.approx(2 / 64 * 100, abs=0.01)
    assert items["ownership"]["value"] == pytest.approx(80.0)  # eight I, two we


def test_a_model_that_skips_an_answer_is_listed_as_missing(store: Store) -> None:
    interview_id = preparing(store, {1: LONG, 2: LONG})
    sv.build_feedback(store, make_env(FakeAi(feedback_reply(1))), config(), interview_id)
    saved = store.get_feedback(interview_id)
    assert saved is not None
    assert saved.missing == [2]
    assert store.get_interview(interview_id).status == st.READY  # type: ignore[union-attr]


def test_two_bad_replies_fail_the_interview_with_a_message(store: Store) -> None:
    interview_id = preparing(store)
    message = sv.build_feedback(store, make_env(FakeAi("garbage")), config(), interview_id)
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.FAILED
    assert row.error == message
    assert "could not be used" in message
    assert "garbage" not in message


def test_a_service_error_fails_the_interview_with_its_message(store: Store) -> None:
    interview_id = preparing(store)
    err = TailorError("The AI service refused the key (HTTP 401).", status=401)
    assert "HTTP 401" in sv.build_feedback(store, make_env(FakeAi(err)), config(), interview_id)


def test_a_failed_interview_can_be_tried_again_from_its_transcripts(store: Store) -> None:
    interview_id = preparing(store)
    sv.build_feedback(store, make_env(FakeAi("garbage")), config(), interview_id)
    assert sv.retry_feedback(store, interview_id, now=T1) == sv.PREPARING_NOW
    assert (
        sv.build_feedback(store, make_env(FakeAi(feedback_reply(1))), config(), interview_id) == ""
    )
    assert store.get_interview(interview_id).status == st.READY  # type: ignore[union-attr]


def test_retry_only_works_on_incomplete_or_failed(store: Store) -> None:
    interview_id = preparing(store)
    with pytest.raises(sv.InterviewError, match="cannot be retried"):
        sv.retry_feedback(store, interview_id, now=T1)
    with pytest.raises(sv.InterviewError, match="not found"):
        sv.retry_feedback(store, 999, now=T1)


def test_get_feedback_on_an_incomplete_interview_with_nothing_to_score_removes_it(
    store: Store,
) -> None:
    interview_id = started(store)
    answer(store, interview_id, 1)
    sv.abandon_interview(store, interview_id, now=T1)
    store.save_answer(interview_id, 1, status=st.SKIPPED, now=T1)
    assert sv.retry_feedback(store, interview_id, now=T1) == sv.DISCARDED
    assert store.get_interview(interview_id) is None


def test_no_cv_at_feedback_time_fails_with_a_message(store: Store) -> None:
    interview_id = preparing(store)
    store.conn.execute("DELETE FROM cv")
    assert "Upload your CV" in sv.build_feedback(
        store, make_env(FakeAi("x")), config(), interview_id
    )


def test_feedback_is_not_built_twice(store: Store) -> None:
    interview_id = preparing(store)
    sv.build_feedback(store, make_env(FakeAi(feedback_reply(1))), config(), interview_id)
    ai = FakeAi(feedback_reply(1))
    assert "not waiting" in sv.build_feedback(store, make_env(ai), config(), interview_id)
    assert ai.calls == []


def test_a_removed_role_still_gives_feedback_from_the_snapshot(store: Store) -> None:
    cv = upload_cv(store)
    role = add_role(store, "100")
    interview_id = sv.create_interview(store, config(), cv, kind=COMPANY_ROLE, now=T0, role=role)
    store.set_questions(interview_id, [sv.qs.Question(1, "behavioural", "What did you do?")])
    answer(store, interview_id, 1)
    sv.finish_interview(store, interview_id, now=T1)
    store.conn.execute("DELETE FROM roles")
    ai = FakeAi(feedback_reply(1))
    assert sv.build_feedback(store, make_env(ai), config(), interview_id) == ""
    assert "Title: Technical Program Manager" in ai.calls[0][1]


# ---- history --------------------------------------------------------------------------------


def ready(
    store: Store, kind: str, started_on: str, *, spoken: int, answer_score: int, **role: str
) -> int:
    cv = Cv("cv.pdf", CV_TEXT, "sha", T0)
    interview_id = store.create_interview(
        kind=kind, questions=[sv.qs.Question(1, "x", "Q?")], now=started_on,
        cv_name=cv.filename, **role,
    )  # fmt: skip
    store.save_feedback(
        interview_id, spoken={}, answers=[], spoken_score=spoken, answer_score=answer_score,
        tips=[], missing=[], now=started_on,
    )  # fmt: skip
    store.set_interview_status(interview_id, st.READY, now=started_on)
    return interview_id


def test_previous_interview_is_the_latest_earlier_ready_one_of_the_same_kind_and_role(
    store: Store,
) -> None:
    a = ready(store, COMPANY_ROLE, "2026-11-01T10:00:00+05:30", spoken=70, answer_score=60,
              role_portal="LinkedIn", role_job_id="1")  # fmt: skip
    b = ready(store, COMPANY_ROLE, "2026-11-02T10:00:00+05:30", spoken=72, answer_score=62,
              role_portal="LinkedIn", role_job_id="2")  # fmt: skip
    c = ready(store, COMPANY_ROLE, "2026-11-03T10:00:00+05:30", spoken=75, answer_score=66,
              role_portal="LinkedIn", role_job_id="1")  # fmt: skip
    now = store.get_interview(c)
    assert now is not None
    previous = store.previous_interview(now)
    assert previous is not None
    assert previous.id == a
    first = store.get_interview(a)
    assert first is not None
    assert store.previous_interview(first) is None
    assert b != a


def test_general_roles_compare_by_title_and_my_questions_never(store: Store) -> None:
    a = ready(store, GENERAL_ROLE, "2026-11-01T10:00:00+05:30", spoken=1, answer_score=1,
              general_title="Product Manager")  # fmt: skip
    b = ready(store, GENERAL_ROLE, "2026-11-02T10:00:00+05:30", spoken=1, answer_score=1,
              general_title="product manager")  # fmt: skip
    row_b = store.get_interview(b)
    assert row_b is not None
    previous = store.previous_interview(row_b)
    assert previous is not None
    assert previous.id == a
    m1 = ready(store, MY_QUESTIONS, "2026-11-01T10:00:00+05:30", spoken=1, answer_score=1)
    m2 = ready(store, MY_QUESTIONS, "2026-11-02T10:00:00+05:30", spoken=1, answer_score=1)
    row = store.get_interview(m2)
    assert row is not None
    assert store.previous_interview(row) is None
    assert m1 != m2


def test_an_unfinished_earlier_interview_is_not_compared(store: Store) -> None:
    a = ready(store, GENERAL_ROLE, "2026-11-01T10:00:00+05:30", spoken=1, answer_score=1,
              general_title="Designer")  # fmt: skip
    store.set_interview_status(a, st.FAILED, now=T0)
    b = ready(store, GENERAL_ROLE, "2026-11-02T10:00:00+05:30", spoken=1, answer_score=1,
              general_title="Designer")  # fmt: skip
    row = store.get_interview(b)
    assert row is not None
    assert store.previous_interview(row) is None


def test_history_lists_newest_first_filters_and_pages(store: Store) -> None:
    ids = [
        ready(
            store,
            GENERAL_ROLE,
            f"2026-11-{d:02d}T10:00:00+05:30",
            spoken=1,
            answer_score=1,
            general_title="Designer",
        )
        for d in range(1, 4)
    ]
    ready(store, MY_QUESTIONS, "2026-11-05T10:00:00+05:30", spoken=1, answer_score=1)
    rows, total = store.list_interviews()
    assert total == 4
    assert rows[0].kind == MY_QUESTIONS
    rows, total = store.list_interviews(GENERAL_ROLE)
    assert total == 3
    assert [r.id for r in rows] == ids[::-1]
    rows, total = store.list_interviews(page=2, page_size=3)
    assert total == 4
    assert len(rows) == 1


def test_delete_removes_everything_about_an_interview(store: Store) -> None:
    interview_id = preparing(store)
    sv.build_feedback(store, make_env(FakeAi(feedback_reply(1))), config(), interview_id)
    store.set_thumb(interview_id, 1, 1)
    assert store.delete_interview(interview_id)
    for table in ("interview", "interview_question", "interview_answer", "interview_feedback",
                  "interview_thumb"):  # fmt: skip
        assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert not store.delete_interview(interview_id)


def test_thumbs_can_be_set_changed_and_cleared(store: Store) -> None:
    interview_id = started(store)
    store.set_thumb(interview_id, 1, 1)
    store.set_thumb(interview_id, 1, -1)
    store.set_thumb(interview_id, 2, 1)
    assert store.get_thumbs(interview_id) == {1: -1, 2: 1}
    store.set_thumb(interview_id, 2, 0)
    assert store.get_thumbs(interview_id) == {1: -1}


def test_pacer_spaces_calls(store: Store) -> None:
    now = [0.0]
    sleeps: list[float] = []
    pacer = sv.Pacer(monotonic=lambda: now[0], sleep=sleeps.append)
    pacer.wait(4)
    now[0] = 5.0
    pacer.wait(4)  # 15 s apart wanted, 5 s have passed
    assert sleeps == [10.0]


def test_stats_hold_counts_only(store: Store) -> None:
    interview_id = started(store)
    store.add_stats(interview_id, ai_calls=1, tokens_in=100)
    store.add_stats(interview_id, ai_calls=1)
    assert store.get_interview(interview_id).stats == {"ai_calls": 2, "tokens_in": 100}  # type: ignore[union-attr]


def test_feedback_error_is_a_plain_exception() -> None:
    assert issubclass(fb.FeedbackError, Exception)
