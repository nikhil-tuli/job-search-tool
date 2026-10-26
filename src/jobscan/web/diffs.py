"""Word-level before and after text with the changes marked, for the review page."""

from __future__ import annotations

import difflib
import re

from markupsafe import Markup, escape

_WORDS = re.compile(r"\s+|[^\s]+")


def word_diff(before: str, after: str) -> tuple[Markup, Markup]:
    """(before, after) as safe HTML: removed words in <del>, added words in <ins>."""
    a, b = _WORDS.findall(before), _WORDS.findall(after)
    left: list[str] = []
    right: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        old, new = "".join(a[i1:i2]), "".join(b[j1:j2])
        if tag == "equal":
            left.append(str(escape(old)))
            right.append(str(escape(new)))
            continue
        if old.strip():
            left.append(f"<del>{escape(old)}</del>")
        else:
            left.append(str(escape(old)))
        if new.strip():
            right.append(f"<ins>{escape(new)}</ins>")
        else:
            right.append(str(escape(new)))
    return Markup("".join(left)), Markup("".join(right))


def moved_items(before: list[str], after: list[str]) -> list[tuple[str, bool]]:
    """Each item of `after` with whether it sits at a different place than before."""
    return [
        (item, index >= len(before) or before[index] != item) for index, item in enumerate(after)
    ]
