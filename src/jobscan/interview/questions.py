"""Writing the interview's questions, and checking what the model sends back."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .feedback import FeedbackError, extract_json

# The three interview types, as shown to the user.
COMPANY_ROLE = "Company role"
GENERAL_ROLE = "General role"
MY_QUESTIONS = "My questions"
INTERVIEW_TYPES = (COMPANY_ROLE, GENERAL_ROLE, MY_QUESTIONS)

QUESTION_COUNT = 5
MAX_QUESTION_WORDS = 30
MIN_OWN_CHARS = 5
MAX_OWN_CHARS = 300
MAX_OWN_QUESTIONS = 5

# slot -> (kind, what it draws on)
SLOTS: tuple[tuple[int, str, str], ...] = (
    (1, "introduction", "Ask the candidate to walk through their background, tied to this role."),
    (2, "behavioural", "Ask about a past situation that uses a must-have marked Met."),
    (3, "behavioural", "Ask about a different past situation that fits another need of the role."),
    (4, "situational", "Ask how the candidate would handle a realistic situation in this role."),
    (5, "gap", "Ask about something the role needs that the CV shows only partly or not at all."),
)

SYSTEM_PROMPT = """You write interview questions for a practice interview. Reply with one JSON \
object and nothing else.

Rules:
- Write exactly the questions asked for, in the order of the slots.
- One question per slot. One sentence. At most 30 words. Plain words.
- Use only facts that are in the CV. Never invent an employer, a title, a tool or a number.
- Questions must fit the role and the candidate's CV. Do not repeat or reword the questions \
listed under ALREADY ASKED.
- For the "gap" question, ask about something the role needs that the CV does not clearly show. \
Ask it as a fair question, not as an accusation.

Reply format:
{"questions": [{"slot": 1, "kind": "introduction", "text": "..."}]}"""


@dataclass(frozen=True)
class Question:
    slot: int
    kind: str
    text: str


def clean_own_questions(raw: Sequence[str]) -> tuple[list[str], str]:
    """The user's own questions: blanks dropped, repeats dropped. Returns (questions, error)."""
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        text = " ".join(item.split())
        if not text:
            continue
        if len(text) < MIN_OWN_CHARS:
            return [], f"A question needs at least {MIN_OWN_CHARS} characters."
        if len(text) > MAX_OWN_CHARS:
            return [], f"A question can have at most {MAX_OWN_CHARS} characters."
        if text.casefold() in seen:
            continue
        seen.add(text.casefold())
        out.append(text)
    if not out:
        return [], "Type at least one question."
    if len(out) > MAX_OWN_QUESTIONS:
        return [], f"Use at most {MAX_OWN_QUESTIONS} questions."
    return out, ""


def own_questions(texts: Sequence[str]) -> list[Question]:
    return [Question(i, "own", text) for i, text in enumerate(texts, start=1)]


def build_user_message(
    *,
    cv_text: str,
    role_lines: Sequence[str],
    must_haves: Sequence[tuple[str, str]],
    recent: Sequence[str],
) -> str:
    lines = ["CANDIDATE CV:", cv_text.strip(), "", "ROLE:", *role_lines]
    if must_haves:
        lines += ["", "MUST-HAVES (status against the CV):"]
        lines += [f"- {item}: {status}" for item, status in must_haves]
    lines += ["", "SLOTS:"]
    lines += [f"{slot}. {kind}: {guide}" for slot, kind, guide in SLOTS]
    if recent:
        lines += ["", "ALREADY ASKED:"]
        lines += [f"- {q}" for q in recent]
    return "\n".join(lines)


def parse_questions(reply: str) -> list[Question]:
    """Read the model's reply. Raises FeedbackError when it does not match the slots."""
    data: dict[str, Any] = extract_json(reply)
    items = data.get("questions")
    if not isinstance(items, list) or len(items) != QUESTION_COUNT:
        raise FeedbackError(f"The reply does not have {QUESTION_COUNT} questions.")
    out: list[Question] = []
    for (slot, kind, _), item in zip(SLOTS, items, strict=True):
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise FeedbackError("A question is empty.")
        text = " ".join(text.split())
        if len(text.split()) > MAX_QUESTION_WORDS:
            raise FeedbackError("A question is too long.")
        out.append(Question(slot, kind, text))
    return out
