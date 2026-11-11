"""Interview Prep pages and JSON endpoints."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from flask.testing import FlaskClient

from jobscan import db
from jobscan.interview import store as st
from jobscan.interview.questions import GENERAL_ROLE, MY_QUESTIONS
from jobscan.interview.service import Pacer
from jobscan.interview.stt import TranscribeError
from jobscan.store import Store
from jobscan.web import create_app
from tests.test_interview_service import (
    LONG,
    FakeAi,
    FakeStt,
    feedback_reply,
    questions_reply,
    spoken,
)
from tests.test_scoring import add_role, upload_cv

T0 = "2026-11-11T17:00:00+05:30"


@dataclass
class Fakes:
    ai: FakeAi = field(default_factory=lambda: FakeAi(questions_reply()))
    stt: FakeStt = field(default_factory=lambda: FakeStt(spoken()))


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def client(store: Store, fakes: Fakes) -> FlaskClient:
    app = create_app(
        store,
        secret_key="t",
        make_interview_ai=lambda _c: fakes.ai,
        make_interview_stt=lambda _c: fakes.stt,
        interview_pacer=Pacer(monotonic=lambda: 0.0, sleep=lambda _s: None),
    )
    return app.test_client()


def text(response: object) -> str:
    return response.get_data(as_text=True)  # type: ignore[attr-defined]


def run_own(client: FlaskClient, store: Store, *questions: str) -> int:
    upload_cv(store)
    client.post(
        "/interview",
        data={"kind": MY_QUESTIONS, "q": list(questions) or ["Why this role?", "Tell me more."]},
    )
    running = store.running_interview()
    assert running is not None
    return running.id


def upload(client: FlaskClient, interview_id: int, idx: int, *, mime: str = "audio/webm") -> object:
    return client.post(
        f"/interview/{interview_id}/answer/{idx}",
        data={"audio": (io.BytesIO(b"audio"), "a.webm", mime), "duration": "20"},
        content_type="multipart/form-data",
    )


# ---- navigation and start -------------------------------------------------------------------


def test_menu_has_interview_prep(client: FlaskClient) -> None:
    assert "Interview Prep</a>" in text(client.get("/roles"))


def test_roles_list_and_panel_have_practice_buttons(client: FlaskClient, store: Store) -> None:
    add_role(store, "100")
    assert "/interview?role=LinkedIn%7C100" in text(client.get("/roles"))
    panel = text(client.get("/roles/LinkedIn/100?partial=1"))
    assert "Practice interview" in panel
    assert "role=LinkedIn%7C100" in panel


def test_start_page_without_cv_blocks_start(client: FlaskClient) -> None:
    page = text(client.get("/interview"))
    assert "Upload your CV to start" in page
    assert "is not saved" in page


def test_start_page_shows_cv_and_selected_role(client: FlaskClient, store: Store) -> None:
    upload_cv(store)
    add_role(store, "100")
    page = text(client.get("/interview?role=LinkedIn|100"))
    assert "Using CV" in page
    assert "cv.pdf" in page
    assert "Technical Program Manager" in page
    assert 'value="Company role"' in page


def test_general_roles_come_from_config(client: FlaskClient) -> None:
    page = text(client.get("/interview"))
    assert '<option value="Product Manager"' in page
    assert "Backend Engineer" in page


def test_start_page_warns_when_an_interview_is_running(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    page = text(client.get("/interview"))
    assert "Finish or end your current interview first" in page
    assert f"/interview/{interview_id}/run" in page


# ---- creating ---------------------------------------------------------------------------------


def test_my_questions_go_straight_to_the_run_page(client: FlaskClient, store: Store) -> None:
    upload_cv(store)
    response = client.post(
        "/interview", data={"kind": MY_QUESTIONS, "q": ["One here?", "", "Two!!"]}
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/run")
    page = text(client.get(response.headers["Location"]))
    assert "One here?" in page
    assert "Two!!" in page
    assert "Microphone check" in page
    assert 'id="mic"' in page


def test_a_general_role_writes_questions_then_runs(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    upload_cv(store)
    response = client.post("/interview", data={"kind": GENERAL_ROLE, "general_title": "Designer"})
    assert response.status_code == 302
    row = store.running_interview()
    assert row is not None
    assert row.general_title == "Designer"
    assert "Question 1?" in text(client.get(response.headers["Location"]))
    assert len(fakes.ai.calls) == 1


def test_failed_question_writing_is_reported_once(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    upload_cv(store)
    fakes.ai = FakeAi("not json")
    response = client.post("/interview", data={"kind": GENERAL_ROLE, "general_title": "Designer"})
    run_url = response.headers["Location"]
    interview_id = int(run_url.split("/")[2])
    status = client.get(f"/interview/{interview_id}/status").get_json()
    assert status["state"] == "error"
    assert "Could not write questions" in status["error"]
    follow = client.get(run_url, follow_redirects=True)
    assert "Could not write questions" in text(follow)
    assert store.running_interview() is None
    assert client.get(run_url).status_code == 404  # the reason is shown only once
    assert client.get(f"/interview/{interview_id}/status").get_json()["state"] == "gone"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"kind": MY_QUESTIONS, "q": [""]}, "at least one"),
        ({"kind": GENERAL_ROLE, "general_title": "Wizard"}, "from the list"),
        ({"kind": "Company role", "role": "LinkedIn|none"}, "Role not found"),
        ({"kind": "nonsense"}, "Choose a way to start"),
    ],
)
def test_bad_starts_flash_a_message(
    client: FlaskClient, store: Store, data: dict[str, object], message: str
) -> None:
    upload_cv(store)
    response = client.post("/interview", data=data, follow_redirects=True)
    assert message in text(response)
    assert store.running_interview() is None


def test_start_without_a_cv_is_refused(client: FlaskClient) -> None:
    response = client.post(
        "/interview", data={"kind": MY_QUESTIONS, "q": ["Why now?"]}, follow_redirects=True
    )
    assert "Upload your CV first" in text(response)


def test_cross_site_posts_are_refused(client: FlaskClient) -> None:
    response = client.post("/interview", data={}, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


def test_a_company_role_interview_uses_the_role(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    upload_cv(store)
    add_role(store, "100")
    client.post("/interview", data={"kind": "Company role", "role": "LinkedIn|100"})
    row = store.running_interview()
    assert row is not None
    assert row.role_job_id == "100"
    assert "payment gateway" in fakes.ai.calls[0][1]


def test_role_search_returns_json(client: FlaskClient, store: Store) -> None:
    add_role(store, "100")
    got = client.get("/interview/roles?q=Program").get_json()["roles"]
    assert got == [{"key": "LinkedIn|100", "title": "Technical Program Manager", "company": "Acme"}]
    assert client.get("/interview/roles?q=zzzz").get_json()["roles"] == []


# ---- answering --------------------------------------------------------------------------------


def test_status_for_a_running_interview(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    assert client.get(f"/interview/{interview_id}/status").get_json()["state"] == "ready"
    assert client.get("/interview/999/status").get_json()["state"] == "gone"


def test_an_answer_is_transcribed(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    data = upload(client, interview_id, 1).get_json()  # type: ignore[attr-defined]
    assert data["status"] == "answered"
    assert data["words"] == 32
    assert store.get_answers(interview_id)[0].transcript == LONG


def test_a_rate_limit_tells_the_browser_to_wait(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    interview_id = run_own(client, store)
    fakes.stt = FakeStt(TranscribeError("busy", status=429, retry_after=9))
    data = upload(client, interview_id, 1).get_json()  # type: ignore[attr-defined]
    assert data["retry_after"] == 9
    assert not data["status"]
    assert store.get_answers(interview_id) == []


def test_bad_uploads_are_refused(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    assert upload(client, interview_id, 1, mime="text/html").status_code == 400  # type: ignore[attr-defined]
    assert upload(client, interview_id, 9).status_code == 400  # type: ignore[attr-defined]
    none = client.post(f"/interview/{interview_id}/answer/1", data={})
    assert none.status_code == 400
    assert none.get_json()["error"]


def test_skip_and_not_scored(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    assert client.post(f"/interview/{interview_id}/skip/1").get_json()["status"] == "skipped"
    reply = client.post(f"/interview/{interview_id}/not-scored/2", data={"note": "Too large"})
    assert reply.get_json()["status"] == "not_scored"
    assert [a.status for a in store.get_answers(interview_id)] == ["skipped", "not_scored"]
    assert client.post("/interview/999/skip/1").status_code == 400


# ---- finishing --------------------------------------------------------------------------------


def test_finishing_with_nothing_to_score_discards_it(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    client.post(f"/interview/{interview_id}/skip/1")
    data = client.post(f"/interview/{interview_id}/finish").get_json()
    assert data["discarded"] is True
    assert data["next"] == "/interview/history"
    assert store.get_interview(interview_id) is None


def test_finishing_makes_feedback(client: FlaskClient, store: Store, fakes: Fakes) -> None:
    interview_id = run_own(client, store)
    upload(client, interview_id, 1)
    fakes.ai = FakeAi(feedback_reply(1))
    data = client.post(f"/interview/{interview_id}/finish").get_json()
    assert data["discarded"] is False
    assert data["next"] == f"/interview/{interview_id}"
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.READY
    page = text(client.get(f"/interview/{interview_id}"))
    assert "AI estimate" in page
    assert "Measured" in page
    assert "I led the lender integration" in page  # the quote
    assert "Your answer, improved" in page
    assert "[add figure]" in page
    assert "99 weeks" not in page
    assert "Practice again" in page
    assert "Back to history" in page
    assert "See examples" in page
    assert "Specificity" in page


def test_finish_only_works_on_a_running_interview(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    upload(client, interview_id, 1)
    client.post(f"/interview/{interview_id}/finish")
    assert client.post(f"/interview/{interview_id}/finish").status_code == 400


def test_the_preparing_page_polls(
    client: FlaskClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    interview_id = run_own(client, store)
    upload(client, interview_id, 1)
    store.set_interview_status(interview_id, st.PREPARING, now=T0, stage="scoring")
    page = text(client.get(f"/interview/{interview_id}"))
    assert "Preparing your feedback" in page
    assert "Scoring" in page
    assert f"/interview/{interview_id}/status" in page
    assert client.get(f"/interview/{interview_id}/status").get_json() == {
        "state": "preparing", "stage": "scoring", "error": "",
    }  # fmt: skip


def test_a_failed_interview_shows_why_and_can_retry(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    interview_id = run_own(client, store)
    upload(client, interview_id, 1)
    fakes.ai = FakeAi("garbage")
    client.post(f"/interview/{interview_id}/finish")
    page = text(client.get(f"/interview/{interview_id}"))
    assert "Feedback could not be made" in page
    assert "Try again" in page
    fakes.ai = FakeAi(feedback_reply(1))
    response = client.post(f"/interview/{interview_id}/retry")
    assert response.status_code == 302
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.READY


def test_leaving_the_page_keeps_the_work(client: FlaskClient, store: Store, fakes: Fakes) -> None:
    interview_id = run_own(client, store)
    upload(client, interview_id, 1)
    assert client.post(f"/interview/{interview_id}/abandon").status_code == 204
    page = text(client.get(f"/interview/{interview_id}"))
    assert "left before the end" in page
    assert "Get feedback" in page
    assert "Go to interview" not in page


def test_an_abandoned_empty_interview_is_dropped(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    client.post(f"/interview/{interview_id}/abandon")
    assert store.get_interview(interview_id) is None


def test_run_page_redirects_when_not_running(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    store.set_interview_status(interview_id, st.READY, now=T0)
    assert (
        client.get(f"/interview/{interview_id}/run").headers["Location"]
        == f"/interview/{interview_id}"
    )
    assert client.get("/interview/999").status_code == 404


# ---- feedback page ----------------------------------------------------------------------------


def finished_general(
    client: FlaskClient, store: Store, fakes: Fakes, title: str = "Designer"
) -> int:
    upload_cv(store)
    fakes.ai = FakeAi(questions_reply())
    client.post("/interview", data={"kind": GENERAL_ROLE, "general_title": title})
    row = store.running_interview()
    assert row is not None
    fakes.stt = FakeStt(spoken(LONG + " " + LONG))
    upload(client, row.id, 1)
    fakes.ai = FakeAi(feedback_reply(1))
    client.post(f"/interview/{row.id}/finish")
    return row.id


def test_feedback_page_shows_both_rubrics(client: FlaskClient, store: Store, fakes: Fakes) -> None:
    interview_id = finished_general(client, store, fakes)
    page = text(client.get(f"/interview/{interview_id}"))
    assert "General role · Designer" in page
    assert "1 of 5 answered" in page
    assert "Answer feedback" in page
    assert "How you spoke" in page
    assert "80" in page
    assert "Strong" in page
    assert "Filler words" in page
    assert "Ownership" in page
    assert "Questions that were not answered" not in page
    assert "Not answered." in page  # questions 2 to 5


def test_short_speech_says_how_you_spoke_cannot_be_measured(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    interview_id = run_own(client, store)
    upload(client, interview_id, 1)  # 32 words: below the 50 needed for the spoken rubric
    fakes.ai = FakeAi(feedback_reply(1))
    client.post(f"/interview/{interview_id}/finish")
    assert "Not enough speech" in text(client.get(f"/interview/{interview_id}"))


def test_change_since_the_last_interview(client: FlaskClient, store: Store, fakes: Fakes) -> None:
    first = finished_general(client, store, fakes)
    store.conn.execute(
        "UPDATE interview_feedback SET answer_score = 60 WHERE interview_id = ?", (first,)
    )
    second = finished_general(client, store, fakes)
    assert "▲ 20" in text(client.get(f"/interview/{second}"))
    assert "▲" not in text(client.get(f"/interview/{first}"))


def test_my_questions_are_never_compared(client: FlaskClient, store: Store, fakes: Fakes) -> None:
    first = run_own(client, store)
    upload(client, first, 1)
    fakes.ai = FakeAi(feedback_reply(1))
    client.post(f"/interview/{first}/finish")
    second = run_own(client, store)
    upload(client, second, 1)
    client.post(f"/interview/{second}/finish")
    page = text(client.get(f"/interview/{second}"))
    assert "▲" not in page
    assert "▼" not in page
    assert "since last time" not in page
    assert "as last time" not in page


def test_thumbs_can_be_saved_and_cleared(client: FlaskClient, store: Store, fakes: Fakes) -> None:
    interview_id = finished_general(client, store, fakes)
    client.post(f"/interview/{interview_id}/thumb", data={"idx": "1", "value": "1"})
    assert store.get_thumbs(interview_id) == {1: 1}
    assert 'aria-pressed="true"' in text(client.get(f"/interview/{interview_id}"))
    client.post(f"/interview/{interview_id}/thumb", data={"idx": "1", "value": "0"})
    assert store.get_thumbs(interview_id) == {}
    assert (
        client.post(f"/interview/{interview_id}/thumb", data={"idx": "1", "value": "7"}).status_code
        == 400
    )
    assert (
        client.post(f"/interview/{interview_id}/thumb", data={"idx": "x", "value": "1"}).status_code
        == 400
    )


def test_practice_again_starts_a_new_interview_of_the_same_kind(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    interview_id = finished_general(client, store, fakes)
    fakes.ai = FakeAi(questions_reply())
    response = client.post(f"/interview/{interview_id}/again")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/run")
    row = store.running_interview()
    assert row is not None
    assert row.id != interview_id
    assert row.general_title == "Designer"


def test_practice_again_for_my_questions_reuses_them(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store, "Alpha question?", "Beta question?")
    store.set_interview_status(interview_id, st.READY, now=T0)
    client.post(f"/interview/{interview_id}/again")
    row = store.running_interview()
    assert row is not None
    assert [q.text for q in store.get_questions(row.id)] == ["Alpha question?", "Beta question?"]


# ---- history ----------------------------------------------------------------------------------


def test_empty_history(client: FlaskClient) -> None:
    page = text(client.get("/interview/history"))
    assert "No interviews yet" in page
    assert "Start an interview" in page


def test_history_rows_and_details(client: FlaskClient, store: Store, fakes: Fakes) -> None:
    finished_general(client, store, fakes)
    add_role(store, "100")
    fakes.ai = FakeAi(questions_reply())
    client.post("/interview", data={"kind": "Company role", "role": "LinkedIn|100"})
    page = text(client.get("/interview/history"))
    assert "Designer" in page
    assert "Acme" in page
    assert "open role" in page
    assert "partial=1&amp;readonly=1" in page  # the read-only role panel
    assert "View feedback" in page
    assert "Go to interview" in page
    assert "In progress" in page
    assert "Ready" in page


def test_history_shows_removed_roles_and_custom_questions(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    upload_cv(store)
    add_role(store, "100")
    client.post("/interview", data={"kind": "Company role", "role": "LinkedIn|100"})
    row = store.running_interview()
    assert row is not None
    store.set_interview_status(row.id, st.FAILED, now=T0, error="It broke.")
    store.conn.execute("DELETE FROM roles")
    page = text(client.get("/interview/history"))
    assert "role removed" in page
    assert "open role" not in page
    assert "It broke." in page
    assert "Try again" in page
    own = run_own(client, store, "Alpha question?", "Beta question?")
    page = text(client.get("/interview/history"))
    assert "2 custom questions" in page
    assert "1. Alpha question?" in page
    assert "2. Beta question?" in page
    assert own


def test_history_filters_by_type(client: FlaskClient, store: Store, fakes: Fakes) -> None:
    finished_general(client, store, fakes)
    run_own(client, store)
    only = text(client.get("/interview/history?type=General role"))
    assert "Designer" in only
    assert "custom question" not in only
    assert "custom question" in text(client.get("/interview/history?type=My questions"))
    assert "Designer" in text(client.get("/interview/history?type=junk"))


def test_history_pages(client: FlaskClient, store: Store) -> None:
    for day in range(1, 28):
        store.create_interview(
            kind=MY_QUESTIONS,
            questions=[],
            now=f"2026-10-{day:02d}T10:00:00+05:30",
            cv_name="cv.pdf",
        )
        store.set_interview_status(day, st.READY, now=T0)
    first = text(client.get("/interview/history"))
    assert "Page 1 of 2" in first
    assert "27 interviews" in first
    assert "Page 2 of 2" in text(client.get("/interview/history?page=2"))
    assert "Page 1 of 2" in text(client.get("/interview/history?page=zzz"))


def test_old_in_progress_interviews_become_incomplete_when_history_opens(
    client: FlaskClient, store: Store
) -> None:
    interview_id = run_own(client, store)
    upload(client, interview_id, 1)
    store.conn.execute("UPDATE interview SET updated_on = '2026-01-01T10:00:00+05:30'")
    page = text(client.get("/interview/history"))
    assert "Incomplete" in page
    assert "Get feedback" in page


def test_delete_asks_first_and_removes_everything(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    interview_id = finished_general(client, store, fakes)
    page = text(client.get("/interview/history"))
    assert "confirm(" in page
    assert "permanently" in page
    response = client.post(f"/interview/{interview_id}/delete", follow_redirects=True)
    assert "Interview deleted" in text(response)
    assert store.get_interview(interview_id) is None


def test_delete_is_blocked_while_running_or_preparing(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    assert "Wait until" in text(
        client.post(f"/interview/{interview_id}/delete", follow_redirects=True)
    )
    store.set_interview_status(interview_id, st.PREPARING, now=T0)
    assert "Wait until" in text(
        client.post(f"/interview/{interview_id}/delete", follow_redirects=True)
    )
    assert store.get_interview(interview_id) is not None
    assert 'http-equiv="refresh"' in text(client.get("/interview/history"))


def test_retry_on_a_ready_interview_is_refused(
    client: FlaskClient, store: Store, fakes: Fakes
) -> None:
    interview_id = finished_general(client, store, fakes)
    response = client.post(f"/interview/{interview_id}/retry", follow_redirects=True)
    assert "cannot be retried" in text(response)


def test_start_up_marks_preparing_interviews_failed(store: Store) -> None:
    upload_cv(store)
    interview_id = store.create_interview(kind=MY_QUESTIONS, questions=[], now=T0, cv_name="c")
    store.set_interview_status(interview_id, st.PREPARING, now=T0)
    create_app(store, secret_key="t")
    row = store.get_interview(interview_id)
    assert row is not None
    assert row.status == st.FAILED


def test_the_page_script_json_is_valid(client: FlaskClient, store: Store) -> None:
    interview_id = run_own(client, store)
    page = text(client.get(f"/interview/{interview_id}/run"))
    blob = page.split("window.INTERVIEW = ")[1].split(";</script>")[0]
    settings = json.loads(blob)
    assert settings["id"] == interview_id
    assert settings["stop"] == 300
    assert settings["urls"]["answer"].endswith("/answer/0")


def test_start_page_matches_the_mock(client: FlaskClient, store: Store) -> None:
    upload_cv(store)
    page = text(client.get("/interview"))
    for heading in ("1. A role you track", "2. A general role", "3. My own questions"):
        assert heading in page
    assert "Picks from the roles already fetched into the Roles tab." in page
    assert "Questions use the role, your CV and the must-haves." in page
    assert "Questions use the title and your CV." in page
    assert "1 to 5. Asked exactly as typed." in page
    assert "+ Add question" in page
    assert "Read questions aloud" in page
    assert "Your voice is sent for transcription and is not saved." in page
    assert page.count('name="q"') == 2


def test_role_list_shows_three_until_you_search(client: FlaskClient, store: Store) -> None:
    for n in range(6):
        add_role(store, str(100 + n))
    assert len(client.get("/interview/roles").get_json()["roles"]) == 3
    assert len(client.get("/interview/roles?q=Acme").get_json()["roles"]) == 5


def test_dates_are_shown_like_the_mock() -> None:
    from jobscan.interview import views as vw

    assert vw.nice_date("2026-11-03T10:00:00+05:30") == "3 Nov 2026"
    assert vw.nice_datetime("2026-11-11T17:20:00+05:30") == "11 Nov, 5:20 pm"
    assert vw.nice_full("2026-11-11T00:05:00+05:30") == "11 Nov 2026, 12:05 am"
    assert vw.nice_date("junk") == "junk"


def test_feedback_follows_the_mock_order(client: FlaskClient, store: Store, fakes: Fakes) -> None:
    interview_id = finished_general(client, store, fakes)
    page = text(client.get(f"/interview/{interview_id}"))
    order = [
        "Interview feedback",
        "Your answers",
        "How you spoke</h2>",
        "See examples",
        "Top 3 tips for next time",
    ]
    positions = [page.index(x) for x in order]
    assert positions == sorted(positions)
    assert "Top finding" in page
    assert "Was this useful?" in page
