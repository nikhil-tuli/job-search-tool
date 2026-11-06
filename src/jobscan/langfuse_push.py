"""Send AI call details and your ratings to a Langfuse project, on request.

The app never talks to Langfuse while it scores. `jobscan push-traces` reads the local call log
afterwards and sends each call not sent before as one trace (OpenTelemetry over HTTP and JSON),
then your Good/Poor ratings as scores. A call is marked sent only after Langfuse accepts it, so a
failed run is simply repeated next time. The CV is never sent, and neither is the evidence line
the AI quoted from it. The role description goes only when Config says so.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests

from .ai_fit import PROMPT_VERSION, band_of
from .ai_fit_store import AiCallRow, AiFitRow
from .config import Config
from .models import LABEL_GOOD, Role
from .store import Store
from .timeutil import from_iso, now_ist, to_iso

BATCH = 100  # calls per request
BATCH_PAUSE_SECONDS = 1.0
SCORE_GAP_SECONDS = 2.5  # Hobby allows about 30 general API requests a minute
REQUEST_TIMEOUT_SECONDS = 30
SPAN_ERROR = 2
SPAN_OK = 1
SPAN_KIND_CLIENT = 3


class LangfuseError(Exception):
    """A failure with a plain-words reason."""


# ---- the service ------------------------------------------------------------------------------


class LangfuseClient:
    def __init__(
        self,
        base_url: str,
        public_key: str,
        secret_key: str,
        *,
        session: requests.Session | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._base = base_url.rstrip("/")
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        self._session = session or requests.Session()
        self._timeout = timeout

    def send_spans(self, spans: list[dict[str, Any]]) -> None:
        body = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_attr("service.name", "jobscan")]},
                    "scopeSpans": [{"scope": {"name": "jobscan"}, "spans": spans}],
                }
            ]
        }
        self._post("/api/public/otel/v1/traces", body, {"x-langfuse-ingestion-version": "4"})

    def send_score(
        self, *, score_id: str, trace_id: str, name: str, value: int, comment: str
    ) -> None:
        body = {
            "id": score_id,
            "traceId": trace_id,
            "name": name,
            "value": value,
            "dataType": "NUMERIC",
            "comment": comment,
        }
        self._post("/api/public/scores", body, {})

    def _post(self, path: str, body: dict[str, Any], extra: dict[str, str]) -> None:
        try:
            response = self._session.post(
                self._base + path,
                json=body,
                headers={**self._headers, **extra},
                timeout=self._timeout,
            )
        except requests.Timeout:
            raise LangfuseError("Langfuse took too long to answer. Try again later.") from None
        except requests.RequestException:
            raise LangfuseError("Langfuse could not be reached. Check your internet.") from None
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise LangfuseError(
                f"Langfuse refused the keys (HTTP {status}). Check the keys, and that the "
                "address in Config, Tracing (Langfuse) matches the region of your project."
            )
        if status == 429:
            raise LangfuseError("Langfuse is rate limiting requests. Try again later.")
        raise LangfuseError(f"Langfuse refused the request (HTTP {status}).{_said(response)}")


def _said(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    return f" {message[:160]}" if isinstance(message, str) else ""


def client_from_env(config: Config, environ: Mapping[str, str] | None = None) -> LangfuseClient:
    env = os.environ if environ is None else environ
    public = env.get(config.langfuse_public_env, "").strip()
    secret = env.get(config.langfuse_secret_env, "").strip()
    if not public or not secret:
        raise LangfuseError(
            f"The Langfuse keys are missing. Set {config.langfuse_public_env} and "
            f"{config.langfuse_secret_env} in the terminal first."
        )
    return LangfuseClient(config.langfuse_base_url, public, secret)


# ---- what is sent -----------------------------------------------------------------------------


def _attr(key: str, value: object) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, list):
        items = [{"stringValue": str(v)} for v in value]
        return {"key": key, "value": {"arrayValue": {"values": items}}}
    return {"key": key, "value": {"stringValue": str(value)}}


def trace_id_of(call: AiCallRow) -> str:
    """A fixed id for the call, so sending it twice cannot make two traces."""
    return hashlib.sha256(
        f"trace|{call.portal}|{call.job_id}|{call.called_on}".encode()
    ).hexdigest()[:32]


def span_id_of(call: AiCallRow) -> str:
    return hashlib.sha256(
        f"span|{call.portal}|{call.job_id}|{call.called_on}".encode()
    ).hexdigest()[:16]


def _nanos(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def build_span(
    call: AiCallRow,
    role: Role | None,
    fit: AiFitRow | None,
    *,
    config: Config,
) -> dict[str, Any]:
    """One AI call as a Langfuse generation. `fit` is given only for the role's latest good call."""
    start = from_iso(call.called_on)
    end_ns = _nanos(start) + (call.ms or 0) * 1_000_000
    sent: dict[str, Any] = {
        "role": role.title if role else "(role removed)",
        "company": role.company if role else "",
    }
    if config.langfuse_send_text and role is not None:
        sent["description"] = role.description or ""
    attributes = [
        _attr("langfuse.observation.type", "generation"),
        _attr("langfuse.trace.name", "fit-score"),
        _attr("langfuse.trace.tags", ["jobscan", call.portal]),
        _attr("langfuse.trace.metadata.portal", call.portal),
        _attr("langfuse.trace.metadata.job_id", call.job_id),
        _attr("langfuse.trace.metadata.prompt_version", PROMPT_VERSION),
        _attr("gen_ai.request.model", call.model or config.ai_model),
        _attr("langfuse.observation.input", json.dumps(sent)),
    ]
    if call.tokens_in is not None and call.tokens_out is not None:
        usage = {"input": call.tokens_in, "output": call.tokens_out}
        attributes.append(_attr("langfuse.observation.usage_details", json.dumps(usage)))
    if fit is not None and call.ok:
        band = band_of(fit.checks, fit.role_type_ok, fit.domain, config.ai_max_missing)
        output = {
            "band": band,
            "reason": fit.reason,
            "role_type_ok": fit.role_type_ok,
            "domain": fit.domain,
            # the must-have and its label only; the CV evidence line is left out
            "must_haves": [{"item": item, "status": status} for item, status, _ in fit.checks],
        }
        attributes.append(_attr("langfuse.observation.output", json.dumps(output)))
    message = call.error_text or call.note
    if call.ok:
        status: dict[str, Any] = {"code": SPAN_OK}
        attributes.append(_attr("langfuse.observation.level", "DEFAULT"))
    else:
        status = {"code": SPAN_ERROR, "message": message[:300]}
        attributes.append(_attr("langfuse.observation.level", "ERROR"))
        attributes.append(_attr("langfuse.observation.status_message", message[:300]))
        if call.status is not None:
            attributes.append(_attr("langfuse.observation.metadata.http_status", call.status))
    return {
        "traceId": trace_id_of(call),
        "spanId": span_id_of(call),
        "name": "fit-score",
        "kind": SPAN_KIND_CLIENT,
        "startTimeUnixNano": str(_nanos(start)),
        "endTimeUnixNano": str(end_ns),
        "attributes": attributes,
        "status": status,
    }


def agreement(rating: str, band: str) -> int:
    """1 when you and the AI agree, 0 when you disagree, -1 when the AI said Partial."""
    if band not in ("good", "bad"):
        return -1
    return int((rating == LABEL_GOOD) == (band == "good"))


# ---- the push ---------------------------------------------------------------------------------


@dataclass
class PushResult:
    traces: int = 0
    ratings: int = 0
    stopped: str = ""
    notes: list[str] = field(default_factory=list)


def push(
    store: Store,
    config: Config,
    emit: Callable[[str], None],
    *,
    make_client: Callable[[Config], LangfuseClient] = client_from_env,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = now_ist,
) -> PushResult:
    """Send what has not been sent. Stops at the first failure; the rest goes next time."""
    result = PushResult()
    try:
        client = make_client(config)
    except LangfuseError as exc:
        result.stopped = str(exc)
        emit(f"Langfuse push stopped: {result.stopped}")
        return result
    try:
        _push_calls(store, config, client, result, emit, sleep, clock)
        if not result.stopped:
            _push_ratings(store, config, client, result, sleep, clock)
    except LangfuseError as exc:
        result.stopped = str(exc)
    store.save_push_state(to_iso(clock()), result.traces, result.ratings, result.stopped)
    if result.stopped:
        emit(f"Langfuse push stopped: {result.stopped}; the rest goes next time")
    emit(f"Langfuse push: {result.traces} traces and {result.ratings} ratings sent")
    return result


def _push_calls(
    store: Store,
    config: Config,
    client: LangfuseClient,
    result: PushResult,
    emit: Callable[[str], None],
    sleep: Callable[[float], None],
    clock: Callable[[], datetime],
) -> None:
    fits = store.get_ai_fits()
    latest = {key: call.id for key, call in store.latest_ok_ai_calls().items()}
    first = True
    while True:
        calls = store.unpushed_ai_calls(BATCH)
        if not calls:
            return
        if not first:
            sleep(BATCH_PAUSE_SECONDS)
        first = False
        spans = []
        for call in calls:
            current = latest.get(call.key) == call.id
            spans.append(
                build_span(
                    call,
                    store.get_role(call.portal, call.job_id),
                    fits.get(call.key) if current else None,
                    config=config,
                )
            )
        client.send_spans(spans)
        store.mark_ai_calls_pushed([c.id for c in calls], to_iso(clock()))
        result.traces += len(calls)
        emit(f"Langfuse push: {result.traces} traces sent so far")


def _push_ratings(
    store: Store,
    config: Config,
    client: LangfuseClient,
    result: PushResult,
    sleep: Callable[[float], None],
    clock: Callable[[], datetime],
) -> None:
    """Your Good/Poor rating, and whether it agrees with the AI, on the scoring trace."""
    fits = store.get_ai_fits()
    sent_before = store.get_langfuse_scores()
    pushed = store.pushed_ai_call_ids()
    latest = store.latest_ok_ai_calls()
    first = True
    for role in store.list_labelled_roles():
        call = latest.get(role.key)
        fit = fits.get(role.key)
        if call is None or fit is None or call.id not in pushed or role.fit_rating is None:
            continue
        human = int(role.fit_rating == LABEL_GOOD)
        band = band_of(fit.checks, fit.role_type_ok, fit.domain, config.ai_max_missing)
        agree = agreement(role.fit_rating, band)
        trace = trace_id_of(call)
        if sent_before.get(role.key) == (trace, human, agree):
            continue
        if not first:
            sleep(SCORE_GAP_SECONDS)
        first = False
        label = "Good" if role.fit_rating == LABEL_GOOD else "Poor"
        client.send_score(
            score_id=f"{trace}-human",
            trace_id=trace,
            name="human_rating",
            value=human,
            comment=f"You rated this role {label}",
        )
        if agree >= 0:
            sleep(SCORE_GAP_SECONDS)
            client.send_score(
                score_id=f"{trace}-agree",
                trace_id=trace,
                name="agrees_with_ai",
                value=agree,
                comment=f"You rated it {label}; the AI said {band.capitalize()}",
            )
        store.save_langfuse_score(
            role.portal,
            role.job_id,
            trace_id=trace,
            human=human,
            agree=agree,
            when=to_iso(clock()),
        )
        result.ratings += 1
