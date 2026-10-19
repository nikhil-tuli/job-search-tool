from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from jobscan import db
from jobscan.config import Config
from jobscan.cv import sha_of
from jobscan.embeddings import EmbedAuthError, EmbedRetry, RateLimiter
from jobscan.models import Cv, JobDetail, NewRole, Role, RoleScore
from jobscan.scoring import (
    TRIGGER_MANUAL,
    TRIGGER_ROLE,
    TRIGGER_SCAN,
    ScoreModel,
    Scorer,
    ScoringUnavailable,
    cv_chunks,
    inputs_fingerprint,
    role_chunks,
    semantic_raw,
)
from jobscan.store import Store
from jobscan.timeutil import IST, to_iso
from tests.fakes import BagEmbedder

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=IST)

CV_TEXT = """\
Acme, Pune
Technical Program Manager
- Led payment gateway migration across engineering teams and vendors
- Drove cloud cost optimization program with platform engineering
- Managed agile delivery with scrum teams and release planning
"""

PAYMENTS_JD = """\
Responsibilities

- Lead payment gateway migration programs with engineering teams
- Drive cloud cost optimization and vendor management for the platform

Qualifications

- Experience with agile delivery, scrum and release planning across teams
"""

WAREHOUSE_JD = """\
Responsibilities

- Supervise forklift drivers and warehouse inventory counts every shift
- Maintain pallet racking safety audits and shipping schedules

Qualifications

- Forklift certification and experience with pallet logistics operations
"""


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


@pytest.fixture
def config() -> Config:
    return Config()


def add_role(
    store: Store,
    job_id: str,
    description: str | None = PAYMENTS_JD,
    *,
    title: str = "Technical Program Manager",
    company: str = "Acme",
    fetched: datetime = NOW - timedelta(hours=2),
) -> Role:
    existing = store.conn.execute("SELECT id FROM runs LIMIT 1").fetchone()
    run = existing[0] if existing else store.start_run(window_seconds=86400, keywords_total=1)
    store.insert_role(
        NewRole(
            portal="LinkedIn",
            job_id=job_id,
            title=title,
            company=company,
            location="Pune",
            link=f"https://www.linkedin.com/jobs/view/{job_id}",
            posted_on=None,
            posted_precision="day",
            fetched_on=to_iso(fetched),
            actively_hiring=False,
            matched_phrase="Technical Program Manager",
            possible_duplicate_of=None,
            dup_key=None,
            raw_datetime=None,
            raw_relative=None,
            run_id=run,
        )
    )
    if description is not None:
        store.save_detail("LinkedIn", job_id, JobDetail(description), when=to_iso(NOW))
    role = store.get_role("LinkedIn", job_id)
    assert role is not None
    return role


def unique_jd(n: int) -> str:
    """A description whose chunks are not shared with any other numbered one."""
    return (
        f"Responsibilities\n\n- Own initiative {n} delivering payment gateway work with teams\n"
        f"- Report weekly on programme {n} to leadership and the steering group\n\n"
        f"Qualifications\n\n- Experience with programme {n} running engineering teams"
    )


def upload_cv(store: Store, text: str = CV_TEXT, name: str = "cv.pdf") -> Cv:
    cv = Cv(name, text, sha_of(text), to_iso(NOW))
    store.save_cv(cv, cv_chunks(text))
    return cv


def make_scorer(
    store: Store,
    config: Config,
    embedder: BagEmbedder,
    *,
    sleeps: list[float] | None = None,
    clock: datetime = NOW,
) -> Scorer:
    log = sleeps if sleeps is not None else []
    return Scorer(
        store=store,
        config=config,
        make_embedder=lambda _config: embedder,
        emit=lambda _line: None,
        sleep=log.append,
        clock=lambda: clock,
    )


def fit_of(store: Store, config: Config, job_id: str) -> int:
    score = store.get_scores()[f"LinkedIn|{job_id}"]
    return ScoreModel.from_config(config).final(score)


# ---- the model ---------------------------------------------------------------------------------


def score(kw: float, sem: float) -> RoleScore:
    return RoleScore("LinkedIn", "1", kw, sem, 5, "x", "t", None)


def test_model_scales_each_part_between_its_floor_and_ceiling() -> None:
    model = ScoreModel.from_config(Config())
    assert model.keyword(score(0.08, 0)) == 0
    assert model.keyword(score(0.14, 0)) == pytest.approx(50)
    assert model.keyword(score(0.9, 0)) == 100
    assert model.semantic(score(0, 0.5)) == 0
    assert model.semantic(score(0, 0.85)) == 100
    assert model.semantic(score(0, 0.1)) == 0


def test_final_is_the_weighted_mean_and_follows_the_weight() -> None:
    config = Config()
    assert ScoreModel.from_config(config).final(score(0.2, 0.5)) == 40  # 100*0.4 + 0*0.6
    keyword_only = replace(config, score_weight_keyword=100)
    assert ScoreModel.from_config(keyword_only).final(score(0.2, 0.5)) == 100
    semantic_only = replace(config, score_weight_keyword=0)
    assert ScoreModel.from_config(semantic_only).final(score(0.2, 0.5)) == 0


def test_bands() -> None:
    model = ScoreModel.from_config(Config())
    assert [model.band(n) for n in (100, 70, 69, 50, 49, 0)] == [
        "strong",
        "strong",
        "partial",
        "partial",
        "weak",
        "weak",
    ]


# ---- chunks and similarity ---------------------------------------------------------------------


def test_role_chunks_drop_company_text_and_carry_weights(config: Config) -> None:
    text = "Responsibilities\n\n- Deliver the programme across three regions.\n\n"
    text += "Qualifications\n\n- Ten years of programme delivery experience overall.\n\n"
    text += "About Acme\n\nAcme is a company that builds things for people."
    chunks = role_chunks(replace(config, score_chunk_words=1), text)
    assert [(c.weight, c.text.split()[0]) for c in chunks] == [(1.0, "Deliver"), (2.0, "Ten")]


def test_role_chunks_are_capped_keeping_the_heaviest(config: Config) -> None:
    bullets = "\n".join(f"- point number {i} that is long enough to stand alone" for i in range(20))
    text = f"Responsibilities\n\n{bullets}\n\nQualifications\n\n{bullets}"
    capped = replace(config, score_chunk_words=1, score_max_chunks=5)
    chunks = role_chunks(capped, text)
    assert len(chunks) == 5
    assert {c.weight for c in chunks} == {2.0}


def test_company_weight_above_zero_brings_company_chunks_back(config: Config) -> None:
    text = "About Acme\n\nAcme is a company that builds things for people."
    assert role_chunks(config, text) == []
    assert len(role_chunks(replace(config, score_weight_company=1.0), text)) == 1


def test_semantic_raw_takes_the_best_cv_match_per_chunk(config: Config) -> None:
    chunks = role_chunks(replace(config, score_chunk_words=1), PAYMENTS_JD)
    one = {c.hash: (1.0, 0.0) for c in chunks}
    cv_vectors = [(1.0, 0.0), (0.0, 1.0)]
    assert semantic_raw(chunks, one, cv_vectors, top_k=1) == pytest.approx(1.0)
    assert semantic_raw(chunks, one, cv_vectors, top_k=2) == pytest.approx(0.5)


def test_semantic_raw_weights_requirements_double() -> None:
    from jobscan.scoring import _RoleChunk

    chunks = [_RoleChunk(2.0, "a", "a"), _RoleChunk(1.0, "b", "b")]
    vectors = {"a": (1.0, 0.0), "b": (0.0, 1.0)}
    assert semantic_raw(chunks, vectors, [(1.0, 0.0)], top_k=1) == pytest.approx(2 / 3)


# ---- running -----------------------------------------------------------------------------------


def test_no_cv_means_scoring_is_unavailable(store: Store, config: Config) -> None:
    with pytest.raises(ScoringUnavailable):
        make_scorer(store, config, BagEmbedder()).run(TRIGGER_MANUAL)
    assert store.list_scoring_runs() == []


def test_a_close_match_outscores_a_distant_one(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1", PAYMENTS_JD)
    add_role(store, "2", WAREHOUSE_JD, title="Technical Program Manager Logistics")
    run = make_scorer(store, config, BagEmbedder()).run(TRIGGER_SCAN)
    scores = store.get_scores()
    assert set(scores) == {"LinkedIn|1", "LinkedIn|2"}
    assert scores["LinkedIn|1"].semantic_raw > scores["LinkedIn|2"].semantic_raw
    assert scores["LinkedIn|1"].keyword_raw > scores["LinkedIn|2"].keyword_raw
    assert fit_of(store, config, "1") > fit_of(store, config, "2")
    assert run.status == "success"
    assert (run.scored, run.failed) == (2, 0)


def test_run_log_counts_everything(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")  # scored
    add_role(store, "2", None)  # no description yet
    add_role(store, "3", company="Amazon")  # excluded: company
    add_role(store, "4", fetched=NOW - timedelta(days=5))  # excluded: recency
    add_role(store, "5", title="Junior Program Manager")  # excluded: seniority
    store.update_status(["LinkedIn|1"], "Not Applied")
    embedder = BagEmbedder(tokens=None)
    run = make_scorer(store, config, embedder).run(TRIGGER_SCAN)
    assert run.trigger == "scan"
    assert run.considered == 5
    assert run.excluded == 3
    assert run.excluded_by_reason == {"company": 1, "recency": 1, "seniority": 1}
    assert run.no_description == 1
    assert run.selected == 1
    assert run.scored == 1
    assert run.strong + run.partial + run.weak == 1
    assert run.texts_embedded == embedder.texts_sent
    assert run.requests == len(embedder.calls)
    assert run.tokens > 0
    assert run.tokens_reported is False  # the fake reports none, so the figure is an estimate
    assert run.finished_on is not None


def test_reported_tokens_are_used_over_estimates(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")
    run = make_scorer(store, config, BagEmbedder(tokens=7)).run(TRIGGER_SCAN)
    assert run.tokens == 7 * run.requests
    assert run.tokens_reported is True


def test_a_second_run_finds_nothing_to_do_and_calls_nothing(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")
    embedder = BagEmbedder()
    scorer = make_scorer(store, config, embedder)
    scorer.run(TRIGGER_SCAN)
    calls = len(embedder.calls)
    again = make_scorer(store, config, embedder).run(TRIGGER_SCAN)
    assert len(embedder.calls) == calls
    assert (again.considered, again.selected, again.note) == (0, 0, "Nothing to score")


def test_identical_text_is_embedded_once(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1", PAYMENTS_JD)
    add_role(store, "2", PAYMENTS_JD)
    embedder = BagEmbedder()
    run = make_scorer(store, config, embedder).run(TRIGGER_SCAN)
    sent = [t for call in embedder.calls for t in call]
    assert len(sent) == len(set(sent))
    assert run.scored == 2
    assert run.texts_embedded == len(sent)


def test_batches_respect_the_size_setting(store: Store, config: Config) -> None:
    upload_cv(store)
    for n in range(6):
        add_role(store, str(n), unique_jd(n))
    embedder = BagEmbedder()
    make_scorer(store, replace(config, embed_batch_size=3), embedder).run(TRIGGER_SCAN)
    assert embedder.calls
    assert max(len(call) for call in embedder.calls) <= 3


def test_changing_the_cv_rescores_with_cached_role_vectors(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")
    embedder = BagEmbedder()
    make_scorer(store, config, embedder).run(TRIGGER_SCAN)
    before = store.get_scores()["LinkedIn|1"]
    sent_before = embedder.texts_sent
    upload_cv(store, CV_TEXT + "- Negotiated payment platform contracts with vendors\n", "v2.pdf")
    run = make_scorer(store, config, embedder).run(TRIGGER_MANUAL)
    after = store.get_scores()["LinkedIn|1"]
    assert run.scored == 1
    assert after.inputs != before.inputs
    assert after.keyword_raw != before.keyword_raw
    new_texts = [t for call in embedder.calls[len(embedder.calls) - run.requests :] for t in call]
    assert embedder.texts_sent - sent_before == len(new_texts)
    assert all("Negotiated" in t for t in new_texts)  # only the new CV bullet was embedded


def test_replacing_the_cv_drops_vectors_of_chunks_that_are_gone(
    store: Store, config: Config
) -> None:
    upload_cv(store)
    add_role(store, "1")
    make_scorer(store, config, BagEmbedder()).run(TRIGGER_SCAN)
    old_hashes = {h for _, h in store.get_cv_chunks()}
    upload_cv(store, "Other Co\nAnalyst\n- Built dashboards for finance reporting and forecasts\n")
    assert store.get_embeddings(old_hashes, config.embed_space) == {}


def test_settings_that_change_the_result_make_scores_stale_without_new_embeddings(
    store: Store, config: Config
) -> None:
    upload_cv(store)
    add_role(store, "1")
    embedder = BagEmbedder()
    make_scorer(store, config, embedder).run(TRIGGER_SCAN)
    sent = embedder.texts_sent
    changed = replace(config, score_weight_requirement=5.0)
    run = make_scorer(store, changed, embedder).run(TRIGGER_MANUAL)
    assert run.scored == 1
    assert embedder.texts_sent == sent  # every vector came from the cache


def test_weights_and_bands_are_not_part_of_the_fingerprint(store: Store, config: Config) -> None:
    cv = upload_cv(store)
    role = add_role(store, "1")
    base = inputs_fingerprint(config, cv, role)
    assert inputs_fingerprint(replace(config, score_weight_keyword=10), cv, role) == base
    assert inputs_fingerprint(replace(config, score_strong_min=90), cv, role) == base
    assert inputs_fingerprint(replace(config, score_top_k=2), cv, role) != base
    assert inputs_fingerprint(replace(config, embed_model="other-model"), cv, role) != base


def test_vectors_of_another_model_are_never_reused(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")
    embedder = BagEmbedder()
    make_scorer(store, config, embedder).run(TRIGGER_SCAN)
    sent = embedder.texts_sent
    other = replace(config, embed_model="gemini-embedding-002")
    make_scorer(store, other, embedder).run(TRIGGER_MANUAL)
    assert embedder.texts_sent == 2 * sent


def test_a_role_scored_on_demand_is_scored_even_if_excluded(store: Store, config: Config) -> None:
    upload_cv(store)
    role = add_role(store, "1", company="Amazon")
    scorer = make_scorer(store, config, BagEmbedder())
    assert scorer.run(TRIGGER_SCAN).scored == 0
    run = make_scorer(store, config, BagEmbedder()).run(TRIGGER_ROLE, only=[role])
    assert (run.trigger, run.scored) == ("role", 1)
    assert "LinkedIn|1" in store.get_scores()


def test_on_demand_scoring_of_a_role_without_description(store: Store, config: Config) -> None:
    upload_cv(store)
    role = add_role(store, "1", None)
    run = make_scorer(store, config, BagEmbedder()).run(TRIGGER_ROLE, only=[role])
    assert (run.scored, run.no_description, run.note) == (0, 1, "Nothing to score")


def test_an_excluded_role_that_already_has_a_score_is_rescored_when_stale(
    store: Store, config: Config
) -> None:
    upload_cv(store)
    role = add_role(store, "1", company="Amazon")
    make_scorer(store, config, BagEmbedder()).run(TRIGGER_ROLE, only=[role])
    upload_cv(store, CV_TEXT + "- Ran vendor negotiations for the finance function\n", "v2.pdf")
    run = make_scorer(store, config, BagEmbedder()).run(TRIGGER_MANUAL)
    assert run.scored == 1


def test_newest_roles_are_scored_first(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "old", fetched=NOW - timedelta(hours=20))
    add_role(
        store, "new", PAYMENTS_JD + "\n- Own stakeholder updates", fetched=NOW - timedelta(hours=1)
    )
    order: list[str] = []
    scorer = make_scorer(store, replace(config, embed_batch_size=1), BagEmbedder())
    real = store.save_score

    def spy(s: RoleScore) -> None:
        order.append(s.job_id)
        real(s)

    store.save_score = spy  # type: ignore[method-assign]
    scorer.run(TRIGGER_SCAN)
    assert order == ["new", "old"]


def test_a_description_with_only_company_text_cannot_be_scored(
    store: Store, config: Config
) -> None:
    upload_cv(store)
    add_role(
        store, "1", "About Acme\n\nAcme is a company that builds things for people everywhere."
    )
    run = make_scorer(store, config, BagEmbedder()).run(TRIGGER_SCAN)
    assert (run.scored, run.failed) == (0, 1)


# ---- failures ----------------------------------------------------------------------------------


def test_a_temporary_failure_is_retried_after_the_wait_the_service_asked_for(
    store: Store, config: Config
) -> None:
    upload_cv(store)
    add_role(store, "1")
    sleeps: list[float] = []
    embedder = BagEmbedder(failures=[EmbedRetry("rate limited", 12.0), EmbedRetry("busy")])
    run = make_scorer(store, config, embedder, sleeps=sleeps).run(TRIGGER_SCAN)
    assert run.status == "success"
    assert sleeps[:2] == [12.0, 10.0]  # then the default backoff: 5s, doubling


def test_retries_give_up_and_leave_the_roles_for_next_time(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")
    embedder = BagEmbedder(failures=[EmbedRetry("busy")] * 10)
    run = make_scorer(store, config, embedder).run(TRIGGER_SCAN)
    assert run.status == "failed"
    assert "still failing" in run.note
    assert store.get_scores() == {}
    again = make_scorer(store, config, BagEmbedder()).run(TRIGGER_SCAN)  # the service is back
    assert again.scored == 1


def test_a_bad_key_ends_the_run_with_a_clear_note(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")
    embedder = BagEmbedder(failures=[EmbedAuthError("no API key; set GEMINI_API_KEY")])
    run = make_scorer(store, config, embedder).run(TRIGGER_SCAN)
    assert run.status == "failed"
    assert "GEMINI_API_KEY" in run.note
    assert store.get_scores() == {}


def test_missing_key_is_reported_when_the_embedder_is_built(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")

    def no_key(_config: Config) -> BagEmbedder:
        raise EmbedAuthError("no API key; set the GEMINI_API_KEY environment variable")

    scorer = Scorer(
        store=store, config=config, make_embedder=no_key, emit=lambda _l: None, clock=lambda: NOW
    )
    run = scorer.run(TRIGGER_SCAN)
    assert run.status == "failed"
    assert "GEMINI_API_KEY" in run.note


def test_a_key_is_not_needed_when_everything_is_cached(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")
    make_scorer(store, config, BagEmbedder()).run(TRIGGER_SCAN)
    upload_cv(store, CV_TEXT, "same-text-again.pdf")  # same text: same fingerprint, nothing to do

    def no_key(_config: Config) -> BagEmbedder:
        raise AssertionError("must not be needed")

    run = Scorer(
        store=store, config=config, make_embedder=no_key, emit=lambda _l: None, clock=lambda: NOW
    ).run(TRIGGER_MANUAL)
    assert run.note == "Nothing to score"


def test_the_daily_limit_stops_the_run_part_way(store: Store, config: Config) -> None:
    upload_cv(store)
    for n in range(4):
        add_role(store, str(n), unique_jd(n))
    limited = replace(config, embed_daily_limit=8, embed_batch_size=3)
    run = make_scorer(store, limited, BagEmbedder()).run(TRIGGER_SCAN)
    assert run.status == "partial"
    assert "daily limit" in run.note
    assert 0 < run.scored < 4
    assert run.texts_embedded <= 8


def test_the_daily_limit_counts_earlier_runs_in_the_last_24_hours(
    store: Store, config: Config
) -> None:
    upload_cv(store)
    add_role(store, "1")
    earlier = store.start_scoring_run("scan", when=to_iso(NOW - timedelta(hours=3)))
    store.update_scoring_run(earlier, {"texts_embedded": 998})
    run = make_scorer(store, replace(config, embed_daily_limit=1000), BagEmbedder()).run(
        TRIGGER_SCAN
    )
    assert run.status == "failed"
    assert "daily limit" in run.note


def test_runs_older_than_24_hours_do_not_count_against_the_daily_limit(
    store: Store, config: Config
) -> None:
    upload_cv(store)
    add_role(store, "1")
    old = store.start_scoring_run("scan", when=to_iso(NOW - timedelta(hours=30)))
    store.update_scoring_run(old, {"texts_embedded": 5000})
    assert make_scorer(store, config, BagEmbedder()).run(TRIGGER_SCAN).status == "success"


def test_interactive_runs_give_up_instead_of_waiting_long(store: Store, config: Config) -> None:
    upload_cv(store)
    role = add_role(store, "1")
    tight = replace(config, embed_texts_per_minute=10, embed_batch_size=10)
    limiter = RateLimiter(10, 25_000, max_wait=5)
    limiter.acquire(10, 10)  # the minute is already used up
    scorer = Scorer(
        store=store,
        config=tight,
        make_embedder=lambda _c: BagEmbedder(),
        emit=lambda _l: None,
        clock=lambda: NOW,
        limiter=limiter,
    )
    run = scorer.run(TRIGGER_ROLE, only=[role])
    assert run.status == "failed"
    assert "try again" in run.note


def test_a_vector_of_the_wrong_size_is_an_error(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")
    run = make_scorer(store, config, BagEmbedder(dims=64)).run(TRIGGER_SCAN)
    assert run.status == "failed"
    assert "expected 768" in run.note


def test_ctrl_c_marks_the_run_failed_and_keeps_earlier_scores(store: Store, config: Config) -> None:
    upload_cv(store)
    add_role(store, "1")
    add_role(store, "2", PAYMENTS_JD + "\n- Own stakeholder updates every week for leaders")
    embedder = BagEmbedder(failures=[])
    scorer = make_scorer(store, replace(config, embed_batch_size=1), embedder)
    real = embedder.embed
    calls = {"n": 0}

    def interrupt(texts):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] > 5:
            raise KeyboardInterrupt
        return real(texts)

    embedder.embed = interrupt  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        scorer.run(TRIGGER_SCAN)
    (run,) = store.list_scoring_runs()
    assert run.status == "failed"
    assert "Interrupted" in run.note


# ---- store pieces ------------------------------------------------------------------------------


def test_save_cv_replaces_the_previous_one(store: Store) -> None:
    upload_cv(store, CV_TEXT, "a.pdf")
    upload_cv(store, "Acme\nManager\n- Did something useful for the business teams\n", "b.pdf")
    cv = store.get_cv()
    assert cv is not None
    assert cv.filename == "b.pdf"
    assert len(store.get_cv_chunks()) == 1
    assert store.conn.execute("SELECT COUNT(*) FROM cv").fetchone()[0] == 1


def test_embedding_cache_round_trip_and_space_separation(store: Store) -> None:
    store.save_embeddings("m@4", {"h1": b"\x00\x00\x80?" * 4})
    assert store.get_embeddings(["h1", "h2"], "m@4") == {"h1": b"\x00\x00\x80?" * 4}
    assert store.get_embeddings(["h1"], "other@4") == {}


def test_embedding_lookup_handles_many_hashes(store: Store) -> None:
    store.save_embeddings("m@4", {f"h{i}": b"1234" for i in range(1200)})
    assert len(store.get_embeddings([f"h{i}" for i in range(1200)], "m@4")) == 1200


def test_scoring_run_updates_reject_unknown_counters(store: Store) -> None:
    run = store.start_scoring_run("scan", when=to_iso(NOW))
    with pytest.raises(Exception, match="unknown scoring counter"):
        store.update_scoring_run(run, {"scored; DROP TABLE roles": 1})


def test_finish_requires_a_final_status(store: Store) -> None:
    run = store.start_scoring_run("scan", when=to_iso(NOW))
    with pytest.raises(Exception, match="invalid scoring run status"):
        store.finish_scoring_run(run, "running", "", when=to_iso(NOW))


def test_stale_running_scoring_runs_are_failed(store: Store) -> None:
    store.start_scoring_run("scan", when=to_iso(NOW))
    assert store.fail_stale_scoring_runs(when=to_iso(NOW)) == 1
    (run,) = store.list_scoring_runs()
    assert run.status == "failed"
    assert store.fail_stale_scoring_runs(when=to_iso(NOW)) == 0


def test_scoring_runs_list_newest_first(store: Store) -> None:
    first = store.start_scoring_run("scan", when=to_iso(NOW - timedelta(hours=2)))
    second = store.start_scoring_run("manual", when=to_iso(NOW))
    assert [r.id for r in store.list_scoring_runs()] == [second, first]
    assert [r.id for r in store.list_scoring_runs(limit=1)] == [second]
