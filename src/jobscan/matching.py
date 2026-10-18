"""Title matching and duplicate-key normalisation."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

MATCH_MODES = ("contains", "exact")

_NON_WORD_RE = re.compile(r"[\W_]+")
_SR_RE = re.compile(r"\bsr\b")

_COMPANY_SUFFIXES = frozenset(
    {"inc", "ltd", "limited", "pvt", "private", "llc", "llp", "corp", "corporation", "co"}
)
_CITY_ALIASES = {
    "bangalore": "bengaluru",
    "gurgaon": "gurugram",
    "bombay": "mumbai",
    "madras": "chennai",
    "calcutta": "kolkata",
}


def normalize_text(value: str) -> str:
    """Lowercase, drop punctuation, collapse spaces, and read 'Sr' as 'Senior'."""
    folded = unicodedata.normalize("NFKC", value).lower()
    spaced = _NON_WORD_RE.sub(" ", folded)
    return " ".join(_SR_RE.sub("senior", spaced).split())


class TitleMatcher:
    """Matches job titles against configured phrases as whole-word phrases.

    'contains' keeps a title that includes a phrase; 'exact' requires the whole normalised
    title to equal a phrase. The longest matching phrase is reported.
    """

    def __init__(self, phrases: Iterable[str], mode: str = "contains") -> None:
        if mode not in MATCH_MODES:
            raise ValueError(f"unknown match mode: {mode!r}")
        self._mode = mode
        pairs = [(normalize_text(p), p) for p in phrases]
        # Longest first so the most specific phrase wins; stable for equal lengths.
        self._phrases = sorted(
            ((norm, raw) for norm, raw in pairs if norm), key=lambda pair: -len(pair[0])
        )

    def match(self, title: str) -> str | None:
        normalized = normalize_text(title)
        if not normalized:
            return None
        padded = f" {normalized} "
        for norm, raw in self._phrases:
            if self._mode == "exact":
                if normalized == norm:
                    return raw
            elif f" {norm} " in padded:
                return raw
        return None


def normalize_company(company: str) -> str:
    tokens = normalize_text(company).split()
    stripped = list(tokens)
    while stripped and stripped[-1] in _COMPANY_SUFFIXES:
        stripped.pop()
    return " ".join(stripped or tokens)


def normalize_city(location: str) -> str:
    first_segment = location.split(",")[0]
    city = normalize_text(first_segment)
    return _CITY_ALIASES.get(city, city)


def dup_key(company: str, title: str, location: str) -> str | None:
    """Key for spotting the same role reposted under a new ID. None if any part is empty."""
    parts = (normalize_company(company), normalize_text(title), normalize_city(location))
    if not all(parts):
        return None
    return "|".join(parts)
