"""Split a job description and a CV into short chunks that can be compared one by one.

A job description is cut by its section headings into requirement, responsibility and company
chunks, because a match on a requirement says more than a match on the company blurb. A CV is cut
into one chunk per achievement bullet (with the employer and title it sits under) and one per
paragraph. Chunks are at most MAX_WORDS long so each stays about one idea.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Bump when the rules below change, so stored scores made with older rules are recomputed.
CHUNKER_VERSION = 1

MAX_WORDS = 100
DEFAULT_TARGET_WORDS = 40  # a description chunk is about two bullets
MIN_WORDS = 6  # shorter fragments are joined to their neighbour

KIND_REQUIREMENT = "requirement"
KIND_RESPONSIBILITY = "responsibility"
KIND_COMPANY = "company"
KIND_OTHER = "other"  # text before any recognised heading, or in a description with none

_BULLETS = "-\u2022\u2b25\u25c6\u25aa\u25cf\u25cb\u25a0\u00b7\u25ba\u27a2\u2713\u2714\u2705*"
_BULLET = re.compile(rf"^\s*[{re.escape(_BULLETS)}]\s*")
_NUMBERING = re.compile(r"^\s*\d{1,2}[.)]\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
_NON_WORD_EDGES = re.compile(r"^[^\w]+|[^\w]+$")

_COMPANY_HEADING = re.compile(
    r"^(about (?!the (role|job|position|opportunity|team)\b)(?!you\b).+"
    r"|who we are|our (team|culture|values|story|mission)|why .+|benefits|perks|compensation"
    r"|what we offer|how to apply|apply|hiring process|how .+ works|equal opportunity"
    r"|job posting statement|ai technology statement|other open roles|ready to .+|tags"
    r"|working arrangement|travel requirements)$"
)
_REQUIREMENT_HEADING = re.compile(
    r"^(.*\b(qualifications?|requirements?|skills?|competenc(y|ies)|attributes)\b.*"
    r"|what we(['\u2019]re| are)? looking for|we(['\u2019]re| are) looking for"
    r"|what we look for( at .+)?|who you are.*|what you bring|candidate profile"
    r"|ideal candidate.*|the person we want|good to have|nice to have|must have"
    r"|what will put you ahead|experience|education(al)?|technical experience.*"
    r"|preferred .*|mandatory .*)$"
)
_RESPONSIBILITY_HEADING = re.compile(
    r"^(.*\b(responsibilit(y|ies)|accountabilit(y|ies)|duties)\b.*"
    r"|what you(['\u2019]ll| will) (do|own)|the role|role (overview|summary|purpose)"
    r"|about the (role|job)|job (description|summary)|position summary.*|overview"
    r"|your job|general role description|project description"
    r"|what success looks like.*)$"
)
# "Key facts" lines that say nothing about fit, and page furniture.
_NOISE_LINE = re.compile(
    r"^(apply\b|enter your email|requisition id|subject line|job id|date\s*:|ctc\b|"
    r"notice period|employment type|reporting to|location\s*[-:]|work location|"
    r"qualified candidates share)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Chunk:
    kind: str
    text: str


def _heading_kind(line: str) -> str | None:
    """The section a heading line opens, or None when the line is not a recognised heading."""
    if len(line.split()) > 9 or line.rstrip().endswith("."):
        return None
    text = _NUMBERING.sub("", line)
    text = _NON_WORD_EDGES.sub("", text).casefold()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    if _COMPANY_HEADING.match(text):
        return KIND_COMPANY
    if _REQUIREMENT_HEADING.match(text):
        return KIND_REQUIREMENT
    if _RESPONSIBILITY_HEADING.match(text):
        return KIND_RESPONSIBILITY
    return None


def _split_long(text: str) -> list[str]:
    """Cut text longer than MAX_WORDS at sentence ends, then by words if one sentence is huge."""
    if len(text.split()) <= MAX_WORDS:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in _SENTENCE_END.split(text):
        words = sentence.split()
        while len(words) > MAX_WORDS:
            if current:
                pieces.append(" ".join(current))
                current, count = [], 0
            pieces.append(" ".join(words[:MAX_WORDS]))
            words = words[MAX_WORDS:]
        if not words:
            continue
        if count + len(words) > MAX_WORDS and current:
            pieces.append(" ".join(current))
            current, count = [], 0
        current.append(" ".join(words))
        count += len(words)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _clean(text: str) -> str:
    return " ".join(_BULLET.sub("", text, count=1).split())


def _pack(pieces: list[str], target_words: int) -> list[str]:
    """Join neighbouring pieces up to about target_words; fragments under MIN_WORDS always join."""
    out: list[str] = []
    current = ""
    for piece in pieces:
        size = len(current.split())
        if current and size >= MIN_WORDS and size + len(piece.split()) > target_words:
            out.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        if len(out) and len(current.split()) < MIN_WORDS:
            out[-1] = f"{out[-1]} {current}"
        else:
            out.append(current)
    return out


def chunk_description(description: str, target_words: int = DEFAULT_TARGET_WORDS) -> list[Chunk]:
    """Chunks of a job description, each labelled with the section it came from.

    Neighbouring bullets of a section are packed together up to about target_words, because every
    chunk costs one embedding call and a typical posting has 25 or more bullets.
    """
    sections: list[tuple[str, list[str]]] = [(KIND_OTHER, [])]
    for raw in description.splitlines():
        line = " ".join(raw.split())
        if not line or _NOISE_LINE.match(line):
            continue
        kind = None if _BULLET.match(line) else _heading_kind(line)
        if kind is not None:
            sections.append((kind, []))
        else:
            sections[-1][1].append(line)
    chunks: list[Chunk] = []
    for kind, lines in sections:
        # Each bullet or paragraph is its own piece; unmarked consecutive lines are one paragraph.
        pieces: list[str] = []
        for line in lines:
            if _BULLET.match(line) or not pieces or _NUMBERING.match(line):
                pieces.append(_clean(line))
            else:
                pieces[-1] = f"{pieces[-1]} {_clean(line)}"
        for piece in _pack(pieces, target_words):
            chunks.extend(Chunk(kind, part) for part in _split_long(piece))
    return chunks


# ---- CV ----------------------------------------------------------------------------------------

_EMAIL = re.compile(r"\S+@\S+\.\S+")
_URL = re.compile(r"(https?://|www\.)\S+|linkedin\.com/\S+", re.IGNORECASE)
_PHONE = re.compile(r"(?:\+?\d[\s().-]?){10,}")
_DATE_RANGE = re.compile(
    r"\b[A-Za-z]{3,9}\.?\s*[\u2019']?\s*\d{2,4}\s*(?:-|\u2013|\u2014|to)\s*"
    r"(?:[A-Za-z]{3,9}\.?\s*[\u2019']?\s*\d{2,4}|present|current|now)\b",
    re.IGNORECASE,
)
_CV_SECTION_TITLES = frozenset(
    {
        "summary",
        "profile",
        "professional summary",
        "experience",
        "work experience",
        "professional experience",
        "employment history",
        "education",
        "skills",
        "core competencies",
        "key skills",
        "technical skills",
        "certifications",
        "projects",
        "achievements",
        "awards",
        "languages",
        "interests",
    }
)
_CV_HEADER_MAX_WORDS = 10
_WRAP_INDENT = 12  # a wrapped bullet line is indented a little; a centred heading far more
_CV_CONTEXT_LINES = 2  # employer and title are enough context for a bullet
_EMPTY_BRACKETS = re.compile(r"\(\s*\)")


_NAME = re.compile(r"[^\W\d_]+(?:[ .'\u2019-][^\W\d_]+){1,3}\.?")


def _looks_like_name(line: str) -> bool:
    """Two to four plain words, no commas or digits: a person's name rather than an employer."""
    return _NAME.fullmatch(line) is not None


def _indent(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _is_section_title(line: str) -> bool:
    plain = _NON_WORD_EDGES.sub("", line).casefold()
    letters = [c for c in line if c.isalpha()]
    shouty = len(letters) >= 3 and all(c.isupper() for c in letters)
    return plain in _CV_SECTION_TITLES or (shouty and len(line.split()) <= 4)


def _is_contact_line(line: str) -> bool:
    return bool(_EMAIL.search(line) or _URL.search(line) or _PHONE.search(line))


def chunk_cv(text: str) -> list[str]:
    """Chunks of a CV: bullets under their employer and title, and paragraphs.

    Contact details are dropped here, so they are neither embedded nor sent to the embedding
    service. Indentation (kept by the text extractor) tells a wrapped bullet line from the next
    heading.
    """
    chunks: list[str] = []
    context: list[str] = []  # heading lines above the bullets being read
    context_used = False
    after_content = False
    body: list[str] = []  # lines of the chunk being built
    body_is_bullet = False
    bullet_indent = 0

    def flush() -> None:
        nonlocal body, body_is_bullet, context_used
        if body:
            piece = " ".join(" ".join(body).split())
            if body_is_bullet and context:
                piece = f"{' | '.join(context)}. {piece}"
                context_used = True
            chunks.extend(_split_long(piece))
        body, body_is_bullet = [], False

    def reset_context() -> None:
        """Start a new heading. One that never had bullets under it (education) is kept as text."""
        nonlocal context, context_used, after_content
        if context and not context_used:
            chunks.append("; ".join(context))
        context, context_used, after_content = [], False, False

    seen_text = False
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or not any(c.isalnum() for c in line):
            flush()
            continue
        if not seen_text:
            seen_text = True
            if _looks_like_name(line):
                continue  # the CV's first line is usually the person's name
        if _is_contact_line(line):
            flush()
            continue
        if _BULLET.match(line):
            flush()
            body, body_is_bullet, bullet_indent = [_clean(line)], True, _indent(raw)
            after_content = True
            continue
        if _is_section_title(line):
            flush()
            reset_context()
            continue
        if body and body_is_bullet and bullet_indent < _indent(raw) <= bullet_indent + _WRAP_INDENT:
            body.append(line)  # a wrapped line of the same bullet
            continue
        in_paragraph = bool(body) and not body_is_bullet
        short = len(line.split()) <= _CV_HEADER_MAX_WORDS and not line.endswith(".")
        if in_paragraph and not _DATE_RANGE.search(line):
            body.append(line)  # the next line of a paragraph, even a short last one
        elif short:
            flush()
            if after_content:
                reset_context()
            heading = _EMPTY_BRACKETS.sub("", _DATE_RANGE.sub("", line)).strip(" |,-\u2013")
            if heading and not heading.endswith(":") and len(context) < _CV_CONTEXT_LINES:
                context.append(heading)
        else:
            flush()
            body, body_is_bullet = [line], False
            after_content = True
    flush()
    reset_context()
    return [c for c in _pack(chunks, MIN_WORDS) if c]
