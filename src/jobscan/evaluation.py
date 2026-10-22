"""Eval: how well the fit score separates roles you liked from roles you did not.

Labels come only from the Fit rating you give a role. Every measure here is computed from the raw
scores, so it follows the current Config settings and needs no scoring beyond the rated roles.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .config import Config, split_list
from .fit_filters import industry_list
from .models import LABEL_BAD, LABEL_GOOD, Cv, Role, RoleScore
from .scoring import BAND_PARTIAL, BAND_STRONG, ScoreModel, inputs_fingerprint
from .store import Store

MIN_EACH = 5  # fewer good or bad roles than this and the numbers are too noisy to act on
MIN_TOTAL = 20
TOP_N = 10
SHOWN_MISSES = 5
SHOWN_SUGGESTIONS = 10
SWEEP_STEP = 10
MIN_SUGGESTION_COUNT = 2  # a word must sit in this many bad titles to be suggested

_WORD = re.compile(r"[a-z][a-z0-9]{2,}")


@dataclass(frozen=True)
class Labelled:
    role: Role
    label: str  # LABEL_GOOD or LABEL_BAD
    score: RoleScore | None  # None when the role has no current score


@dataclass(frozen=True)
class Variant:
    name: str
    weight_keyword: int
    auc: float | None  # chance that a good role outscores a bad one; 0.5 is a coin toss
    mean_good: float
    mean_bad: float
    top_hits: int  # good roles among the top N by this score


@dataclass(frozen=True)
class Miss:
    role: Role
    label: str
    final: int


@dataclass(frozen=True)
class Cutoff:
    score: int
    caught_good: float  # share of good roles at or above the score
    let_through_bad: float  # share of bad roles at or above the score


@dataclass(frozen=True)
class PhraseYield:
    phrase: str
    good: int
    bad: int


@dataclass(frozen=True)
class Suggestion:
    word: str
    bad_roles: int


@dataclass(frozen=True)
class EvalReport:
    good: int  # labelled roles, scored or not
    bad: int
    scored_good: int
    scored_bad: int
    no_description: int  # labelled roles that cannot be scored yet
    unscored: int  # labelled roles with a description but no current score
    top_n: int
    random_hits: float  # good roles a random top N would hold
    variants: tuple[Variant, ...]  # keyword only, meaning only, and the current mix
    sweep: tuple[tuple[int, float | None], ...]  # (keyword weight, AUC)
    best_weight: int | None
    cutoff: Cutoff | None
    missed_good: tuple[Miss, ...]  # good roles with the lowest scores
    false_good: tuple[Miss, ...]  # bad roles with the highest scores
    phrases: tuple[PhraseYield, ...]
    suggestions: tuple[Suggestion, ...]  # title words
    industry_suggestions: tuple[Suggestion, ...]  # LinkedIn industries

    @property
    def labelled(self) -> int:
        return self.good + self.bad

    @property
    def scored(self) -> int:
        return self.scored_good + self.scored_bad

    @property
    def measurable(self) -> bool:
        """Both kinds of role have a score, so the measures mean something at all."""
        return self.scored_good > 0 and self.scored_bad > 0

    @property
    def reliable(self) -> bool:
        return (
            self.scored_good >= MIN_EACH
            and self.scored_bad >= MIN_EACH
            and self.scored >= MIN_TOTAL
        )


def load_labelled(store: Store, config: Config, cv: Cv | None) -> list[Labelled]:
    """Every labelled role with its current score (a stale or missing one counts as none)."""
    saved = store.get_scores()
    found: list[Labelled] = []
    for role in store.list_labelled_roles():
        label = role.label
        if label is None:
            continue
        score = saved.get(role.key)
        if score is not None and (
            cv is None or score.inputs != inputs_fingerprint(config, cv, role)
        ):
            score = None
        found.append(Labelled(role, label, score))
    return found


@dataclass(frozen=True)
class Bands:
    """Roles with a current score, by band."""

    total: int
    strong: int
    partial: int
    weak: int


def score_bands(store: Store, config: Config, cv: Cv | None) -> Bands:
    """Every role with a current score (not stale), counted by Strong, Partial and Weak."""
    if cv is None:
        return Bands(0, 0, 0, 0)
    model = ScoreModel.from_config(config)
    saved = store.get_scores()
    counts = {BAND_STRONG: 0, BAND_PARTIAL: 0, "weak": 0}
    for role in store.list_roles():
        score = saved.get(role.key)
        if score is None or score.inputs != inputs_fingerprint(config, cv, role):
            continue
        counts[model.band(model.final(score))] += 1
    return Bands(sum(counts.values()), counts[BAND_STRONG], counts[BAND_PARTIAL], counts["weak"])


def roles_to_score(labelled: Sequence[Labelled]) -> list[Role]:
    """Labelled roles that have a description but no current score."""
    return [item.role for item in labelled if item.score is None and item.role.description]


def auc(good: Sequence[float], bad: Sequence[float]) -> float | None:
    """The chance a random good role outscores a random bad one; a tie counts half."""
    if not good or not bad:
        return None
    wins = sum((g > b) + 0.5 * (g == b) for g in good for b in bad)
    return wins / (len(good) * len(bad))


def _finals(model: ScoreModel, scored: Sequence[Labelled]) -> list[int]:
    return [model.final(item.score) for item in scored if item.score is not None]


def _variant(name: str, model: ScoreModel, scored: Sequence[Labelled], top_n: int) -> Variant:
    finals = _finals(model, scored)
    good = [f for f, item in zip(finals, scored, strict=True) if item.label == LABEL_GOOD]
    bad = [f for f, item in zip(finals, scored, strict=True) if item.label == LABEL_BAD]
    # Ties are broken against us (bad first), so a flat score cannot look better than it is.
    ranked = sorted(
        zip(finals, scored, strict=True), key=lambda p: (-p[0], p[1].label == LABEL_GOOD)
    )
    hits = sum(1 for _, item in ranked[:top_n] if item.label == LABEL_GOOD)
    return Variant(
        name=name,
        weight_keyword=model.weight_keyword,
        auc=auc(good, bad),
        mean_good=sum(good) / len(good) if good else 0.0,
        mean_bad=sum(bad) / len(bad) if bad else 0.0,
        top_hits=hits,
    )


def _cutoff(model: ScoreModel, scored: Sequence[Labelled]) -> Cutoff | None:
    finals = _finals(model, scored)
    good = [f for f, item in zip(finals, scored, strict=True) if item.label == LABEL_GOOD]
    bad = [f for f, item in zip(finals, scored, strict=True) if item.label == LABEL_BAD]
    if not good or not bad:
        return None
    best: tuple[float, float, int] | None = None
    for threshold in sorted(set(finals)):
        caught = sum(f >= threshold for f in good) / len(good)
        let_through = sum(f >= threshold for f in bad) / len(bad)
        candidate = (caught - let_through, caught, threshold)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    assert best is not None
    return Cutoff(
        score=best[2],
        caught_good=best[1],
        let_through_bad=best[1] - best[0],
    )


def _misses(
    model: ScoreModel, scored: Sequence[Labelled], label: str, *, highest: bool
) -> tuple[Miss, ...]:
    items = [
        Miss(item.role, item.label, model.final(item.score))
        for item in scored
        if item.score is not None and item.label == label
    ]
    items.sort(key=lambda m: (-m.final if highest else m.final, m.role.title))
    return tuple(items[:SHOWN_MISSES])


def phrase_yield(labelled: Sequence[Labelled]) -> tuple[PhraseYield, ...]:
    """Per search phrase, how many labelled roles turned out good and bad, worst first."""
    good: Counter[str] = Counter()
    bad: Counter[str] = Counter()
    for item in labelled:
        (good if item.label == LABEL_GOOD else bad)[item.role.matched_phrase] += 1
    rows = [PhraseYield(p, good[p], bad[p]) for p in set(good) | set(bad)]
    rows.sort(key=lambda r: (-r.bad, r.good, r.phrase.casefold()))
    return tuple(rows)


def _title_words(title: str) -> set[str]:
    return set(_WORD.findall(title.lower()))


def suggest_deny_words(
    labelled: Sequence[Labelled], config: Config, phrases: Sequence[str]
) -> tuple[Suggestion, ...]:
    """Title words found in several bad roles and in no good one, and not already denied.

    Words from the search phrases themselves are never suggested, since they would remove the
    roles you are looking for.
    """
    skip = {w for p in phrases for w in _title_words(p)}
    skip |= {w for entry in split_list(config.fit_deny_keywords) for w in _title_words(entry)}
    in_good: set[str] = set()
    in_bad: Counter[str] = Counter()
    for item in labelled:
        words = _title_words(item.role.title)
        if item.label == LABEL_GOOD:
            in_good |= words
        else:
            in_bad.update(words)
    found = [
        Suggestion(word, count)
        for word, count in in_bad.items()
        if count >= MIN_SUGGESTION_COUNT and word not in in_good and word not in skip
    ]
    found.sort(key=lambda s: (-s.bad_roles, s.word))
    return tuple(found[:SHOWN_SUGGESTIONS])


def suggest_excluded_industries(
    labelled: Sequence[Labelled], config: Config
) -> tuple[Suggestion, ...]:
    """LinkedIn industries listed on several bad roles and on no good one, not yet excluded."""
    skip = {name.casefold() for name in split_list(config.fit_excluded_industries)}
    in_good: set[str] = set()
    in_bad: Counter[str] = Counter()
    names: dict[str, str] = {}
    for item in labelled:
        found = {name.casefold(): name for name in industry_list(item.role.industries)}
        for key, name in found.items():
            names.setdefault(key, name)  # keep the first spelling seen
        if item.label == LABEL_GOOD:
            in_good |= found.keys()
        else:
            in_bad.update(found.keys())
    picked = [
        Suggestion(names[key], count)
        for key, count in in_bad.items()
        if count >= MIN_SUGGESTION_COUNT and key not in in_good and key not in skip
    ]
    picked.sort(key=lambda s: (-s.bad_roles, s.word.casefold()))
    return tuple(picked[:SHOWN_SUGGESTIONS])


def build_report(
    labelled: Sequence[Labelled], config: Config, phrases: Sequence[str]
) -> EvalReport:
    model = ScoreModel.from_config(config)
    scored = [item for item in labelled if item.score is not None]
    good = sum(item.label == LABEL_GOOD for item in labelled)
    scored_good = sum(item.label == LABEL_GOOD for item in scored)
    top_n = min(TOP_N, len(scored))
    random_hits = top_n * scored_good / len(scored) if scored else 0.0

    variants = (
        _variant("Keywords only", replace(model, weight_keyword=100), scored, top_n),
        _variant("Meaning only", replace(model, weight_keyword=0), scored, top_n),
        _variant(f"Current mix ({model.weight_keyword}% keywords)", model, scored, top_n),
    )
    sweep = tuple(
        (w, _variant("", replace(model, weight_keyword=w), scored, top_n).auc)
        for w in range(0, 101, SWEEP_STEP)
    )
    rated = [(w, a) for w, a in sweep if a is not None]
    best = max(rated, key=lambda p: (p[1], -abs(p[0] - model.weight_keyword)), default=None)
    return EvalReport(
        good=good,
        bad=len(labelled) - good,
        scored_good=scored_good,
        scored_bad=len(scored) - scored_good,
        no_description=sum(1 for i in labelled if i.score is None and not i.role.description),
        unscored=len(roles_to_score(labelled)),
        top_n=top_n,
        random_hits=random_hits,
        variants=variants,
        sweep=sweep,
        best_weight=best[0] if best else None,
        cutoff=_cutoff(model, scored),
        missed_good=_misses(model, scored, LABEL_GOOD, highest=False),
        false_good=_misses(model, scored, LABEL_BAD, highest=True),
        phrases=phrase_yield(labelled),
        suggestions=suggest_deny_words(labelled, config, phrases),
        industry_suggestions=suggest_excluded_industries(labelled, config),
    )
