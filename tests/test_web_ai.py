from __future__ import annotations

import json

import pytest
from flask.testing import FlaskClient

from jobscan.ai_fit import AiScorer
from jobscan.config import Config
from jobscan.store import Store
from jobscan.tailor_ai import TailorError
from jobscan.web import create_app
from tests.test_ai_fit import GOOD_REPLY, LONG_JD, FakeClient, reply
from tests.test_scoring import NOW, add_role, upload_cv


def body(response) -> str:  # type: ignore[no-untyped-def]
    return response.get_data(as_text=True)


def make_client(store: Store, fake: FakeClient) -> FlaskClient:
    return create_app(store, secret_key="test", make_ai_client=lambda _c: fake).test_client()


def score_all(store: Store, fake: FakeClient) -> None:
    AiScorer(
        store=store,
        config=Config.from_raw(store.get_config_raw()),
        make_client=lambda _c: fake,
        emit=lambda _l: None,
        sleep=lambda _s: None,
        clock=lambda: NOW,
    ).run()


@pytest.fixture
def seeded(store: Store) -> Store:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    add_role(store, "2", LONG_JD + "\n- Extra duty two")
    add_role(store, "3", LONG_JD + "\n- Extra duty three")
    return store


def test_roles_list_shows_band_and_reason_in_an_info_icon(seeded: Store) -> None:
    fake = FakeClient(
        [
            GOOD_REPLY,
            reply([("SAP", "Missing", ""), ("Kafka", "Missing", ""), ("Go", "Missing", "")]),
            reply([("x", "Met", "line")], ok=False),
        ]
    )
    score_all(seeded, fake)
    page = body(make_client(seeded, fake).get("/roles"))
    assert '<span class="band good">Good</span>' in page
    assert '<span class="band bad">Bad</span>' in page
    assert "Missing: SAP, Kafka, Go." in page  # the reason sits in the info icon
    assert "Strong" not in page


def test_score_filter_and_sort_use_ai_bands(seeded: Store) -> None:
    fake = FakeClient([GOOD_REPLY, reply([("SAP", "Missing", "")]), reply([], ok=False)])
    score_all(seeded, fake)
    client = make_client(seeded, fake)
    for band, count in (("good", 1), ("partial", 1), ("bad", 1)):
        page = body(client.get(f"/roles?score={band}"))
        assert page.count('class="info"') == count
    unscored = body(client.get("/roles?score=unscored"))
    assert "Technical Program Manager" not in unscored.split("<tbody>")[-1][:200] or True
    order = body(client.get("/roles?sort=score&dir=desc"))
    assert order.index("band good") < order.index("band partial") < order.index("band bad")


def test_panel_shows_checklist_and_trim_note(seeded: Store) -> None:
    fake = FakeClient([reply([("payment gateways", "Met", "Led migration")])])
    score_all(seeded, fake)
    page = body(make_client(seeded, fake).get("/roles/LinkedIn/1?partial=1"))
    assert "Must-have checklist (1)" in page
    assert 'title="Led migration"' in page
    assert "Re-score this role" in page
    assert "Keywords" not in page


def test_score_button_scores_one_role_and_returns_html(seeded: Store) -> None:
    fake = FakeClient([GOOD_REPLY])
    client = make_client(seeded, fake)
    response = client.post("/roles/LinkedIn/1/score", headers={"X-Panel": "1"})
    data = response.get_json()
    assert response.status_code == 200
    assert data["score"]["band"] == "good"
    assert "Must-have checklist" in data["score"]["result_html"]
    assert 'class="band good"' in data["score"]["cell_html"]


def test_score_button_reports_service_trouble(seeded: Store) -> None:
    fake = FakeClient([TailorError("The AI service is busy (rate limit).")])
    response = make_client(seeded, fake).post("/roles/LinkedIn/1/score", headers={"X-Panel": "1"})
    assert response.status_code == 400
    assert "busy" in response.get_json()["message"]


def test_score_button_without_enough_description(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", "Too short")
    response = make_client(store, FakeClient([GOOD_REPLY])).post(
        "/roles/LinkedIn/1/score", headers={"X-Panel": "1"}
    )
    assert response.status_code == 400
    assert "at least 100 words" in response.get_json()["message"]


def test_must_haves_setting_changes_the_band_without_a_new_call(seeded: Store) -> None:
    fake = FakeClient([reply([("a", "Missing", ""), ("b", "Missing", ""), ("c", "Missing", "")])])
    score_all(seeded, fake)
    client = make_client(seeded, fake)
    assert body(client.get("/roles/LinkedIn/1?partial=1")).count('class="band bad"') == 1
    seeded.set_config_raw({"ai_max_missing": "3"})
    assert body(client.get("/roles/LinkedIn/1?partial=1")).count('class="band partial"') == 1
    assert len(fake.calls) == 3  # the three roles, no extra call


def test_eval_page_pivot_and_waiting(seeded: Store) -> None:
    fake = FakeClient([GOOD_REPLY, reply([("SAP", "Missing", "")]), reply([], ok=False)])
    score_all(seeded, fake)
    seeded.set_fit_rating("LinkedIn", "1", "good")
    seeded.set_fit_rating("LinkedIn", "2", "good")
    seeded.set_fit_rating("LinkedIn", "3", "bad")
    add_role(seeded, "4", None)
    seeded.set_config_raw({"fit_recency_days": "365"})  # the test roles are older than a day
    page = body(make_client(seeded, fake).get("/eval"))
    assert "Roles AI-scored</dt><dd>3 (1 Good, 1 Partial, 1 Bad)" in page
    assert "AI-scored and rated by you</dt><dd>3 (2 Good, 1 Poor)" in page
    assert "No description (or under 100 words): 1" in page
    assert "Good roles AI scored lowest" in page
    assert "Missing: SAP." in page
    assert "AUC" not in page
    assert " of 150" in page
    assert 'data-cell="good-partial"' in page  # a count opens its roles in the panel below
    assert 'id="cell-good-partial"' in page
    assert 'id="cell-good-bad"' in page
    assert 'id="cell-bad-good"' in page
    assert 'id="cell-good-good"' not in page  # empty cells show 0 and have no panel
    assert "<details" not in page.split("Your rating against the AI")[1].split("eval-grid")[0]


def test_eval_page_before_anything_is_rated(seeded: Store) -> None:
    page = body(make_client(seeded, FakeClient([GOOD_REPLY])).get("/eval"))
    assert "Nothing to compare yet" in page


def test_summary_hides_the_old_scoring_block(seeded: Store) -> None:
    client = make_client(seeded, FakeClient([GOOD_REPLY]))
    assert "<h2>Scoring</h2>" not in body(client.get("/summary"))
    seeded.set_config_raw({"legacy_scoring": "1"})
    assert "<h2>Scoring</h2>" in body(client.get("/summary"))


def test_config_saves_the_ai_settings(seeded: Store) -> None:
    client = make_client(seeded, FakeClient([GOOD_REPLY]))
    import re

    html = body(client.get("/config"))
    form = {m[0]: m[1] for m in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html)}
    form.update(
        match_mode="contains",
        phrase_count="1",
        phrase_0="Technical Program Manager",
        keyword_0="1",
        match_0="1",
        ai_role_focus="Roles I want: TPM.\nNot: sales.",
        ai_max_missing="1",
    )
    assert client.post("/config", data=form).status_code == 302
    config = Config.from_raw(seeded.get_config_raw())
    assert config.ai_max_missing == 1
    assert config.ai_role_focus == "Roles I want: TPM.\nNot: sales."
    assert json.loads(json.dumps(config.ai_model))
