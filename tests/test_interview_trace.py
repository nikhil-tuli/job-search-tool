"""Interview traces: the spans and numbers, what is sent, and that no text is in them."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jobscan import cli, db
from jobscan.config import Config
from jobscan.interview import service as sv
from jobscan.interview import store as st
from jobscan.interview import trace as tr
from jobscan.interview.service import Pacer
from jobscan.langfuse_push import LangfuseError
from jobscan.store import Store
from jobscan.web import create_app
from tests.test_interview_calls import MeteredAi
from tests.test_interview_service import (
    LONG,
    FakeAi,
    FakeStt,
    feedback_reply,
    make_env,
    preparing,
    spoken,
)
from tests.test_interview_web import run_own, upload
from tests.test_langfuse_push import ON, Runner, Session, daily, lf, run_push, scored, span_attrs

TZ = "+05:30"


def at(clock: str) -> str:
    return f"2026-11-16T{clock}{TZ}"


def interview(kind: str = "Company role") -> st.InterviewRow:
    return st.InterviewRow(
        id=41, kind=kind, role_portal="LinkedIn", role_job_id="1", role_company="Acme Secret Co",
        role_title="Secret Role Title", general_title="", status=st.READY,
        started_on=at("10:00:00"), updated_on=at("10:10:00"), cv_name="cv.docx",
        cv_uploaded_on="", read_aloud=True, question_model="ministral-14b",
        feedback_model="ministral-14b", prompt_version="1", stage="", error="", note="",
        stats={}, questions=5, answered=4, spoken_score=70, answer_score=80,
    )  # fmt: skip


_ids = iter(range(1, 1000))


def call(step: str, started: str, ms: int, **kw: Any) -> st.CallRow:
    fields: dict[str, Any] = {
        "idx": None, "attempt": 1, "ok": True, "http_status": None, "tokens_in": None,
        "tokens_out": None, "length_s": None, "time_info": "", "wait_ms": None,
    }  # fmt: skip
    fields.update(kw)
    return st.CallRow(id=next(_ids), step=step, started_on=started, ms=ms, **fields)


def spans_of(rows: list[st.CallRow], row: st.InterviewRow | None = None) -> list[dict[str, Any]]:
    return tr.build_trace(row or interview(), rows, config=Config())


def by_name(spans: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in spans}


def seconds(span: dict[str, Any]) -> float:
    return (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e9


def interview_41() -> list[st.CallRow]:
    """Company role, 4 answers, one speech retry (section 17.2 of the requirements)."""
    return [
        call("questions", at("10:00:00"), 3000, tokens_in=612, tokens_out=148),
        call("speech", at("10:02:05"), 2900, idx=1, length_s=62.4, time_info="word times"),
        call("speech", at("10:04:30"), 3600, idx=2, length_s=88.0, time_info="word times"),
        call("speech", at("10:06:00"), 1900, idx=3, length_s=41.2, time_info="sentence times only"),
        call("speech", at("10:08:00"), 400, idx=4, length_s=75.5, ok=False, http_status=429),
        call(
            "speech", "2026-11-16T10:08:00.400000" + TZ, 3100, idx=4, attempt=2, length_s=75.5,
            time_info="word times",
        ),
        call(
            "feedback", at("10:08:52"), 20000, tokens_in=4180, tokens_out=1350, wait_ms=24500
        ),
    ]  # fmt: skip


# ---- the three mock interviews --------------------------------------------------------------


def test_interview_41_matches_the_mock_numbers() -> None:
    spans = spans_of(interview_41())
    root = spans[0]
    meta = span_attrs(root)
    assert root["name"] == "Interview 41"
    assert seconds(root) == 552.0
    assert meta["langfuse.trace.metadata.active_time_s"] == "302.0"
    counts = {
        k.removeprefix("langfuse.trace.metadata."): v
        for k, v in meta.items()
        if k.startswith("langfuse.trace.metadata.") and k.endswith(("calls", "errors", "retries"))
    }
    assert counts == {
        "model_calls": "2", "model_errors": "0", "model_retries": "0",
        "speech_calls": "5", "speech_errors": "1", "speech_retries": "1",
    }  # fmt: skip
    named = [s["name"] for s in spans]
    assert named == [
        "Interview 41", "Question writing", "Answer 1", "Speech to text", "Answer 2",
        "Speech to text", "Answer 3", "Speech to text", "Answer 4", "Speech to text (error)",
        "Speech to text (retry)", "Feedback writing",
    ]  # fmt: skip


def test_interview_41_answer_and_model_spans() -> None:
    spans = spans_of(interview_41())
    answers = [s for s in spans if s["name"].startswith("Answer")]
    assert [round(seconds(a), 1) for a in answers] == [65.3, 91.6, 43.1, 79.0]
    questions = by_name(spans)["Question writing"]
    q = span_attrs(questions)
    assert q["langfuse.observation.type"] == "generation"
    assert json.loads(q["langfuse.observation.usage_details"]) == {"input": 612, "output": 148}
    assert q["langfuse.observation.metadata.input_tokens_per_s"] == "204.0"
    assert q["langfuse.observation.metadata.output_tokens_per_s"] == "49.3"
    assert q["langfuse.observation.metadata.model_time_s"] == "3.0"
    feedback = span_attrs(by_name(spans)["Feedback writing"])
    assert feedback["langfuse.observation.metadata.input_tokens_per_s"] == "209.0"
    assert feedback["langfuse.observation.metadata.output_tokens_per_s"] == "67.5"
    assert feedback["langfuse.observation.metadata.wait_time_s"] == "24.5"
    assert feedback["langfuse.observation.metadata.model_time_s"] == "20.0"


def test_interview_41_answer_attributes_and_speech_children() -> None:
    spans = spans_of(interview_41())
    answers = {s["name"]: span_attrs(s) for s in spans if s["name"].startswith("Answer")}
    one = answers["Answer 1"]
    assert one["langfuse.observation.metadata.audio_length_s"] == "62.4"
    assert one["langfuse.observation.metadata.speech_response_time_s"] == "2.9"
    assert one["langfuse.observation.metadata.time_info"] == "word times"
    assert answers["Answer 3"]["langfuse.observation.metadata.time_info"] == "sentence times only"
    four = answers["Answer 4"]
    assert four["langfuse.observation.metadata.speech_attempts"] == "2"
    assert four["langfuse.observation.metadata.speech_response_time_s"] == "3.1"
    error = next(s for s in spans if s["name"] == "Speech to text (error)")
    attrs = span_attrs(error)
    assert attrs["langfuse.observation.level"] == "ERROR"
    assert attrs["langfuse.observation.metadata.http_status"] == "429"
    assert error["status"]["message"] == "failed (HTTP 429)"


def test_spans_nest_as_in_the_design() -> None:
    spans = spans_of(interview_41())
    root_id = spans[0]["spanId"]
    ids = {s["spanId"]: s for s in spans}
    assert len(ids) == len(spans)
    assert {s["traceId"] for s in spans} == {tr.trace_id_of(interview())}
    for s in spans[1:]:
        parent = ids[s["parentSpanId"]]
        if s["name"].startswith("Speech to text"):
            assert parent["name"].startswith("Answer")
        else:
            assert parent["spanId"] == root_id  # Question and Feedback writing sit beside answers
    assert "parentSpanId" not in spans[0]


def test_interview_42_ended_early_after_one_answer() -> None:
    rows = [
        call("questions", at("10:00:00"), 2800, tokens_in=540, tokens_out=131),
        call("speech", at("10:01:10"), 2400, idx=1, length_s=54.0, time_info="word times"),
        call("feedback", at("10:02:05"), 6000, tokens_in=980, tokens_out=420, wait_ms=6000),
    ]
    spans = spans_of(rows, interview("General role"))
    assert seconds(spans[0]) == 131.0
    assert span_attrs(spans[0])["langfuse.trace.metadata.active_time_s"] == "65.2"
    q = span_attrs(by_name(spans)["Question writing"])
    assert q["langfuse.observation.metadata.input_tokens_per_s"] == "192.9"
    assert q["langfuse.observation.metadata.output_tokens_per_s"] == "46.8"
    f = span_attrs(by_name(spans)["Feedback writing"])
    assert f["langfuse.observation.metadata.input_tokens_per_s"] == "163.3"
    assert f["langfuse.observation.metadata.output_tokens_per_s"] == "70.0"


def test_interview_43_my_questions_has_no_question_writing() -> None:
    rows = [
        call("speech", at("10:01:00"), 2200, idx=1, length_s=47.3, time_info="word times"),
        call("speech", at("10:03:00"), 2700, idx=2, length_s=70.8, time_info="word times"),
        call(
            "feedback", "2026-11-16T10:04:20.200000" + TZ, 12500, tokens_in=1960,
            tokens_out=880, wait_ms=12500,
        ),
    ]  # fmt: skip
    spans = spans_of(rows, interview("My questions"))
    assert "Question writing" not in [s["name"] for s in spans]
    assert round(seconds(spans[0]), 1) == 260.0
    assert span_attrs(spans[0])["langfuse.trace.metadata.active_time_s"] == "135.5"
    f = span_attrs(by_name(spans)["Feedback writing"])
    assert f["langfuse.observation.metadata.input_tokens_per_s"] == "156.8"
    assert f["langfuse.observation.metadata.output_tokens_per_s"] == "70.4"


def test_missing_token_counts_say_not_available() -> None:
    rows = [
        call("speech", at("10:01:00"), 2000, idx=1, length_s=30.0, time_info="none"),
        call("feedback", at("10:02:00"), 5000),
    ]
    feedback = span_attrs(by_name(spans_of(rows))["Feedback writing"])
    assert "langfuse.observation.usage_details" not in feedback
    assert feedback["langfuse.observation.metadata.tokens"] == "not available"
    assert feedback["langfuse.observation.metadata.output_tokens_per_s"] == "not available"


def test_a_failed_model_call_and_its_retry() -> None:
    rows = [
        call("questions", at("10:00:00"), 400, ok=False, http_status=429),
        call("questions", at("10:00:31"), 3000, attempt=2, tokens_in=10, tokens_out=5),
        call("speech", at("10:02:00"), 2000, idx=1, length_s=30.0, time_info="word times"),
        call("feedback", at("10:03:00"), 5000, tokens_in=10, tokens_out=5),
    ]
    spans = spans_of(rows)
    assert [s["name"] for s in spans if s["name"].startswith("Question")] == [
        "Question writing (error)",
        "Question writing (retry)",
    ]
    meta = span_attrs(spans[0])
    assert meta["langfuse.trace.metadata.model_errors"] == "1"
    assert meta["langfuse.trace.metadata.model_retries"] == "1"


def test_an_answer_whose_speech_never_worked_is_marked_as_an_error() -> None:
    rows = [call("speech", at("10:01:00"), 500, idx=1, length_s=20.0, ok=False, http_status=500)]
    spans = spans_of(rows)
    answer_span = next(s for s in spans if s["name"] == "Answer 1")
    assert span_attrs(answer_span)["langfuse.observation.level"] == "ERROR"
    assert span_attrs(answer_span)["langfuse.observation.metadata.time_info"] == "none"


def test_ids_are_the_same_every_time() -> None:
    rows = interview_41()
    one = [(s["traceId"], s["spanId"]) for s in spans_of(rows)]
    two = [(s["traceId"], s["spanId"]) for s in spans_of(rows)]
    assert one == two


def test_no_text_is_in_the_payload() -> None:
    payload = json.dumps(spans_of(interview_41()))
    for text in ("Acme Secret Co", "Secret Role Title", "cv.docx", "LinkedIn"):
        assert text not in payload
    assert '"langfuse.observation.input"' not in payload
    assert '"langfuse.observation.output"' not in payload


# ---- sending ----------------------------------------------------------------------------------


def ready(store: Store, *, tokens: tuple[int | None, int | None] = (4180, 1350)) -> int:
    interview_id = preparing(store, {1: LONG, 2: LONG})
    ai = MeteredAi(feedback_reply(1, 2), tokens=tokens)
    assert sv.build_feedback(store, make_env(ai), Config(), interview_id) == ""
    return interview_id


def test_a_ready_interview_is_sent_once_and_then_marked(store: Store) -> None:
    interview_id = ready(store)
    session = Session()
    assert tr.send_interview_trace(store, Config(), interview_id, lf(session))
    [(url, body, _headers)] = session.posts
    assert url.endswith("/api/public/otel/v1/traces")
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    names = [s["name"] for s in spans]
    assert names[0] == f"Interview {interview_id}"
    assert "Answer 1" in names
    assert "Speech to text" in names
    assert "Feedback writing" in names
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.traced_on
    assert not tr.send_interview_trace(store, Config(), interview_id, lf(session))  # not twice
    assert len(session.posts) == 1


def test_what_was_said_and_written_never_leaves(store: Store) -> None:
    interview_id = ready(store)
    session = Session()
    tr.send_interview_trace(store, Config(), interview_id, lf(session))
    sent = json.dumps(session.posts[0][1])
    for text in ("lender integration", "Add one number.", "Question one here?", "Good."):
        assert text not in sent


def test_a_refusal_leaves_the_interview_unsent(store: Store) -> None:
    interview_id = ready(store)
    with pytest.raises(LangfuseError):
        tr.send_interview_trace(store, Config(), interview_id, lf(Session(status=401)))
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.traced_on == ""


@pytest.mark.parametrize("status", [st.INCOMPLETE, st.FAILED, st.PREPARING, st.IN_PROGRESS])
def test_only_a_ready_interview_is_sent(store: Store, status: str) -> None:
    interview_id = ready(store)
    store.set_interview_status(interview_id, status, now="2026-11-16T11:00:00+05:30")
    session = Session()
    assert not tr.send_interview_trace(store, Config(), interview_id, lf(session))
    assert session.posts == []


def test_an_interview_from_before_call_numbers_is_skipped(store: Store) -> None:
    interview_id = ready(store)
    store.conn.execute("DELETE FROM interview_call")
    session = Session()
    assert not tr.send_interview_trace(store, Config(), interview_id, lf(session))
    assert store.untraced_ready_interviews() == []


# ---- the live send and the backup ------------------------------------------------------------


def test_the_live_send_sends_when_on(store: Store) -> None:
    interview_id = ready(store)
    session = Session()
    tr.live_tracer(lambda _c: lf(session))(store, Config(), interview_id)
    assert len(session.posts) == 1


def test_the_live_send_does_nothing_when_the_switch_is_off(store: Store) -> None:
    interview_id = ready(store)
    session = Session()
    off = Config(interview_langfuse_on=0)
    tr.live_tracer(lambda _c: lf(session))(store, off, interview_id)
    assert session.posts == []


def test_the_live_send_skips_quietly_without_keys(store: Store) -> None:
    interview_id = ready(store)

    def no_keys(_config: Config) -> Any:
        raise LangfuseError("The Langfuse keys are missing.")

    tr.live_tracer(no_keys)(store, Config(), interview_id)
    assert store.untraced_ready_interviews() == [interview_id]


def test_a_failed_live_send_changes_nothing(store: Store) -> None:
    interview_id = ready(store)
    session = Session(status=500)
    tr.live_tracer(lambda _c: lf(session))(store, Config(), interview_id)
    assert store.untraced_ready_interviews() == [interview_id]


def test_the_backup_sends_what_is_left_and_skips_what_was_sent(store: Store) -> None:
    first = ready(store)
    session = Session()
    tr.send_interview_trace(store, Config(), first, lf(session))
    second = ready(store)
    lines: list[str] = []
    sent = tr.push_interviews(store, Config(), lf(session), lines.append, sleep=lambda _s: None)
    assert sent == 1
    assert len(session.posts) == 2
    assert store.untraced_ready_interviews() == []
    assert second != first


def test_the_backup_stops_at_the_first_refusal(store: Store) -> None:
    ready(store)
    with pytest.raises(LangfuseError):
        tr.push_interviews(
            store, Config(), lf(Session(status=401)), lambda _l: None, sleep=lambda _s: None
        )
    assert len(store.untraced_ready_interviews()) == 1


# ---- wiring: the feedback step, the web app, the push command, the daily run ----------------


def test_the_tracer_runs_after_the_interview_is_ready(store: Store) -> None:
    interview_id = preparing(store)
    seen: list[str] = []

    def tracer(s: Store, _config: Config, i: int) -> None:
        row = s.get_interview(i)
        assert row is not None
        seen.append(row.status)

    env = make_env(FakeAi(feedback_reply(1)))
    env.trace = tracer
    assert sv.build_feedback(store, env, Config(), interview_id) == ""
    assert seen == [st.READY]


def test_a_tracer_that_blows_up_changes_nothing(store: Store) -> None:
    interview_id = preparing(store)

    def tracer(_s: Store, _c: Config, _i: int) -> None:
        raise RuntimeError("boom")

    env = make_env(FakeAi(feedback_reply(1)))
    env.trace = tracer
    assert sv.build_feedback(store, env, Config(), interview_id) == ""
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.READY


def web_client(store: Store, ai: FakeAi, make_langfuse: Any) -> Any:
    return create_app(
        store,
        secret_key="t",
        make_interview_ai=lambda _c: ai,
        make_interview_stt=lambda _c: FakeStt(spoken()),
        interview_pacer=Pacer(monotonic=lambda: 0.0, sleep=lambda _s: None),
        make_langfuse=make_langfuse,
    ).test_client()


def test_finishing_in_the_web_app_sends_the_trace(store: Store) -> None:
    session = Session()
    ai = FakeAi(feedback_reply(1))
    client = web_client(store, ai, lambda _c: lf(session))
    interview_id = run_own(client, store)
    upload(client, interview_id, 1)
    assert client.post(f"/interview/{interview_id}/finish").get_json()["discarded"] is False
    assert len(session.posts) == 1
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.READY
    assert row.traced_on


def test_the_feedback_page_works_when_langfuse_is_unreachable(store: Store) -> None:
    def down(_config: Config) -> Any:
        raise LangfuseError("The Langfuse keys are missing.")

    client = web_client(store, FakeAi(feedback_reply(1)), down)
    interview_id = run_own(client, store)
    upload(client, interview_id, 1)
    client.post(f"/interview/{interview_id}/finish")
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.READY
    assert row.traced_on == ""
    assert "AI estimate" in client.get(f"/interview/{interview_id}").get_data(as_text=True)


def bodies(session: Session) -> list[list[str]]:
    return [
        [s["name"] for s in body["resourceSpans"][0]["scopeSpans"][0]["spans"]]
        for _url, body, _h in session.posts
    ]


def test_fit_scoring_only_when_only_its_switch_is_on(store: Store) -> None:
    scored(store)
    ready(store)
    session = Session()
    result = run_push(store, session, replace(ON, interview_langfuse_on=0))
    assert (result.traces, result.interviews) == (1, 0)
    assert [n for names in bodies(session) for n in names] == ["fit-score"]


def test_interviews_only_when_only_their_switch_is_on(store: Store) -> None:
    scored(store)
    interview_id = ready(store)
    session = Session()
    result = run_push(store, session, replace(ON, langfuse_on=0))
    assert (result.traces, result.interviews) == (0, 1)
    assert bodies(session)[0][0] == f"Interview {interview_id}"
    assert "fit-score" not in [n for names in bodies(session) for n in names]


def test_both_switches_send_both(store: Store) -> None:
    scored(store)
    ready(store)
    session = Session()
    result = run_push(store, session, replace(ON, interview_langfuse_on=1))
    assert (result.traces, result.interviews) == (1, 1)


def test_the_push_command_runs_with_only_the_interview_switch_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session()
    monkeypatch.setattr(cli, "LANGFUSE_CLIENT_FACTORY", lambda _c: lf(session))
    conn = db.connect(tmp_path / "j.db")
    db.migrate(conn)
    Store(conn).set_config_raw({"langfuse_on": "0", "interview_langfuse_on": "1"})
    conn.close()
    out = io.StringIO()
    code = cli.main(["--db", str(tmp_path / "j.db"), "push-traces"], out=out, sleep=lambda _s: None)
    assert code == cli.EXIT_OK
    assert "0 interview traces" in out.getvalue()


def test_the_daily_job_pushes_with_only_the_interview_switch_on(
    store: Store, tmp_path: Path
) -> None:
    store.set_config_raw({"langfuse_on": "0", "interview_langfuse_on": "1"})
    seen, notes = daily(store, tmp_path, Runner(), [0])
    assert seen[0][0][-1] == "push-traces"
    assert notes == []
