"""The AI call log: what the client records, the 429 retry, and how Eval and the panel show it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
import requests
from flask.testing import FlaskClient

from jobscan import db
from jobscan.ai_eval import build_call_stats
from jobscan.ai_fit import RATE_LIMIT_WAIT_SECONDS, AiScorer
from jobscan.config import Config
from jobscan.store import Store
from jobscan.tailor_ai import CallInfo, TailorClient, TailorError
from jobscan.timeutil import to_iso
from jobscan.web import create_app
from tests.test_ai_fit import GOOD_REPLY, LONG_JD, FakeClient
from tests.test_scoring import NOW, add_role, upload_cv


class Session:
    def __init__(self, status: int = 200, body: object = None) -> None:
        self.status, self.body = status, body

    def post(self, *args: object, **kwargs: object) -> requests.Response:
        response = requests.Response()
        response.status_code = self.status
        response._content = json.dumps(self.body if self.body is not None else {}).encode()
        return response


def tailor(session: Session) -> TailorClient:
    return TailorClient("https://x/v1", "m", "key", session=session)  # type: ignore[arg-type]


def ok_body(content: object = "hello") -> dict[str, object]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 4100, "completion_tokens": 380},
    }


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


# ---- the client -----------------------------------------------------------------------------


def test_client_records_time_and_tokens() -> None:
    client = tailor(Session(200, ok_body()))
    assert client.complete("s", "u") == "hello"
    info = client.last_call
    assert info is not None
    assert (info.status, info.tokens_in, info.tokens_out) == (200, 4100, 380)
    assert info.ms >= 0


def test_a_refusal_carries_status_and_the_providers_own_words() -> None:
    client = tailor(Session(400, {"object": "error", "message": "Invalid model: x"}))
    with pytest.raises(TailorError) as caught:
        client.complete("s", "u")
    assert caught.value.status == 400
    assert caught.value.provider_message == "Invalid model: x"
    assert "Invalid model: x" in str(caught.value)  # shown on screen too


def test_nested_error_messages_are_read_too() -> None:
    with pytest.raises(TailorError) as caught:
        tailor(Session(429, {"error": {"message": "slow down"}})).complete("s", "u")
    assert caught.value.status == 429
    assert caught.value.provider_message == "slow down"


def test_a_reasoning_models_text_block_is_used() -> None:
    blocks = [
        {"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
        {"type": "text", "text": '{"a": 1}'},
    ]
    assert tailor(Session(200, ok_body(blocks))).complete("s", "u") == '{"a": 1}'


def test_a_reply_of_only_thinking_says_to_pick_another_model() -> None:
    blocks = [{"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]}]
    with pytest.raises(TailorError) as caught:
        tailor(Session(200, ok_body(blocks))).complete("s", "u")
    assert "reasoning" in str(caught.value)
    assert "Config" in str(caught.value)


# ---- the scorer -----------------------------------------------------------------------------


class Informed(FakeClient):
    """A fake that, like the real client, reports how the last call went."""

    def __init__(self, replies: list[str | TailorError]) -> None:
        super().__init__(replies)
        self.last_call: CallInfo | None = None

    def complete(
        self, system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 0
    ) -> str:
        self.last_call = CallInfo(ms=3200, status=200, tokens_in=4210, tokens_out=360)
        return super().complete(system, user, temperature=temperature, max_tokens=max_tokens)


def run(
    store: Store, client: FakeClient, *, retry: bool = True, sleeps: list[float] | None = None
) -> object:
    waits = sleeps if sleeps is not None else []
    return AiScorer(
        store=store,
        config=replace(Config(), ai_model="ministral-14b-2512"),
        make_client=lambda _c: client,
        emit=lambda _l: None,
        sleep=waits.append,
        clock=lambda: NOW,
        retry_rate_limit=retry,
    ).run()


def busy() -> TailorError:
    return TailorError("The AI service is busy (rate limit).", status=429, provider_message="slow")


def test_each_call_is_logged_with_model_time_and_tokens(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    run(store, Informed([GOOD_REPLY]))
    [call] = store.ai_calls_between("2000-01-01")
    assert call.ok
    assert call.model == "ministral-14b-2512"
    assert (call.ms, call.tokens_in, call.tokens_out) == (3200, 4210, 360)


def test_a_busy_reply_waits_a_minute_and_retries_once(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    sleeps: list[float] = []
    result = run(store, Informed([busy(), GOOD_REPLY]), sleeps=sleeps)
    assert result.scored == 1  # type: ignore[attr-defined]
    assert RATE_LIMIT_WAIT_SECONDS in sleeps
    calls = store.ai_calls_between("2000-01-01")
    assert [c.ok for c in calls] == [False, True]
    assert calls[0].status == 429
    assert calls[0].error_text == "slow"


def test_a_second_busy_reply_stops_the_run(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    add_role(store, "2", LONG_JD + "\n- Extra duty")
    client = Informed([busy()])
    result = run(store, client)
    assert result.scored == 0  # type: ignore[attr-defined]
    assert result.waiting == 2  # type: ignore[attr-defined]
    assert len(client.calls) == 2  # the first try and the one retry


def test_the_one_role_path_does_not_wait(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    sleeps: list[float] = []
    client = Informed([busy(), GOOD_REPLY])
    result = run(store, client, retry=False, sleeps=sleeps)
    assert result.scored == 0  # type: ignore[attr-defined]
    assert RATE_LIMIT_WAIT_SECONDS not in sleeps
    assert len(client.calls) == 1


def test_calls_per_minute_now_defaults_to_four() -> None:
    assert Config().ai_calls_per_minute == 4


# ---- stats ----------------------------------------------------------------------------------


def log(store: Store, minutes_ago: int, **kw: object) -> None:
    when = to_iso(NOW - timedelta(minutes=minutes_ago))
    store.log_ai_call("LinkedIn", "1", when=when, **kw)  # type: ignore[arg-type]


def test_stats_count_errors_time_tokens_and_failures(store: Store) -> None:
    add_role(store, "1", LONG_JD)
    log(store, 5, ok=True, model="a", ms=3000, tokens_in=4000, tokens_out=400, status=200)
    log(store, 5, ok=True, model="a", ms=5000, tokens_in=6000, tokens_out=600, status=200)
    log(store, 4, ok=False, model="b", status=429, error_text="Rate limit", note="busy")
    log(store, 3, ok=False, model="b", status=400, error_text="Bad model", note="refused")
    log(store, 60 * 24 * 10, ok=True, model="old")  # outside a 7 day window
    stats = build_call_stats(store, NOW, "7d")
    assert (stats.calls, stats.ok, stats.rate_limited, stats.other_errors) == (4, 2, 1, 1)
    assert (stats.median_s, stats.slowest_s) == (4.0, 5.0)
    assert (stats.avg_in, stats.avg_out, stats.max_in) == (5000, 500, 6000)
    assert stats.busiest_minute == 11_000  # both ok calls fell in the same minute
    assert next(m.model for m in stats.by_model) == "a"
    assert [f.message for f in stats.failures] == [
        "400 Bad model",
        "429 Rate limit",
    ]  # newest first
    assert build_call_stats(store, NOW, "30d").calls == 5


def test_unknown_period_falls_back_to_seven_days(store: Store) -> None:
    assert build_call_stats(store, NOW, "bogus").period == "7d"


def test_old_rows_without_a_status_still_count_as_rate_limited(store: Store) -> None:
    log(store, 1, ok=False, note="The AI service is busy (rate limit). Try again in a minute.")
    assert build_call_stats(store, NOW, "24h").rate_limited == 1


# ---- the pages ------------------------------------------------------------------------------


def web(store: Store, fake: FakeClient) -> FlaskClient:
    return create_app(store, secret_key="t", make_ai_client=lambda _c: fake).test_client()


def test_eval_shows_the_ai_calls_section(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    run(store, Informed([GOOD_REPLY]))
    store.log_ai_call(
        "LinkedIn", "1", when=to_iso(NOW), ok=False, status=429, error_text="Rate limit exceeded"
    )
    store.set_config_raw({"fit_recency_days": "365"})
    page = web(store, FakeClient([GOOD_REPLY])).get("/eval?calls=30d").get_data(as_text=True)
    assert '<h2 id="ai-calls">AI calls</h2>' in page
    assert "ministral-14b-2512" in page
    assert "Rate limit exceeded" in page
    assert "4,210" in page


def test_the_panel_says_how_the_score_was_made(store: Store) -> None:
    upload_cv(store)
    add_role(store, "1", LONG_JD)
    run(store, Informed([GOOD_REPLY]))
    store.set_config_raw({"fit_recency_days": "365"})
    page = web(store, FakeClient([GOOD_REPLY])).get("/roles/LinkedIn/1").get_data(as_text=True)
    assert "by ministral-14b-2512 in 3.2 s, 4,210 tokens in, 360 out." in page
