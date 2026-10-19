"""How saved scores are shown: the 0 to 100 value, its band, and whether it is out of date."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

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

    @property
    def detail(self) -> str:
        text = f"Keywords {self.keyword}, meaning {self.semantic}"
        return f"{text} (out of date: run jobscan score)" if self.stale else text


def score_views(
    store: Store, config: Config, cv: Cv | None, roles: Iterable[Role]
) -> dict[str, ScoreView]:
    """A view for every given role that has a saved score."""
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
