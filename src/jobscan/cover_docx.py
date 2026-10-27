"""Read a Word cover letter template and write a new letter into it, keeping its layout.

The template is a finished letter of the user's. Only the text inside existing runs changes:
the date, the company line, the opening paragraph, the lead-in line, the bullets (a bold label
and plain text) and the closing paragraph. Bullets are copied from the template's own bullets,
so fonts, bullets and spacing stay as the person made them. The name, contact line, salutation
and sign-off are never touched.
"""

from __future__ import annotations

import copy
import io
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from docx.text.paragraph import Paragraph

from .tailor_docx import DocxError, _is_bullet, _open, _split_ends, numbers_in

MAX_BULLETS = 7
MIN_BULLETS = 1
MAX_PAGES = 2
MAX_LETTER_CHARS = 4_000  # stands in for the 2-page check when LibreOffice is not installed
LIMITS = {"opening": 900, "lead": 140, "label": 70, "text": 360, "closing": 800}

_SALUTATION = re.compile(r"^\s*dear\b", re.I)
_DATE = re.compile(
    r"^\s*(?:[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4})\s*$"
)


@dataclass(frozen=True)
class LetterBullet:
    label: str  # without the trailing colon
    text: str


@dataclass(frozen=True)
class LetterStructure:
    opening: str
    lead_in: str  # "" when the template has none
    bullets: tuple[LetterBullet, ...]
    closing: str
    date_text: str  # "" when the template has no date line
    company_text: str


@dataclass(frozen=True)
class LetterPlan:
    opening: str
    lead_in: str
    bullets: tuple[LetterBullet, ...]
    closing: str
    date_text: str
    company_text: str


@dataclass
class _Parts:
    date: Any = None
    company: Any = None
    opening: Any = None
    lead_in: Any = None
    bullets: list[Any] | None = None
    closing: Any = None
    fixed_before: list[str] | None = None
    fixed_after: list[str] | None = None


def _runs(paragraph: Any) -> list[Any]:
    return [r for r in paragraph.runs if r.text.strip()]


def _bullet_runs(paragraph: Any) -> tuple[Any, Any]:
    bold = [r for r in _runs(paragraph) if r.bold]
    plain = [r for r in _runs(paragraph) if not r.bold]
    if len(bold) != 1 or len(plain) != 1:
        raise DocxError(
            "The bullets in this template need a bold label followed by plain text, "
            "so it cannot be used."
        )
    return bold[0], plain[0]


def _find_parts(document: Any) -> _Parts:
    paragraphs = document.paragraphs
    salutation = next((i for i, p in enumerate(paragraphs) if _SALUTATION.match(p.text)), None)
    if salutation is None:
        raise DocxError(
            "Could not find a greeting such as 'Dear Hiring Manager,' in this template."
        )
    parts = _Parts()
    index = None
    for i in range(salutation):
        if _DATE.match(paragraphs[i].text):
            index = i
            break
    used: set[int] = set()
    if index is not None:
        parts.date = paragraphs[index]
        used.add(index)
        for i in range(index + 1, salutation):
            if paragraphs[i].text.strip():
                parts.company = paragraphs[i]
                used.add(i)
                break

    position = salutation + 1
    total = len(paragraphs)

    def next_text(start: int) -> int | None:
        for i in range(start, total):
            if paragraphs[i].text.strip():
                return i
        return None

    opening = next_text(position)
    if opening is None or _is_bullet(paragraphs[opening]):
        raise DocxError("Could not find the opening paragraph after the greeting.")
    parts.opening = paragraphs[opening]
    used.add(opening)
    cursor = next_text(opening + 1)
    if cursor is not None and not _is_bullet(paragraphs[cursor]):
        parts.lead_in = paragraphs[cursor]
        used.add(cursor)
        cursor = next_text(cursor + 1)
    bullets: list[Any] = []
    while cursor is not None and _is_bullet(paragraphs[cursor]):
        bullets.append(paragraphs[cursor])
        used.add(cursor)
        cursor = next_text(cursor + 1)
    if not bullets:
        raise DocxError("Could not find the bullets in this template.")
    for paragraph in bullets:
        _bullet_runs(paragraph)
    parts.bullets = bullets
    if cursor is None:
        raise DocxError("Could not find a closing paragraph after the bullets.")
    parts.closing = paragraphs[cursor]
    used.add(cursor)
    parts.fixed_before = [p.text for i, p in enumerate(paragraphs) if i not in used and i < cursor]
    parts.fixed_after = [p.text for i, p in enumerate(paragraphs) if i > cursor]
    return parts


def _main_run(paragraph: Any) -> Any:
    runs = _runs(paragraph)
    if not runs:
        raise DocxError("An empty paragraph was found where text was expected.")
    return max(runs, key=lambda r: len(r.text))


def _set_paragraph(paragraph: Any, text: str) -> None:
    """Put `text` in the paragraph's main run, keeping that run's formatting."""
    main = _main_run(paragraph)
    original = main.text
    lead, _core, tail = _split_ends(original)
    for run in paragraph.runs:
        if run is not main and run.text.strip():
            run.text = ""
    main.text = lead + text + tail


def analyse_letter(data: bytes) -> LetterStructure:
    document = _open(data)
    parts = _find_parts(document)
    bullets = []
    for paragraph in parts.bullets or []:
        bold, plain = _bullet_runs(paragraph)
        _lead, core, _tail = _split_ends(plain.text)
        bullets.append(LetterBullet(bold.text.strip().rstrip(":").strip(), core))
    return LetterStructure(
        opening=_split_ends(_main_run(parts.opening).text)[1],
        lead_in=_split_ends(_main_run(parts.lead_in).text)[1] if parts.lead_in else "",
        bullets=tuple(bullets),
        closing=_split_ends(_main_run(parts.closing).text)[1],
        date_text=parts.date.text.strip() if parts.date else "",
        company_text=parts.company.text.strip() if parts.company else "",
    )


def format_date(day: date) -> str:
    """The date the way the template writes it: 'Jan 11, 2026'."""
    return f"{day.strftime('%b')} {day.day}, {day.year}"


def build_letter(data: bytes, plan: LetterPlan) -> bytes:
    """A copy of the template with the plan written into it. The input is never changed."""
    document = _open(data)
    parts = _find_parts(document)
    templates = list(parts.bullets or [])
    count = len(plan.bullets)
    if count < MIN_BULLETS:
        raise DocxError("A letter needs at least one bullet.")

    first_p, last_p = templates[0], templates[-1]
    middle_p = templates[1] if len(templates) >= 3 else templates[0]
    tail_last = _split_ends(_bullet_runs(last_p)[1].text)[2]
    tail_mid = _split_ends(_bullet_runs(middle_p)[1].text)[2]
    suffix = _label_suffix(_bullet_runs(middle_p)[0].text)
    ppr = {k: copy.deepcopy(p._p.pPr) for k, p in (("f", first_p), ("m", middle_p), ("l", last_p))}
    model = copy.deepcopy(middle_p._p)

    rows = list(templates)
    while len(rows) < count:  # new bullets are copies of a middle template bullet
        clone = copy.deepcopy(model)
        rows[-1]._p.addnext(clone)
        rows.append(Paragraph(clone, rows[-1]._parent))
    for extra in rows[count:]:
        extra._p.getparent().remove(extra._p)
    rows = rows[:count]

    for number, (paragraph, bullet) in enumerate(zip(rows, plan.bullets, strict=True)):
        bold, plain = _bullet_runs(paragraph)
        lead = _split_ends(plain.text)[0]
        last = number == count - 1
        key = "f" if number == 0 else ("l" if last else "m")
        old = paragraph._p.pPr
        if old is not None:
            paragraph._p.replace(old, copy.deepcopy(ppr[key]))
        bold.text = bullet.label.strip().rstrip(":") + suffix
        plain.text = lead + bullet.text.strip() + (tail_last if last else tail_mid)

    _set_paragraph(parts.opening, plan.opening.strip())
    if parts.lead_in is not None and plan.lead_in.strip():
        _set_paragraph(parts.lead_in, plan.lead_in.strip())
    _set_paragraph(parts.closing, plan.closing.strip())
    if parts.date is not None and plan.date_text:
        _set_paragraph(parts.date, plan.date_text)
    if parts.company is not None and plan.company_text:
        _set_paragraph(parts.company, plan.company_text)

    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _label_suffix(text: str) -> str:
    stripped = text.rstrip()
    return ":" if stripped.endswith(":") else ""


def verify_letter(original: bytes, edited: bytes) -> str | None:
    """Why the new letter breaks the template's fixed parts or bullet layout, or None."""
    before, after = _find_parts(_open(original)), _find_parts(_open(edited))
    if before.fixed_before != after.fixed_before or before.fixed_after != after.fixed_after:
        return "the name, contact line, greeting or sign-off changed"
    rows = after.bullets or []
    for number, paragraph in enumerate(rows):
        expected = (before.bullets or [])[
            0
            if number == 0
            else len(before.bullets or []) - 1
            if number == len(rows) - 1
            else 1
            if len(before.bullets or []) >= 3
            else 0
        ]
        if paragraph._p.pPr.xml != expected._p.pPr.xml:
            return "a bullet lost its formatting"
    return None


def letter_chars(plan: LetterPlan) -> int:
    return (
        len(plan.opening)
        + len(plan.lead_in)
        + len(plan.closing)
        + sum(len(b.label) + len(b.text) for b in plan.bullets)
    )


def check_text(kind: str, text: str, allowed_numbers: set[str]) -> str | None:
    """Why a piece of letter text must be rejected, or None when it is acceptable."""
    if not text.strip():
        return "it is empty"
    if "\n" in text or "•" in text or "**" in text:
        return "it has line breaks or formatting marks"
    if len(text.strip()) > LIMITS[kind]:
        return f"it is longer than {LIMITS[kind]} characters"
    if not numbers_in(text) <= allowed_numbers:
        return "it has a number that is not in your CV or template"
    return None


def too_long(plan: LetterPlan, edited: bytes, convert: Any) -> str | None:
    """Why the letter would run past two pages, or None. Uses LibreOffice when installed."""
    from .tailor_docx import page_count

    pdf = convert(edited)
    pages = page_count(pdf) if pdf is not None else None
    if pages is not None:
        if pages > MAX_PAGES:
            return f"the letter would run to {pages} pages (the limit is {MAX_PAGES})"
        return None
    if letter_chars(plan) > MAX_LETTER_CHARS:
        return "the letter is too long for two pages"
    return None
