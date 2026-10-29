"""Work out what an email means for a role: the stage it shows, which role it is about, and
how sure that is. Plain rules only; the AI is asked only when these cannot decide.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .mailbox import Mail
from .models import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    Role,
)

STAGE_INTERVIEW = "interview"
STAGE_REJECTED = "rejected"
STAGE_OFFER = "offer"
STAGE_CLOSED = "closed"
STAGE_RECEIVED = "received"  # "thanks for applying": nothing to change
STAGE_UNCLEAR = "unclear"  # signs of several stages at once
STAGES = (STAGE_INTERVIEW, STAGE_REJECTED, STAGE_OFFER, STAGE_CLOSED)

_REJECT = (
    "we will not be moving forward",
    "not moving forward",
    "not to move forward",
    "unable to move forward",
    "not able to move forward",
    "won't be moving forward",
    "decided not to continue",
    "will not be proceeding",
    "not be proceeding",
    "regret to inform",
    "unable to proceed",
    "decided not to proceed",
    "not shortlisted",
    "not been selected",
    "not selected",
    "move forward with other candidates",
    "moving forward with other candidates",
    "decided to pursue other candidates",
    "not be able to take your application forward",
    "not take your application forward",
)
_CLOSED = (
    "position has been closed",
    "role has been closed",
    "position has been filled",
    "role has been filled",
    "no longer accepting applications",
    "requisition has been closed",
    "hiring for this role has been paused",
    "role is on hold",
    "position is on hold",
    "no longer hiring for this",
)
_OFFER = (
    "pleased to offer",
    "happy to offer",
    "we would like to offer",
    "extend an offer",
    "offer letter",
    "delighted to offer",
)
_INTERVIEW = (
    "schedule an interview",
    "schedule a call",
    "interview scheduled",
    "interview invitation",
    "invite you for an interview",
    "invite you to an interview",
    "invitation to interview",
    "interview with",
    "next round",
    "technical round",
    "intro call",
    "introductory call",
    "telephonic",
    "video interview",
    "your availability",
    "round 1",
    "round 2",
    "round 3",
    "first round",
    "second round",
    "final round",
)
_RECEIVED = (
    "thank you for applying",
    "thanks for applying",
    "application received",
    "we have received your application",
    "we've received your application",
    "your application has been submitted",
    "application was sent",
)

# Senders that deliver mail for many companies, so the sender says nothing about the company.
GENERIC_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "yahoo.com",
        "hotmail.com",
        "linkedin.com",
        "naukri.com",
        "greenhouse.io",
        "lever.co",
        "workday.com",
        "myworkday.com",
        "smartrecruiters.com",
        "icims.com",
        "zohorecruit.com",
        "recruitee.com",
        "ashbyhq.com",
        "breezy.hr",
        "bamboohr.com",
        "successfactors.com",
    }
)
_COMPANY_NOISE = frozenset(
    {
        "pvt",
        "ltd",
        "limited",
        "private",
        "inc",
        "llc",
        "llp",
        "corp",
        "corporation",
        "technologies",
        "technology",
        "india",
        "solutions",
        "services",
        "systems",
        "software",
        "global",
        "group",
        "labs",
        "the",
    }
)
_TITLE_NOISE = frozenset({"and", "the", "of", "for", "to", "in", "at", "a", "an"})
_WORD = re.compile(r"[a-z0-9]+")
_APOSTROPHE = chr(0x2019)


def _text(mail: Mail) -> str:
    return f"{mail.subject}\n{mail.body[:1500]}".lower().replace(_APOSTROPHE, "'")


def _has(text: str, phrases: Sequence[str]) -> bool:
    return any(p in text for p in phrases)


def detect_stage(mail: Mail) -> str | None:
    """The stage the email shows, STAGE_UNCLEAR for mixed signs, None when it shows none."""
    text = _text(mail)
    found = [
        stage
        for stage, phrases in (
            (STAGE_REJECTED, _REJECT),
            (STAGE_CLOSED, _CLOSED),
            (STAGE_OFFER, _OFFER),
            (STAGE_INTERVIEW, _INTERVIEW),
        )
        if _has(text, phrases)
    ]
    if mail.invite and STAGE_INTERVIEW not in found:
        found.append(STAGE_INTERVIEW)
    if len(found) > 1:
        return STAGE_UNCLEAR
    if found:
        return found[0]
    return STAGE_RECEIVED if _has(text, _RECEIVED) else None


# ---- which role ---------------------------------------------------------------------------


def company_key(name: str) -> str:
    """The company name without legal suffixes, lower case, words joined: "YouTrip Pte Ltd"."""
    words = [w for w in _WORD.findall(name.lower()) if w not in _COMPANY_NOISE]
    return "".join(words) or "".join(_WORD.findall(name.lower()))


def _company_words(name: str) -> list[str]:
    words = [w for w in _WORD.findall(name.lower()) if w not in _COMPANY_NOISE]
    return words or _WORD.findall(name.lower())


def sender_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def _mentions(text: str, name: str) -> bool:
    words = _company_words(name)
    if not words or len("".join(words)) < 3:
        return False
    pattern = r"\b" + r"[\s\-]*".join(re.escape(w) for w in words) + r"\b"
    return re.search(pattern, text) is not None


def _domain_matches(address: str, name: str) -> bool:
    domain = sender_domain(address)
    key = company_key(name)
    if not domain or domain in GENERIC_DOMAINS or len(key) < 3:
        return False
    label = domain.replace("-", "").replace(".", "")
    return key in label


@dataclass(frozen=True)
class Candidate:
    role: Role
    signal: str  # "domain", "subject" or "body"


def candidates(mail: Mail, roles: Sequence[Role]) -> list[Candidate]:
    """Roles the email could be about, by company. The strongest signal found is kept."""
    subject = mail.subject.lower()
    body = mail.body[:2000].lower()
    found: list[Candidate] = []
    for role in roles:
        if _domain_matches(mail.sender, role.company):
            found.append(Candidate(role, "domain"))
        elif _mentions(subject, role.company):
            found.append(Candidate(role, "subject"))
        elif _mentions(body, role.company):
            found.append(Candidate(role, "body"))
    return found


def _title_words(title: str) -> list[str]:
    return [w for w in _WORD.findall(title.lower()) if w not in _TITLE_NOISE and len(w) > 1]


def title_score(title: str, mail: Mail) -> float:
    words = _title_words(title)
    if not words:
        return 0.0
    text = set(_WORD.findall(_text(mail)))
    return sum(1 for w in words if w in text) / len(words)


@dataclass(frozen=True)
class Match:
    role: Role
    confidence: str


def pick_role(
    mail: Mail, found: Sequence[Candidate], *, decided_by_ai: bool, ai_sure: bool
) -> Match:
    """The role to point at, and how sure we are. See docs/REQUIREMENTS.md, Round 11."""
    if len(found) == 1:
        only = found[0]
        level = CONFIDENCE_HIGH if only.signal in ("domain", "subject") else CONFIDENCE_MEDIUM
        return Match(only.role, _after_ai(level, decided_by_ai, ai_sure))
    scored = sorted(
        ((title_score(c.role.title, mail), c) for c in found),
        key=lambda pair: (-pair[0], pair[1].role.status != "Interview Ongoing"),
    )
    best, second = scored[0][0], scored[1][0]
    if best >= 0.8 and best - second >= 0.2:
        return Match(scored[0][1].role, _after_ai(CONFIDENCE_MEDIUM, decided_by_ai, ai_sure))
    return Match(scored[0][1].role, CONFIDENCE_LOW)


def _after_ai(level: str, decided_by_ai: bool, ai_sure: bool) -> str:
    if not decided_by_ai:
        return level
    if not ai_sure:
        return CONFIDENCE_LOW
    return CONFIDENCE_MEDIUM if level == CONFIDENCE_HIGH else level
