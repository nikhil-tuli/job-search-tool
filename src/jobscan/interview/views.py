"""What the pages show: values, ratings and wording worked out from what is stored."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import rules as r

SAME_UNDER_POINTS = 5

_DOT = {r.GOOD: "d-g", r.NEEDS_WORK: "d-a", r.WEAK: "d-r"}
_BAND_CLASS = {r.GOOD: "good", r.NEEDS_WORK: "nw", r.WEAK: "weak"}

SPOKEN_LABEL_TIP = (
    "Measured: the app counted these from your transcript. They can be wrong only if the "
    "transcript is wrong."
)
ANSWER_LABEL_TIP = (
    "AI estimate: a model rated your answers against a fixed scale and had to quote your words. "
    "There is no single true score. It does not compare you with other candidates."
)


def band_class(band: str) -> str:
    return _BAND_CLASS.get(band, "")


def value_text(key: str, value: float) -> str:
    if key == "pace":
        return str(round(value))
    if key in ("thinking", "fillers", "hedging"):
        return f"{value:.1f}"
    if key in ("pauses", "length"):
        return str(round(value))
    if key == "ownership":
        return f"{round(value)}%"
    return f"{value:.1f}"


@dataclass(frozen=True)
class SpokenCard:
    key: str
    label: str
    value: str
    unit: str
    rating: str
    dot: str
    benchmark: str
    tip: str
    approximate: bool


def spoken_cards(spoken: dict[str, Any]) -> tuple[list[SpokenCard], int | None]:
    """Cards and the rubric score. Ratings are worked out again from the stored values, so a
    change to the limits in rules.py applies to old interviews too."""
    cards: list[SpokenCard] = []
    points: list[int] = []
    for item in spoken.get("items", []):
        aspect = r.ASPECT_BY_KEY.get(item["key"])
        if aspect is None:
            continue
        value = float(item["value"])
        rating = aspect.rate(value)
        points.append(r.POINTS[rating])
        cards.append(
            SpokenCard(
                key=aspect.key,
                label=aspect.label,
                value=value_text(aspect.key, value),
                unit=aspect.unit,
                rating=rating,
                dot=_DOT[rating],
                benchmark=aspect.benchmark,
                tip="" if rating == r.GOOD else aspect.tip,
                approximate=bool(item.get("approximate")),
            )
        )
    return cards, r.average_score(points)


def change_text(current: int | None, earlier: int | None) -> str:
    """ "▲ 3", "▼ 2" or "about the same". Empty when there is nothing to compare."""
    if current is None or earlier is None:
        return ""
    diff = current - earlier
    if abs(diff) < SAME_UNDER_POINTS:
        return "about the same"
    return f"▲ {diff}" if diff > 0 else f"▼ {-diff}"


def change_class(text: str) -> str:
    """ "" for up (green), "dn" for down (red) and "same" for no real change."""
    if text.startswith("▲"):
        return ""
    return "dn" if text.startswith("▼") else "same"


def answer_band(score: int) -> str:
    return r.band_of(score)


def aspect_label(key: str) -> str:
    return dict(r.ANSWER_ASPECTS).get(key, key)


def minutes_seconds(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


@dataclass(frozen=True)
class Example:
    rubric: str  # "spoken" or "answer"
    label: str
    weak: str
    strong: str
    why: str


# Fixed text, written once (requirements section 7.3). Not produced by a model.
EXAMPLES: tuple[Example, ...] = (
    Example(
        "spoken",
        "Pace",
        "Rushed at 190 words a minute, no breaks.",
        "About 140 words a minute, with pauses at key points.",
        "Listeners need time to take in a number or a name.",
    ),
    Example(
        "spoken",
        "Thinking time",
        "\u201cUmm, so, let me, yeah\u201d for 8 seconds before the first real word.",
        "A 2 second pause, then a one-line headline.",
        "A short silent pause sounds calm. Filling it with noise sounds unsure.",
    ),
    Example(
        "spoken",
        "Filler words",
        "\u201cSo basically I like managed the, you know, team.\u201d",
        "\u201cI managed a team of 12.\u201d",
        "Fillers hide the point and make the answer longer.",
    ),
    Example(
        "spoken",
        "Hedging",
        "\u201cI think I maybe led it.\u201d",
        "\u201cI led it.\u201d",
        "Hedges make a clear fact sound doubtful.",
    ),
    Example(
        "spoken",
        "Ownership (I vs we)",
        "\u201cWe delivered the launch\u201d for every step.",
        "\u201cI wrote the brief. We shipped in May.\u201d",
        "The interviewer is hiring you. They need to hear what you did.",
    ),
    Example(
        "answer",
        "Structure",
        "The result comes at the end of a long story.",
        "\u201cI cut checkout failures by 30%. Here is how.\u201d",
        "Leading with the result keeps the listener with you.",
    ),
    Example(
        "answer",
        "Specificity",
        "\u201cI improved processes.\u201d",
        "\u201cI moved 14 lender integrations onto one API.\u201d",
        "A named thing and a number can be checked. A general claim cannot.",
    ),
)


def nice_date(iso: str) -> str:
    """ "3 Nov 2026"."""
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.day} {d:%b %Y}"


def nice_datetime(iso: str) -> str:
    """ "11 Nov, 5:20 pm"."""
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    hour = d.hour % 12 or 12
    return f"{d.day} {d:%b}, {hour}:{d:%M} {'am' if d.hour < 12 else 'pm'}"


def nice_full(iso: str) -> str:
    """ "11 Nov 2026, 5:20 pm"."""
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    hour = d.hour % 12 or 12
    return f"{d.day} {d:%b %Y}, {hour}:{d:%M} {'am' if d.hour < 12 else 'pm'}"
