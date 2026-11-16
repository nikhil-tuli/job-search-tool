"""Interview traces for Langfuse: numbers and labels only. Never a question, a transcript, the
feedback, the CV or the role text.

One trace is one interview. Inside it: Question writing (not for My questions), one Answer span
per answer holding its Speech to text calls, and Feedback writing. Question writing and Feedback
writing happen once per interview, so they sit beside the Answer spans, not inside them.

An interview is sent once, when its feedback is ready. It is marked sent only after Langfuse
accepts it, and its ids come from the interview, so a repeat never makes a second trace.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..config import Config
from ..langfuse_push import (
    BATCH_PAUSE_SECONDS,
    SPAN_ERROR,
    SPAN_KIND_CLIENT,
    SPAN_OK,
    LangfuseClient,
    LangfuseError,
    _attr,
    _nanos,
    client_from_env,
)
from ..store import Store
from ..timeutil import from_iso, now_ist, to_iso
from . import store as st

NOT_AVAILABLE = "not available"
QUESTION_WRITING = "Question writing"
FEEDBACK_WRITING = "Feedback writing"
SPEECH_TO_TEXT = "Speech to text"

Tracer = Callable[[Store, Config, int], None]


def trace_id_of(row: st.InterviewRow) -> str:
    return hashlib.sha256(f"interview-trace|{row.id}|{row.started_on}".encode()).hexdigest()[:32]


def _span_id(row: st.InterviewRow, *parts: object) -> str:
    key = "|".join(str(p) for p in (row.id, row.started_on, *parts))
    return hashlib.sha256(f"interview-span|{key}".encode()).hexdigest()[:16]


def _seconds(ms: int) -> float:
    return round(ms / 1000, 1)


def _per_second(tokens: int | None, ms: int) -> float | str:
    """Tokens divided by seconds. "not available" when the provider gave no count."""
    if tokens is None or ms <= 0:
        return NOT_AVAILABLE
    return round(tokens / (ms / 1000), 1)


def _name(base: str, call: st.CallRow) -> str:
    if not call.ok:
        return f"{base} (error)"
    return f"{base} (retry)" if call.attempt > 1 else base


def _status(call: st.CallRow) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The span status and its level attributes. Never any provider text."""
    if call.ok:
        return {"code": SPAN_OK}, [_attr("langfuse.observation.level", "DEFAULT")]
    reason = f"failed (HTTP {call.http_status})" if call.http_status else "failed"
    return (
        {"code": SPAN_ERROR, "message": reason},
        [
            _attr("langfuse.observation.level", "ERROR"),
            _attr("langfuse.observation.status_message", reason),
        ],
    )


def _span(
    trace_id: str,
    span_id: str,
    name: str,
    start: datetime,
    end: datetime,
    attributes: list[dict[str, Any]],
    status: dict[str, Any],
    *,
    parent: str = "",
) -> dict[str, Any]:
    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": SPAN_KIND_CLIENT,
        "startTimeUnixNano": str(_nanos(start)),
        "endTimeUnixNano": str(_nanos(end)),
        "attributes": attributes,
        "status": status,
    }
    if parent:
        span["parentSpanId"] = parent
    return span


def _model_span(
    row: st.InterviewRow, call: st.CallRow, trace_id: str, parent: str, base: str, model: str
) -> tuple[dict[str, Any], datetime, datetime]:
    start = from_iso(call.started_on)
    end = start + timedelta(milliseconds=call.ms)
    status, level = _status(call)
    meta = "langfuse.observation.metadata."
    attributes = [
        _attr("langfuse.observation.type", "generation"),
        _attr("gen_ai.request.model", model),
        _attr(meta + "attempt", call.attempt),
        _attr(meta + "model_time_s", _seconds(call.ms)),
        _attr(meta + "input_tokens_per_s", _per_second(call.tokens_in, call.ms)),
        _attr(meta + "output_tokens_per_s", _per_second(call.tokens_out, call.ms)),
        *level,
    ]
    if call.tokens_in is not None and call.tokens_out is not None:
        usage = f'{{"input": {call.tokens_in}, "output": {call.tokens_out}}}'
        attributes.append(_attr("langfuse.observation.usage_details", usage))
    else:
        attributes.append(_attr(meta + "tokens", NOT_AVAILABLE))
    if call.wait_ms is not None:
        attributes.append(_attr(meta + "wait_time_s", _seconds(call.wait_ms)))
    if call.http_status is not None and not call.ok:
        attributes.append(_attr(meta + "http_status", call.http_status))
    span = _span(
        trace_id, _span_id(row, call.step, call.id), _name(base, call), start, end, attributes,
        status, parent=parent,
    )  # fmt: skip
    return span, start, end


def build_trace(
    row: st.InterviewRow, calls: list[st.CallRow], *, config: Config
) -> list[dict[str, Any]]:
    """The spans of one interview, from its saved call numbers. No text goes in."""
    trace_id = trace_id_of(row)
    root_id = _span_id(row, "root")
    spans: list[dict[str, Any]] = []
    top: list[tuple[datetime, datetime]] = []  # the top-level steps, for active time

    for call in (c for c in calls if c.step == "questions"):
        span, start, end = _model_span(
            row, call, trace_id, root_id, QUESTION_WRITING, row.question_model or config.ai_model
        )
        spans.append(span)
        top.append((start, end))

    answers: dict[int, list[st.CallRow]] = {}
    for call in calls:
        if call.step == "speech" and call.idx is not None:
            answers.setdefault(call.idx, []).append(call)
    for idx in sorted(answers):
        _, start, end = _answer_spans(row, answers[idx], trace_id, root_id, idx, config, spans)
        top.append((start, end))

    for call in (c for c in calls if c.step == "feedback"):
        span, start, end = _model_span(
            row, call, trace_id, root_id, FEEDBACK_WRITING, row.feedback_model or config.ai_model
        )
        spans.append(span)
        top.append((start, end))

    first = min(s for s, _ in top) if top else from_iso(row.started_on)
    last = max(e for _, e in top) if top else first
    active = sum((e - s).total_seconds() for s, e in top)
    spans.insert(0, _root_span(row, calls, trace_id, root_id, first, last, active, config))
    return spans


def _answer_spans(
    row: st.InterviewRow,
    attempts: list[st.CallRow],
    trace_id: str,
    root_id: str,
    idx: int,
    config: Config,
    spans: list[dict[str, Any]],
) -> tuple[dict[str, Any], datetime, datetime]:
    """One Answer span, from the estimated recording start to the moment the text returned."""
    first, last = attempts[0], attempts[-1]
    length = first.length_s or 0.0
    arrival = from_iso(first.started_on)
    start = arrival - timedelta(seconds=length)
    end = from_iso(last.started_on) + timedelta(milliseconds=last.ms)
    done = next((c for c in attempts if c.ok), None)
    answer_id = _span_id(row, "answer", idx)
    meta = "langfuse.observation.metadata."
    attributes = [
        _attr("langfuse.observation.type", "span"),
        _attr(meta + "audio_length_s", round(length, 1)),
        _attr(meta + "speech_response_time_s", _seconds((done or last).ms)),
        _attr(meta + "time_info", done.time_info if done else "none"),
        _attr(meta + "speech_attempts", len(attempts)),
        _attr("langfuse.observation.level", "DEFAULT" if done else "ERROR"),
    ]
    answer = _span(
        trace_id, answer_id, f"Answer {idx}", start, end, attributes,
        {"code": SPAN_OK if done else SPAN_ERROR}, parent=root_id,
    )  # fmt: skip
    spans.append(answer)
    for call in attempts:
        call_start = from_iso(call.started_on)
        status, level = _status(call)
        child_attributes = [
            _attr("langfuse.observation.type", "span"),
            _attr(meta + "model", config.interview_stt_model),
            _attr(meta + "speech_response_time_s", _seconds(call.ms)),
            _attr(meta + "audio_length_s", round(call.length_s or 0.0, 1)),
            _attr(meta + "time_info", call.time_info or "none"),
            *level,
        ]
        if call.http_status is not None and not call.ok:
            child_attributes.append(_attr(meta + "http_status", call.http_status))
        spans.append(
            _span(
                trace_id,
                _span_id(row, "speech", call.id),
                _name(SPEECH_TO_TEXT, call),
                call_start,
                call_start + timedelta(milliseconds=call.ms),
                child_attributes,
                status,
                parent=answer_id,
            )
        )
    return answer, start, end


def _root_span(
    row: st.InterviewRow,
    calls: list[st.CallRow],
    trace_id: str,
    root_id: str,
    start: datetime,
    end: datetime,
    active_s: float,
    config: Config,
) -> dict[str, Any]:
    model = [c for c in calls if c.step in ("questions", "feedback")]
    speech = [c for c in calls if c.step == "speech"]
    trace = "langfuse.trace.metadata."
    attributes = [
        _attr("langfuse.observation.type", "span"),
        _attr("langfuse.trace.name", f"Interview {row.id}"),
        _attr("langfuse.trace.tags", ["jobscan", "interview", row.kind]),
        _attr(trace + "interview_id", row.id),
        _attr(trace + "type", row.kind),
        _attr(trace + "question_model", row.question_model),
        _attr(trace + "feedback_model", row.feedback_model),
        _attr(trace + "speech_model", config.interview_stt_model),
        _attr(trace + "prompt_version", row.prompt_version),
        _attr(trace + "active_time_s", round(active_s, 1)),
        _attr(trace + "model_calls", len(model)),
        _attr(trace + "model_errors", sum(not c.ok for c in model)),
        _attr(trace + "model_retries", sum(c.attempt > 1 for c in model)),
        _attr(trace + "speech_calls", len(speech)),
        _attr(trace + "speech_errors", sum(not c.ok for c in speech)),
        _attr(trace + "speech_retries", sum(c.attempt > 1 for c in speech)),
        _attr("langfuse.observation.level", "DEFAULT"),
    ]
    return _span(
        trace_id, root_id, f"Interview {row.id}", start, end, attributes, {"code": SPAN_OK}
    )


# ---- sending ----------------------------------------------------------------------------------


def send_interview_trace(
    store: Store,
    config: Config,
    interview_id: int,
    client: LangfuseClient,
    *,
    clock: Callable[[], datetime] = now_ist,
) -> bool:
    """Send one interview if it is Ready and not yet sent. True when it was sent.

    Raises LangfuseError when Langfuse does not accept it; the interview then stays unsent.
    """
    row = store.get_interview(interview_id)
    if row is None or row.status != st.READY or row.traced_on:
        return False
    calls = store.get_calls(interview_id)
    if not calls:  # from before call numbers were kept
        return False
    client.send_spans(build_trace(row, calls, config=config))
    store.mark_interview_traced(interview_id, to_iso(clock()))
    return True


def live_tracer(make_client: Callable[[Config], LangfuseClient] = client_from_env) -> Tracer:
    """Send an interview the moment its feedback is ready. A failure changes nothing.

    With the switch off, or no keys in this terminal, nothing is sent. The push command and the
    daily run send what is left.
    """

    def trace(store: Store, config: Config, interview_id: int) -> None:
        if not config.interview_langfuse_on:
            return
        try:
            send_interview_trace(store, config, interview_id, make_client(config))
        except LangfuseError:
            return

    return trace


def push_interviews(
    store: Store,
    config: Config,
    client: LangfuseClient,
    emit: Callable[[str], None],
    *,
    clock: Callable[[], datetime] = now_ist,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The backup: send every Ready interview that was not sent. Stops at the first failure."""
    sent = 0
    for interview_id in store.untraced_ready_interviews():
        if sent:
            sleep(BATCH_PAUSE_SECONDS)
        if send_interview_trace(store, config, interview_id, client, clock=clock):
            sent += 1
            emit(f"Langfuse push: {sent} interview traces sent so far")
    return sent
