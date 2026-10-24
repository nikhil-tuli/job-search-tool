from __future__ import annotations

import re

import pytest
from flask.testing import FlaskClient

from jobscan.config import Config
from jobscan.models import STATUSES
from jobscan.web.app import label_note
from tests.conftest import Harness
from tests.test_evaluation import make_role
from tests.test_web_scoring import add_role, body, upload_cv


def rate(client: FlaskClient, job_id: str, choice: str, *, panel: bool = True):  # type: ignore[no-untyped-def]
    headers = {"X-Panel": "1"} if panel else {}
    return client.post(f"/roles/LinkedIn/{job_id}/rating", data={"rating": choice}, headers=headers)


# ---- rating --------------------------------------------------------------------------------


def test_rating_saves_and_explains_itself(web: Harness) -> None:
    add_role(web.store, "1")
    response = rate(web.client, "1", "good")
    assert response.status_code == 200
    assert response.get_json() == {
        "rating": "good",
        "note": "Counts in the eval.",
    }
    role = web.store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.fit_rating == "good"


def test_rating_can_be_changed_and_cleared(web: Harness) -> None:
    add_role(web.store, "1")
    rate(web.client, "1", "good")
    assert rate(web.client, "1", "bad").get_json()["rating"] == "bad"
    data = rate(web.client, "1", "clear").get_json()
    assert data["rating"] == ""
    assert "Not in the eval" in data["note"]


def test_a_cleared_rating_leaves_the_role_out_of_the_eval(web: Harness) -> None:
    add_role(web.store, "1")
    web.store.update_status(["LinkedIn|1"], "Applied")
    assert rate(web.client, "1", "good").get_json()["note"] == "Counts in the eval."
    data = rate(web.client, "1", "clear").get_json()
    assert data["note"] == "Not in the eval."


def test_rating_form_post_redirects_with_a_message(web: Harness) -> None:
    add_role(web.store, "1")
    response = rate(web.client, "1", "bad", panel=False)
    assert response.status_code == 302
    assert "Counts in the eval." in body(web.client.get("/roles"))


@pytest.mark.parametrize("choice", ["", "great", "GOOD"])
def test_unknown_rating_is_refused(web: Harness, choice: str) -> None:
    add_role(web.store, "1")
    assert rate(web.client, "1", choice).status_code == 400
    role = web.store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.fit_rating is None


def test_rating_an_unknown_role_is_404(client: FlaskClient) -> None:
    assert rate(client, "999", "good").status_code == 404


def test_panel_has_rating_buttons_and_a_label_note(web: Harness) -> None:
    add_role(web.store, "1")
    rate(web.client, "1", "good")
    page = body(web.client.get("/roles/LinkedIn/1?partial=1"))
    assert 'name="rating" value="good" aria-pressed="true"' in page
    assert 'name="rating" value="bad" aria-pressed="false"' in page
    assert 'value="clear"' in page
    assert "Counts in the eval." in page


def test_list_marks_rated_roles(web: Harness) -> None:
    add_role(web.store, "1")
    add_role(web.store, "2")
    rate(web.client, "1", "good")
    rate(web.client, "2", "bad")
    page = body(web.client.get("/roles"))
    assert "&#128077;" in page
    assert "&#128078;" in page


def test_saving_a_status_never_puts_a_role_in_the_eval(web: Harness) -> None:
    add_role(web.store, "1")
    response = web.client.post(
        "/roles/LinkedIn/1",
        data={"status": "Applied", "applied_on": "", "contact_person": "", "comments": ""},
        headers={"X-Panel": "1"},
    )
    assert response.get_json()["note"] == "Not in the eval."


@pytest.mark.parametrize("status", STATUSES)
def test_label_note_covers_every_status(status: str) -> None:
    assert label_note(make_role("1", status=status))


# ---- eval page -----------------------------------------------------------------------------


def test_eval_page_without_a_cv(client: FlaskClient) -> None:
    page = body(client.get("/eval"))
    assert "Upload your CV" in page


def test_eval_page_with_nothing_labelled(web: Harness) -> None:
    upload_cv(web.store)
    page = body(web.client.get("/eval"))
    assert "Nothing to measure yet" in page
    assert "jobscan eval" in page


def label_and_score(web: Harness, good: int, bad: int) -> None:
    upload_cv(web.store)
    for i in range(good):
        add_role(web.store, f"g{i}", f"Responsibilities\n\n- Lead payment gateway migration {i}\n")
        web.store.set_fit_rating("LinkedIn", f"g{i}", "good")
    for i in range(bad):
        add_role(web.store, f"b{i}", f"Responsibilities\n\n- Forklift pallet racking {i}\n")
        web.store.set_fit_rating("LinkedIn", f"b{i}", "bad")
        web.store.conn.execute(
            "UPDATE roles SET title = 'Warehouse Staffing Lead' WHERE job_id = ?", (f"b{i}",)
        )
    for role in web.store.list_labelled_roles():
        assert web.client.post(f"/roles/LinkedIn/{role.job_id}/score").status_code == 302


def test_eval_page_funnel_counts_scored_roles_and_then_rated_ones(web: Harness) -> None:
    label_and_score(web, 3, 2)
    add_role(web.store, "u1", "Responsibilities\n\n- Lead payment gateway migration\n")
    assert web.client.post("/roles/LinkedIn/u1/score").status_code == 302  # scored, not rated
    add_role(web.store, "u2", "Responsibilities\n\n- Lead payment gateway migration\n")  # neither
    page = body(web.client.get("/eval"))
    scored = page.split("Roles with a current score</dt><dd>")[1].split("</dd>")[0]
    assert scored.startswith("6 (")
    total, strong, partial, weak = (int(n) for n in re.findall(r"\d+", scored))
    assert strong + partial + weak == total
    assert "Strong" in scored
    assert "Partial" in scored
    assert "Weak" in scored
    assert "Roles with a current score and rating</dt><dd>5 (3 good, 2 bad)" in page


def test_eval_page_reports_measures(web: Harness) -> None:
    label_and_score(web, 3, 3)
    page = body(web.client.get("/eval"))
    assert "6 (3 good, 3 bad)" in page  # scored and rated
    assert "Which score works best" in page
    for name in ("Keywords only", "Meaning only", "Current mix (40% keywords)"):
        assert name in page
    assert "Directional only" in page
    assert "Cut-off" in page
    assert "Good roles scored lowest" in page
    assert "Bad roles scored highest" in page
    assert "Search phrases" in page


def test_eval_page_lists_unscored_labelled_roles(web: Harness) -> None:
    upload_cv(web.store)
    add_role(web.store, "1", None)
    web.store.set_fit_rating("LinkedIn", "1", "good")
    page = body(web.client.get("/eval"))
    assert "No description" in page
    assert "cannot be scored" in page


def test_eval_page_suggests_deny_words_and_adds_them(web: Harness) -> None:
    label_and_score(web, 2, 3)
    page = body(web.client.get("/eval"))
    assert "Deny word ideas" in page
    assert "<strong>staffing</strong>" in page
    response = web.client.post("/eval/exclude", data={"kind": "title", "value": "staffing"})
    assert response.status_code == 302
    config = Config.from_raw(web.store.get_config_raw())
    assert config.fit_deny_keywords.endswith(", staffing")
    after = body(web.client.get("/eval"))
    assert "is now a title deny word" in after
    assert "<strong>staffing</strong>" not in after


def test_adding_a_deny_word_twice_changes_nothing(web: Harness) -> None:
    web.client.post("/eval/exclude", data={"kind": "title", "value": "staffing"})
    first = web.store.get_config_raw()["fit_deny_keywords"]
    web.client.post("/eval/exclude", data={"kind": "title", "value": "Staffing"})
    assert web.store.get_config_raw()["fit_deny_keywords"] == first


def test_a_new_deny_word_takes_effect_on_the_fit_filter_at_once(web: Harness) -> None:
    add_role(web.store, "1")
    web.store.conn.execute("UPDATE roles SET title = 'Staffing Lead'")
    assert "Excluded: title keyword" not in body(web.client.get("/roles"))
    web.client.post("/eval/exclude", data={"kind": "title", "value": "staffing"})
    assert "Excluded: title keyword" in body(web.client.get("/roles"))


@pytest.mark.parametrize("word", ["", "a", "two words", "x" * 5 + "!", "<b>", "123"])
def test_unusable_deny_words_are_refused(web: Harness, word: str) -> None:
    response = web.client.post("/eval/exclude", data={"kind": "title", "value": word})
    assert response.status_code == 400
    assert "fit_deny_keywords" not in web.store.get_config_raw()


def test_eval_is_in_the_navigation(client: FlaskClient) -> None:
    assert 'href="/eval"' in body(client.get("/roles"))


# ---- advanced filters ----------------------------------------------------------------------


def test_fit_score_and_sort_live_in_a_collapsed_advanced_section(web: Harness) -> None:
    add_role(web.store, "1")
    page = body(web.client.get("/roles"))
    head, _, advanced = page.partition('<details class="advanced"')
    inside, _, _ = advanced.partition("</details>")
    assert advanced.startswith(">")  # no open attribute
    for name in ('name="fit"', 'name="score"'):
        assert name in inside
        assert name not in head
    assert 'name="q"' in head
    assert 'name="status"' in head
    assert 'name="dup"' in head


@pytest.mark.parametrize("query", ["fit=eligible", "score=weak"])
def test_the_section_opens_when_an_advanced_filter_is_on(web: Harness, query: str) -> None:
    add_role(web.store, "1")
    page = body(web.client.get(f"/roles?{query}"))
    assert '<details class="advanced" open>' in page
    assert "Advanced filters <span" in page


def test_basic_filters_leave_the_section_collapsed(web: Harness) -> None:
    add_role(web.store, "1")
    page = body(web.client.get("/roles?status=Not+Applied&q=manager&dup=1"))
    assert '<details class="advanced">' in page
    assert "Advanced filters <span" not in page


def test_advanced_choices_survive_the_round_trip(web: Harness) -> None:
    add_role(web.store, "1")
    page = body(web.client.get("/roles?fit=eligible&score=unscored"))
    assert '<option value="eligible" selected>' in page
    assert '<option value="unscored" selected>' in page


# ---- excluding industries ------------------------------------------------------------------


def with_industries(web: Harness, job_id: str, rating: str | None, industries: str) -> None:
    add_role(web.store, job_id)
    if rating is not None:
        web.store.set_fit_rating("LinkedIn", job_id, rating)
    web.store.conn.execute("UPDATE roles SET industries = ? WHERE job_id = ?", (industries, job_id))


def test_eval_page_suggests_industries_and_excludes_them(web: Harness) -> None:
    upload_cv(web.store)
    for i in range(2):
        with_industries(web, f"b{i}", "bad", "Staffing and Recruiting")
    with_industries(web, "g", "good", "Software Development")
    page = body(web.client.get("/eval"))
    assert "Industry ideas" in page
    assert "<strong>Staffing and Recruiting</strong>" in page
    response = web.client.post(
        "/eval/exclude", data={"kind": "industry", "value": "Staffing and Recruiting"}
    )
    assert response.status_code == 302
    config = Config.from_raw(web.store.get_config_raw())
    assert config.fit_excluded_industries == "Staffing and Recruiting"
    after = body(web.client.get("/eval"))
    assert "is now an excluded industry" in after
    assert "<strong>Staffing and Recruiting</strong>" not in after


def test_an_excluded_industry_tags_roles_at_once(web: Harness) -> None:
    with_industries(web, "1", None, "Construction")
    assert "Excluded: industry" not in body(web.client.get("/roles"))
    web.client.post("/eval/exclude", data={"kind": "industry", "value": "Construction"})
    assert "Excluded: industry" in body(web.client.get("/roles"))


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "industry", "value": ""},
        {"kind": "industry", "value": "a"},
        {"kind": "industry", "value": "Staffing, Mining"},
        {"kind": "industry", "value": "<script>"},
        {"kind": "colour", "value": "Mining"},
        {"value": "Mining"},
    ],
)
def test_unusable_exclusions_are_refused(web: Harness, data: dict[str, str]) -> None:
    assert web.client.post("/eval/exclude", data=data).status_code == 400
    assert "fit_excluded_industries" not in web.store.get_config_raw()


def test_industry_names_with_punctuation_are_accepted(web: Harness) -> None:
    value = "Oil & Gas (Upstream)"
    assert (
        web.client.post("/eval/exclude", data={"kind": "industry", "value": value}).status_code
        == 302
    )
    assert web.store.get_config_raw()["fit_excluded_industries"] == value


def test_the_config_page_lists_excluded_industries(client: FlaskClient) -> None:
    page = body(client.get("/config"))
    assert 'name="fit_excluded_industries" value=""' in page
    assert "Excluded industries" in page
