"""The Eval page for AI scoring: your own ratings checked against the AI's bands.

There is no tuning here. You rate roles Good or Poor in the side panel; this counts how often the
AI agrees, lists the roles where it did not, and says what is waiting to be scored.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .ai_fit import (
    BAD,
    BANDS,
    DAILY_WINDOW,
    GOOD,
    MIN_WORDS,
    PARTIAL,
    band_of,
    inputs_fingerprint,
    reason_on_read,
)
from .ai_fit_store import AiCallRow
from .config import Config
from .evaluation import (
    Labelled,
    PhraseYield,
    Suggestion,
    phrase_yield,
    suggest_deny_words,
    suggest_excluded_industries,
)
from .fit_filters import FitRules, evaluate
from .models import LABEL_BAD, LABEL_GOOD, Cv, Role
from .store import Store
from .timeutil import to_iso

MIN_EACH = 5  # rated roles of each kind before the table is more than a guide
MIN_TOTAL = 20
SHOWN_MISSES = 10

REASON_NO_DESCRIPTION = "No description (or under 100 words)"
REASON_ERROR = "AI error on the last try"
REASON_NEXT = "Waiting for the next run"


@dataclass(frozen=True)
class Miss:
    role: Role
    band: str
    reason: str


@dataclass(frozen=True)
class ModelStats:
    model: str
    calls: int
    ok: int
    errors: int
    median_s: float | None
    avg_in: int | None
    avg_out: int | None


@dataclass(frozen=True)
class Failure:
    when: str
    role: Role | None
    portal: str
    job_id: str
    model: str
    message: str


@dataclass(frozen=True)
class CallStats:
    period: str  # "24h", "7d" or "30d"
    calls: int
    ok: int
    rate_limited: int
    other_errors: int
    median_s: float | None
    slowest_s: float | None
    avg_in: int | None
    avg_out: int | None
    max_in: int | None
    busiest_minute: int | None  # most tokens (in plus out) in one minute
    by_model: tuple[ModelStats, ...]
    failures: tuple[Failure, ...]


CALL_PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
DEFAULT_CALL_PERIOD = "7d"
MAX_FAILURES = 10


def _median_s(rows: Sequence[AiCallRow]) -> float | None:
    times = [r.ms / 1000 for r in rows if r.ms is not None]
    return round(statistics.median(times), 1) if times else None


def _avg(values: Sequence[int]) -> int | None:
    return round(sum(values) / len(values)) if values else None


def _is_rate_limited(row: AiCallRow) -> bool:
    return row.status == 429 or (row.status is None and "rate limit" in row.note.lower())


def build_call_stats(store: Store, now: datetime, period: str) -> CallStats:
    """How the AI calls went over the period: errors, time, tokens, and the latest failures."""
    period = period if period in CALL_PERIODS else DEFAULT_CALL_PERIOD
    rows = store.ai_calls_between(to_iso(now - CALL_PERIODS[period]))
    good = [r for r in rows if r.ok]
    failed = [r for r in rows if not r.ok]
    rate_limited = sum(1 for r in failed if _is_rate_limited(r))
    slow = [r.ms / 1000 for r in rows if r.ms is not None]
    tokens_in = [r.tokens_in for r in rows if r.tokens_in is not None]
    tokens_out = [r.tokens_out for r in rows if r.tokens_out is not None]
    per_minute: defaultdict[str, int] = defaultdict(int)
    for r in rows:
        if r.tokens_in is not None and r.tokens_out is not None:
            per_minute[r.called_on[:16]] += r.tokens_in + r.tokens_out
    groups: defaultdict[str, list[AiCallRow]] = defaultdict(list)
    for r in rows:
        groups[r.model or "(not recorded)"].append(r)
    by_model = tuple(
        ModelStats(
            model=name,
            calls=len(items),
            ok=sum(1 for r in items if r.ok),
            errors=sum(1 for r in items if not r.ok),
            median_s=_median_s(items),
            avg_in=_avg([r.tokens_in for r in items if r.tokens_in is not None]),
            avg_out=_avg([r.tokens_out for r in items if r.tokens_out is not None]),
        )
        for name, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    )
    failures = tuple(
        Failure(
            when=r.called_on,
            role=store.get_role(r.portal, r.job_id),
            portal=r.portal,
            job_id=r.job_id,
            model=r.model or "(not recorded)",
            message=(f"{r.status} " if r.status else "") + (r.error_text or r.note),
        )
        for r in reversed(failed[-MAX_FAILURES:])
    )
    return CallStats(
        period=period,
        calls=len(rows),
        ok=len(good),
        rate_limited=rate_limited,
        other_errors=len(failed) - rate_limited,
        median_s=round(statistics.median(slow), 1) if slow else None,
        slowest_s=round(max(slow), 1) if slow else None,
        avg_in=_avg(tokens_in),
        avg_out=_avg(tokens_out),
        max_in=max(tokens_in) if tokens_in else None,
        busiest_minute=max(per_minute.values()) if per_minute else None,
        by_model=by_model,
        failures=failures,
    )


@dataclass(frozen=True)
class AiEval:
    scored: int
    by_band: dict[str, int]
    rated_scored: int
    rated_good: int
    rated_bad: int
    # (your rating, AI band) -> roles. Your rating is LABEL_GOOD or LABEL_BAD.
    pivot: dict[tuple[str, str], list[Role]]
    missed_good: tuple[Miss, ...]  # you rated Good, the AI said Bad (then Partial)
    false_good: tuple[Miss, ...]  # you rated Poor, the AI said Good (then Partial)
    waiting: Counter[str]
    stale: int
    calls_used: int
    phrases: tuple[PhraseYield, ...] = ()
    suggestions: tuple[Suggestion, ...] = ()
    industry_suggestions: tuple[Suggestion, ...] = ()
    notes: list[str] = field(default_factory=list)

    @property
    def waiting_total(self) -> int:
        return sum(self.waiting.values())

    @property
    def reliable(self) -> bool:
        return (
            self.rated_good >= MIN_EACH
            and self.rated_bad >= MIN_EACH
            and self.rated_scored >= MIN_TOTAL
        )


def build_ai_eval(
    store: Store,
    config: Config,
    cv: Cv | None,
    portals: Sequence[str],
    phrases: Sequence[str],
    now: datetime,
) -> AiEval:
    saved = store.get_ai_fits()
    errors = store.last_ai_errors()
    rules = FitRules.from_config(config)
    used = store.ai_calls_since(to_iso(now - DAILY_WINDOW))
    wanted = set(portals)

    scored = 0
    by_band = dict.fromkeys(BANDS, 0)
    waiting: Counter[str] = Counter()
    stale = 0
    for role in store.list_roles():
        if wanted and role.portal not in wanted:
            continue
        row = saved.get(role.key)
        current = (
            row is not None
            and cv is not None
            and row.inputs == inputs_fingerprint(config, cv, role)
        )
        if row is not None:
            scored += 1
            by_band[band_of(row.checks, row.role_type_ok, config.ai_max_missing)] += 1
        if current:
            continue
        if row is None and not evaluate(role, rules, now).eligible:
            continue
        if row is not None:
            stale += 1
        if len((role.description or "").split()) < MIN_WORDS:
            waiting[REASON_NO_DESCRIPTION] += 1
        elif role.key in errors:
            waiting[REASON_ERROR] += 1
        else:
            waiting[REASON_NEXT] += 1

    labelled: list[Labelled] = []
    pivot: dict[tuple[str, str], list[Role]] = {
        (label, band): [] for label in (LABEL_GOOD, LABEL_BAD) for band in BANDS
    }
    verdicts: dict[str, Miss] = {}
    for role in store.list_labelled_roles(list(portals)):
        label = role.label
        if label is None:
            continue
        labelled.append(Labelled(role, label, None))
        row = saved.get(role.key)
        if row is None:
            continue
        band = band_of(row.checks, row.role_type_ok, config.ai_max_missing)
        pivot[(label, band)].append(role)
        verdicts[role.key] = Miss(role, band, reason_on_read(row.reason, row.role_type_ok))

    def misses(label: str, order: tuple[str, str]) -> tuple[Miss, ...]:
        picked = [verdicts[r.key] for band in order for r in pivot[(label, band)]]
        return tuple(picked[:SHOWN_MISSES])

    rated_good = sum(len(pivot[(LABEL_GOOD, b)]) for b in BANDS)
    rated_bad = sum(len(pivot[(LABEL_BAD, b)]) for b in BANDS)
    return AiEval(
        scored=scored,
        by_band=by_band,
        rated_scored=rated_good + rated_bad,
        rated_good=rated_good,
        rated_bad=rated_bad,
        pivot=pivot,
        missed_good=misses(LABEL_GOOD, (BAD, PARTIAL)),
        false_good=misses(LABEL_BAD, (GOOD, PARTIAL)),
        waiting=waiting,
        stale=stale,
        calls_used=used,
        phrases=phrase_yield(labelled),
        suggestions=suggest_deny_words(labelled, config, phrases),
        industry_suggestions=suggest_excluded_industries(labelled, config),
    )
