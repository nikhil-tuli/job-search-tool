from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from flask.testing import FlaskClient

from jobscan import db
from jobscan.config import Config
from jobscan.models import (
    DEFAULT_PHRASES,
    STATUSES,
    JobDetail,
    KeywordRecord,
    NewRole,
    PageStat,
    RoleFilter,
)
from jobscan.store import RUN_SUCCESS, Store
from jobscan.timeutil import now_ist, to_iso
from jobscan.web import create_app
from jobscan.web.app import GROUP_INFO


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


def add_role(store: Store, run_id: int, job_id: str, **overrides: object) -> None:
    fields: dict[str, object] = {
        "portal": "LinkedIn",
        "job_id": job_id,
        "title": f"Technical Program Manager {job_id}",
        "company": "Acme",
        "location": "Pune, India",
        "link": f"https://www.linkedin.com/jobs/view/{job_id}",
        "posted_on": "2026-10-14T11:00:00+05:30",
        "posted_precision": "hour",
        "fetched_on": f"2026-10-15T09:{int(job_id) % 60:02d}:00+05:30",
        "actively_hiring": False,
        "matched_phrase": "Technical Program Manager",
        "possible_duplicate_of": None,
        "dup_key": None,
        "raw_datetime": "2026-10-14",
        "raw_relative": "22 hours ago",
        "run_id": run_id,
    }
    fields.update(overrides)
    store.insert_role(NewRole(**fields))  # type: ignore[arg-type]


def text(response) -> str:  # type: ignore[no-untyped-def]
    return response.get_data(as_text=True)


# ---- roles list ----------------------------------------------------------------------------


def test_index_redirects_to_roles(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/roles")


def test_empty_roles_page(client: FlaskClient) -> None:
    body = text(client.get("/roles"))
    assert "No roles match" in body
    assert "none yet" in body


def test_roles_page_lists_roles_and_last_run(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", actively_hiring=True, possible_duplicate_of="LinkedIn|9")
    store.finish_run(run_id, RUN_SUCCESS)
    body = text(client.get("/roles"))
    assert "Technical Program Manager 1" in body
    assert "Actively hiring" in body
    assert "Possible duplicate" in body
    assert "2026-10-14 11:00" in body  # posted, IST with time
    assert "2026-10-15 09:01" in body  # fetched
    assert "IST" in body
    assert "none yet" not in body
    assert "https://www.linkedin.com/jobs/view/1" in body


def test_posted_display_by_precision(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", posted_precision="day", posted_on="2026-10-14T09:00:00+05:30")
    add_role(store, run_id, "2", posted_precision="week", posted_on="2026-10-08T09:00:00+05:30")
    add_role(store, run_id, "3", posted_precision="unknown", posted_on=None)
    body = text(client.get("/roles"))
    assert ">2026-10-14<" in body
    assert ">~2026-10-08<" in body
    assert ">unknown<" in body


def test_filters(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", title="Delivery Manager", company="Globex")
    add_role(store, run_id, "2", possible_duplicate_of="LinkedIn|1")
    add_role(store, run_id, "3")
    store.update_status(["LinkedIn|3"], "Applied")
    store.update_status(["LinkedIn|1"], "Not relevant")

    def listed(query: str) -> str:
        body = text(client.get(f"/roles?{query}"))
        return "".join(c for c in ("1", "2", "3") if f'/jobs/view/{c}"' in body)

    assert listed("q=globex") == "1"
    assert listed("status=Applied") == "3"
    assert listed("status=Not relevant") == "1"
    assert listed("status=Not+Applied") == "2"
    assert listed("dup=1") == "2"
    assert listed("") == "123"
    store.update_status(["LinkedIn|2"], "Closed")
    assert listed("status=Closed") == "2"
    store.update_status(["LinkedIn|2"], "Skipped")
    assert listed("status=Skipped") == "2"


def test_invalid_filter_values_are_ignored(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    response = client.get("/roles?status=Bogus&page=abc")
    assert response.status_code == 200
    assert "Technical Program Manager 1" in text(response)


def test_pagination(client: FlaskClient, store: Store, run_id: int) -> None:
    for i in range(1, 106):
        add_role(store, run_id, str(i), fetched_on=f"2026-10-15T09:00:{i % 60:02d}+05:30")
    first = text(client.get("/roles?per=100"))
    second = text(client.get("/roles?per=100&page=2"))
    assert first.count('form="bulk"') == 100  # one selection checkbox per row
    assert second.count('form="bulk"') == 5
    assert first.count("Page 1 of 2") == 2  # pager above and below the table
    assert "Page 2 of 2" in second
    assert "Page 2 of 2" in text(client.get("/roles?per=100&page=999"))
    assert "Page 1 of 2" in text(client.get("/roles?per=100&page=0"))


def test_rows_per_page_defaults_to_20_and_is_remembered(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    for i in range(1, 106):
        add_role(store, run_id, str(i), fetched_on=f"2026-10-15T09:00:{i % 60:02d}+05:30")
    default = text(client.get("/roles"))
    assert default.count('form="bulk"') == 20
    assert "Page 1 of 6" in default
    assert 'name="per"' in default
    assert text(client.get("/roles?per=50")).count('form="bulk"') == 50
    assert text(client.get("/roles")).count('form="bulk"') == 50  # remembered
    assert text(client.get("/roles?per=7")).count('form="bulk"') == 50  # invalid: keeps choice
    assert "per=50" in text(client.get("/roles?per=50"))  # pager links keep the size
    assert text(client.get("/roles?per=20")).count('form="bulk"') == 20


def test_changing_rows_per_page_does_not_keep_the_page(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    for i in range(1, 60):
        add_role(store, run_id, str(i), fetched_on=f"2026-10-15T09:00:{i % 60:02d}+05:30")
    assert "Page 1 of 3" in text(client.get("/roles"))
    # The dropdown submits the form, which has no page field, so it lands on page 1.
    assert "Page 1 of 2" in text(client.get("/roles?per=50"))


def test_search_term_is_escaped_in_the_page(client: FlaskClient) -> None:
    body = text(client.get('/roles?q="><script>alert(1)</script>'))
    assert "<script>alert(1)</script>" not in body
    assert "&#34;&gt;&lt;script&gt;" in body


def test_titles_and_notes_are_escaped(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", title="<script>alert(1)</script> TPM", company="<b>Evil</b>")
    store.update_details(
        "LinkedIn",
        "1",
        status="Not Applied",
        applied_on=None,
        contact_person="<i>x</i>",
        comments="<img src=x onerror=alert(1)>",
    )
    for url in ("/roles", "/roles/LinkedIn/1"):
        body = text(client.get(url))
        assert "<script>alert(1)</script>" not in body
        assert "<b>Evil</b>" not in body
        assert "<img src=x" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text(client.get("/roles"))


def test_non_https_link_is_not_rendered(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", link="javascript:alert(1)")
    assert "javascript:alert" not in text(client.get("/roles"))


# ---- status updates ------------------------------------------------------------------------


def test_single_status_update(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status",
        data={"ids": "LinkedIn|1", "status": "Applied", "next": "/roles?q=x"},
        follow_redirects=True,
    )
    assert "1 role(s) set to Applied" in text(response)
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.status == "Applied"
    assert role.applied_on is not None
    assert role.applied_on.endswith("+05:30")


def test_status_post_redirects_to_next(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status", data={"ids": "LinkedIn|1", "status": "Applied", "next": "/runs"}
    )
    assert response.headers["Location"].endswith("/runs")


@pytest.mark.parametrize("target", ["https://evil.com", "//evil.com", "javascript:alert(1)"])
def test_open_redirect_is_blocked(
    client: FlaskClient, store: Store, run_id: int, target: str
) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status", data={"ids": "LinkedIn|1", "status": "Applied", "next": target}
    )
    assert response.headers["Location"].endswith("/roles")


def test_bulk_status_update(client: FlaskClient, store: Store, run_id: int) -> None:
    for i in "123":
        add_role(store, run_id, i)
    client.post(
        "/roles/status",
        data={"ids": ["LinkedIn|1", "LinkedIn|3"], "status": "Interview Ongoing"},
    )
    assert store.count_roles(RoleFilter(status="Interview Ongoing")) == 2
    assert store.count_roles(RoleFilter(status="Not Applied")) == 1


def test_bulk_update_with_unknown_id_changes_nothing(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status",
        data={"ids": ["LinkedIn|1", "LinkedIn|404"], "status": "Applied"},
        follow_redirects=True,
    )
    assert "unknown role" in text(response)
    assert store.count_roles(RoleFilter(status="Applied")) == 0


def test_no_selection_is_reported(client: FlaskClient) -> None:
    response = client.post("/roles/status", data={"status": "Applied"}, follow_redirects=True)
    assert "Select at least one role" in text(response)


def test_invalid_status_is_reported(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status", data={"ids": "LinkedIn|1", "status": "Bogus"}, follow_redirects=True
    )
    assert "unknown status" in text(response)
    assert store.count_roles(RoleFilter(status="Not Applied")) == 1


# ---- role page -----------------------------------------------------------------------------


def test_role_page_shows_details(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", possible_duplicate_of="LinkedIn|9")
    body = text(client.get("/roles/LinkedIn/1?next=/roles%3Fq%3Dx"))
    assert "Technical Program Manager 1" in body
    assert "22 hours ago" in body
    assert "LinkedIn|9" in body
    assert 'value="/roles?q=x"' in body


def test_role_partial_is_form_only(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    body = text(client.get("/roles/LinkedIn/1?partial=1&next=/roles"))
    assert "<html" not in body
    assert "Back" not in body
    assert 'action="/roles/LinkedIn/1"' in body
    assert "Technical Program Manager 1" in body


def test_roles_rows_carry_panel_urls(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    body = text(client.get("/roles"))
    assert 'data-panel="/roles/LinkedIn/1?next=' in body
    assert "partial=1" in body
    assert "panel.js" in body
    script = client.get("/static/panel.js")
    script.close()
    assert script.status_code == 200


def test_panel_save_returns_json_and_stores(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/LinkedIn/1",
        data={"status": "Applied", "applied_on": "", "contact_person": "Asha", "comments": "hi"},
        headers={"X-Panel": "1"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "Applied"
    assert payload["applied_on"]
    saved = store.get_role("LinkedIn", "1")
    assert saved is not None
    assert saved.contact_person == "Asha"


def test_panel_save_validation_error_returns_fragment(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/LinkedIn/1",
        data={
            "status": "Applied",
            "applied_on": "not a date",
            "contact_person": "",
            "comments": "",
        },
        headers={"X-Panel": "1"},
    )
    assert response.status_code == 400
    body = text(response)
    assert "<html" not in body
    assert 'value="not a date"' in body
    saved = store.get_role("LinkedIn", "1")
    assert saved is not None
    assert saved.status == "Not Applied"


def test_background_status_change_returns_json(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status", data={"ids": "LinkedIn|1", "status": "Applied"}, headers={"X-Panel": "1"}
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "Applied"
    assert payload["applied_on"]["LinkedIn|1"]
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.status == "Applied"


def test_background_status_change_errors_are_json_and_change_nothing(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    bad = client.post(
        "/roles/status", data={"ids": "LinkedIn|1", "status": "Bogus"}, headers={"X-Panel": "1"}
    )
    assert bad.status_code == 400
    assert "unknown status" in bad.get_json()["error"]
    empty = client.post("/roles/status", data={"status": "Applied"}, headers={"X-Panel": "1"})
    assert empty.status_code == 400
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.status == "Not Applied"


def test_roles_page_loads_autosave_and_scroll_scripts(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    body = text(client.get("/roles?status=Not+Applied"))
    assert "autosave.js" in body
    assert "scroll.js" in body
    assert 'data-filter-status="Not Applied"' in body
    assert "data-autosave" in body
    assert "onchange" not in body
    for name in ("autosave.js", "scroll.js", "flash.js"):
        script = client.get(f"/static/{name}")
        script.close()
        assert script.status_code == 200


def test_unknown_role_is_404(client: FlaskClient) -> None:
    assert client.get("/roles/LinkedIn/404").status_code == 404
    assert client.post("/roles/LinkedIn/404", data={"status": "Applied"}).status_code == 404


def test_role_save(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/LinkedIn/1",
        data={
            "status": "Applied",
            "applied_on": "2026-10-10",
            "contact_person": "Priya",
            "comments": "call Monday",
            "next": "/roles",
        },
        follow_redirects=True,
    )
    assert "Saved." in text(response)
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.status, role.contact_person) == ("Applied", "Priya")
    assert role.applied_on == "2026-10-10T00:00:00+05:30"


def test_role_save_applied_without_date_uses_today(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    client.post("/roles/LinkedIn/1", data={"status": "Applied"})
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.applied_on is not None
    assert role.applied_on[:10] == now_ist().strftime("%Y-%m-%d")


def test_role_save_not_applied_clears_date(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    store.update_status(["LinkedIn|1"], "Applied")
    client.post("/roles/LinkedIn/1", data={"status": "Not Applied", "applied_on": "2026-10-10"})
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.applied_on is None


def test_role_save_validation_error_keeps_input_and_saves_nothing(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/LinkedIn/1",
        data={"status": "Applied", "applied_on": "yesterday", "comments": "keep me"},
    )
    body = text(response)
    assert response.status_code == 400
    assert "Use the format YYYY-MM-DD" in body
    assert "keep me" in body
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert (role.status, role.comments) == ("Not Applied", "")


# ---- run log -------------------------------------------------------------------------------


def test_empty_run_log(client: FlaskClient) -> None:
    body = text(client.get("/summary"))
    assert "No runs yet" in body


def seed_run(store: Store) -> int:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    store.record_keyword(
        run,
        KeywordRecord("Technical Program Manager", pages=2, cards=20, matched=8, new_roles=5,
                      possible_dups=1, throttled=3, retries=3, completed=True, stopped_by="end"),
    )  # fmt: skip
    store.record_pages(run, "Technical Program Manager", [PageStat(0, 10, 6), PageStat(1, 10, 2)])
    store.add_phrase_matches(run, {"Technical Program Manager": 8})
    store.add_skipped(run, [("LinkedIn", "9", "<b>Chef</b>", "Kitchen", fresh())])
    store.finish_run(run, RUN_SUCCESS, "all good")
    return run


def test_summary_shows_run_details(client: FlaskClient, store: Store) -> None:
    seed_run(store)
    body = text(client.get("/summary"))
    assert "Details of run #1" in body
    assert "40% of cards matched" in body
    assert "60%" in body
    assert "20%" in body
    assert "Kitchen" in body
    assert "&lt;b&gt;Chef&lt;/b&gt;" in body
    assert "<b>Chef</b>" not in body
    assert "all good" in body
    assert "end" in body


def test_run_details_start_closed_and_open_for_a_chosen_run(
    client: FlaskClient, store: Store
) -> None:
    seed_run(store)
    seed_run(store)
    assert '<details id="run-detail" class="run-detail">' in text(client.get("/summary"))
    chosen = text(client.get("/summary?run=1"))
    assert '<details id="run-detail" class="run-detail" open>' in chosen
    assert "Details of run #1" in chosen
    assert "Details of run #2" in text(client.get("/summary"))  # the latest by default
    assert "Details of run #2" in text(client.get("/summary?run=999"))
    assert client.get("/summary?run=abc").status_code == 200


def test_scans_table_shows_ten_runs_and_a_link_for_more(client: FlaskClient, store: Store) -> None:
    for _ in range(12):
        seed_run(store)
    short = text(client.get("/summary"))
    assert short.count('class="status success"') >= 10
    assert "Show more runs" in short
    assert "#2<" not in short.split('id="scans"')[1].split("Details of run")[0]  # run 2 is cut
    longer = text(client.get("/summary?runs=all"))
    assert "Show fewer runs" in longer
    assert "Show more runs" not in longer
    assert ">#1<" in longer


def test_old_run_log_address_redirects_to_the_summary(client: FlaskClient) -> None:
    plain = client.get("/runs")
    assert plain.status_code == 302
    assert plain.headers["Location"].endswith("/summary#scans")
    chosen = client.get("/runs?run=3")
    assert chosen.headers["Location"].endswith("/summary?run=3#run-detail")


def test_nav_has_no_run_log_tab(client: FlaskClient) -> None:
    assert "Run Log" not in text(client.get("/summary"))


# ---- config --------------------------------------------------------------------------------


def config_post(**overrides: str) -> dict[str, str]:
    form = Config().to_raw()
    form["phrase_count"] = str(len(DEFAULT_PHRASES))
    for i, phrase in enumerate(DEFAULT_PHRASES):
        form[f"phrase_{i}"] = phrase
        form[f"keyword_{i}"] = "1"
        form[f"match_{i}"] = "1"
    form.update(overrides)
    return form


def test_config_page_shows_phrases_and_settings(client: FlaskClient) -> None:
    body = text(client.get("/config"))
    for phrase in DEFAULT_PHRASES:
        assert f'value="{phrase}"' in body
    assert 'name="max_pages_per_keyword" value="10"' in body
    assert "86400" in body


def test_config_save_persists_settings_and_phrases(client: FlaskClient, store: Store) -> None:
    form = config_post(
        max_pages_per_keyword="5",
        match_mode="exact",
        phrase_0="Program Lead",
        remove_1="1",
        phrase_count=str(len(DEFAULT_PHRASES) + 1),
        **{
            f"phrase_{len(DEFAULT_PHRASES)}": "Scrum Master",
            f"keyword_{len(DEFAULT_PHRASES)}": "1",
        },
    )
    response = client.post("/config", data=form, follow_redirects=True)
    assert "Configuration saved" in text(response)
    config = Config.from_raw(store.get_config_raw())
    assert (config.max_pages_per_keyword, config.match_mode) == (5, "exact")
    phrases = [p.phrase for p in store.list_phrases()]
    assert phrases[0] == "Program Lead"
    assert "Senior Technical Program Manager" not in phrases
    assert phrases[-1] == "Scrum Master"
    scrum = store.list_phrases()[-1]
    assert (scrum.use_as_keyword, scrum.use_for_match) == (True, False)


def test_config_save_rejects_invalid_input_and_saves_nothing(
    client: FlaskClient, store: Store
) -> None:
    form = config_post(max_pages_per_keyword="0", retry_wait_min="90", retry_wait_max="30",
                       phrase_0="Changed")  # fmt: skip
    response = client.post("/config", data=form)
    body = text(response)
    assert response.status_code == 400
    assert body.count('class="error"') >= 2
    assert 'value="Changed"' in body  # input is kept for correction
    assert store.get_config_raw() == {}
    assert [p.phrase for p in store.list_phrases()] == list(DEFAULT_PHRASES)


def test_config_save_requires_a_keyword_phrase(client: FlaskClient, store: Store) -> None:
    form = config_post()
    for i in range(len(DEFAULT_PHRASES)):
        form.pop(f"keyword_{i}")
    response = client.post("/config", data=form)
    assert response.status_code == 400
    assert "at least one phrase" in text(response)
    assert all(p.use_as_keyword for p in store.list_phrases())


def test_config_page_survives_invalid_stored_settings(client: FlaskClient, store: Store) -> None:
    store.set_config_raw({"max_pages_per_keyword": "banana"})
    response = client.get("/config")
    assert response.status_code == 200
    assert "Stored settings were invalid" in text(response)


# ---- csv export ----------------------------------------------------------------------------


def test_csv_export(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", title="=cmd|' /C calc'!A0 Program Manager")
    add_role(store, run_id, "2", title="Delivery Manager")
    response = client.get("/export.csv")
    assert response.mimetype == "text/csv"
    assert 'attachment; filename="roles_' in response.headers["Content-Disposition"]
    body = text(response)
    assert body.startswith("Job ID,Title,Company")
    assert "'=cmd|" in body
    assert ",=cmd" not in body
    assert body.count("\r\n") == 3


def test_csv_export_respects_filters(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", title="Delivery Manager")
    add_role(store, run_id, "2")
    body = text(client.get("/export.csv?q=delivery"))
    assert "Delivery Manager" in body
    assert "Technical Program Manager 2" not in body


# ---- request guards ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.com"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_site_posts_are_refused(
    client: FlaskClient, store: Store, run_id: int, headers: dict[str, str]
) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status", data={"ids": "LinkedIn|1", "status": "Applied"}, headers=headers
    )
    assert response.status_code == 403
    assert store.count_roles(RoleFilter(status="Applied")) == 0


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://localhost"},
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
    ],
)
def test_same_site_posts_are_allowed(
    client: FlaskClient, store: Store, run_id: int, headers: dict[str, str]
) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status", data={"ids": "LinkedIn|1", "status": "Applied"}, headers=headers
    )
    assert response.status_code == 302


def test_unexpected_host_is_refused(store: Store) -> None:
    client = create_app(store, secret_key="t").test_client()
    assert client.get("/roles", base_url="http://evil.example").status_code == 400
    assert client.get("/roles", base_url="http://127.0.0.1:5000").status_code == 200
    assert client.get("/roles", base_url="http://[::1]:5000").status_code == 200


def test_extra_allowed_host(store: Store) -> None:
    client = create_app(store, secret_key="t", allowed_hosts={"mypc.local"}).test_client()
    assert client.get("/roles", base_url="http://mypc.local:5000").status_code == 200
    assert client.get("/roles", base_url="http://localhost").status_code == 400


def test_security_headers(client: FlaskClient) -> None:
    headers = client.get("/roles").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "same-origin"


def test_error_page_for_unknown_url(client: FlaskClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert "404" in text(response)


def test_static_stylesheet_is_served(client: FlaskClient) -> None:
    response = client.get("/static/style.css")
    response.close()
    assert response.status_code == 200


def test_config_save_reports_store_level_failures(
    client: FlaskClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobscan.store import StoreError

    def refuse(_phrases: object) -> None:
        raise StoreError("duplicate phrase: x")

    monkeypatch.setattr(store, "replace_phrases", refuse)
    response = client.post("/config", data=config_post(max_pages_per_keyword="7"))
    assert response.status_code == 400
    assert "duplicate phrase: x" in text(response)
    assert store.get_config_raw() == {}  # settings rolled back with the phrases


def test_browser_form_post_with_opaque_origin_is_allowed_when_fetch_metadata_says_same_origin(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    # Regression: a browser can send "Origin: null" on a same-origin form post.
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status",
        data={"ids": "LinkedIn|1", "status": "Applied"},
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 302
    assert store.count_roles(RoleFilter(status="Applied")) == 1


def test_opaque_origin_without_fetch_metadata_is_refused(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status",
        data={"ids": "LinkedIn|1", "status": "Applied"},
        headers={"Origin": "null"},
    )
    assert response.status_code == 403
    assert store.count_roles(RoleFilter(status="Applied")) == 0


def test_fetch_metadata_cross_site_wins_over_a_matching_origin(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    response = client.post(
        "/roles/status",
        data={"ids": "LinkedIn|1", "status": "Applied"},
        headers={"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def test_status_dropdowns_are_sorted_a_to_z(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    for url in ("/roles", "/roles/LinkedIn/1?partial=1"):
        body = text(client.get(url))
        select = body.split('name="status"')[1] if "partial" in url else body
        options = re.findall(r'<option value="([^"]+)"', select)
        picked = [o for o in options if o in STATUSES]
        assert picked[: len(STATUSES)] == sorted(STATUSES, key=str.casefold)


def test_new_roles_still_default_to_not_applied(store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.status == "Not Applied"


# ---- summary tab and fetch-date filter -------------------------------------------------------


def _add_on(store: Store, run_id: int, job_id: str, day: str, status: str = "Not Applied") -> None:
    add_role(store, run_id, job_id, fetched_on=f"{day}T09:00:00+05:30")
    if status != "Not Applied":
        store.update_status([f"LinkedIn|{job_id}"], status)


def test_summary_empty_state(client: FlaskClient) -> None:
    assert "No roles yet" in text(client.get("/summary"))


def test_summary_lists_every_status_in_workflow_order(client: FlaskClient, store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    _add_on(store, run, "1", "2026-10-15")
    body = text(client.get("/summary"))
    headers = re.findall(r'<th class="num">([^<]+)</th>', body)
    assert headers == ["Fetched", "Took", *STATUSES]
    assert 'href="/summary"' in body


def test_summary_numbers_and_links(client: FlaskClient, store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    _add_on(store, run, "1", "2026-10-15")
    _add_on(store, run, "2", "2026-10-15", "Applied")
    _add_on(store, run, "3", "2026-10-17", "Skipped")
    body = text(client.get("/summary"))
    assert "2026-10-16" in body  # the gap day is shown
    assert 'class="quiet"' in body
    assert 'href="/roles?fetched=2026-10-15"' in body
    assert 'href="/roles?fetched=2026-10-15&amp;status=Applied"' in body
    assert 'href="/roles?fetched=2026-10-17&amp;status=Skipped"' in body
    assert 'href="/roles?status=Applied"' in body  # totals row
    assert '<a href="/roles">3</a>' in body  # grand total


def test_every_summary_link_lands_on_the_counted_roles(client: FlaskClient, store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    for number, status in enumerate(STATUSES, start=1):
        _add_on(store, run, str(number), "2026-10-15", status)
        _add_on(store, run, str(number + 20), "2026-10-16", status)
    summary = text(client.get("/summary"))
    links = re.findall(r'<a href="(/roles\?[^"]+)">(\d+)</a>', summary)
    assert len(links) >= 2 * (len(STATUSES) + 1)
    for href, count in links:
        page = text(client.get(href.replace("&amp;", "&")))
        assert f'<h1>Roles <span class="muted">{count}</span>' in page, href


def test_roles_fetch_date_filter_chip_and_clear(client: FlaskClient, store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    _add_on(store, run, "1", "2026-10-15")
    _add_on(store, run, "2", "2026-10-16")
    body = text(client.get("/roles?fetched=2026-10-15"))
    assert "/jobs/view/1" in body
    assert "/jobs/view/2" not in body
    assert 'name="fetched" value="2026-10-15" checked' in body


def test_invalid_fetch_date_is_ignored(client: FlaskClient, store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    _add_on(store, run, "1", "2026-10-15")
    response = client.get("/roles?fetched=not-a-date")
    assert response.status_code == 200
    body = text(response)
    assert "/jobs/view/1" in body
    assert "Fetched not-a-date" not in body


def test_csv_export_honours_fetch_date(client: FlaskClient, store: Store) -> None:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    _add_on(store, run, "1", "2026-10-15")
    _add_on(store, run, "2", "2026-10-16")
    body = text(client.get("/export.csv?fetched=2026-10-15"))
    assert "/jobs/view/1" in body
    assert "/jobs/view/2" not in body


# ---- job descriptions ----------------------------------------------------------------------


def _describe(store: Store, job_id: str, text: str, **criteria: str) -> None:
    store.save_detail(
        "LinkedIn", job_id, JobDetail(text, **criteria), when="2026-10-16T09:00:00+05:30"
    )


def test_role_page_without_a_description_says_so(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    body = text(client.get("/roles/LinkedIn/1"))
    assert "No description saved yet" in body


def test_role_page_and_panel_show_description_and_criteria(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    _describe(
        store,
        "1",
        "Own delivery.\n\n- Plan\n- Ship",
        seniority="Director",
        employment_type="Full-time",
        industries="Software",
    )
    for url in ("/roles/LinkedIn/1", "/roles/LinkedIn/1?partial=1"):
        body = text(client.get(url))
        assert 'class="desc"' in body
        assert "Own delivery." in body
        assert "- Plan" in body
        assert "Director" in body
        assert "Full-time" in body
        assert "Software" in body
        assert "No description saved yet" not in body


def test_description_markup_is_escaped(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    _describe(store, "1", "<script>alert(1)</script> & <b>bold</b>")
    body = text(client.get("/roles/LinkedIn/1"))
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in body


def test_saving_a_role_keeps_its_description(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    _describe(store, "1", "Keep me")
    client.post(
        "/roles/LinkedIn/1",
        data={"status": "Applied", "applied_on": "", "contact_person": "", "comments": "x"},
    )
    role = store.get_role("LinkedIn", "1")
    assert role is not None
    assert role.description == "Keep me"


def test_roles_search_finds_text_in_the_description(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1")
    add_role(store, run_id, "2")
    _describe(store, "2", "Must know Kubernetes")
    body = text(client.get("/roles?q=kubernetes"))
    assert "/jobs/view/2" in body
    assert "/jobs/view/1" not in body
    assert "description" in text(client.get("/roles"))


def test_config_page_lists_the_description_settings(client: FlaskClient) -> None:
    body = text(client.get("/config"))
    assert 'name="detail_max_per_run" value="60"' in body
    assert 'name="detail_max_age_days" value="3"' in body
    assert "Job descriptions" in body


# ---- fit filter ----------------------------------------------------------------------------


def fresh() -> str:
    return to_iso(now_ist())


def test_fit_column_tags_roles(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", fetched_on=fresh())
    add_role(store, run_id, "2", fetched_on=fresh(), company="Amazon Development Centre")
    add_role(store, run_id, "3")  # fetched days ago
    body = text(client.get("/roles"))
    assert ">Fit</a></th>" in body
    assert body.count('muted">Eligible<') == 1
    assert "Excluded: company" in body
    assert "Excluded: recency" in body
    assert "company matches Amazon" in body  # the full reason is on hover


def test_fit_tag_counts_extra_reasons(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", fetched_on=fresh(), company="Intuit", title="Junior Sales PM")
    body = text(client.get("/roles"))
    assert "Excluded: company" in body
    assert ">+2<" in body


def test_fit_filter_eligible_and_excluded(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", fetched_on=fresh(), title="Technical Program Manager One")
    add_role(store, run_id, "2", fetched_on=fresh(), company="Amazon", title="Program Two")
    eligible = text(client.get("/roles?fit=eligible"))
    assert "Technical Program Manager One" in eligible
    assert "Program Two" not in eligible
    excluded = text(client.get("/roles?fit=excluded"))
    assert "Program Two" in excluded
    assert "Technical Program Manager One" not in excluded
    assert 'name="fit" value="excluded" checked' in excluded
    assert 'Roles <span class="muted">1</span>' in excluded


def test_fit_filter_ignores_unknown_values(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    body = text(client.get("/roles?fit=banana"))
    assert "Technical Program Manager 1" in body


def test_fit_filter_combines_with_other_filters(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", fetched_on=fresh(), title="Alpha Program Manager")
    add_role(store, run_id, "2", fetched_on=fresh(), title="Beta Program Manager")
    body = text(client.get("/roles?fit=eligible&q=Alpha"))
    assert "Alpha Program Manager" in body
    assert "Beta Program Manager" not in body


def test_fit_filter_paginates_after_filtering(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    for n in range(1, 106):
        add_role(store, run_id, str(n), fetched_on=fresh(), company="Amazon")
    add_role(store, run_id, "900", fetched_on=fresh(), title="Lone Eligible Role")
    page_one = text(client.get("/roles?fit=excluded&per=100"))
    page_two = text(client.get("/roles?fit=excluded&per=100&page=2"))
    assert "Page 1 of 2" in page_one
    assert page_two.count('class="row"') == 5
    assert "Lone Eligible Role" not in page_one + page_two
    assert "fit=excluded" in page_one  # the pager keeps the filter


def test_fit_uses_the_saved_settings(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", fetched_on=fresh(), company="Initech")
    assert 'muted">Eligible<' in text(client.get("/roles"))
    store.set_config_raw({"fit_excluded_companies": "Initech"})
    assert "Excluded: company" in text(client.get("/roles"))


def test_fit_falls_back_to_defaults_when_settings_are_invalid(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", fetched_on=fresh())
    store.set_config_raw({"fit_min_years": "banana"})
    assert client.get("/roles").status_code == 200


def test_fit_years_rule_uses_the_description(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", fetched_on=fresh())
    store.save_detail("LinkedIn", "1", JobDetail(description="Experience: 1-2 years"), when=fresh())
    body = text(client.get("/roles"))
    assert "Excluded: years" in body
    assert "asks for 1 year, below 3" in body


def test_panel_shows_why_a_role_is_excluded(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", fetched_on=fresh(), company="Intuit")
    store.update_status(["LinkedIn|1"], "Skipped")
    body = text(client.get("/roles/LinkedIn/1?partial=1"))
    assert "Fit filter" in body
    assert "Excluded: status" in body
    assert "<li>status is Skipped</li>" in body
    assert "<li>company matches Intuit</li>" in body


def test_panel_says_eligible(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", fetched_on=fresh())
    body = text(client.get("/roles/LinkedIn/1?partial=1"))
    assert "Fit filter" in body
    assert "Eligible" in body


def test_role_page_shows_the_fit_result_too(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", fetched_on=fresh(), company="Amazon")
    assert "company matches Amazon" in text(client.get("/roles/LinkedIn/1"))


def test_invalid_save_still_shows_the_fit_result(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", fetched_on=fresh(), company="Amazon")
    response = client.post("/roles/LinkedIn/1", data={"status": "Banana", "next": "/roles"})
    assert response.status_code == 400
    assert "company matches Amazon" in text(response)


def test_export_follows_the_fit_filter(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", fetched_on=fresh(), title="Keep Program Manager")
    add_role(store, run_id, "2", fetched_on=fresh(), company="Amazon", title="Drop Program Manager")
    body = text(client.get("/export.csv?fit=eligible"))
    assert "Keep Program Manager" in body
    assert "Drop Program Manager" not in body


def test_config_page_has_the_fit_group(client: FlaskClient) -> None:
    body = text(client.get("/config"))
    assert "<h2>Fit filters" in body
    assert 'name="fit_recency_days" value="1"' in body
    assert 'name="fit_min_years" value="3"' in body
    assert 'name="fit_excluded_companies" value="Amazon, Intuit"' in body
    assert 'name="fit_deny_keywords" value="sales, warehouse, SAP functional"' in body
    assert 'name="fit_skip_statuses" value="Applied, Closed, Skipped, Not relevant"' in body


def test_config_save_stores_fit_settings_tidily(client: FlaskClient, store: Store) -> None:
    form = config_post(
        fit_excluded_companies=" Foo ,Bar,, foo ",
        fit_skip_statuses="applied, closed",
        fit_min_years="5",
    )
    response = client.post("/config", data=form, follow_redirects=True)
    assert "Configuration saved" in text(response)
    config = Config.from_raw(store.get_config_raw())
    assert config.fit_excluded_companies == "Foo, Bar"
    assert config.fit_skip_statuses == "Applied, Closed"
    assert config.fit_min_years == 5


def test_config_save_rejects_an_unknown_status(client: FlaskClient, store: Store) -> None:
    response = client.post("/config", data=config_post(fit_skip_statuses="Applied, Banana"))
    assert response.status_code == 400
    assert "not a status: Banana" in text(response)
    assert store.get_config_raw() == {}


def test_config_fit_group_has_info_icon(client: FlaskClient) -> None:
    html = client.get("/config").get_data(as_text=True)
    assert html.count('class="info"') == len(GROUP_INFO)
    assert "apply immediately" in html


def order(page: str, ids: list[str]) -> list[str]:
    return sorted(ids, key=lambda j: page.index(f'/jobs/view/{j}"'))


def test_headers_sort_columns_and_flip_direction(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", company="Beta", title="Gamma role")
    add_role(store, run_id, "2", company="alpha", title="Zed role")
    add_role(store, run_id, "3", company="Cleo", title="Alpha role")
    ids = ["1", "2", "3"]
    by_company = text(client.get("/roles?sort=company"))  # first click: A to Z
    assert order(by_company, ids) == ["2", "1", "3"]
    assert "sort=company&amp;dir=desc" in by_company  # next click flips it
    assert 'aria-sort="ascending"' in by_company
    desc = text(client.get("/roles?sort=company&dir=desc"))
    assert order(desc, ids) == ["3", "1", "2"]
    assert "sort=company&amp;dir=asc" in desc
    assert order(text(client.get("/roles?sort=title")), ids) == ["3", "1", "2"]
    # With no sort the list is newest fetched first, shown on the Fetched header.
    assert 'aria-sort="descending"' in text(client.get("/roles"))


def test_sorting_puts_empty_values_last_and_spans_pages(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    for i in range(1, 31):
        add_role(store, run_id, str(i), company=f"Co {i:02d}")
    add_role(store, run_id, "99", company="", fetched_on="2026-10-14T08:00:00+05:30")
    first = text(client.get("/roles?sort=company&dir=desc&per=20"))
    assert '/jobs/view/30"' in first
    assert '/jobs/view/99"' not in first
    last = text(client.get("/roles?sort=company&dir=desc&per=20&page=2"))
    assert last.index('/jobs/view/99"') > last.index('/jobs/view/1"')  # empty company last
    assert "sort=company" in first  # the pager keeps the sort
    assert "dir=desc" in first


def test_bad_sort_values_are_ignored(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1")
    assert client.get("/roles?sort=bogus&dir=sideways").status_code == 200


def test_summary_shows_descriptions_pending(client: FlaskClient, store: Store, run_id: int) -> None:
    add_role(store, run_id, "1", fetched_on=fresh())
    add_role(store, run_id, "2", fetched_on=fresh())
    assert "Descriptions pending: 2 (roles from the last 3 days)" in text(client.get("/summary"))


def skip_rows(store: Store, run: int, ids: range, fetched_on: str) -> None:
    store.add_skipped(
        run, [("LinkedIn", str(i), f"Skipped title {i}", "Co", fetched_on) for i in ids]
    )


def test_skipped_titles_show_ten_then_more_on_request(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    skip_rows(store, run_id, range(1, 26), fresh())
    body = text(client.get("/summary"))
    assert "Skipped titles, last 7 days (25)" in body
    assert body.count("Skipped title ") == 10
    assert "Show more" in body
    assert '<details id="skipped" class="run-detail">' in body  # closed until asked
    more = text(client.get("/summary?skipped=all"))
    assert more.count("Skipped title ") == 25
    assert '<details id="skipped" class="run-detail" open>' in more
    assert "Show fewer" in more


def test_skipped_titles_span_runs_without_repeats_and_ignore_old_ones(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    second = store.start_run(window_seconds=86400, keywords_total=1)
    skip_rows(store, run_id, range(1, 4), fresh())
    skip_rows(store, second, range(2, 6), fresh())  # 2 and 3 were seen again
    skip_rows(store, second, range(90, 92), "2026-01-01T09:00:00+05:30")  # too old
    body = text(client.get("/summary"))
    assert "Skipped titles, last 7 days (5)" in body
    assert body.count("Skipped title ") == 5
    assert "Skipped title 90" not in body
    assert "Skipped title 1<" in body  # from the first run, still listed


def test_skipped_titles_are_not_part_of_the_run_details(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    skip_rows(store, run_id, range(1, 3), fresh())
    body = text(client.get("/summary"))
    detail = body.split('id="run-detail"')[1].split("</details>")[0]
    assert "Skipped title" not in detail
    assert "Match rate by page depth" in detail
    assert "Matches by phrase" in detail


def test_a_background_status_save_returns_the_new_fit(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", fetched_on=fresh())
    assert 'muted">Eligible<' in text(client.get("/roles"))
    response = client.post(
        "/roles/status",
        data={"ids": "LinkedIn|1", "status": "Not relevant"},
        headers={"X-Panel": "1"},
    )
    cell = response.get_json()["fit"]["LinkedIn|1"]
    assert "Excluded: status" in cell
    assert "Eligible" not in cell
    back = client.post(
        "/roles/status",
        data={"ids": "LinkedIn|1", "status": "Not Applied"},
        headers={"X-Panel": "1"},
    )
    assert "Eligible" in back.get_json()["fit"]["LinkedIn|1"]


def test_a_panel_save_returns_the_new_fit_cell_and_detail(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", fetched_on=fresh())
    response = client.post(
        "/roles/LinkedIn/1",
        data={"status": "Skipped", "applied_on": "", "contact_person": "", "comments": ""},
        headers={"X-Panel": "1"},
    )
    fit = response.get_json()["fit"]
    assert "Excluded: status" in fit["cell"]
    assert "status is Skipped" in fit["detail"]


def test_the_roles_page_marks_the_fit_cell_for_updates(
    client: FlaskClient, store: Store, run_id: int
) -> None:
    add_role(store, run_id, "1", fetched_on=fresh())
    assert '<td class="fit">' in text(client.get("/roles"))
    assert "data-fit-detail" in text(client.get("/roles/LinkedIn/1?partial=1"))
