"""Ratings, points, bands and tips for Interview Prep. One place for every limit.

Spoken sub-aspects are rated Good, Needs work or Weak from a measured value. Each rating is worth
points (100, 60, 25). A rubric score is the average of its points, rounded. A band is the word
for a score. See docs/INTERVIEW_PREP_REQUIREMENTS.md, sections 5 and 6.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

GOOD = "Good"
NEEDS_WORK = "Needs work"
WEAK = "Weak"
POINTS = {GOOD: 100, NEEDS_WORK: 60, WEAK: 25}
RATING_ORDER = {WEAK: 0, NEEDS_WORK: 1, GOOD: 2}  # worst first

STRONG = "Strong"
BAND_STRONG_MIN = 80
BAND_GOOD_MIN = 65
BAND_NEEDS_WORK_MIN = 50

MIN_ANSWER_WORDS = 10  # fewer words: "too short to score"
MIN_SPOKEN_WORDS = 50  # fewer in total: not enough speech to measure the spoken rubric
UNSUPPORTED_RATING_CAP = 60  # an answer rating with no checkable quote is capped here
MAX_QUOTE_WORDS = 15


def round_half_up(value: float) -> int:
    return int(value + 0.5)


def band_of(score: int) -> str:
    """The word for a rubric or answer score."""
    if score >= BAND_STRONG_MIN:
        return STRONG
    if score >= BAND_GOOD_MIN:
        return GOOD
    if score >= BAND_NEEDS_WORK_MIN:
        return NEEDS_WORK
    return WEAK


def average_score(values: Sequence[int | float]) -> int | None:
    """The mean rounded half up, or None when there is nothing to average."""
    if not values:
        return None
    return round_half_up(sum(values) / len(values))


# ---- spoken sub-aspects ----------------------------------------------------------------------


@dataclass(frozen=True)
class Aspect:
    key: str
    label: str
    unit: str
    benchmark: str  # what Good looks like, in words
    tip: str  # shown when the rating is not Good
    rate: Callable[[float], str]


def _rate_pace(wpm: float) -> str:
    if wpm < 100 or wpm > 180:
        return WEAK
    if 120 <= wpm <= 160:
        return GOOD
    return NEEDS_WORK


def _rate_thinking(seconds: float) -> str:
    if seconds <= 3:
        return GOOD
    return NEEDS_WORK if seconds <= 6 else WEAK


def _rate_fillers(per_100: float) -> str:
    if per_100 < 3:
        return GOOD
    return NEEDS_WORK if per_100 <= 5 else WEAK


def _rate_hedging(per_100: float) -> str:
    if per_100 < 2:
        return GOOD
    return NEEDS_WORK if per_100 <= 4 else WEAK


def _rate_pauses(count: float) -> str:
    if count == 0:
        return GOOD
    return NEEDS_WORK if count <= 2 else WEAK


def _rate_length(seconds: float) -> str:
    if 60 <= seconds <= 120:
        return GOOD
    if 30 <= seconds <= 180:
        return NEEDS_WORK
    return WEAK


def _rate_ownership(percent: float) -> str:
    if percent >= 50:
        return GOOD
    return NEEDS_WORK if percent >= 30 else WEAK


SPOKEN_ASPECTS: tuple[Aspect, ...] = (
    Aspect(
        "pace",
        "Pace",
        "words a minute",
        "120 to 160 words a minute",
        "Aim for about 140 words a minute.",
        _rate_pace,
    ),
    Aspect(
        "thinking",
        "Thinking time",
        "seconds",
        "up to 3 seconds",
        "Take a breath. Then open with your headline.",
        _rate_thinking,
    ),
    Aspect(
        "fillers",
        "Filler words",
        "per 100 words",
        "under 3 per 100 words",
        "Pause silently instead of saying um or like.",
        _rate_fillers,
    ),
    Aspect(
        "hedging",
        "Hedging phrases",
        "per 100 words",
        "under 2 per 100 words",
        "Say 'I did', not 'I think I did'.",
        _rate_hedging,
    ),
    Aspect(
        "pauses",
        "Long pauses",
        "silences over 3 seconds",
        "none",
        "Say 'let me think' and carry on.",
        _rate_pauses,
    ),
    Aspect(
        "length",
        "Answer length",
        "seconds",
        "60 to 120 seconds",
        "Keep to 60 to 90 seconds.",
        _rate_length,
    ),
    Aspect(
        "ownership",
        "Ownership (I vs we)",
        "% I",
        "50% or more 'I'",
        "Say 'I' for what you did yourself.",
        _rate_ownership,
    ),
)
ASPECT_BY_KEY = {a.key: a for a in SPOKEN_ASPECTS}

# ---- answer sub-aspects (rated by the model) -------------------------------------------------

ANSWER_ASPECTS: tuple[tuple[str, str], ...] = (
    ("relevance", "Relevance"),
    ("structure", "Structure"),
    ("specificity", "Specificity"),
    ("impact", "Impact & ownership"),
    ("role_fit", "Role fit"),
)
ANSWER_ASPECT_KEYS = tuple(k for k, _ in ANSWER_ASPECTS)
