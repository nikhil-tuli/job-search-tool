"""What the pages show: values, ratings and wording worked out from what is stored."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import rules as r

SAME_UNDER_POINTS = 5

_DOT = {r.GOOD: "ok", r.NEEDS_WORK: "warn", r.WEAK: "err"}
_BAND_CLASS = {r.STRONG: "ok", r.GOOD: "lok", r.NEEDS_WORK: "warn", r.WEAK: "err"}

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
    if text.startswith("▲"):
        return "ok"
    return "err" if text.startswith("▼") else ""


def answer_band(score: int) -> str:
    return r.band_of(score)


def aspect_label(key: str) -> str:
    return dict(r.ANSWER_ASPECTS).get(key, key)


def minutes_seconds(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}:{whole % 60:02d}"
