"""Scores roles against the CV: keyword coverage plus embedding similarity.

Scoring saves only raw values. Weights, floors and bands from the Config page apply when a score
is read, so changing them needs no new run, the same as the fit filter. Embeddings are cached by the
hash of their text, so re-scoring after a settings change or a CV edit only pays for new text.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .chunking import (
    CHUNKER_VERSION,
    KIND_COMPANY,
    KIND_REQUIREMENT,
    KIND_RESPONSIBILITY,
    chunk_cv,
    chunk_description,
)
from .config import Config
from .cv import sha_of as text_hash
from .embeddings import (
    API_KEY_ENV,
    EmbedBatch,
    Embedder,
    EmbedError,
    EmbedRetry,
    GeminiEmbedder,
    RateLimited,
    RateLimiter,
    dot,
    estimate_tokens,
    normalise,
    pack,
    unpack,
)
from .fit_filters import FitRules, evaluate
from .keywords import Corpus, CvTerms, coverage
from .models import Cv, Role, RoleScore, ScoringRun
from .store import Store
from .timeutil import now_ist, to_iso

TRIGGER_SCAN = "scan"
TRIGGER_MANUAL = "manual"
TRIGGER_ROLE = "role"

BAND_STRONG = "strong"
BAND_PARTIAL = "partial"
BAND_WEAK = "weak"

RETRY_ATTEMPTS = 4
RETRY_BASE_SECONDS = 5.0
RETRY_MAX_SECONDS = 90.0
DAILY_WINDOW = timedelta(hours=24)


class ScoringUnavailable(RuntimeError):
    """Scoring cannot run at all (for example, no CV has been uploaded)."""


class _StopRun(Exception):
    """The embedding service cannot be used any more in this run; the rest waits."""


@dataclass(frozen=True)
class ScoreModel:
    """How a saved raw score reads as 0 to 100 and a band. Applied on read, never stored."""

    weight_keyword: int
    kw_floor: float
    kw_ceiling: float
    sem_floor: float
    sem_ceiling: float
    strong_min: int
    partial_min: int

    @classmethod
    def from_config(cls, config: Config) -> ScoreModel:
        return cls(
            weight_keyword=config.score_weight_keyword,
            kw_floor=config.score_kw_floor,
            kw_ceiling=config.score_kw_ceiling,
            sem_floor=config.score_sem_floor,
            sem_ceiling=config.score_sem_ceiling,
            strong_min=config.score_strong_min,
            partial_min=config.score_partial_min,
        )

    @staticmethod
    def _scale(raw: float, floor: float, ceiling: float) -> float:
        return min(max((raw - floor) / (ceiling - floor), 0.0), 1.0) * 100

    def keyword(self, score: RoleScore) -> float:
        return self._scale(score.keyword_raw, self.kw_floor, self.kw_ceiling)

    def semantic(self, score: RoleScore) -> float:
        return self._scale(score.semantic_raw, self.sem_floor, self.sem_ceiling)

    def final(self, score: RoleScore) -> int:
        share = self.weight_keyword / 100
        return round(share * self.keyword(score) + (1 - share) * self.semantic(score))

    def band(self, final: int) -> str:
        if final >= self.strong_min:
            return BAND_STRONG
        return BAND_PARTIAL if final >= self.partial_min else BAND_WEAK


def cv_chunks(text: str) -> list[tuple[str, str]]:
    """The CV's (text, hash) chunks, as stored."""
    return [(chunk, text_hash(chunk)) for chunk in chunk_cv(text)]


def inputs_fingerprint(config: Config, cv: Cv, role: Role) -> str:
    """Names everything a role's saved score depends on. A mismatch means the score is stale."""
    payload = [
        CHUNKER_VERSION,
        cv.sha,
        config.embed_space,
        config.score_weight_requirement,
        config.score_weight_responsibility,
        config.score_weight_company,
        config.score_top_k,
        config.score_max_chunks,
        config.score_chunk_words,
        role.description or "",
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


@dataclass(frozen=True)
class _RoleChunk:
    weight: float
    text: str
    hash: str


def role_chunks(config: Config, description: str) -> list[_RoleChunk]:
    """Weighted chunks of a description. Zero-weight chunks are dropped, and the count capped
    with the heaviest chunks kept first."""
    weights = {
        KIND_REQUIREMENT: config.score_weight_requirement,
        KIND_RESPONSIBILITY: config.score_weight_responsibility,
        KIND_COMPANY: config.score_weight_company,
    }
    chunks = [
        _RoleChunk(weights.get(c.kind, 1.0), c.text, text_hash(c.text))
        for c in chunk_description(description, config.score_chunk_words)
    ]
    chunks = [c for c in chunks if c.weight > 0]
    if len(chunks) > config.score_max_chunks:
        keep = sorted(range(len(chunks)), key=lambda i: (-chunks[i].weight, i))
        chunks = [chunks[i] for i in sorted(keep[: config.score_max_chunks])]
    return chunks


def semantic_raw(
    chunks: Sequence[_RoleChunk],
    vectors: dict[str, tuple[float, ...]],
    cv_vectors: Sequence[tuple[float, ...]],
    top_k: int,
) -> float:
    """Weighted mean, over the role's chunks, of the similarity to the closest CV chunk(s)."""
    total = 0.0
    weight_sum = 0.0
    for chunk in chunks:
        vector = vectors[chunk.hash]
        sims = sorted((dot(vector, cv) for cv in cv_vectors), reverse=True)[:top_k]
        total += chunk.weight * (sum(sims) / len(sims))
        weight_sum += chunk.weight
    return total / weight_sum


def gemini_from_env(config: Config, env: dict[str, str] | None = None) -> Embedder:
    key = (os.environ if env is None else env).get(API_KEY_ENV, "")
    return GeminiEmbedder(key, model=config.embed_model, dimensions=config.embed_dims)


@dataclass
class ScoringPlan:
    """What a run would do, worked out before any network call."""

    considered: int = 0  # roles lacking a current score
    excluded: Counter[str] = field(default_factory=Counter)  # by the first reason that applies
    no_description: int = 0
    selected: list[Role] = field(default_factory=list)
    all_roles: list[Role] = field(default_factory=list)

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())


def plan_scoring(
    store: Store,
    config: Config,
    cv: Cv,
    now: datetime,
    *,
    only: Sequence[Role] | None = None,
) -> ScoringPlan:
    """Which roles need scoring: those without a current score that pass the fit rules.

    A role that already has a stale score is scored again whatever the rules say, since it was
    scored on purpose before. With `only`, exactly those roles are scored.
    """
    plan = ScoringPlan(all_roles=store.list_roles())  # newest first
    if only is not None:
        for role in only:
            plan.considered += 1
            if role.description:
                plan.selected.append(role)
            else:
                plan.no_description += 1
        return plan
    scores = store.get_scores()
    rules = FitRules.from_config(config)
    for role in plan.all_roles:  # newest first, so a partial run covers the freshest
        current = scores.get(role.key)
        if current and current.inputs == inputs_fingerprint(config, cv, role):
            continue
        plan.considered += 1
        if current is None:
            fit = evaluate(role, rules, now)
            if not fit.eligible:
                plan.excluded[fit.reasons[0].kind] += 1
                continue
        if role.description:
            plan.selected.append(role)
        else:
            plan.no_description += 1
    return plan


class Scorer:
    def __init__(
        self,
        *,
        store: Store,
        config: Config,
        make_embedder: Callable[[Config], Embedder] = gemini_from_env,
        emit: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = now_ist,
        limiter: RateLimiter | None = None,
        max_wait: float | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._make_embedder = make_embedder
        self._embedder: Embedder | None = None
        self._emit = emit
        self._sleep = sleep
        self._clock = clock
        self._limiter = limiter or RateLimiter(
            config.embed_texts_per_minute,
            config.embed_tokens_per_minute,
            sleep=sleep,
            max_wait=max_wait,
        )
        self._model = ScoreModel.from_config(config)
        self._run_id = 0
        self._counters: Counter[str] = Counter()
        self._all_tokens_reported = True

    # ---- public -------------------------------------------------------------------------

    def run(
        self, trigger: str, *, only: Sequence[Role] | None = None, source: str = ""
    ) -> ScoringRun:
        """Score what needs scoring: every role without a current score, or just `only`.

        Raises ScoringUnavailable when there is no CV. Any other problem ends the run with a
        status and a note instead, and the roles left over are picked up by the next run.
        """
        cv = self._store.get_cv()
        if cv is None:
            raise ScoringUnavailable("no CV uploaded yet")
        self._run_id = self._store.start_scoring_run(
            trigger, when=to_iso(self._clock()), source=source
        )
        status, note = "failed", "Interrupted"
        try:
            status, note = self._run(cv, only)
        except KeyboardInterrupt:
            note = "Interrupted by the user"
            self._emit("Scoring interrupted; roles scored so far are kept.")
            raise
        except BaseException as exc:
            note = f"Unexpected error: {type(exc).__name__}"
            raise
        finally:
            self._store.update_scoring_run(self._run_id, dict(self._counters))
            self._store.finish_scoring_run(self._run_id, status, note, when=to_iso(self._clock()))
        run = self._store.get_scoring_run(self._run_id)
        assert run is not None
        return run

    # ---- the run ------------------------------------------------------------------------

    def _run(self, cv: Cv, only: Sequence[Role] | None) -> tuple[str, str]:
        plan = plan_scoring(self._store, self._config, cv, self._clock(), only=only)
        self._counters.update(
            considered=plan.considered,
            excluded=plan.excluded_total,
            no_description=plan.no_description,
            selected=len(plan.selected),
        )
        self._store.update_scoring_run(
            self._run_id, dict(self._counters), excluded_by_reason=plan.excluded
        )
        self._emit(
            f"Scoring: {plan.considered} roles lacking a current score, "
            f"{plan.excluded_total} excluded by rule, {plan.no_description} without a "
            f"description, {len(plan.selected)} to score"
        )
        if not plan.selected:
            return "success", "Nothing to score"
        try:
            self._score_all(cv, plan)
        except _StopRun as stop:
            note = str(stop)
            self._emit(f"Scoring stopped: {note}")
            return ("partial" if self._counters["scored"] else "failed"), note
        return "success", ""

    def _score_all(self, cv: Cv, plan: ScoringPlan) -> None:
        config = self._config
        chunks = self._store.get_cv_chunks()
        cv_hashes = [h for _, h in chunks]
        self._ensure({h: t for t, h in chunks})
        cv_vectors = [
            unpack(blob)
            for blob in self._store.get_embeddings(cv_hashes, config.embed_space).values()
        ]
        if not cv_vectors:
            raise _StopRun("the CV has no text to compare against")
        cv_terms = CvTerms(cv.text)
        corpus = Corpus(r.description for r in plan.all_roles if r.description)
        scored_at = to_iso(self._clock())

        planned = [(role, role_chunks(config, role.description or "")) for role in plan.selected]
        wave: list[tuple[Role, list[_RoleChunk]]] = []
        missing: dict[str, str] = {}
        for role, chunks_of_role in planned:
            wave.append((role, chunks_of_role))
            missing.update({c.hash: c.text for c in chunks_of_role})
            if len(missing) >= config.embed_batch_size:
                self._finish_wave(cv, wave, missing, cv_vectors, cv_terms, corpus, scored_at)
                wave, missing = [], {}
        if wave:
            self._finish_wave(cv, wave, missing, cv_vectors, cv_terms, corpus, scored_at)

    def _finish_wave(
        self,
        cv: Cv,
        wave: list[tuple[Role, list[_RoleChunk]]],
        texts: dict[str, str],
        cv_vectors: Sequence[tuple[float, ...]],
        cv_terms: CvTerms,
        corpus: Corpus,
        scored_at: str,
    ) -> None:
        self._ensure(texts)
        blobs = self._store.get_embeddings(texts, self._config.embed_space)
        vectors = {h: unpack(blob) for h, blob in blobs.items()}
        with self._store.transaction():
            for role, chunks in wave:
                if not chunks:  # a description with nothing to compare, such as only company text
                    self._counters["failed"] += 1
                    continue
                score = RoleScore(
                    portal=role.portal,
                    job_id=role.job_id,
                    keyword_raw=coverage(cv_terms, corpus, [(c.weight, c.text) for c in chunks]),
                    semantic_raw=semantic_raw(
                        chunks, vectors, cv_vectors, self._config.score_top_k
                    ),
                    chunks=len(chunks),
                    inputs=inputs_fingerprint(self._config, cv, role),
                    scored_on=scored_at,
                    run_id=self._run_id,
                )
                self._store.save_score(score)
                self._counters["scored"] += 1
                self._counters[self._model.band(self._model.final(score))] += 1
        self._store.update_scoring_run(self._run_id, dict(self._counters))
        self._emit(f"Scored {self._counters['scored']} of {self._counters['selected']} roles")

    # ---- embeddings ---------------------------------------------------------------------

    def _ensure(self, texts: dict[str, str]) -> None:
        """Make sure every text has a stored vector, embedding only those that lack one."""
        space = self._config.embed_space
        have = self._store.get_embeddings(texts, space)
        self._counters["texts_cached"] += len(have)
        todo = [(h, t) for h, t in texts.items() if h not in have]
        for batch in self._batches(todo):
            self._embed_batch(batch, space)

    def _batches(self, items: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
        """Batches under the size limit and under the per-minute token limit."""
        config = self._config
        batches: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        tokens = 0
        for item in items:
            cost = estimate_tokens(item[1])
            if current and (
                len(current) >= config.embed_batch_size
                or tokens + cost > config.embed_tokens_per_minute
            ):
                batches.append(current)
                current, tokens = [], 0
            current.append(item)
            tokens += cost
        if current:
            batches.append(current)
        return batches

    def _embed_batch(self, batch: list[tuple[str, str]], space: str) -> None:
        config = self._config
        estimate = sum(estimate_tokens(text) for _, text in batch)
        since = to_iso(self._clock() - DAILY_WINDOW)
        used = self._store.texts_embedded_since(since)
        if used + len(batch) > config.embed_daily_limit:
            raise _StopRun(
                f"daily limit of {config.embed_daily_limit} texts reached; "
                "the rest is scored on the next run"
            )
        try:
            self._limiter.acquire(len(batch), estimate)
            result = self._call_with_retries([text for _, text in batch])
        except RateLimited as exc:
            raise _StopRun(f"{exc}; try again in a minute") from None
        except EmbedError as exc:
            raise _StopRun(f"embedding failed: {exc}") from None
        if len(result.vectors[0]) != config.embed_dims:
            raise _StopRun(
                f"the service returned {len(result.vectors[0])} numbers per vector, "
                f"expected {config.embed_dims}"
            )
        self._store.save_embeddings(
            space, {h: pack(normalise(v)) for (h, _), v in zip(batch, result.vectors, strict=True)}
        )
        self._counters["texts_embedded"] += len(batch)
        self._counters["requests"] += 1
        if result.tokens is None:
            self._all_tokens_reported = False
            self._counters["tokens"] += estimate
        else:
            self._counters["tokens"] += result.tokens
        self._counters["tokens_reported"] = int(self._all_tokens_reported)
        self._store.update_scoring_run(self._run_id, dict(self._counters))

    def _call_with_retries(self, texts: list[str]) -> EmbedBatch:
        if self._embedder is None:
            self._embedder = self._make_embedder(self._config)
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return self._embedder.embed(texts)
            except EmbedRetry as exc:
                if attempt == RETRY_ATTEMPTS - 1:
                    raise EmbedError(f"still failing after {RETRY_ATTEMPTS} tries: {exc}") from None
                wait = min(exc.retry_after or RETRY_BASE_SECONDS * 2**attempt, RETRY_MAX_SECONDS)
                self._emit(f"Embedding service said: {exc}. Waiting {wait:.0f}s")
                self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover
