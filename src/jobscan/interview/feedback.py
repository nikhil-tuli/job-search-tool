"""Answer feedback from the model: read the reply, check it, and work out the scores.

The model is not trusted. The app checks that each quote is in the transcript (otherwise the
rating is capped), replaces numbers in the improved answer that are not in the CV or the answer,
and works out every average itself. See docs/INTERVIEW_PREP_REQUIREMENTS.md, section 6.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .rules import (
    ANSWER_ASPECT_KEYS,
    MAX_QUOTE_WORDS,
    UNSUPPORTED_RATING_CAP,
    average_score,
)

MAX_REASON_CHARS = 400
MAX_TEXT_CHARS = 1_500
MAX_IMPROVED_CHARS = 2_500
MAX_WORKED = 3
MAX_NEXT = 3
MAX_TOP_TIPS = 3
NUMBER_PLACEHOLDER = "[add figure]"


class FeedbackError(Exception):
    """The reply could not be used. The message says why, for the log (never shown with text)."""


def extract_json(text: str) -> dict[str, Any]:
    """The JSON object in a reply, allowing a code fence or words around it."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise FeedbackError("The reply has no JSON object.")
    body = cleaned[start : end + 1]
    try:
        # strict=False allows raw line breaks inside text. Models often write them.
        data = json.loads(body, strict=False)
    except ValueError:
        try:  # a trailing comma before } or ] is another common slip
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", body), strict=False)
        except json.JSONDecodeError as exc:
            raise FeedbackError(
                f"The reply is not valid JSON: {exc.msg} at character {exc.pos} of {len(body)}."
            ) from None
    if not isinstance(data, dict):
        raise FeedbackError("The reply is not a JSON object.")
    return data


def normalise(text: str) -> str:
    """Lower case, letters and digits only, single spaces. For comparing quotes."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower().replace("\u2019", "'").replace("'", "")))


def quote_found(quote: str, transcript: str) -> bool:
    """True when the quote is a short, non-empty piece of the transcript."""
    q = normalise(quote)
    if not q or len(q.split()) > MAX_QUOTE_WORDS:
        return False
    return q in normalise(transcript)


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _digits(number: str) -> str:
    return number.replace(",", "").rstrip(".")


def guard_numbers(improved: str, *allowed_sources: str) -> tuple[str, int]:
    """Replace each number that is in none of the sources. Numbers in [brackets] are kept."""
    allowed = {_digits(n) for source in allowed_sources for n in _NUMBER.findall(source)}
    replaced = 0

    def fix(match: re.Match[str]) -> str:
        nonlocal replaced
        if _digits(match.group(0)) in allowed:
            return match.group(0)
        replaced += 1
        return NUMBER_PLACEHOLDER

    parts = re.split(r"(\[[^\]]*\])", improved)
    out = [p if p.startswith("[") and p.endswith("]") else _NUMBER.sub(fix, p) for p in parts]
    return "".join(out), replaced


# ---- what the app keeps ---------------------------------------------------------------------


@dataclass(frozen=True)
class AspectRating:
    key: str
    score: int  # after any cap
    reason: str
    quote: str
    capped: bool  # True when the quote was missing or not in the transcript


@dataclass(frozen=True)
class NextStep:
    title: str
    why: str
    fix: str


@dataclass(frozen=True)
class AnswerFeedback:
    idx: int  # the question number, from 1
    ratings: tuple[AspectRating, ...]
    verdict: str
    worked: tuple[str, ...]
    next_steps: tuple[NextStep, ...]
    improved: str
    numbers_replaced: int
    score: int  # mean of the five ratings

    def to_dict(self) -> dict[str, Any]:
        return {
            "idx": self.idx,
            "ratings": [
                {
                    "key": r.key,
                    "score": r.score,
                    "reason": r.reason,
                    "quote": r.quote,
                    "capped": r.capped,
                }
                for r in self.ratings
            ],
            "verdict": self.verdict,
            "worked": list(self.worked),
            "next": [{"title": n.title, "why": n.why, "fix": n.fix} for n in self.next_steps],
            "improved": self.improved,
            "numbers_replaced": self.numbers_replaced,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnswerFeedback:
        return cls(
            idx=int(data["idx"]),
            ratings=tuple(
                AspectRating(r["key"], int(r["score"]), r["reason"], r["quote"], bool(r["capped"]))
                for r in data["ratings"]
            ),
            verdict=data["verdict"],
            worked=tuple(data["worked"]),
            next_steps=tuple(NextStep(n["title"], n["why"], n["fix"]) for n in data["next"]),
            improved=data["improved"],
            numbers_replaced=int(data.get("numbers_replaced", 0)),
            score=int(data["score"]),
        )


@dataclass(frozen=True)
class FeedbackResult:
    answers: tuple[AnswerFeedback, ...]
    missing: tuple[int, ...]  # question numbers the model gave nothing for
    top_tips: tuple[str, ...]
    score: int | None  # the rubric score: mean of the answer scores
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnswerText:
    """One answer sent to the model."""

    idx: int  # the question number, from 1
    question: str
    transcript: str


def _text(value: Any, limit: int, what: str) -> str:
    if not isinstance(value, str):
        raise FeedbackError(f"{what} is not text.")
    return " ".join(value.split())[:limit]


def _score(value: Any) -> int:
    if isinstance(value, str):
        try:
            value = float(value.strip())  # some models quote the number
        except ValueError:
            raise FeedbackError("A rating is not a number.") from None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FeedbackError("A rating is not a number.")
    if not 0 <= value <= 100:
        raise FeedbackError("A rating is outside 0 to 100.")
    return int(value + 0.5)


def _rating(key: str, raw: Any, transcript: str) -> AspectRating:
    if not isinstance(raw, dict):
        raise FeedbackError(f"The {key} rating is missing.")
    score = _score(raw.get("score"))
    reason = _text(raw.get("reason", ""), MAX_REASON_CHARS, "A reason")
    quote = _text(raw.get("quote", ""), MAX_REASON_CHARS, "A quote")
    capped = not quote_found(quote, transcript)
    if capped:
        score = min(score, UNSUPPORTED_RATING_CAP)
    return AspectRating(key, score, reason, quote, capped)


def _list_of_text(value: Any, limit: int, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FeedbackError(f"{what} is not a list.")
    return [
        _text(v, MAX_TEXT_CHARS, what) for v in value[:limit] if isinstance(v, str) and v.strip()
    ]


def _next_steps(value: Any) -> tuple[NextStep, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise FeedbackError("The next steps are not a list.")
    steps: list[NextStep] = []
    for item in value[:MAX_NEXT]:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title", ""), MAX_TEXT_CHARS, "A title")
        if title:
            steps.append(
                NextStep(
                    title,
                    _text(item.get("why", ""), MAX_TEXT_CHARS, "A reason"),
                    _text(item.get("fix", ""), MAX_TEXT_CHARS, "A fix"),
                )
            )
    return tuple(steps)


def parse_feedback(reply: str, answers: Sequence[AnswerText], cv_text: str) -> FeedbackResult:
    """Read and check the model's reply. Raises FeedbackError when nothing is usable."""
    data = extract_json(reply)
    items = data.get("answers")
    if not isinstance(items, list):
        raise FeedbackError("The reply has no answers list.")
    by_idx = {a.idx: a for a in answers}
    done: dict[int, AnswerFeedback] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        q = item.get("q")
        if isinstance(q, str) and q.strip().isdigit():
            q = int(q)  # some models quote the number
        if isinstance(q, bool) or not isinstance(q, int) or q not in by_idx or q in done:
            continue
        sent = by_idx[q]
        ratings_raw = item.get("ratings")
        if not isinstance(ratings_raw, dict):
            raise FeedbackError("An answer has no ratings.")
        ratings = tuple(_rating(k, ratings_raw.get(k), sent.transcript) for k in ANSWER_ASPECT_KEYS)
        improved_raw = item.get("improved", "")
        if not isinstance(improved_raw, str):
            raise FeedbackError("The improved answer is not text.")
        improved, replaced = guard_numbers(
            improved_raw.strip()[:MAX_IMPROVED_CHARS], cv_text, sent.transcript
        )
        score = average_score([r.score for r in ratings])
        assert score is not None
        done[q] = AnswerFeedback(
            idx=q,
            ratings=ratings,
            verdict=_text(item.get("verdict", ""), MAX_TEXT_CHARS, "The verdict"),
            worked=tuple(_list_of_text(item.get("worked"), MAX_WORKED, "What worked")),
            next_steps=_next_steps(item.get("next")),
            improved=improved,
            numbers_replaced=replaced,
            score=score,
        )
    if not done:
        raise FeedbackError("The reply has no usable answers.")
    ordered = tuple(done[i] for i in sorted(done))
    missing = tuple(sorted(set(by_idx) - set(done)))
    tips = tuple(_list_of_text(data.get("top_tips"), MAX_TOP_TIPS, "A tip"))
    return FeedbackResult(
        answers=ordered,
        missing=missing,
        top_tips=tips,
        score=average_score([a.score for a in ordered]),
    )


def combine_tips(spoken_tip: str, model_tips: Sequence[str]) -> tuple[str, ...]:
    """Top tips: the spoken tip (if any) first, then the model's, up to three, no repeats."""
    tips: list[str] = []
    for tip in (spoken_tip, *model_tips):
        if tip and tip not in tips:
            tips.append(tip)
    return tuple(tips[:MAX_TOP_TIPS])


# ---- the request -----------------------------------------------------------------------------

SCALE_TEXT = """\
Rate each answer from 0 to 100 on five things. Use these ranges.
- relevance: 80-100 answers exactly what was asked, nothing off-topic. 60-79 partly answers or \
drifts. Under 60 misses the question.
- structure: behavioural answers need situation, action, result. Situational answers need \
steps. An introduction needs present, past, future. 80-100 clear order, headline early. 60-79 \
hard to follow. Under 60 no order.
- specificity: 80-100 several concrete facts (names, numbers, details). 60-79 a few. Under 60 \
generic.
- impact: own decisions and a real result. 80-100 own action and a measured result. 60-79 result \
or ownership unclear. Under 60 neither stated.
- role_fit: links the answer to the role's needs and must-haves. 80-100 clear link. 60-79 weak \
link. Under 60 no link."""

SYSTEM_PROMPT = f"""You are a strict, fair interview coach. You read a candidate's spoken \
answers (as transcripts) and give feedback. Reply with one JSON object and nothing else.

{SCALE_TEXT}

Rules:
- Judge only what the transcript says. Use the CV and the role only to judge relevance, \
specificity and role fit.
- For every rating give a short reason and a quote: at most 15 words copied exactly from the \
answer. A rating with no exact quote will be lowered.
- Never add facts that are not in the CV or the answer. In "improved", write a model answer of \
130 to 200 words. Where a fact is missing, write a placeholder in square brackets, such as \
[add your budget figure]. Never invent employers, titles or numbers.
- Write short plain sentences. "worked" has 2 or 3 points. "next" has 1 to 3 items, each with \
a short title, why it matters, and one concrete fix. "top_tips" has up to 3 short tips for \
the next interview.

Reply format:
{{"answers": [{{"q": 1,
  "ratings": {{"relevance": {{"score": 0, "reason": "", "quote": ""}},
              "structure": {{"score": 0, "reason": "", "quote": ""}},
              "specificity": {{"score": 0, "reason": "", "quote": ""}},
              "impact": {{"score": 0, "reason": "", "quote": ""}},
              "role_fit": {{"score": 0, "reason": "", "quote": ""}}}},
  "verdict": "", "worked": [""], "next": [{{"title": "", "why": "", "fix": ""}}],
  "improved": ""}}],
 "top_tips": [""]}}"""


def build_user_message(
    *,
    cv_text: str,
    role_lines: Sequence[str],
    answers: Sequence[AnswerText],
    must_haves: Sequence[tuple[str, str]] = (),
) -> str:
    lines = ["CANDIDATE CV:", cv_text.strip(), "", "ROLE:", *role_lines]
    if must_haves:
        lines += ["", "MUST-HAVES (status against the CV):"]
        lines += [f"- {item}: {status}" for item, status in must_haves]
    lines += ["", "ANSWERS:"]
    for a in answers:
        lines += [f"[Question {a.idx}] {a.question}", f"[Answer {a.idx}] {a.transcript}", ""]
    return "\n".join(lines).strip()
