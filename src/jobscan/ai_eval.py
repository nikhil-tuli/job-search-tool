"""The Eval page for AI scoring: your own ratings checked against the AI's bands.

There is no tuning here. You rate roles Good or Poor in the side panel; this counts how often the
AI agrees, lists the roles where it did not, and says what is waiting to be scored.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .ai_fit import BAD, BANDS, DAILY_WINDOW, GOOD, MIN_WORDS, PARTIAL, band_of, inputs_fingerprint
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
REASON_CAP = "Daily cap reached"
REASON_NEXT = "Waiting for the next run"


@dataclass(frozen=True)
class Miss:
    role: Role
    band: str
    reason: str


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
    calls_cap: int
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
    capped = used >= config.ai_daily_cap
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
            by_band[band_of(row.checks, row.role_type_ok, row.domain, config.ai_max_missing)] += 1
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
        elif capped:
            waiting[REASON_CAP] += 1
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
        band = band_of(row.checks, row.role_type_ok, row.domain, config.ai_max_missing)
        pivot[(label, band)].append(role)
        verdicts[role.key] = Miss(role, band, row.reason)

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
        calls_cap=config.ai_daily_cap,
        phrases=phrase_yield(labelled),
        suggestions=suggest_deny_words(labelled, config, phrases),
        industry_suggestions=suggest_excluded_industries(labelled, config),
    )
