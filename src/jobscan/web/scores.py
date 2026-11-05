"""How saved scores are shown: the 0 to 100 value, its band, and whether it is out of date."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..ai_fit import BAND_RANK, band_of
from ..ai_fit import inputs_fingerprint as ai_fingerprint
from ..ai_fit_store import AiFitRow
from ..config import Config
from ..models import Cv, Role, RoleScore
from ..scoring import ScoreModel, inputs_fingerprint
from ..store import Store


@dataclass(frozen=True)
class ScoreView:
    final: int
    band: str
    keyword: int  # the keyword part, scaled 0 to 100
    semantic: int  # the meaning part, scaled 0 to 100
    chunks: int
    scored_on: str
    stale: bool  # made against an earlier CV or different scoring settings
    ai: bool = False  # an AI verdict: band is good, partial or bad
    reason: str = ""
    checks: tuple[tuple[str, str, str], ...] = ()
    trimmed: bool = False

    @property
    def detail(self) -> str:
        if self.ai:
            return f"{self.reason} (out of date)" if self.stale else self.reason
        text = f"Keywords {self.keyword}, meaning {self.semantic}"
        return f"{text} (out of date: run jobscan score)" if self.stale else text


def score_views(
    store: Store, config: Config, cv: Cv | None, roles: Iterable[Role]
) -> dict[str, ScoreView]:
    """A view for every given role that has a saved score (or AI verdict, unless legacy)."""
    if not config.legacy_scoring:
        return _ai_views(store, config, cv, roles)
    saved = store.get_scores()
    if not saved:
        return {}
    model = ScoreModel.from_config(config)
    views: dict[str, ScoreView] = {}
    for role in roles:
        score = saved.get(role.key)
        if score is not None:
            views[role.key] = _view(model, score, stale=_is_stale(config, cv, role, score))
    return views


def _is_stale(config: Config, cv: Cv | None, role: Role, score: RoleScore) -> bool:
    return cv is None or score.inputs != inputs_fingerprint(config, cv, role)


def _view(model: ScoreModel, score: RoleScore, *, stale: bool) -> ScoreView:
    final = model.final(score)
    return ScoreView(
        final=final,
        band=model.band(final),
        keyword=round(model.keyword(score)),
        semantic=round(model.semantic(score)),
        chunks=score.chunks,
        scored_on=score.scored_on,
        stale=stale,
    )


def _ai_views(
    store: Store, config: Config, cv: Cv | None, roles: Iterable[Role]
) -> dict[str, ScoreView]:
    saved = store.get_ai_fits()
    if not saved:
        return {}
    views: dict[str, ScoreView] = {}
    for role in roles:
        row = saved.get(role.key)
        if row is not None:
            stale = cv is None or row.inputs != ai_fingerprint(config, cv, role)
            views[role.key] = ai_view(config, row, stale=stale)
    return views


def ai_view(config: Config, row: AiFitRow, *, stale: bool) -> ScoreView:
    band = band_of(row.checks, row.role_type_ok, row.domain, config.ai_max_missing)
    return ScoreView(
        final=BAND_RANK[band],
        band=band,
        keyword=0,
        semantic=0,
        chunks=0,
        scored_on=row.scored_on,
        stale=stale,
        ai=True,
        reason=row.reason,
        checks=row.checks,
        trimmed=row.trimmed,
    )
