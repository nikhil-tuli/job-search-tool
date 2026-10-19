"""Keyword score: how much of a role's vocabulary the CV covers, weighted the BM25 way.

Each distinctive word of the role counts by its IDF (a word found in few job descriptions says more
than "team" or "experience") and by how often the CV uses it, with BM25's saturation so a word
repeated ten times is not worth ten times one mention. Words from requirement chunks count double
(the chunk weights). The result is the weighted share of the role's vocabulary the CV covers, from
0 to 1, and it does not depend on how other roles score.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

K1 = 1.2  # BM25 term-frequency saturation

_TOKEN = re.compile(r"[a-z0-9]+(?:[+#]+)?")
_STOPWORD_TEXT = """
    a ability able about above across additional after all also an and any are as at be been
    being both but by can candidate candidates company could daily day days demonstrated
    description did do does doing duties each environment etc excellent experience
    experienced following for from good great had has have having he her his how i ideal if
    in including into is it its job join key knowledge looking make may more most must need
    needs new no not of on one only or other our out over own per plus position preferred
    required requirements responsibilities responsible role same should skills so some
    strong such support team teams than that the their them then there these they this those
    through to too under understanding up us use used using very via was we well were what
    when where which while who will with within without work working would year years you
    your
"""
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def _stem(word: str) -> str:
    """Light suffix stripping, enough that managers, managed and managing meet."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for suffix in ("ing", "ed"):
        if len(word) > 5 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def terms(text: str) -> list[str]:
    """Searchable words of a text: lower case, no stop words or bare numbers, lightly stemmed."""
    found: list[str] = []
    for token in _TOKEN.findall(text.casefold()):
        if token in _STOPWORDS or token.isdigit() or (len(token) < 2 and token not in {"c", "r"}):
            continue
        found.append(_stem(token))
    return found


class Corpus:
    """Document frequencies over a set of job descriptions, for the IDF part."""

    def __init__(self, documents: Iterable[str]) -> None:
        self.size = 0
        self._df: Counter[str] = Counter()
        for document in documents:
            self.size += 1
            self._df.update(set(terms(document)))

    def idf(self, term: str) -> float:
        """Never negative, so a word in every document counts a little instead of nothing."""
        n = self._df.get(term, 0)
        return math.log(1 + (self.size - n + 0.5) / (n + 0.5))


class CvTerms:
    """Term counts of a CV, built once and reused for every role."""

    def __init__(self, text: str) -> None:
        self.counts: Counter[str] = Counter(terms(text))


def coverage(cv: CvTerms, corpus: Corpus, chunks: Iterable[tuple[float, str]]) -> float:
    """Weighted, IDF-scaled share of the role's words that the CV covers (0 to 1).

    `chunks` are (weight, text) of the role. A word takes the highest weight among the chunks it
    appears in.
    """
    weights: dict[str, float] = {}
    for weight, text in chunks:
        if weight <= 0:
            continue
        for term in set(terms(text)):
            weights[term] = max(weights.get(term, 0.0), weight)
    total = 0.0
    covered = 0.0
    for term, weight in weights.items():
        value = weight * corpus.idf(term)
        total += value
        tf = cv.counts.get(term, 0)
        if tf:
            covered += value * (tf * (K1 + 1) / (tf + K1)) / (K1 + 1)
    return covered / total if total else 0.0
