"""AI fit scoring: an AI model labels a role's must-haves against the CV, and the app sets the band.

The AI only labels: each must-have is Met, Partly or Missing (with the CV line as evidence),
the role type is right or wrong against the user's Role focus, and the domain matches, is close
or is different. The band (Good, Partial, Bad) and the one-line reason are worked out here from
those labels, so they always agree with the checklist and with the settings, and a change to
"must-haves allowed missing" needs no new AI call.

Sent: the role's title, company, location and description, the CV text without contact details,
the Role focus and the extra rules. One call per role.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from .ai_fit_store import AiFitRow
from .chunking import KIND_COMPANY, KIND_REQUIREMENT, chunk_description
from .config import Config
from .fit_filters import FitRules, evaluate
from .models import Cv, Role
from .store import Store
from .tailor_ai import CallInfo, TailorClient, TailorError, _extract_json
from .timeutil import now_ist, to_iso

PROMPT_VERSION = "1"
WHERE = "Config, Fit scoring (AI)"
MIN_WORDS = 100  # a shorter description is not scored: there is nothing to check
MAX_MUST_HAVES = 12
MAX_REPLY_TOKENS = 2_000
DAILY_WINDOW = timedelta(hours=24)

GOOD = "good"
PARTIAL = "partial"
BAD = "bad"
BANDS = (GOOD, PARTIAL, BAD)
BAND_RANK = {GOOD: 3, PARTIAL: 2, BAD: 1}

MET, PARTLY, MISSING = "Met", "Partly", "Missing"
STATUSES = (MET, PARTLY, MISSING)
DOMAINS = ("match", "close", "different")

SYSTEM_PROMPT = """You check how well one job posting fits one candidate. Reply with a single \
JSON object and nothing else: no markdown, no commentary.

Steps:
1. MUST-HAVES. List what the posting states as required: words like must, required, mandatory, \
minimum, or a named tool, skill, domain, degree or certification in the requirements. Ignore \
"preferred", "plus" and "good to have". Ignore years of experience. Merge similar items. List \
at most 12, each as a short phrase.
2. MATCH EACH to the CV by meaning, not exact words. Met = a CV line clearly shows it; copy \
that line (shortened) as evidence. Partly = the CV shows related experience. Missing = nothing \
in the CV. Never assume a skill that is not written in the CV. When unsure between two \
labels, choose the lower one.
3. ROLE TYPE. Compare the role's day-to-day work (read the responsibilities, not only the \
title) with the candidate's ROLE FOCUS. role_type_ok is false if the work is a kind the focus \
says the candidate does not want, or is clearly not a kind it wants, even when the title or \
some skills match. If there is no ROLE FOCUS, role_type_ok is true. When false, give a short \
role_type_note (at most 15 words) saying what the work really is.
4. DOMAIN. The role's industry against the ROLE FOCUS domains: "match", "close" or "different".

Reply format:
{"must_haves": [{"item": "...", "status": "Met|Partly|Missing", "evidence": "..."}],
 "role_type_ok": true, "role_type_note": "", "domain": "match|close|different"}"""


class AiFitError(Exception):
    """The AI reply could not be used."""


class Completer(Protocol):
    def complete(
        self, system: str, user: str, *, temperature: float = ..., max_tokens: int = ...
    ) -> str: ...


RATE_LIMIT_WAIT_SECONDS = 60.0


# ---- what goes in -------------------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://|www\.)\S+|(?:linkedin|github)\.com\S*", re.I)
_PHONE = re.compile(r"(?:\+?\d[\d\s().-]{8,}\d)")


def redact_cv(text: str) -> str:
    """The CV text without the name line, emails, links and phone numbers."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    seen_text = False
    for raw in lines:
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if not seen_text:
            seen_text = True
            if len(line.split()) <= 5 and not any(ch.isdigit() for ch in line):
                continue  # the candidate's name
        line = _PHONE.sub("", _URL.sub("", _EMAIL.sub("", line)))
        line = re.sub(r"\s*[|•·]\s*(?=[|•·]|$)", "", line).strip(" |•·\t")
        if line:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def word_count(text: str) -> int:
    return len(text.split())


def trim_description(description: str, max_words: int) -> tuple[str, bool]:
    """The description, cut only when it is over the limit.

    Company, benefits and legal sections go first, then the end of the rest. Requirement
    sections are never cut.
    """
    if word_count(description) <= max_words:
        return description, False
    chunks = [c for c in chunk_description(description) if c.kind != KIND_COMPANY]
    total = sum(word_count(c.text) for c in chunks)
    while total > max_words:
        drop = next(
            (i for i in range(len(chunks) - 1, -1, -1) if chunks[i].kind != KIND_REQUIREMENT),
            None,
        )
        if drop is None:
            break
        total -= word_count(chunks[drop].text)
        del chunks[drop]
    return "\n".join(c.text for c in chunks), True


def build_user_message(config: Config, cv: Cv, role: Role, description: str) -> str:
    focus = config.ai_role_focus or "(none given)"
    lines = ["ROLE FOCUS:", focus, ""]
    if config.ai_extra_rules:
        lines += ["EXTRA RULES:", config.ai_extra_rules, ""]
    lines += [
        "CV:",
        redact_cv(cv.text),
        "",
        f"POSTING: {role.title} at {role.company}",
        f"Location: {role.location}",
        "",
        description,
    ]
    return "\n".join(lines)


def inputs_fingerprint(config: Config, cv: Cv, role: Role) -> str:
    """Names everything a saved verdict depends on. A mismatch means it is stale."""
    payload = [
        PROMPT_VERSION,
        cv.sha,
        config.ai_model,
        config.ai_role_focus,
        config.ai_extra_rules,
        config.ai_max_words,
        role.title,
        role.company,
        role.location,
        role.description or "",
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


# ---- what comes back ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    checks: tuple[tuple[str, str, str], ...]
    role_type_ok: bool
    role_type_note: str
    domain: str


def parse_reply(text: str) -> Verdict:
    try:
        data = _extract_json(text)
    except Exception:
        raise AiFitError("the reply was not JSON") from None
    items = data.get("must_haves")
    if not isinstance(items, list):
        raise AiFitError("the reply has no must_haves list")
    checks: list[tuple[str, str, str]] = []
    for item in items[:MAX_MUST_HAVES]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("item", "")).strip()[:200]
        status = str(item.get("status", "")).strip().capitalize()
        evidence = str(item.get("evidence", "") or "").strip()[:300]
        if not name or status not in STATUSES:
            continue
        if status == MET and not evidence:
            status = PARTLY  # Met needs a CV line to point to
        checks.append((name, status, evidence))
    role_ok = data.get("role_type_ok")
    if not isinstance(role_ok, bool):
        raise AiFitError("role_type_ok is missing")
    domain = str(data.get("domain", "")).strip().lower()
    if domain not in DOMAINS:
        raise AiFitError("domain is missing")
    note = str(data.get("role_type_note", "") or "").strip()[:160]
    return Verdict(tuple(checks), role_ok, note, domain)


def band_of(
    checks: Sequence[tuple[str, str, str]],
    role_type_ok: bool,
    domain: str,
    max_missing: int,
) -> str:
    """The band rules. Applied on read, so the setting needs no new AI call."""
    if not role_type_ok:
        return BAD
    missing = sum(1 for _, status, _ in checks if status == MISSING)
    partly = sum(1 for _, status, _ in checks if status == PARTLY)
    if missing > max_missing:
        return BAD
    if missing >= 1 or partly >= 2:
        return PARTIAL
    return PARTIAL if domain == "different" else GOOD


def reason_of(
    checks: Sequence[tuple[str, str, str]], role_type_ok: bool, role_type_note: str, domain: str
) -> str:
    """One line built from the labels, so it always agrees with the checklist."""
    if not role_type_ok:
        return f"Wrong role type: {role_type_note}" if role_type_note else "Wrong role type."
    parts: list[str] = []
    missing = [name for name, status, _ in checks if status == MISSING]
    partly = [name for name, status, _ in checks if status == PARTLY]
    if missing:
        parts.append("Missing: " + ", ".join(missing[:4]) + ("..." if len(missing) > 4 else ""))
    if partly:
        parts.append("Partly: " + ", ".join(partly[:3]) + ("..." if len(partly) > 3 else ""))
    if domain == "different":
        parts.append("Different domain")
    if parts:
        return ". ".join(parts) + "."
    if not checks:
        return "No stated must-haves; role type and domain fit."
    return "All must-haves met; role type and domain fit."


# ---- the service --------------------------------------------------------------------------------


def client_from_env(config: Config, environ: Mapping[str, str] | None = None) -> Completer:
    env = os.environ if environ is None else environ
    key = env.get(config.ai_key_env, "").strip()
    if not key:
        raise TailorError(
            f"The AI key is missing. Set {config.ai_key_env} in the terminal, then start "
            "jobscan from that terminal."
        )
    return TailorClient(config.ai_base_url, config.ai_model, key, where=WHERE)


@dataclass
class AiRunResult:
    considered: int = 0
    scored: int = 0
    failed: int = 0
    no_description: int = 0
    excluded: int = 0
    waiting: int = 0  # roles left for the next run (cap reached or the service stopped)
    stale_kept: int = 0  # scored under older settings and left alone (see rescore_stale)
    stopped: str = ""
    notes: list[str] = field(default_factory=list)


class AiScoringUnavailable(RuntimeError):
    """Scoring cannot run at all (for example, no CV has been uploaded)."""


class AiScorer:
    def __init__(
        self,
        *,
        store: Store,
        config: Config,
        make_client: Callable[[Config], Completer] = client_from_env,
        emit: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = now_ist,
        monotonic: Callable[[], float] = time.monotonic,
        retry_rate_limit: bool = True,
        rescore_stale: bool = False,
    ) -> None:
        self._retry_rate_limit = retry_rate_limit
        self._rescore_stale = rescore_stale
        self._store = store
        self._config = config
        self._make_client = make_client
        self._emit = emit
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._last_call: float | None = None

    def pending(self, cv: Cv, *, only: Sequence[Role] | None = None) -> AiRunResult:
        """Count what a run would do, without calling the service."""
        result = AiRunResult()
        self._select(cv, only, result)
        return result

    def run(self, *, only: Sequence[Role] | None = None) -> AiRunResult:
        cv = self._store.get_cv()
        if cv is None:
            raise AiScoringUnavailable("no CV uploaded yet")
        result = AiRunResult()
        todo = self._select(cv, only, result)
        if result.stale_kept:
            self._emit(
                f"AI scoring: {result.stale_kept} role(s) were scored under older settings and "
                "are left as they are. Run jobscan score --stale to score them again."
            )
        if not todo:
            self._emit("AI scoring: nothing to score")
            return result
        used = self._store.ai_calls_since(to_iso(self._clock() - DAILY_WINDOW))
        remaining = max(self._config.ai_daily_cap - used, 0)
        if remaining == 0:
            result.stopped = f"daily cap of {self._config.ai_daily_cap} AI calls reached"
            result.waiting = len(todo)
            self._emit(f"AI scoring stopped: {result.stopped}; the rest waits for tomorrow")
            return result
        try:
            client = self._make_client(self._config)
        except TailorError as exc:
            result.stopped = str(exc)
            result.waiting = len(todo)
            self._emit(f"AI scoring stopped: {result.stopped}")
            return result
        rules = FitRules.from_config(self._config)
        for index, role in enumerate(todo):
            if index >= remaining:
                result.stopped = f"daily cap of {self._config.ai_daily_cap} AI calls reached"
                result.waiting = len(todo) - index
                break
            if not self._score_one(client, cv, role, rules, result):
                result.waiting = len(todo) - index
                break
        if result.stopped:
            self._emit(f"AI scoring stopped: {result.stopped}; the rest waits for the next run")
        self._emit(
            f"AI scoring: {result.scored} scored, {result.failed} failed, "
            f"{result.waiting} waiting, {result.no_description} without enough description"
        )
        return result

    # ---- internals ---------------------------------------------------------------------

    def _select(self, cv: Cv, only: Sequence[Role] | None, result: AiRunResult) -> list[Role]:
        now = self._clock()
        rules = FitRules.from_config(self._config)
        saved = self._store.get_ai_fits()
        roles = list(only) if only is not None else self._store.list_roles()  # newest first
        todo: list[Role] = []
        for role in roles:
            current = saved.get(role.key)
            if only is None:
                if current and current.inputs == inputs_fingerprint(self._config, cv, role):
                    continue
                if current and not self._rescore_stale:
                    result.stale_kept += 1  # keep the old verdict; the user re-scores on purpose
                    continue
                if current is None and not evaluate(role, rules, now).eligible:
                    result.excluded += 1
                    continue
            result.considered += 1
            if word_count(role.description or "") < MIN_WORDS:
                result.no_description += 1
                continue
            todo.append(role)
        return todo

    def _log(
        self,
        role: Role,
        when: str,
        ok: bool,
        note: str,
        client: Completer,
        error: TailorError | None = None,
    ) -> None:
        info: CallInfo | None = getattr(client, "last_call", None)
        self._store.log_ai_call(
            role.portal,
            role.job_id,
            when=when,
            ok=ok,
            note=note,
            model=self._config.ai_model,
            status=error.status if error is not None else (info.status if info else None),
            error_text=error.provider_message if error is not None else "",
            ms=info.ms if info else None,
            tokens_in=info.tokens_in if info else None,
            tokens_out=info.tokens_out if info else None,
        )

    def _pace(self) -> None:
        gap = 60.0 / self._config.ai_calls_per_minute
        if self._last_call is not None:
            wait = gap - (self._monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._monotonic()

    def _score_one(
        self,
        client: Completer,
        cv: Cv,
        role: Role,
        rules: FitRules,
        result: AiRunResult,
    ) -> bool:
        """Score one role. False when the run must stop (the service cannot be used now)."""
        config = self._config
        description, trimmed = trim_description(role.description or "", config.ai_max_words)
        message = build_user_message(config, cv, role, description)
        verdict: Verdict | None = None
        unreadable = 0  # one retry when the reply cannot be read
        waited = False  # one wait and retry when the service says it is busy
        while True:
            self._pace()
            when = to_iso(self._clock())
            try:
                reply = client.complete(
                    SYSTEM_PROMPT, message, temperature=0.0, max_tokens=MAX_REPLY_TOKENS
                )
            except TailorError as exc:
                self._log(role, when, False, str(exc), client, exc)
                if exc.status == 429 and self._retry_rate_limit and not waited:
                    waited = True
                    self._emit("AI service is busy (rate limit): waiting a minute, then retrying")
                    self._sleep(RATE_LIMIT_WAIT_SECONDS)
                    continue
                result.stopped = str(exc)
                return False
            try:
                verdict = parse_reply(reply)
            except AiFitError as exc:
                note = f"The AI reply could not be read: {exc}."
                self._log(role, when, False, note, client)
                unreadable += 1
                if unreadable == 2:
                    result.failed += 1
                    return True
                continue
            self._log(role, when, True, "", client)
            break
        assert verdict is not None
        now = self._clock()
        inputs = inputs_fingerprint(config, cv, role)
        with self._store.transaction():
            self._store.save_ai_fit(
                AiFitRow(
                    portal=role.portal,
                    job_id=role.job_id,
                    checks=verdict.checks,
                    role_type_ok=verdict.role_type_ok,
                    domain=verdict.domain,
                    reason=reason_of(
                        verdict.checks, verdict.role_type_ok, verdict.role_type_note, verdict.domain
                    ),
                    trimmed=trimmed,
                    inputs=inputs,
                    model=config.ai_model,
                    scored_on=to_iso(now),
                )
            )
            fit = evaluate(role, rules, now, ignore_recency=True)
            self._store.save_fits([(role.portal, role.job_id, fit, inputs)], when=to_iso(now))
        result.scored += 1
        return True
