"""Follow-up tracking, contact details and multi-select filters."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from flask.testing import FlaskClient

from jobscan import db
from jobscan.store import Store
from jobscan.timeutil import now_ist, to_iso
from jobscan.web import create_app
from jobscan.web.forms import linkedin_url
from tests.test_web import add_role, text


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


@pytest.fixture
def run_id(store: Store) -> int:
    return store.start_run(window_seconds=86400, keywords_total=1)


@pytest.fixture
def client(store: Store) -> FlaskClient:
    return create_app(store, secret_key="test").test_client()


def day(offset: int = 0) -> str:
    return to_iso(now_ist() + timedelta(days=offset))[:10]


def save(client: FlaskClient, job_id: str, **fields: str):  # type: ignore[no-untyped-def]
    data = {"status": "Not Applied", "applied_on": "", "contact_person": "", "comments": ""}
    data.update(fields)
    return client.post(f"/roles/LinkedIn/{job_id}", data=data, headers={"X-Panel": "1"})


def listed(client: FlaskClient, query: str) -> list[str]:
    body = text(client.get(f"/roles?{query}"))
    return sorted({j for j in "123456" if f"/jobs/view/{j}" in body})


def test_follow_up_and_contact_fields_save(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    response = save(
        client,
        "1",
        follow_up_needed="1",
        follow_up_on=day(2),
        contact_email="asha@acme.com",
        contact_phone="+91 98765 43210",
        contact_linkedin="linkedin.com/in/asha",
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["links"] == {
        "email": "mailto:asha@acme.com",
        "linkedin": "https://linkedin.com/in/asha",
    }
    assert "Follow-up:" in payload["follow_up"]
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.follow_up_needed
    assert role.follow_up_on == day(2)
    assert role.contact_phone == "+91 98765 43210"


def test_bad_contact_fields_are_rejected(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    for field, value in (
        ("follow_up_on", "tomorrow"),
        ("contact_email", "nope"),
        ("contact_phone", "call me"),
        ("contact_linkedin", "https://example.com/in/x"),
    ):
        assert save(client, "1", **{field: value}).status_code == 400, field


def test_linkedin_must_be_a_linkedin_address() -> None:
    assert linkedin_url("https://www.linkedin.com/in/a") == "https://www.linkedin.com/in/a"
    assert linkedin_url("linkedin.com/in/a") == "https://linkedin.com/in/a"
    assert linkedin_url("https://evil.com/linkedin.com") is None
    assert linkedin_url("https://notlinkedin.com/in/a") is None
    assert linkedin_url("javascript:alert(1)") is None


def _setup(client: FlaskClient, store: Store, run_id: int) -> None:
    for job in "12345":
        add_role(store, run_id, job)
    save(client, "1", follow_up_needed="1", follow_up_on=day(0))  # due
    save(client, "2", follow_up_needed="1", follow_up_on=day(3))  # upcoming
    save(client, "3", follow_up_needed="1", follow_up_on=day(-2))  # overdue
    save(client, "4", follow_up_needed="1")  # no date
    save(client, "5", follow_up_on=day(-2))  # flag off: date ignored


def test_follow_up_states_filter(client: FlaskClient, store: Store, run_id: int) -> None:
    _setup(client, store, run_id)
    assert listed(client, "followup=due") == ["1"]
    assert listed(client, "followup=upcoming") == ["2"]
    assert listed(client, "followup=overdue") == ["3"]
    assert listed(client, "followup=nodate") == ["4"]
    assert listed(client, "followup=due&followup=overdue") == ["1", "3"]
    assert listed(client, "") == ["1", "2", "3", "4", "5"]


def test_follow_up_filter_sorts_earliest_first(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    _setup(client, store, run_id)
    body = text(client.get("/roles?followup=due&followup=upcoming&followup=overdue"))
    order = [body.index(f"/jobs/view/{j}") for j in ("3", "1", "2")]
    assert order == sorted(order)


def test_title_tag_shows_state(client: FlaskClient, store: Store, run_id: int) -> None:
    _setup(client, store, run_id)
    body = text(client.get("/roles"))
    assert "Follow-up: Overdue (" in body
    assert "Follow-up: Due today" in body
    assert "Follow-up: no date" in body
    assert body.count("follow-tag") == 4  # the flag-off role has no tag


def test_status_and_fit_and_fetched_take_several_values(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    for job in "123":
        add_role(store, run_id, job, fetched_on=to_iso(now_ist()))
    store.update_status(["LinkedIn|1"], "Applied")
    store.update_status(["LinkedIn|2"], "Interview Ongoing")
    assert listed(client, "status=Applied&status=Interview+Ongoing") == ["1", "2"]
    assert listed(client, "status=Applied") == ["1"]
    assert listed(client, "fit=eligible&fit=excluded") == ["1", "2", "3"]
    assert listed(client, f"fetched={day(0)}&fetched={day(-1)}") == ["1", "2", "3"]
    assert listed(client, f"fetched={day(-1)}") == []


def test_fetched_dropdown_lists_recent_days_with_counts(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", fetched_on=to_iso(now_ist()))
    body = text(client.get("/roles"))
    assert f'name="fetched" value="{day(0)}"' in body
    assert "(1)" in body


def test_multi_values_survive_paging_sorting_and_csv(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    for job in "123":
        add_role(store, run_id, job)
    store.update_status(["LinkedIn|1"], "Applied")
    store.update_status(["LinkedIn|2"], "Interview Ongoing")
    body = text(client.get("/roles?status=Applied&status=Interview+Ongoing&per=20"))
    assert body.count("status=Applied&amp;status=Interview+Ongoing") >= 1
    csv = text(client.get("/export.csv?status=Applied&status=Interview+Ongoing"))
    assert "Follow-up Needed" in csv.splitlines()[0]
    assert "jobs/view/3" not in csv
    assert "jobs/view/1" in csv
    assert "jobs/view/2" in csv
