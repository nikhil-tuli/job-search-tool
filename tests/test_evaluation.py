from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

from jobscan import db
from jobscan.config import Config
from jobscan.cv import sha_of
from jobscan.evaluation import (
    Labelled,
    auc,
    build_report,
    load_labelled,
    phrase_yield,
    roles_to_score,
    suggest_deny_words,
    suggest_excluded_industries,
)
from jobscan.models import (
    BAD_STATUSES,
    GOOD_STATUSES,
    LABEL_BAD,
    LABEL_GOOD,
    LABEL_STATUSES,
    STATUSES,
    Cv,
    JobDetail,
    NewRole,
    Role,
    RoleScore,
)
from jobscan.scoring import cv_chunks, inputs_fingerprint
from jobscan.store import Store, StoreError
from jobscan.timeutil import now_ist, to_iso

CV_TEXT = "Acme\n- Led payment gateway migration across engineering teams\n"


def make_role(
    job_id: str,
    *,
    title: str = "Technical Program Manager",
    status: str = "Not Applied",
    rating: str | None = None,
    phrase: str = "Technical Program Manager",
    description: str | None = "Lead programs",
) -> Role:
    return Role(
        portal="LinkedIn",
        job_id=job_id,
        title=title,
        company="Acme",
        location="Pune",
        link=f"https://www.linkedin.com/jobs/view/{job_id}",
        posted_on=None,
        posted_precision="day",
        fetched_on="2026-10-19T10:00:00+05:30",
        actively_hiring=False,
        matched_phrase=phrase,
        possible_duplicate_of=None,
        status=status,
        applied_on=None,
        contact_person="",
        comments="",
        raw_datetime=None,
        raw_relative=None,
        first_run_id=None,
        description=description,
        fit_rating=rating,
    )


def make_score(role: Role, keyword: float, semantic: float) -> RoleScore:
    return RoleScore(
        "LinkedIn", role.job_id, keyword, semantic, 5, "x", "2026-10-19T10:00:00+05:30", None
    )


def item(
    role: Role, keyword: float = 0.14, semantic: float = 0.675, *, scored: bool = True
) -> Labelled:
    label = role.label
    assert label is not None
    return Labelled(role, label, make_score(role, keyword, semantic) if scored else None)


# ---- labels --------------------------------------------------------------------------------


@pytest.mark.parametrize("status", GOOD_STATUSES)
def test_actioned_statuses_are_good(status: str) -> None:
    assert make_role("1", status=status).label == LABEL_GOOD


def test_not_relevant_is_bad() -> None:
    assert make_role("1", status="Not relevant").label == LABEL_BAD


@pytest.mark.parametrize("status", ["Not Applied", "Closed", "Skipped"])
def test_other_statuses_say_nothing(status: str) -> None:
    assert make_role("1", status=status).label is None


def test_rating_overrides_status() -> None:
    assert make_role("1", status="Applied", rating="bad").label == LABEL_BAD
    assert make_role("1", status="Not relevant", rating="good").label == LABEL_GOOD
    assert make_role("1", status="Closed", rating="good").label == LABEL_GOOD


def test_every_label_status_is_a_real_status() -> None:
    assert set(LABEL_STATUSES) <= set(STATUSES)
    assert not set(GOOD_STATUSES) & set(BAD_STATUSES)


# ---- AUC -----------------------------------------------------------------------------------


def test_auc_perfect_reversed_and_tied() -> None:
    assert auc([80, 90], [10, 20]) == 1.0
    assert auc([10, 20], [80, 90]) == 0.0
    assert auc([50, 50], [50, 50]) == 0.5
    assert auc([60], [50, 70]) == 0.5


def test_auc_needs_both_kinds() -> None:
    assert auc([], [1]) is None
    assert auc([1], []) is None


# ---- the report ----------------------------------------------------------------------------

GOOD_HIGH = (0.19, 0.84)  # scores near 100 on both parts
BAD_LOW = (0.09, 0.52)  # scores near 0 on both parts


def separable(good: int = 6, bad: int = 6) -> list[Labelled]:
    found = [item(make_role(f"g{i}", status="Applied"), *GOOD_HIGH) for i in range(good)]
    found += [item(make_role(f"b{i}", status="Not relevant"), *BAD_LOW) for i in range(bad)]
    return found


def test_perfectly_separated_roles_score_perfectly() -> None:
    report = build_report(separable(), Config(), [])
    assert (report.good, report.bad, report.scored) == (6, 6, 12)
    assert report.measurable
    assert [v.auc for v in report.variants] == [1.0, 1.0, 1.0]
    assert report.variants[2].top_hits == 6
    assert report.variants[2].mean_good > 90 > 10 > report.variants[2].mean_bad
    assert report.cutoff is not None
    assert (report.cutoff.caught_good, report.cutoff.let_through_bad) == (1.0, 0.0)
    assert report.top_n == 10
    assert report.random_hits == 5.0


def test_variants_separate_the_two_signals() -> None:
    # Keywords point the right way and meaning the wrong way.
    found = [item(make_role(f"g{i}", status="Applied"), 0.19, 0.52) for i in range(3)]
    found += [item(make_role(f"b{i}", status="Not relevant"), 0.09, 0.84) for i in range(3)]
    report = build_report(found, Config(), [])
    keywords, meaning, mix = report.variants
    assert keywords.auc == 1.0
    assert meaning.auc == 0.0
    assert mix.auc == 0.0  # at 40% keywords, meaning still wins
    assert report.best_weight == 60  # the lowest keyword share that separates them perfectly
    assert dict(report.sweep)[100] == 1.0
    assert dict(report.sweep)[0] == 0.0


def test_best_weight_prefers_the_current_one_on_a_tie() -> None:
    report = build_report(separable(), Config(score_weight_keyword=30), [])
    assert report.best_weight == 30


def test_ties_do_not_flatter_the_top_n() -> None:
    flat = [item(make_role(f"g{i}", status="Applied"), 0.14, 0.675) for i in range(5)]
    flat += [item(make_role(f"b{i}", status="Not relevant"), 0.14, 0.675) for i in range(5)]
    report = build_report(flat, Config(), [])
    assert report.variants[2].top_hits == 5  # all tied: no better than random
    assert report.variants[2].auc == 0.5


def test_top_n_is_capped_by_the_number_scored() -> None:
    report = build_report(separable(2, 1), Config(), [])
    assert report.top_n == 3


def test_misses_are_the_worst_of_each_kind() -> None:
    found = separable(6, 6)
    found.append(item(make_role("lowgood", status="Applied", title="Odd Role"), *BAD_LOW))
    found.append(item(make_role("highbad", status="Not relevant", title="Fake Hit"), *GOOD_HIGH))
    report = build_report(found, Config(), [])
    assert report.missed_good[0].role.job_id == "lowgood"
    assert report.false_good[0].role.job_id == "highbad"
    assert len(report.missed_good) == 5
    assert all(m.label == LABEL_GOOD for m in report.missed_good)
    assert all(m.label == LABEL_BAD for m in report.false_good)


def test_unscored_roles_are_counted_but_not_measured() -> None:
    found = separable(5, 5)
    found.append(item(make_role("nodesc", status="Applied", description=None), scored=False))
    found.append(item(make_role("todo", status="Not relevant"), scored=False))
    report = build_report(found, Config(), [])
    assert (report.good, report.bad) == (6, 6)
    assert (report.scored_good, report.scored_bad) == (5, 5)
    assert report.no_description == 1
    assert report.unscored == 1
    assert [r.job_id for r in roles_to_score(found)] == ["todo"]


def test_not_measurable_without_both_kinds() -> None:
    only_good = build_report(separable(3, 0), Config(), [])
    assert not only_good.measurable
    assert only_good.cutoff is None
    assert all(v.auc is None for v in only_good.variants)
    assert build_report([], Config(), []).measurable is False


def test_reliability_needs_enough_roles_of_each_kind() -> None:
    assert not build_report(separable(5, 5), Config(), []).reliable
    assert not build_report(separable(15, 4), Config(), []).reliable
    assert build_report(separable(10, 10), Config(), []).reliable


# ---- search phrases and deny-word ideas ----------------------------------------------------


def test_phrase_yield_lists_the_noisiest_phrase_first() -> None:
    found = [
        item(make_role("1", status="Applied", phrase="TPM")),
        item(make_role("2", status="Not relevant", phrase="Delivery Manager")),
        item(make_role("3", status="Not relevant", phrase="Delivery Manager")),
        item(make_role("4", status="Applied", phrase="Delivery Manager")),
    ]
    rows = phrase_yield(found)
    assert [(r.phrase, r.good, r.bad) for r in rows] == [
        ("Delivery Manager", 1, 2),
        ("TPM", 1, 0),
    ]


def bad_titles(*titles: str) -> list[Labelled]:
    return [item(make_role(f"b{i}", title=t, status="Not relevant")) for i, t in enumerate(titles)]


def test_suggests_words_in_two_bad_titles_and_no_good_one() -> None:
    found = bad_titles("Construction Project Manager", "Senior Construction Lead", "Vehicle Lead")
    found.append(item(make_role("g", title="Technical Program Manager", status="Applied")))
    words = {s.word: s.bad_roles for s in suggest_deny_words(found, Config(), [])}
    assert words == {"construction": 2, "lead": 2}


def test_a_word_in_a_good_title_is_never_suggested() -> None:
    found = bad_titles("Construction Manager", "Construction Lead")
    found.append(
        item(make_role("g", title="Construction Technology Program Manager", status="Applied"))
    )
    assert suggest_deny_words(found, Config(), []) == ()


def test_search_phrase_words_and_existing_deny_words_are_skipped() -> None:
    found = bad_titles(
        "Delivery Manager Sales",
        "Delivery Manager Sales",
        "Delivery Manager Staffing",
        "Staffing Manager",
    )
    config = Config(fit_deny_keywords="sales")
    suggestions = suggest_deny_words(found, config, ["Delivery Manager"])
    assert [s.word for s in suggestions] == ["staffing"]


def test_suggestions_ignore_short_words_and_are_capped() -> None:
    found = bad_titles(*["Zz Alpha Beta"] * 2)
    assert [s.word for s in suggest_deny_words(found, Config(), [])] == ["alpha", "beta"]
    many = bad_titles(*["a1x b1x c1x d1x e1x f1x g1x h1x i1x j1x k1x l1x"] * 2)
    assert len(suggest_deny_words(many, Config(), [])) == 10


# ---- loading from the store ----------------------------------------------------------------


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


def add(
    store: Store, job_id: str, *, status: str = "Not Applied", description: str | None = "x"
) -> None:
    run = store.conn.execute("SELECT id FROM runs LIMIT 1").fetchone()
    run_id = run[0] if run else store.start_run(window_seconds=86400, keywords_total=1)
    store.insert_role(
        NewRole(
            portal="LinkedIn",
            job_id=job_id,
            title=f"Role {job_id}",
            company="Acme",
            location="Pune",
            link=f"https://www.linkedin.com/jobs/view/{job_id}",
            posted_on=None,
            posted_precision="day",
            fetched_on=to_iso(now_ist()),
            actively_hiring=False,
            matched_phrase="Technical Program Manager",
            possible_duplicate_of=None,
            dup_key=None,
            raw_datetime=None,
            raw_relative=None,
            run_id=run_id,
        )
    )
    if description is not None:
        store.save_detail("LinkedIn", job_id, JobDetail(description), when=to_iso(now_ist()))
    if status != "Not Applied":
        store.update_status([f"LinkedIn|{job_id}"], status)


def test_rating_round_trip_and_validation(store: Store) -> None:
    add(store, "1")
    store.set_fit_rating("LinkedIn", "1", "good")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.fit_rating == "good"
    store.set_fit_rating("LinkedIn", "1", None)
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.fit_rating is None
    with pytest.raises(StoreError):
        store.set_fit_rating("LinkedIn", "1", "great")
    with pytest.raises(StoreError):
        store.set_fit_rating("LinkedIn", "999", "good")


def test_only_labelled_roles_are_listed(store: Store) -> None:
    add(store, "1", status="Applied")
    add(store, "2", status="Not relevant")
    add(store, "3", status="Closed")
    add(store, "4")
    add(store, "5")
    store.set_fit_rating("LinkedIn", "5", "bad")
    assert sorted(r.job_id for r in store.list_labelled_roles()) == ["1", "2", "5"]


def test_load_labelled_drops_stale_scores(store: Store) -> None:
    config = Config()
    add(store, "1", status="Applied")
    add(store, "2", status="Not relevant")
    store.save_cv(Cv("cv.txt", CV_TEXT, sha_of(CV_TEXT), to_iso(now_ist())), cv_chunks(CV_TEXT))
    cv = store.get_cv()
    assert cv is not None
    fresh, old = store.get_role("LinkedIn", "1"), store.get_role("LinkedIn", "2")
    assert fresh is not None
    assert old is not None
    store.save_score(
        replace(make_score(fresh, 0.1, 0.6), inputs=inputs_fingerprint(config, cv, fresh))
    )
    store.save_score(make_score(old, 0.1, 0.6))  # a fingerprint that no longer matches
    loaded = {i.role.job_id: i for i in load_labelled(store, config, cv)}
    assert loaded["1"].score is not None
    assert loaded["2"].score is None
    assert [r.job_id for r in roles_to_score(list(loaded.values()))] == ["2"]
    assert all(i.score is None for i in load_labelled(store, config, None))


def test_pending_labelled_details_ignores_age_and_unlabelled(store: Store) -> None:
    add(store, "1", status="Applied", description=None)
    add(store, "2", status="Not relevant", description=None)
    add(store, "3", description=None)  # not labelled
    add(store, "4", status="Applied")  # already has one
    store.set_fit_rating("LinkedIn", "3", "good")
    store.conn.execute("UPDATE roles SET fetched_on = '2025-01-01T00:00:00+05:30'")
    assert sorted(j for _, j in store.pending_labelled_details(10)) == ["1", "2", "3"]
    assert len(store.pending_labelled_details(2)) == 2
    assert store.pending_labelled_details(0) == []
    for _ in range(3):
        store.record_detail_failure("LinkedIn", "1")
    assert "1" not in {j for _, j in store.pending_labelled_details(10)}


# ---- industry ideas ------------------------------------------------------------------------


def with_industries(job_id: str, status: str, industries: str | None) -> Labelled:
    return item(replace(make_role(job_id, status=status), industries=industries))


def test_industries_on_several_bad_roles_and_no_good_one_are_suggested() -> None:
    found = [
        with_industries("1", "Not relevant", "Construction, Staffing and Recruiting"),
        with_industries("2", "Not relevant", "construction"),
        with_industries("3", "Not relevant", "Mining"),
        with_industries("4", "Applied", "Staffing and Recruiting, Software Development"),
        with_industries("5", "Applied", None),
    ]
    found_names = {s.word: s.bad_roles for s in suggest_excluded_industries(found, Config())}
    assert found_names == {"Construction": 2}


def test_an_industry_already_excluded_is_not_suggested_again() -> None:
    found = [with_industries(str(i), "Not relevant", "Construction") for i in range(3)]
    config = Config(fit_excluded_industries="construction")
    assert suggest_excluded_industries(found, config) == ()


def test_the_report_carries_both_kinds_of_idea() -> None:
    found = separable(3, 3)
    found = [
        replace(i, role=replace(i.role, industries="Construction")) if i.label == LABEL_BAD else i
        for i in found
    ]
    report = build_report(found, Config(), [])
    assert [s.word for s in report.industry_suggestions] == ["Construction"]
