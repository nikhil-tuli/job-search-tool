"""The Naukri changes across the app's pages: Roles, Summary, scoring labels and Eval."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from flask.testing import FlaskClient

from jobscan import db
from jobscan.models import PORTAL_LINKEDIN, PORTAL_NAUKRI
from jobscan.store import RUN_SUCCESS, Store
from jobscan.timeutil import now_ist, to_iso
from jobscan.web import create_app
from tests.test_web import add_role, text


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


@pytest.fixture
def client(store: Store) -> FlaskClient:
    return create_app(store, secret_key="test").test_client()


@pytest.fixture
def both(store: Store) -> Store:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    nrun = store.start_run(window_seconds=86400, keywords_total=1, portal=PORTAL_NAUKRI)
    store.finish_run(run, RUN_SUCCESS)
    store.finish_run(nrun, RUN_SUCCESS)
    add_role(store, run, "1", fetched_on=to_iso(now_ist()))
    add_role(store, nrun, "2", portal=PORTAL_NAUKRI, fetched_on=to_iso(now_ist()))
    add_role(store, nrun, "3", portal=PORTAL_NAUKRI, fetched_on=to_iso(now_ist()))
    return store


def listed(client: FlaskClient, query: str) -> list[str]:
    body = text(client.get(f"/roles?{query}"))
    return sorted({j for j in "123" if f"/jobs/view/{j}" in body})


def test_roles_have_a_portal_column_and_filter(client: FlaskClient, both: Store) -> None:
    body = text(client.get("/roles"))
    assert ">Portal</a></th>" in body
    assert '<td class="portal">Naukri</td>' in body
    assert '<td class="portal">LinkedIn</td>' in body
    assert 'name="portal" value="Naukri"' in body
    assert body.index('name="portal"') < body.index('name="q"')  # before the search box


def test_portal_filter_takes_one_or_both(client: FlaskClient, both: Store) -> None:
    assert listed(client, f"portal={PORTAL_NAUKRI}") == ["2", "3"]
    assert listed(client, f"portal={PORTAL_LINKEDIN}") == ["1"]
    assert listed(client, f"portal={PORTAL_LINKEDIN}&portal={PORTAL_NAUKRI}") == ["1", "2", "3"]
    assert listed(client, "portal=Indeed") == ["1", "2", "3"]  # unknown values are ignored
    ticked = text(client.get(f"/roles?portal={PORTAL_NAUKRI}"))
    assert 'name="portal" value="Naukri" checked' in ticked


def test_portal_sorts_and_exports(client: FlaskClient, both: Store) -> None:
    body = text(client.get("/roles?sort=portal&dir=asc"))
    assert body.index("/jobs/view/1") < body.index("/jobs/view/2")
    csv = text(client.get(f"/export.csv?portal={PORTAL_NAUKRI}"))
    assert "Portal" in csv.splitlines()[0]
    assert "jobs/view/1" not in csv
    assert "jobs/view/2" in csv


def test_the_reminder_shows_only_when_a_naukri_run_is_old(
    client: FlaskClient, store: Store
) -> None:
    assert "jobscan naukri" not in text(client.get("/roles"))
    run = store.start_run(window_seconds=1, keywords_total=1, portal=PORTAL_NAUKRI)
    store.finish_run(run, RUN_SUCCESS)
    assert "jobscan naukri" not in text(client.get("/roles"))
    old = to_iso(now_ist() - timedelta(days=4))
    store._conn.execute("UPDATE runs SET started_on = ? WHERE id = ?", (old, run))
    assert "Naukri last run: 4 days ago" in text(client.get("/roles"))
    assert "Naukri last run: 4 days ago" in text(client.get("/summary"))


def test_summary_selector_limits_the_tables(client: FlaskClient, both: Store) -> None:
    everything = text(client.get("/summary"))
    assert "3 roles" in everything
    assert 'class="chip-link on" href="/summary">All' in everything
    naukri = text(client.get("/summary?portal=Naukri"))
    assert "2 roles" in naukri
    assert "portal=Naukri" in naukri  # the counts link on to the Naukri roles
    linkedin = text(client.get("/summary?portal=LinkedIn"))
    assert "1 roles" in linkedin


def test_scans_table_names_the_portal_and_headings_have_hover_text(
    client: FlaskClient, both: Store
) -> None:
    body = text(client.get("/summary"))
    scans = body.split('<table class="runs">')[1].split("</table>")[0]
    assert '<th title="Portal">Portal</th>' in scans
    assert "<td>Naukri</td>" in scans
    assert "<td>LinkedIn</td>" in scans
    only = text(client.get("/summary?portal=Naukri")).split('<table class="runs">')[1]
    only = only.split("</table>")[0]
    assert "<td>LinkedIn</td>" not in only
    assert 'title="Initial screening rejected"' in body


def test_latest_run_of_each_portal_is_linked(client: FlaskClient, both: Store) -> None:
    body = text(client.get("/summary"))
    assert re.search(r">LinkedIn #1</a>", body)
    assert re.search(r">Naukri #2</a>", body)
    assert "Details of run #2 (Naukri)" in text(client.get("/summary?run=2"))


def test_skipped_titles_name_the_portal(client: FlaskClient, both: Store) -> None:
    run = 2
    both.add_skipped(run, [(PORTAL_NAUKRI, "9", "Plant Manager", "Acme", to_iso(now_ist()))])
    body = text(client.get("/summary"))
    skipped = body.split('id="skipped"')[1]
    assert "<th>Portal</th>" in skipped
    assert "<td>Plant Manager</td><td>Acme</td><td>Naukri</td>" in skipped
    only = text(client.get("/summary?portal=LinkedIn")).split('id="skipped"')[1]
    assert "Plant Manager" not in only


def test_scoring_trigger_says_which_portal_started_it(client: FlaskClient, both: Store) -> None:
    when = to_iso(now_ist())
    both.start_scoring_run("scan", when=when, source=PORTAL_NAUKRI)
    both.start_scoring_run("scan", when=when, source=PORTAL_LINKEDIN)
    both.start_scoring_run("scan", when=when)
    both.start_scoring_run("manual", when=when)
    from jobscan.models import Cv

    both.save_cv(Cv("cv.txt", "x " * 50, "sha", when), [("x", "h")])
    body = text(client.get("/summary"))
    assert "After Naukri run" in body
    assert "After LinkedIn scan" in body
    assert "After a scan" in body
    assert "jobscan score" in body


def test_eval_can_be_limited_to_one_portal(client: FlaskClient, both: Store) -> None:
    both.set_fit_rating(PORTAL_LINKEDIN, "1", "good")
    both.set_fit_rating(PORTAL_NAUKRI, "2", "bad")
    page = text(client.get("/eval"))
    assert "?portal=Naukri" in page
    assert "2 roles" in page
    only = text(client.get("/eval?portal=Naukri"))
    assert "1 roles" in only
    assert 'class="chip-link on" href="/eval?portal=Naukri"' in only
    csv = text(client.get("/eval/rated.csv?portal=Naukri"))
    assert "2" in csv
    assert csv.count("\n") < text(client.get("/eval/rated.csv")).count("\n")


def test_eval_remembers_the_portal_until_you_pick_all(client: FlaskClient, both: Store) -> None:
    both.set_fit_rating(PORTAL_LINKEDIN, "1", "good")
    both.set_fit_rating(PORTAL_NAUKRI, "2", "bad")
    client.get("/eval?portal=Naukri")
    assert "1 roles" in text(client.get("/eval"))
    client.get("/eval?portal=all")
    assert "2 roles" in text(client.get("/eval"))


def test_config_page_has_the_view_picker(client: FlaskClient) -> None:
    body = text(client.get("/config"))
    for label in ("All", "Common", "LinkedIn only", "Naukri only"):
        assert f">{label}</button>" in body
    assert 'data-section="Naukri"' in body
    assert "cfgview.js" in body


def test_the_panel_link_names_the_role_portal(both: Store, client: FlaskClient) -> None:
    naukri = client.get("/roles/Naukri/2?partial=1").get_data(as_text=True)
    linkedin = client.get("/roles/LinkedIn/1?partial=1").get_data(as_text=True)
    assert "Open on Naukri" in naukri
    assert "Open on LinkedIn" not in naukri
    assert "Open on LinkedIn" in linkedin
