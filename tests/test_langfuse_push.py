"""Sending the AI call log and your ratings to Langfuse."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import requests

from jobscan import cli, db, schedule
from jobscan.ai_fit import AiScorer
from jobscan.config import Config
from jobscan.langfuse_push import (
    LangfuseClient,
    LangfuseError,
    agreement,
    build_span,
    client_from_env,
    push,
    trace_id_of,
)
from jobscan.store import Store
from jobscan.timeutil import to_iso
from tests.test_ai_calls import Informed
from tests.test_ai_fit import GOOD_REPLY, LONG_JD, reply
from tests.test_scoring import NOW, add_role, upload_cv

ON = replace(Config(), langfuse_on=1)


class Session:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], **_: object):  # type: ignore[no-untyped-def]
        self.posts.append((url, json, headers))
        response = requests.Response()
        response.status_code = self.status
        response._content = b"{}"
        return response


def lf(session: Session) -> LangfuseClient:
    return LangfuseClient("https://cloud.langfuse.com", "pk-1", "sk-1", session=session)  # type: ignore[arg-type]


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


def scored(store: Store, replies: list[str] | None = None, roles: int = 1) -> None:
    upload_cv(store)
    for n in range(roles):
        add_role(store, str(n + 1), LONG_JD + f"\n- Extra duty {n}")
    AiScorer(
        store=store,
        config=Config(),
        make_client=lambda _c: Informed(replies or [GOOD_REPLY]),
        emit=lambda _l: None,
        sleep=lambda _s: None,
        clock=lambda: NOW,
    ).run()


def run_push(store: Store, session: Session, config: Config = ON, **kw: Any):  # type: ignore[no-untyped-def]
    return push(
        store, config, lambda _l: None, make_client=lambda _c: lf(session),
        sleep=lambda _s: None, clock=lambda: NOW, **kw,
    )  # fmt: skip


def span_attrs(span: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in span["attributes"]:
        value = a["value"]
        out[a["key"]] = next(iter(value.values()))
    return out


# ---- what is sent ---------------------------------------------------------------------------


def test_one_trace_per_call_with_timing_tokens_and_labels(store: Store) -> None:
    scored(store)
    session = Session()
    result = run_push(store, session)
    assert result.traces == 1
    url, body, headers = session.posts[0]
    assert url == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    assert headers["x-langfuse-ingestion-version"] == "4"
    token = base64.b64decode(headers["Authorization"].removeprefix("Basic ")).decode()
    assert token == "pk-1:sk-1"
    [span] = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    attrs = span_attrs(span)
    assert attrs["langfuse.observation.type"] == "generation"
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {"input": 4210, "output": 360}
    output = json.loads(attrs["langfuse.observation.output"])
    assert output["band"] == "good"
    assert output["must_haves"] == [{"item": "payment gateways", "status": "Met"}]
    assert int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"]) == 3_200_000_000


def test_cv_text_and_the_evidence_line_are_never_sent(store: Store) -> None:
    scored(store)
    session = Session()
    run_push(store, session)
    text = json.dumps(session.posts[0][1])
    assert "Led payment gateway migration" not in text  # the evidence quoted from the CV
    assert "Own delivery item" not in text  # the description stays home by default


def test_the_description_goes_only_when_config_says_so(store: Store) -> None:
    scored(store)
    session = Session()
    run_push(store, session, replace(ON, langfuse_send_text=1))
    assert "Own delivery item" in json.dumps(session.posts[0][1])


def test_a_failed_call_is_sent_as_an_error(store: Store) -> None:
    add_role(store, "1", LONG_JD)
    store.log_ai_call(
        "LinkedIn", "1", when=to_iso(NOW), ok=False, model="m", status=429, error_text="slow down"
    )
    session = Session()
    run_push(store, session)
    [span] = session.posts[0][1]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    attrs = span_attrs(span)
    assert attrs["langfuse.observation.level"] == "ERROR"
    assert attrs["langfuse.observation.status_message"] == "slow down"
    assert span["status"]["code"] == 2


def test_an_older_call_carries_no_verdict(store: Store) -> None:
    """Only the latest good call for a role still has the verdict that call made."""
    scored(store)
    first = store.ai_calls_between("2000-01-01")[0]
    store.log_ai_call("LinkedIn", "1", when=to_iso(NOW + timedelta(minutes=1)), ok=True, model="m")
    fits = store.get_ai_fits()
    span = build_span(first, None, fits.get(first.key), config=Config())
    assert "langfuse.observation.output" in span_attrs(span)  # given the verdict: shown
    older = build_span(first, None, None, config=Config())
    assert "langfuse.observation.output" not in span_attrs(older)


def test_trace_ids_are_fixed_so_a_repeat_makes_no_duplicate(store: Store) -> None:
    scored(store)
    call = store.ai_calls_between("2000-01-01")[0]
    assert trace_id_of(call) == trace_id_of(call)
    assert len(trace_id_of(call)) == 32


# ---- sent once ------------------------------------------------------------------------------


def test_calls_are_sent_once(store: Store) -> None:
    scored(store)
    session = Session()
    run_push(store, session)
    run_push(store, session)
    assert len(session.posts) == 1


def test_a_refusal_leaves_the_calls_unsent_for_next_time(store: Store) -> None:
    scored(store)
    bad = Session(401)
    result = run_push(store, bad)
    assert "refused the keys" in result.stopped
    assert result.traces == 0
    assert len(store.unpushed_ai_calls(10)) == 1
    good = Session()
    assert run_push(store, good).traces == 1


def test_large_backlogs_go_in_batches_of_100(store: Store) -> None:
    add_role(store, "1", LONG_JD)
    for n in range(230):
        store.log_ai_call("LinkedIn", "1", when=to_iso(NOW + timedelta(seconds=n)), ok=True)
    session = Session()
    result = run_push(store, session)
    sizes = [len(p[1]["resourceSpans"][0]["scopeSpans"][0]["spans"]) for p in session.posts]
    assert sizes == [100, 100, 30]
    assert result.traces == 230


def test_missing_keys_send_nothing_and_say_so(store: Store) -> None:
    scored(store)
    result = push(
        store, ON, lambda _l: None,
        make_client=lambda c: client_from_env(c, {}), sleep=lambda _s: None, clock=lambda: NOW,
    )  # fmt: skip
    assert "keys are missing" in result.stopped
    assert len(store.unpushed_ai_calls(10)) == 1


def test_keys_come_from_the_named_variables() -> None:
    config = replace(ON, langfuse_public_env="MY_PK", langfuse_secret_env="MY_SK")
    client = client_from_env(config, {"MY_PK": "pk", "MY_SK": "sk"})
    assert isinstance(client, LangfuseClient)
    with pytest.raises(LangfuseError):
        client_from_env(config, {"MY_PK": "pk"})


# ---- ratings --------------------------------------------------------------------------------


def test_agreement_rules() -> None:
    assert agreement("good", "good") == 1
    assert agreement("bad", "bad") == 1
    assert agreement("good", "bad") == 0
    assert agreement("bad", "good") == 0
    assert agreement("good", "partial") == -1


def rate(store: Store, job_id: str, rating: str) -> None:
    store._conn.execute("UPDATE roles SET fit_rating = ? WHERE job_id = ?", (rating, job_id))


def test_ratings_are_sent_as_scores_once_and_again_when_changed(store: Store) -> None:
    scored(store)
    rate(store, "1", "good")
    session = Session()
    result = run_push(store, session)
    assert result.ratings == 1
    scores = [p for p in session.posts if p[0].endswith("/api/public/scores")]
    assert [(s[1]["name"], s[1]["value"]) for s in scores] == [
        ("human_rating", 1),
        ("agrees_with_ai", 1),
    ]
    assert all(s[1]["traceId"] == trace_id_of(store.ai_calls_between("2000")[0]) for s in scores)
    run_push(store, session)
    assert len([p for p in session.posts if p[0].endswith("/scores")]) == 2  # nothing new
    rate(store, "1", "bad")
    run_push(store, session)
    names = [(p[1]["name"], p[1]["value"]) for p in session.posts if p[0].endswith("/scores")]
    assert names[-2:] == [("human_rating", 0), ("agrees_with_ai", 0)]


def test_a_partial_band_sends_only_the_rating(store: Store) -> None:
    scored(store, [reply([("SAP", "Missing", "")])])
    rate(store, "1", "good")
    session = Session()
    run_push(store, session)
    scores = [p[1]["name"] for p in session.posts if p[0].endswith("/scores")]
    assert scores == ["human_rating"]


def test_the_push_is_remembered_for_the_eval_page(store: Store) -> None:
    scored(store)
    run_push(store, Session())
    state = store.get_push_state()
    assert state is not None
    assert state[1:] == (1, 0, "")


# ---- the command and the daily job ----------------------------------------------------------


def test_the_command_is_off_until_config_says_so(tmp_path: Path) -> None:
    out = io.StringIO()
    code = cli.main(["--db", str(tmp_path / "j.db"), "push-traces"], out=out)
    assert code == cli.EXIT_USAGE
    assert "switched off" in out.getvalue()


def test_the_command_pushes_when_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session()
    monkeypatch.setattr(cli, "LANGFUSE_CLIENT_FACTORY", lambda _c: lf(session))
    conn = db.connect(tmp_path / "j.db")
    db.migrate(conn)
    local = Store(conn)
    local.set_config_raw({"langfuse_on": "1"})
    local.log_ai_call("LinkedIn", "1", when=to_iso(NOW), ok=True, model="m")
    conn.close()
    out = io.StringIO()
    code = cli.main(["--db", str(tmp_path / "j.db"), "push-traces"], out=out, sleep=lambda _s: None)
    assert code == cli.EXIT_OK
    assert "1 traces" in out.getvalue()


class Runner:
    def __init__(self, key: str | None = "secret") -> None:
        self.key = key
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]):  # type: ignore[no-untyped-def]
        import subprocess

        self.calls.append(list(command))
        if command[0] == schedule.SECURITY and self.key:
            return subprocess.CompletedProcess(command, 0, stdout=self.key + "\n", stderr="")
        return subprocess.CompletedProcess(
            command, 44 if command[0] == schedule.SECURITY else 0, "", ""
        )


def daily(store: Store, tmp_path: Path, runner: Runner, codes: list[int | None]):  # type: ignore[no-untyped-def]
    seen: list[tuple[list[str], dict[str, str]]] = []
    notes: list[str] = []

    def spawner(command, env, timeout):  # type: ignore[no-untyped-def]
        seen.append((list(command), dict(env)))
        return codes.pop(0)

    schedule.push_traces_scheduled(
        store, tmp_path / "j.db", io.StringIO(), env={}, runner=runner, spawner=spawner,
        notifier=lambda _t, m: notes.append(m), python="py",
    )  # fmt: skip
    return seen, notes


def test_the_daily_job_pushes_with_keys_from_the_keychain(store: Store, tmp_path: Path) -> None:
    store.set_config_raw({"langfuse_on": "1"})
    seen, notes = daily(store, tmp_path, Runner(), [0])
    assert seen[0][0][-1] == "push-traces"
    assert seen[0][1]["LANGFUSE_PUBLIC_KEY"] == "secret"
    assert seen[0][1]["LANGFUSE_SECRET_KEY"] == "secret"
    assert notes == []


def test_the_daily_job_does_nothing_when_off(store: Store, tmp_path: Path) -> None:
    seen, notes = daily(store, tmp_path, Runner(), [0])
    assert seen == []
    assert notes == []


def test_missing_keychain_keys_notify_and_skip(store: Store, tmp_path: Path) -> None:
    store.set_config_raw({"langfuse_on": "1"})
    seen, notes = daily(store, tmp_path, Runner(key=None), [0])
    assert seen == []
    assert any("Langfuse keys" in n for n in notes)


def test_a_failed_push_notifies_but_never_changes_the_job(store: Store, tmp_path: Path) -> None:
    store.set_config_raw({"langfuse_on": "1"})
    _, notes = daily(store, tmp_path, Runner(), [1])
    assert any("failed" in n for n in notes)


def test_install_names_the_langfuse_keychain_items_when_on(tmp_path: Path) -> None:
    out = io.StringIO()
    schedule.install(
        tmp_path / "j.db", replace(Config(), langfuse_on=1), out, runner=Runner(key=None),
        home=tmp_path, uid=1, platform="darwin",
    )  # fmt: skip
    assert "jobscan-langfuse-public" in out.getvalue()
    assert "jobscan-langfuse-secret" in out.getvalue()


# ---- the pages ------------------------------------------------------------------------------


def test_config_has_the_tracing_group_and_validates_it() -> None:
    assert Config.from_raw({"langfuse_on": "1"}).langfuse_on == 1
    assert Config().langfuse_on == 0
    assert Config().langfuse_send_text == 0
    from jobscan.config import ConfigError

    with pytest.raises(ConfigError) as caught:
        Config.from_raw({"langfuse_base_url": "http://example.com", "langfuse_public_env": "a b"})
    assert set(caught.value.errors) == {"langfuse_base_url", "langfuse_public_env"}


def test_eval_shows_the_last_push(store: Store) -> None:
    from jobscan.web import create_app

    scored(store)
    run_push(store, Session())
    store.set_config_raw({"langfuse_on": "1", "fit_recency_days": "365"})
    page = create_app(store, secret_key="t").test_client().get("/eval").get_data(as_text=True)
    assert "last push" in page
    assert "1 traces and 0 ratings sent" in page


def test_config_page_lists_the_group(store: Store) -> None:
    from jobscan.web import create_app

    page = create_app(store, secret_key="t").test_client().get("/config").get_data(as_text=True)
    assert "Tracing (Langfuse)" in page
    assert "langfuse_send_text" in page
