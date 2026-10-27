"""Ask the AI service to write the changing parts of a cover letter, and check what comes back.

One call per request. The AI is told to use only facts from the CV; the code then checks every
number against the CV and the template, and drops anything unusable. The name, phone, email and
links are never sent: only the role text, the CV's experience text and the template's wording.
"""

from __future__ import annotations

from typing import Any

from .cover_docx import (
    LIMITS,
    MAX_BULLETS,
    LetterStructure,
    check_text,
)
from .tailor_ai import (
    MAX_DESCRIPTION_CHARS,
    MAX_GAP_CHARS,
    MAX_GAPS,
    Proposal,
    TailorClient,
    TailorError,
    _extract_json,
)
from .tailor_docx import CvStructure, numbers_in

MIN_PROPOSED_BULLETS = 3

SYSTEM_PROMPT = """You write the body of a cover letter for one job, for the CV owner. Reply \
with a single JSON object and nothing else: no markdown, no commentary.

Rules:
- Facts about the candidate come only from the CV (and the example letter). Never add an \
employer, job title, date, skill, tool or achievement that is not there.
- Every number you write must appear in the CV or the example letter.
- Statements about the company may come from the role description.
- Follow the example letter's voice, length and structure, but write for THIS role.
- Choose 5 to 7 bullets. Each has a short label (2-5 words) and one or two sentences.
- No line breaks, bullet characters or markdown inside any text.
- List what the role asks for that the CV does not show in "gaps". Never write those into \
the letter.

Reply format:
{
  "opening": "opening paragraph",
  "lead_in": "one line introducing the bullets",
  "bullets": [{"label": "short label", "text": "supporting text"}],
  "closing": "closing paragraph",
  "gaps": ["short things the role asks for that the CV does not show"]
}
Limits in characters: opening 900, lead_in 140, label 70, bullet text 360, closing 800. \
"gaps" has at most 6 short strings."""


def cv_text(structure: CvStructure) -> str:
    parts = [structure.summary, " ".join(structure.competencies)]
    for job in structure.jobs:
        parts.append(job.header)
        parts += [f"{b.label} {b.core}" for b in job.bullets]
    return "\n".join(p for p in parts if p)


def template_text(template: LetterStructure) -> str:
    parts = [template.opening, template.lead_in, template.closing]
    parts += [f"{b.label} {b.text}" for b in template.bullets]
    return "\n".join(p for p in parts if p)


def build_user_message(
    structure: CvStructure,
    template: LetterStructure,
    title: str,
    company: str,
    description: str,
) -> str:
    lines = [f"ROLE: {title} at {company}", "", "ROLE DESCRIPTION:"]
    lines += [description.strip()[:MAX_DESCRIPTION_CHARS], "", "CV", ""]
    if structure.summary:
        lines += [f"SUMMARY: {structure.summary}", ""]
    for job in structure.jobs:
        lines.append(f"JOB: {job.header}")
        for bullet in job.bullets:
            label = f"{bullet.label}: " if bullet.label else ""
            lines.append(f"  - {label}{bullet.core}")
        lines.append("")
    lines += [
        "EXAMPLE LETTER (written for a different company; copy its voice, not its content):",
        f"Opening: {template.opening}",
    ]
    if template.lead_in:
        lines.append(f"Lead-in: {template.lead_in}")
    lines += [f"Bullet: {b.label}: {b.text}" for b in template.bullets]
    lines.append(f"Closing: {template.closing}")
    return "\n".join(lines)


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_cover(template: LetterStructure, allowed_text: str, reply: str) -> Proposal:
    """Check every part. Raises ValueError if the reply is not usable JSON or has too little."""
    data = _extract_json(reply)
    allowed = numbers_in(allowed_text)
    changes: list[dict[str, Any]] = []
    dropped = 0

    def add(**fields: Any) -> None:
        changes.append({"id": f"c{len(changes) + 1}", **fields})

    for kind, key, label, before in (
        ("opening", "opening", "Opening paragraph", template.opening),
        ("lead_in", "lead_in", "Lead-in line", template.lead_in),
    ):
        text = _clean(data.get(key))
        if kind == "lead_in" and not template.lead_in:
            continue
        if check_text(_limit_key(kind), text, allowed) is None:
            add(kind=kind, label=label, before=before, after=text)
        else:
            dropped += 1

    bullets = data.get("bullets")
    good = 0
    for entry in bullets if isinstance(bullets, list) else []:
        if not isinstance(entry, dict) or good >= MAX_BULLETS:
            dropped += 1
            continue
        label = _clean(entry.get("label")).rstrip(":").strip()
        text = _clean(entry.get("text"))
        if check_text("label", label, allowed) or check_text("text", text, allowed):
            dropped += 1
            continue
        old = template.bullets[good] if good < len(template.bullets) else None
        add(
            kind="bullet",
            label="Bullet",
            before_label=old.label if old else "",
            before=old.text if old else "",
            after_label=label,
            after=text,
        )
        good += 1
    if good < MIN_PROPOSED_BULLETS:
        raise ValueError(f"only {good} usable bullets")

    closing = _clean(data.get("closing"))
    if check_text("closing", closing, allowed) is None:
        add(kind="closing", label="Closing paragraph", before=template.closing, after=closing)
    else:
        dropped += 1
    if not any(c["kind"] == "opening" for c in changes) or not any(
        c["kind"] == "closing" for c in changes
    ):
        raise ValueError("the opening or closing was missing or not allowed")

    gaps: list[str] = []
    for gap in data.get("gaps") or []:
        if isinstance(gap, str) and gap.strip() and len(gaps) < MAX_GAPS:
            gaps.append(gap.strip()[:MAX_GAP_CHARS])
    return Proposal(tuple(changes), tuple(gaps), dropped)


def _limit_key(kind: str) -> str:
    return "lead" if kind == "lead_in" else kind


def propose_cover(
    client: TailorClient,
    structure: CvStructure,
    template: LetterStructure,
    title: str,
    company: str,
    description: str,
) -> Proposal:
    """One AI call, with one more only if the reply could not be used."""
    user = build_user_message(structure, template, title, company, description)
    allowed = cv_text(structure) + "\n" + template_text(template)
    last_error = ""
    for attempt in range(2):
        message = (
            user
            if attempt == 0
            else user
            + f"\n\nYour last reply could not be used ({last_error}). Reply with the JSON "
            "object only, following every rule and limit."
        )
        reply = client.complete(SYSTEM_PROMPT, message)
        try:
            return parse_cover(template, allowed, reply)
        except (ValueError, TypeError) as exc:
            last_error = str(exc)
    raise TailorError(f"The AI reply could not be used ({last_error}). Try again.")


__all__ = ["LIMITS", "cv_text", "parse_cover", "propose_cover", "template_text"]
