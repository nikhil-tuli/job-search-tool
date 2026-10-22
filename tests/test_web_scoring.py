from __future__ import annotations

import io

import pytest
from flask.testing import FlaskClient

from jobscan.config import Config
from jobscan.cv import sha_of
from jobscan.embeddings import API_KEY_ENV, EmbedAuthError
from jobscan.models import Cv, JobDetail, NewRole
from jobscan.scoring import cv_chunks
from jobscan.store import Store
from jobscan.timeutil import now_ist, to_iso
from tests.conftest import Harness
from tests.test_cv import docx_bytes, pdf_bytes
from tests.test_scoring import CV_TEXT, PAYMENTS_JD, WAREHOUSE_JD


def body(response) -> str:  # type: ignore[no-untyped-def]
    return response.get_data(as_text=True)


def add_role(store: Store, job_id: str, description: str | None = PAYMENTS_JD) -> None:
    run = store.conn.execute("SELECT id FROM runs LIMIT 1").fetchone()
    run_id = run[0] if run else store.start_run(window_seconds=86400, keywords_total=1)
    store.insert_role(
        NewRole(
            portal="LinkedIn",
            job_id=job_id,
            title=f"Technical Program Manager {job_id}",
            company="Acme",
            location="Pune, India",
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


def upload_cv(store: Store, text: str = CV_TEXT) -> None:
    store.save_cv(Cv("cv.pdf", text, sha_of(text), to_iso(now_ist())), cv_chunks(text))


def post_cv(client: FlaskClient, name: str, data: bytes):  # type: ignore[no-untyped-def]
    return client.post(
        "/cv", data={"cv": (io.BytesIO(data), name)}, content_type="multipart/form-data"
    )


# ---- CV page -------------------------------------------------------------------------------


def test_cv_page_before_upload(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    page = body(client.get("/cv"))
    assert "No CV yet" in page


def test_cv_page_key_status(
    client: FlaskClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_cv(store)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    missing = body(client.get("/cv"))
    monkeypatch.setenv(API_KEY_ENV, "not-a-real-key-value")
    present = body(client.get("/cv"))
    assert "not set" in missing
    assert API_KEY_ENV in missing
    assert "found in this app" in present
    assert "not-a-real-key-value" not in present


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("cv.txt", CV_TEXT.encode()),
        ("cv.docx", docx_bytes([("Technical Program Manager", False), ("Led migrations", True)])),
        ("cv.pdf", pdf_bytes(["Technical Program Manager", "Led payment gateway migration"])),
    ],
)
def test_upload_stores_cv_and_chunks(web: Harness, name: str, data: bytes) -> None:
    response = post_cv(web.client, name, data)
    assert response.status_code == 302
    cv = web.store.get_cv()
    assert cv is not None
    assert cv.filename == name
    assert web.store.get_cv_chunks()
    page = body(web.client.get("/cv"))
    assert name in page
    assert "CV saved as" in page


def test_same_cv_again_changes_nothing(web: Harness) -> None:
    post_cv(web.client, "cv.txt", CV_TEXT.encode())
    first = web.store.get_cv()
    response = post_cv(web.client, "other.txt", CV_TEXT.encode())
    assert response.status_code == 302
    assert web.store.get_cv() == first
    assert "same CV" in body(web.client.get("/cv"))


def test_new_cv_replaces_old(web: Harness) -> None:
    post_cv(web.client, "a.txt", CV_TEXT.encode())
    post_cv(web.client, "b.txt", (CV_TEXT + "- Ran vendor negotiations for hosting\n").encode())
    cv = web.store.get_cv()
    assert cv is not None
    assert cv.filename == "b.txt"


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("cv.exe", b"MZ", "Upload a"),
        ("cv.txt", b"   \n  ", ""),
        ("cv.pdf", b"not a pdf", ""),
    ],
)
def test_bad_uploads_explain_and_store_nothing(
    web: Harness, name: str, data: bytes, message: str
) -> None:
    response = post_cv(web.client, name, data)
    assert response.status_code == 400
    assert message.lower() in body(response).lower()
    assert web.store.get_cv() is None


def test_upload_without_file(client: FlaskClient) -> None:
    response = client.post("/cv", data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "Choose a file" in body(response)


def test_oversized_upload_is_413(client: FlaskClient) -> None:
    # A raw body avoids the test client spooling a multipart encoding to a temp file.
    response = client.post(
        "/cv", data=b"x" * (6 * 1024 * 1024), content_type="multipart/form-data; boundary=b"
    )
    assert response.status_code == 413


def test_upload_counts_waiting_roles(web: Harness) -> None:
    add_role(web.store, "1")
    post_cv(web.client, "cv.txt", CV_TEXT.encode())
    assert "1 roles will be scored" in body(web.client.get("/cv"))


# ---- scoring a role ------------------------------------------------------------------------


def score(client: FlaskClient, job_id: str, *, panel: bool = False):  # type: ignore[no-untyped-def]
    headers = {"X-Panel": "1"} if panel else {}
    return client.post(f"/roles/LinkedIn/{job_id}/score", headers=headers)


def test_score_button_scores_role(web: Harness) -> None:
    add_role(web.store, "1")
    upload_cv(web.store)
    response = score(web.client, "1", panel=True)
    assert response.status_code == 200
    data = response.get_json()
    assert 0 <= data["score"]["final"] <= 100
    assert data["score"]["band"] in {"strong", "partial", "weak"}
    assert "chunks" in data["score"]["detail"]
    assert web.store.get_scores()
    assert web.store.list_scoring_runs(1)[0].trigger == "role"


def test_score_without_cv(web: Harness) -> None:
    add_role(web.store, "1")
    response = score(web.client, "1", panel=True)
    assert response.status_code == 400
    assert "CV" in response.get_json()["message"]
    assert web.embedder.calls == []


def test_score_without_description(web: Harness) -> None:
    add_role(web.store, "1", description=None)
    upload_cv(web.store)
    response = score(web.client, "1", panel=True)
    assert response.status_code == 400
    assert "no description" in response.get_json()["message"]


def test_score_failure_message_is_shown(web: Harness) -> None:
    add_role(web.store, "1")
    upload_cv(web.store)
    web.embedder.failures.append(EmbedAuthError("API key rejected"))
    response = score(web.client, "1", panel=True)
    assert response.status_code == 400
    assert "key" in response.get_json()["message"].lower()


def test_score_form_post_redirects_with_flash(web: Harness) -> None:
    add_role(web.store, "1")
    upload_cv(web.store)
    response = score(web.client, "1")
    assert response.status_code == 302
    assert "Scored" in body(web.client.get("/roles"))


def test_score_unknown_role_is_404(client: FlaskClient) -> None:
    assert score(client, "999").status_code == 404


# ---- Score column, filter, sort ------------------------------------------------------------


def listed(client: FlaskClient, query: str = "") -> list[str]:
    page = body(client.get(f"/roles?{query}"))
    return [j for j in ("1", "2", "3") if f'/jobs/view/{j}"' in page]


@pytest.fixture
def scored(web: Harness) -> Harness:
    add_role(web.store, "1", PAYMENTS_JD)
    add_role(web.store, "2", WAREHOUSE_JD)
    add_role(web.store, "3", PAYMENTS_JD)
    upload_cv(web.store)
    assert score(web.client, "1").status_code == 302
    assert score(web.client, "2").status_code == 302
    return web


def test_score_column_shows_band_and_dash(scored: Harness) -> None:
    page = body(scored.client.get("/roles"))
    assert 'class="band' in page
    assert "Score" in page


def test_score_filter(scored: Harness) -> None:
    assert listed(scored.client, "score=unscored") == ["3"]
    assert sorted(listed(scored.client, "score=scored")) == ["1", "2"]
    assert len(scored.store.get_scores()) == 2
    both = (
        listed(scored.client, "score=strong")
        + listed(scored.client, "score=partial")
        + listed(scored.client, "score=weak")
    )
    assert sorted(both) == ["1", "2"]


def test_sort_puts_best_fit_first_and_unscored_last(scored: Harness) -> None:
    page = body(scored.client.get("/roles?sort=score"))
    order = [page.index(f'/jobs/view/{j}"') for j in ("1", "2", "3")]
    assert order[0] < order[1] < order[2]


def test_score_filters_ignore_unknown_values(scored: Harness) -> None:
    assert sorted(listed(scored.client, "score=bogus&sort=bogus")) == ["1", "2", "3"]


def test_score_filter_paginates(web: Harness) -> None:
    upload_cv(web.store)
    for n in range(1, 121):
        add_role(web.store, str(n), None)
    page = body(web.client.get("/roles?score=unscored"))
    assert "Next" in page


def test_panel_shows_score_fact(scored: Harness) -> None:
    page = body(scored.client.get("/roles/LinkedIn/1?partial=1"))
    assert "Fit score" in page
    assert "Re-score" in page
    unscored = body(scored.client.get("/roles/LinkedIn/3?partial=1"))
    assert "Fit score" in unscored


def test_changed_weights_apply_without_rescoring(scored: Harness) -> None:
    calls = len(scored.embedder.calls)
    before = scored.store.get_scores()
    scored.client.post("/config", data={**Config().to_raw(), "score_weight_keyword": "100"})
    assert len(scored.embedder.calls) == calls
    assert scored.store.get_scores() == before


def test_csv_has_score_columns(scored: Harness) -> None:
    csv_text = body(scored.client.get("/export.csv?score=unscored"))
    header = csv_text.splitlines()[0]
    assert "Fit Score" in header
    assert "Fit Band" in header
    assert "view/3" in csv_text
    assert "view/1" not in csv_text


# ---- Summary and Config --------------------------------------------------------------------


def test_summary_shows_scoring_history(scored: Harness) -> None:
    page = body(scored.client.get("/summary"))
    assert "Scoring" in page
    assert "Run #" in page or "#1" in page


def test_summary_renders_scoring_once_with_tooltips(scored: Harness) -> None:
    page = body(scored.client.get("/summary"))
    assert page.count("<h2>Scoring</h2>") == 1
    title = page.split("<title>")[1].split("</title>")[0]
    assert "<" not in title
    assert 'data-tip="Skipped by the Fit rules' in page
    assert "tip.js" in page


def test_summary_without_cv(client: FlaskClient) -> None:
    assert client.get("/summary").status_code == 200


def test_config_scoring_group_validates(client: FlaskClient) -> None:
    page = body(client.get("/config"))
    assert "score_weight_keyword" in page
    assert "embed_model" in page
    bad = {**Config().to_raw(), "score_sem_floor": "0.9", "score_sem_ceiling": "0.8"}
    response = client.post("/config", data=bad)
    assert response.status_code == 400
    assert "below" in body(response)
