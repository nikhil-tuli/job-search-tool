"""Interview Prep: the numbers kept for every model and speech call (for tracing). No text."""

from __future__ import annotations

from jobscan.config import Config
from jobscan.interview import service as sv
from jobscan.interview.questions import GENERAL_ROLE
from jobscan.interview.stt import TranscribeError, Transcription
from jobscan.store import Store
from jobscan.tailor_ai import CallInfo, TailorError
from tests.test_interview_service import (
    LONG,
    T0,
    T1,
    FakeAi,
    FakeStt,
    answer,
    feedback_reply,
    make_env,
    preparing,
    questions_reply,
    spoken,
    started,
)
from tests.test_scoring import upload_cv


class MeteredAi(FakeAi):
    """A fake model that reports its time and tokens, as the real client does."""

    def __init__(self, *replies: str | TailorError, tokens: tuple[int | None, int | None]) -> None:
        super().__init__(*replies)
        self.tokens = tokens

    def complete(
        self, system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 0
    ) -> str:
        self.last_call = CallInfo(3000, 200, *self.tokens)
        return super().complete(system, user, temperature=temperature, max_tokens=max_tokens)


def general(store: Store) -> int:
    return sv.create_interview(
        store,
        Config(),
        upload_cv(store),
        kind=GENERAL_ROLE,
        now=T0,
        general_title="Product Manager",
    )


def test_question_writing_keeps_its_numbers(store: Store) -> None:
    interview_id = general(store)
    ai = MeteredAi(questions_reply(), tokens=(612, 148))
    sv.write_questions(store, make_env(ai), Config(), interview_id)
    [call] = store.get_calls(interview_id)
    assert (call.step, call.idx, call.attempt, call.ok) == ("questions", None, 1, True)
    assert (call.ms, call.tokens_in, call.tokens_out) == (3000, 612, 148)
    assert call.started_on == T1


def test_missing_token_counts_stay_empty_not_zero(store: Store) -> None:
    interview_id = general(store)
    ai = MeteredAi(questions_reply(), tokens=(None, None))
    sv.write_questions(store, make_env(ai), Config(), interview_id)
    [call] = store.get_calls(interview_id)
    assert call.tokens_in is None
    assert call.tokens_out is None


def test_a_bad_reply_then_a_good_one_make_attempts_1_and_2(store: Store) -> None:
    interview_id = general(store)
    ai = MeteredAi("not json", questions_reply(), tokens=(100, 10))
    sv.write_questions(store, make_env(ai), Config(), interview_id)
    assert [(c.attempt, c.ok) for c in store.get_calls(interview_id)] == [(1, True), (2, True)]


def test_a_rate_limit_is_kept_as_a_failed_call(store: Store) -> None:
    interview_id = general(store)
    ai = FakeAi(TailorError("busy", status=429), questions_reply())
    sv.write_questions(store, make_env(ai), Config(), interview_id)
    first, second = store.get_calls(interview_id)
    assert (first.ok, first.http_status, first.attempt) == (False, 429, 1)
    assert (second.ok, second.attempt) == (True, 2)


def test_each_answer_keeps_its_speech_numbers(store: Store) -> None:
    interview_id = started(store)
    answer(store, interview_id, 1)
    answer(store, interview_id, 2)
    one, two = store.get_calls(interview_id)
    assert (one.step, one.idx, one.attempt, one.ok) == ("speech", 1, 1, True)
    assert (one.ms, one.length_s, one.time_info, one.started_on) == (900, 20.0, "word times", T0)
    assert two.idx == 2
    assert two.attempt == 1  # attempts count per answer


def test_a_failed_speech_attempt_then_a_retry_are_two_calls(store: Store) -> None:
    interview_id = started(store)
    for result in (TranscribeError("busy", status=429, retry_after=1), spoken(LONG)):
        sv.submit_answer(
            store, FakeStt(result), interview_id, 1, audio=b"a", filename="a.webm",
            content_type="audio/webm", duration_s=20.0, now=T0,
        )  # fmt: skip
    first, second = store.get_calls(interview_id)
    assert (first.ok, first.http_status, first.attempt) == (False, 429, 1)
    assert (second.ok, second.attempt) == (True, 2)


def test_time_info_says_which_times_came_back(store: Store) -> None:
    interview_id = started(store)
    sentences = Transcription(LONG, (), spoken().words[:2], 20.0, ms=500)
    nothing = Transcription(LONG, (), (), 20.0, ms=500)
    for idx, result in ((1, sentences), (2, nothing)):
        sv.submit_answer(
            store, FakeStt(result), interview_id, idx, audio=b"a", filename="a.webm",
            content_type="audio/webm", duration_s=20.0, now=T0,
        )  # fmt: skip
    assert [c.time_info for c in store.get_calls(interview_id)] == ["sentence times only", "none"]


def test_feedback_keeps_its_numbers_and_the_wait(store: Store) -> None:
    interview_id = preparing(store)
    ai = MeteredAi(feedback_reply(1), tokens=(4180, 1350))
    env = make_env(ai)
    env.now = lambda: "2026-11-11T17:10:24.500000+05:30"  # 24.5 s after the click (T1)
    sv.build_feedback(store, env, Config(), interview_id)
    feedback = [c for c in store.get_calls(interview_id) if c.step == "feedback"]
    [call] = feedback
    assert (call.tokens_in, call.tokens_out, call.ms) == (4180, 1350, 3000)
    assert call.wait_ms == 24_500


def test_nothing_in_the_call_rows_is_text(store: Store) -> None:
    interview_id = preparing(store)
    sv.build_feedback(store, make_env(FakeAi(feedback_reply(1))), Config(), interview_id)
    rows = store.conn.execute("SELECT * FROM interview_call").fetchall()
    assert rows
    for row in rows:
        for value in tuple(row):
            assert not isinstance(value, str) or value in {
                "", "speech", "feedback", "questions", "word times", T0, T1,
            }, value  # fmt: skip


def test_deleting_an_interview_removes_its_call_rows(store: Store) -> None:
    interview_id = started(store)
    answer(store, interview_id, 1)
    store.delete_interview(interview_id)
    assert store.conn.execute("SELECT COUNT(*) FROM interview_call").fetchone()[0] == 0
