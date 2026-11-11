"""Spoken metrics: measured from the transcript and the time of each word. No model is used.

Per answer: counts and times. Per interview: the seven values of the "How you spoke" rubric.
Word times come from the speech service. If it gave none, sentence times are used, and the
timing values are marked approximate. If it gave neither, only the recording length is used.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .rules import (
    ASPECT_BY_KEY,
    GOOD,
    MIN_ANSWER_WORDS,
    MIN_SPOKEN_WORDS,
    POINTS,
    RATING_ORDER,
    SPOKEN_ASPECTS,
    average_score,
)

PAUSE_NOT_SPEAKING_SECONDS = 2.0  # a gap longer than this is not speaking time
LONG_PAUSE_SECONDS = 3.0
READING_WORDS_PER_SECOND = 3.3  # to estimate how long the user needed to read a question

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

FILLER_WORDS = frozenset({"um", "uh", "er", "erm", "uhm"})
FILLER_PHRASES = (("you", "know"), ("i", "mean"), ("basically",), ("actually",))
# A phrase is counted once. "kind of" and "sort of" are hedges here, not fillers.
HEDGE_PHRASES = (
    ("i", "feel", "like"),
    ("i", "think"),
    ("i", "guess"),
    ("might", "be"),
    ("kind", "of"),
    ("sort", "of"),
    ("maybe",),
    ("probably",),
)
# "like" is a filler only when it is not a verb or a comparison.
_LIKE_NOT_FILLER_AFTER = frozenset(
    {
        "would", "i", "we", "you", "they", "he", "she", "looks", "look", "looked", "feels",
        "feel", "felt", "seems", "seem", "sounds", "sound", "something", "anything", "nothing",
    }
)  # fmt: skip
I_WORDS = frozenset({"i", "i'm", "i've", "i'll", "i'd"})
WE_WORDS = frozenset({"we", "we're", "we've", "we'll", "we'd"})


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("\u2019", "'"))


def _count_phrases(toks: Sequence[str], phrases: Sequence[tuple[str, ...]]) -> tuple[int, set[int]]:
    """Count phrase matches, longest first, each word used once. Returns the used positions."""
    used: set[int] = set()
    count = 0
    ordered = sorted(phrases, key=len, reverse=True)
    for i in range(len(toks)):
        if i in used:
            continue
        for phrase in ordered:
            end = i + len(phrase)
            if tuple(toks[i:end]) == phrase and not used.intersection(range(i, end)):
                used.update(range(i, end))
                count += 1
                break
    return count, used


def count_fillers_and_hedges(text: str) -> tuple[int, int]:
    toks = tokens(text)
    hedges, hedge_used = _count_phrases(toks, HEDGE_PHRASES)
    fillers = 0
    filler_used: set[int] = set()
    phrase_count, phrase_used = _count_phrases(toks, FILLER_PHRASES)
    fillers += phrase_count
    filler_used |= phrase_used
    for i, tok in enumerate(toks):
        if i in hedge_used or i in filler_used:
            continue
        if tok in FILLER_WORDS:
            fillers += 1
        elif tok == "like":
            before = toks[i - 1] if i > 0 else ""
            after = toks[i + 1] if i + 1 < len(toks) else ""
            if before not in _LIKE_NOT_FILLER_AFTER and after != "to":
                fillers += 1
    return fillers, hedges


def count_ownership(text: str) -> tuple[int, int]:
    """How many times the speaker says "I" and "we"."""
    toks = tokens(text)
    return sum(t in I_WORDS for t in toks), sum(t in WE_WORDS for t in toks)


@dataclass(frozen=True)
class TimedWord:
    start: float
    end: float


@dataclass(frozen=True)
class AnswerMeasure:
    """What was measured in one answer. Stored as JSON so ratings can be redone later."""

    words: int
    fillers: int
    hedges: int
    i_count: int
    we_count: int
    duration_s: float
    speaking_s: float | None  # None when there are no times at all
    first_word_s: float | None
    long_pauses: int | None
    exact: bool  # True when word times were used

    def to_dict(self) -> dict[str, object]:
        return {
            "words": self.words,
            "fillers": self.fillers,
            "hedges": self.hedges,
            "i_count": self.i_count,
            "we_count": self.we_count,
            "duration_s": round(self.duration_s, 2),
            "speaking_s": None if self.speaking_s is None else round(self.speaking_s, 2),
            "first_word_s": None if self.first_word_s is None else round(self.first_word_s, 2),
            "long_pauses": self.long_pauses,
            "exact": self.exact,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AnswerMeasure:
        def num(key: str) -> float | None:
            value = data.get(key)
            return float(value) if isinstance(value, int | float) else None

        pauses = data.get("long_pauses")
        return cls(
            words=int(num("words") or 0),
            fillers=int(num("fillers") or 0),
            hedges=int(num("hedges") or 0),
            i_count=int(num("i_count") or 0),
            we_count=int(num("we_count") or 0),
            duration_s=num("duration_s") or 0.0,
            speaking_s=num("speaking_s"),
            first_word_s=num("first_word_s"),
            long_pauses=int(pauses) if isinstance(pauses, int) else None,
            exact=bool(data.get("exact")),
        )


def measure_answer(
    transcript: str,
    times: Sequence[TimedWord],
    *,
    duration_s: float,
    exact: bool,
) -> AnswerMeasure:
    """Measure one answer. `times` are word times (exact) or sentence times (not exact)."""
    fillers, hedges = count_fillers_and_hedges(transcript)
    i_count, we_count = count_ownership(transcript)
    words = len(tokens(transcript))
    if not times:
        return AnswerMeasure(
            words, fillers, hedges, i_count, we_count, duration_s, None, None, None, False
        )
    ordered = sorted(times, key=lambda t: t.start)
    gaps = [max(0.0, nxt.start - prev.end) for prev, nxt in itertools.pairwise(ordered)]
    span = max(t.end for t in ordered) - ordered[0].start
    not_speaking = sum(g for g in gaps if g > PAUSE_NOT_SPEAKING_SECONDS)
    return AnswerMeasure(
        words=words,
        fillers=fillers,
        hedges=hedges,
        i_count=i_count,
        we_count=we_count,
        duration_s=duration_s,
        speaking_s=max(0.0, span - not_speaking),
        first_word_s=ordered[0].start,
        long_pauses=sum(g > LONG_PAUSE_SECONDS for g in gaps),
        exact=exact,
    )


def is_scorable(words: int) -> bool:
    return words >= MIN_ANSWER_WORDS


# ---- the interview's spoken rubric ----------------------------------------------------------


@dataclass(frozen=True)
class SpokenItem:
    key: str
    label: str
    value: float
    unit: str
    rating: str
    points: int
    benchmark: str
    tip: str  # empty when the rating is Good
    approximate: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "value": round(self.value, 2),
            "rating": self.rating,
            "points": self.points,
            "approximate": self.approximate,
        }


@dataclass(frozen=True)
class SpokenResult:
    enough: bool  # False: fewer than MIN_SPOKEN_WORDS in total
    items: tuple[SpokenItem, ...] = ()
    score: int | None = None
    raw: dict[str, float] = field(default_factory=dict)

    def worst_tip(self) -> str:
        """The tip of the lowest-rated sub-aspect, or "" when every one is Good."""
        weak = [i for i in self.items if i.rating != GOOD]
        if not weak:
            return ""
        return min(weak, key=lambda i: RATING_ORDER[i.rating]).tip


def spoken_result(
    answers: Sequence[AnswerMeasure],
    *,
    reading_aloud: bool,
    question_words: Sequence[int],
) -> SpokenResult:
    """The seven values, ratings and the rubric score, from the scorable answers.

    `question_words` has one number per answer: the words in the question asked. It is used
    to take the time a person needs to read a question out of their thinking time when the
    question was not read aloud.
    """
    pairs = [(a, q) for a, q in zip(answers, question_words, strict=True) if is_scorable(a.words)]
    total_words = sum(a.words for a, _ in pairs)
    if total_words < MIN_SPOKEN_WORDS:
        return SpokenResult(enough=False)
    scorable = [a for a, _ in pairs]
    values: dict[str, float] = {}
    approximate: set[str] = set()

    speaking = [a.speaking_s for a in scorable]
    if all(s is not None for s in speaking) and sum(s or 0 for s in speaking) > 0:
        values["pace"] = total_words / (sum(s or 0 for s in speaking) / 60)
    elif sum(a.duration_s for a in scorable) > 0:
        values["pace"] = total_words / (sum(a.duration_s for a in scorable) / 60)
        approximate.add("pace")
    thinking = [
        max(0.0, a.first_word_s - (0 if reading_aloud else q / READING_WORDS_PER_SECOND))
        for a, q in pairs
        if a.first_word_s is not None
    ]
    if thinking:
        values["thinking"] = sum(thinking) / len(thinking)
    values["fillers"] = sum(a.fillers for a in scorable) / total_words * 100
    values["hedging"] = sum(a.hedges for a in scorable) / total_words * 100
    if all(a.long_pauses is not None for a in scorable):
        values["pauses"] = float(sum(a.long_pauses or 0 for a in scorable))
    values["length"] = sum(a.duration_s for a in scorable) / len(scorable)
    i_total, we_total = sum(a.i_count for a in scorable), sum(a.we_count for a in scorable)
    if i_total + we_total > 0:
        values["ownership"] = i_total / (i_total + we_total) * 100
    if not all(a.exact for a in scorable):
        approximate.update({"thinking", "pauses"})

    items: list[SpokenItem] = []
    for aspect in SPOKEN_ASPECTS:
        if aspect.key not in values:
            continue
        rating = aspect.rate(values[aspect.key])
        items.append(
            SpokenItem(
                key=aspect.key,
                label=aspect.label,
                value=values[aspect.key],
                unit=aspect.unit,
                rating=rating,
                points=POINTS[rating],
                benchmark=aspect.benchmark,
                tip="" if rating == GOOD else ASPECT_BY_KEY[aspect.key].tip,
                approximate=aspect.key in approximate,
            )
        )
    return SpokenResult(
        enough=True,
        items=tuple(items),
        score=average_score([i.points for i in items]),
        raw=values,
    )
