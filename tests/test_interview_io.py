"""Interview Prep: question writing and the speech service client."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from jobscan.interview import questions as qs
from jobscan.interview.feedback import FeedbackError
from jobscan.interview.stt import (
    MAX_AUDIO_BYTES,
    GroqTranscriber,
    TranscribeError,
    parse_reply,
)

# ---- the user's own questions ---------------------------------------------------------------


def test_own_questions_drop_blanks_and_repeats() -> None:
    got, error = qs.clean_own_questions(["  Why this role?  ", "", "why this role?", "How?? ok"])
    assert error == ""
    assert got == ["Why this role?", "How?? ok"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "at least one"),
        (["", "  "], "at least one"),
        (["abcd"], "at least 5"),
        (["word " * 31], "at most 30 words"),
        (["x" * 401], "at most 400"),
        ([f"Question number {i}" for i in range(6)], "at most 5"),
    ],
)
def test_own_question_errors(raw: list[str], message: str) -> None:
    got, error = qs.clean_own_questions(raw)
    assert got == []
    assert message in error


def test_own_questions_are_numbered_in_order() -> None:
    assert [(q.slot, q.kind) for q in qs.own_questions(["A one", "B two"])] == [
        (1, "own"),
        (2, "own"),
    ]


# ---- the model's questions ------------------------------------------------------------------


def _questions_reply(count: int = 5, text: str = "What did you do?") -> str:
    return json.dumps(
        {"questions": [{"slot": i, "kind": "x", "text": text} for i in range(1, count + 1)]}
    )


def test_parse_questions_follows_the_slots() -> None:
    got = qs.parse_questions(_questions_reply())
    assert [(q.slot, q.kind) for q in got] == [
        (1, "introduction"), (2, "behavioural"), (3, "behavioural"), (4, "situational"), (5, "gap"),
    ]  # fmt: skip


@pytest.mark.parametrize(
    "reply",
    [
        _questions_reply(4),
        _questions_reply(6),
        _questions_reply(text=" ".join(["word"] * 31)),
        _questions_reply(text="   "),
        json.dumps({"questions": ["a", "b", "c", "d", "e"]}),
        "not json",
    ],
)
def test_parse_questions_rejects_bad_replies(reply: str) -> None:
    with pytest.raises(FeedbackError):
        qs.parse_questions(reply)


def test_question_prompt_has_cv_role_must_haves_and_recent_questions() -> None:
    msg = qs.build_user_message(
        cv_text="CV body",
        role_lines=["Title: TPM", "Company: Acme"],
        must_haves=[("API integrations", "Met"), ("Payments", "Missing")],
        recent=["Old question?"],
    )
    assert "CV body" in msg
    assert "Title: TPM" in msg
    assert "- API integrations: Met" in msg
    assert "- Payments: Missing" in msg
    assert "ALREADY ASKED" in msg
    assert "- Old question?" in msg
    assert "5. gap" in msg


def test_question_prompt_leaves_out_empty_parts() -> None:
    msg = qs.build_user_message(cv_text="CV", role_lines=["Title: TPM"], must_haves=[], recent=[])
    assert "MUST-HAVES" not in msg
    assert "ALREADY ASKED" not in msg


# ---- the speech service ---------------------------------------------------------------------


class Session:
    def __init__(self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, **kwargs})
        response = requests.Response()
        response.status_code = self.status
        response.headers.update(self.headers)
        response._content = json.dumps(self.body if self.body is not None else {}).encode()
        return response


GOOD_BODY = {
    "text": " I led it. ",
    "duration": 4.2,
    "words": [{"word": "I", "start": 0.5, "end": 0.7}, {"word": "led", "start": 0.7, "end": 1.0}],
    "segments": [{"start": 0.5, "end": 1.5, "text": "I led it."}],
}


def _groq(session: Session, **kw: Any) -> GroqTranscriber:
    return GroqTranscriber(
        "https://api.example/openai/v1/", "whisper-large-v3", "k", session=session, **kw
    )  # type: ignore[arg-type]


def test_transcribe_sends_the_request_and_reads_times() -> None:
    session = Session(body=GOOD_BODY)
    result = _groq(session).transcribe(b"audio", "a.webm", "audio/webm")
    call = session.calls[0]
    assert call["url"] == "https://api.example/openai/v1/audio/transcriptions"
    assert call["headers"] == {"Authorization": "Bearer k"}
    fields = call["data"]
    assert ("model", "whisper-large-v3") in fields
    assert ("language", "en") in fields
    assert ("response_format", "verbose_json") in fields
    assert ("timestamp_granularities[]", "word") in fields
    assert not any(k == "prompt" for k, _ in fields)
    assert result.text == "I led it."
    assert result.duration_s == 4.2
    assert [(w.start, w.end) for w in result.words] == [(0.5, 0.7), (0.7, 1.0)]
    assert [(s.start, s.end) for s in result.sentences] == [(0.5, 1.5)]


def test_transcribe_can_send_a_prompt_hint() -> None:
    session = Session(body=GOOD_BODY)
    _groq(session, prompt="Umm, uh, like.").transcribe(b"a", "a.webm", "audio/webm")
    assert ("prompt", "Umm, uh, like.") in session.calls[0]["data"]


def test_missing_word_times_are_allowed() -> None:
    result = parse_reply({"text": "hello there"})
    assert result.words == ()
    assert result.sentences == ()
    assert result.duration_s is None


def test_a_reply_without_text_is_refused() -> None:
    with pytest.raises(TranscribeError):
        parse_reply({"words": []})


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(401, "refused the key"), (403, "refused the key"), (404, "not found"),
     (500, "had a problem"), (503, "had a problem"), (400, "refused the recording")],
)  # fmt: skip
def test_http_errors_have_plain_messages(status: int, fragment: str) -> None:
    with pytest.raises(TranscribeError) as info:
        _groq(Session(status=status)).transcribe(b"a", "a.webm", "audio/webm")
    assert fragment in str(info.value)
    assert info.value.status == status


def test_rate_limit_carries_the_wait() -> None:
    with pytest.raises(TranscribeError) as info:
        _groq(Session(status=429, headers={"retry-after": "12"})).transcribe(
            b"a", "a.webm", "audio/webm"
        )
    assert info.value.status == 429
    assert info.value.retry_after == 12.0


def test_oversize_audio_is_refused_before_sending() -> None:
    session = Session(body=GOOD_BODY)
    with pytest.raises(TranscribeError, match="too large"):
        _groq(session).transcribe(b"x" * (MAX_AUDIO_BYTES + 1), "a.webm", "audio/webm")
    assert session.calls == []


def test_network_failures_have_plain_messages() -> None:
    class Down:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        def post(self, *a: Any, **k: Any) -> requests.Response:
            raise self.exc

    for exc, text in ((requests.Timeout(), "too long"), (requests.ConnectionError(), "reached")):
        with pytest.raises(TranscribeError, match=text):
            GroqTranscriber("https://x/v1", "m", "k", session=Down(exc)).transcribe(  # type: ignore[arg-type]
                b"a", "a.webm", "audio/webm"
            )


def test_audio_is_never_in_an_error_message() -> None:
    with pytest.raises(TranscribeError) as info:
        _groq(Session(status=500)).transcribe(b"SECRET-AUDIO-BYTES", "a.webm", "audio/webm")
    assert "SECRET" not in str(info.value)
