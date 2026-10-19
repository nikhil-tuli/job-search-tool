from __future__ import annotations

import json
import math

import pytest
import requests

from jobscan.embeddings import (
    EmbedAuthError,
    EmbedDailyLimit,
    EmbedError,
    EmbedRetry,
    GeminiEmbedder,
    RateLimited,
    RateLimiter,
    dot,
    estimate_tokens,
    normalise,
    pack,
    unpack,
)


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        payload: object = None,
        text: str = "",
        headers: dict | None = None,  # type: ignore[type-arg]
    ) -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload)
        self.headers = headers or {}

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, *replies: FakeResponse | BaseException) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []  # type: ignore[type-arg]

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def gemini(session: FakeSession, key: str = "secret-key") -> GeminiEmbedder:
    return GeminiEmbedder(key, model="gemini-embedding-001", dimensions=4, session=session)  # type: ignore[arg-type]


def ok(*vectors: list[float], usage: int | None = None) -> FakeResponse:
    body: dict[str, object] = {"embeddings": [{"values": v} for v in vectors]}
    if usage is not None:
        body["usageMetadata"] = {"totalTokenCount": usage}
    return FakeResponse(200, body)


# ---- vectors -----------------------------------------------------------------------------------


def test_normalise_gives_unit_length() -> None:
    vector = normalise([3.0, 4.0])
    assert vector == pytest.approx([0.6, 0.8])
    assert math.sqrt(sum(x * x for x in vector)) == pytest.approx(1.0)


def test_zero_vector_is_an_error() -> None:
    with pytest.raises(EmbedError):
        normalise([0.0, 0.0])


def test_pack_round_trips_as_float32() -> None:
    vector = [0.25, -1.5, 3.0]
    blob = pack(vector)
    assert len(blob) == 12
    assert list(unpack(blob)) == vector


def test_dot_of_unit_vectors_is_cosine() -> None:
    a, b = normalise([1.0, 0.0]), normalise([1.0, 1.0])
    assert dot(a, b) == pytest.approx(math.sqrt(0.5))
    assert dot(a, a) == pytest.approx(1.0)


def test_token_estimate_is_cautious_and_never_zero() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("x" * 300) == 100


# ---- rate limiter ------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_limiter_allows_up_to_the_limit_without_waiting() -> None:
    clock = Clock()
    limiter = RateLimiter(10, 1000, clock=clock, sleep=clock.sleep)
    limiter.acquire(5, 100)
    limiter.acquire(5, 100)
    assert clock.sleeps == []


def test_limiter_waits_for_the_window_to_clear_on_texts() -> None:
    clock = Clock()
    limiter = RateLimiter(10, 10_000, clock=clock, sleep=clock.sleep)
    limiter.acquire(10, 10)
    clock.now = 20.0
    limiter.acquire(1, 10)
    assert clock.sleeps == [pytest.approx(40.0)]  # the first batch leaves the window at 60s


def test_limiter_waits_on_tokens_too() -> None:
    clock = Clock()
    limiter = RateLimiter(100, 1000, clock=clock, sleep=clock.sleep)
    limiter.acquire(1, 900)
    limiter.acquire(1, 200)
    assert clock.sleeps == [pytest.approx(60.0)]


def test_limiter_gives_up_when_the_wait_is_too_long() -> None:
    clock = Clock()
    limiter = RateLimiter(1, 1000, clock=clock, sleep=clock.sleep, max_wait=10)
    limiter.acquire(1, 1)
    with pytest.raises(RateLimited, match="60s"):
        limiter.acquire(1, 1)


def test_limiter_refuses_a_batch_that_can_never_fit() -> None:
    limiter = RateLimiter(5, 100)
    with pytest.raises(EmbedError):
        limiter.acquire(6, 1)
    with pytest.raises(EmbedError):
        limiter.acquire(1, 101)


# ---- Gemini ------------------------------------------------------------------------------------


def test_request_shape_and_key_header() -> None:
    session = FakeSession(ok([1, 0, 0, 0], [0, 1, 0, 0]))
    batch = gemini(session).embed(["first text", "second text"])
    (sent,) = session.requests
    assert sent["url"].endswith("/models/gemini-embedding-001:batchEmbedContents")
    assert sent["headers"] == {"x-goog-api-key": "secret-key"}
    assert "secret-key" not in sent["url"]
    items = sent["json"]["requests"]
    assert [i["content"]["parts"][0]["text"] for i in items] == ["first text", "second text"]
    assert {i["taskType"] for i in items} == {"SEMANTIC_SIMILARITY"}
    assert {i["outputDimensionality"] for i in items} == {4}
    assert {i["model"] for i in items} == {"models/gemini-embedding-001"}
    assert batch.vectors == [[1, 0, 0, 0], [0, 1, 0, 0]]
    assert batch.tokens is None


def test_token_usage_is_returned_when_the_service_reports_it() -> None:
    assert gemini(FakeSession(ok([1, 0, 0, 0], usage=42))).embed(["x"]).tokens == 42


def test_missing_key_is_an_auth_error_naming_the_variable() -> None:
    with pytest.raises(EmbedAuthError, match="GEMINI_API_KEY"):
        gemini(FakeSession(), key="")


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures(status: int) -> None:
    body = {"error": {"message": "API key not valid"}}
    with pytest.raises(EmbedAuthError, match="API key not valid"):
        gemini(FakeSession(FakeResponse(status, body))).embed(["x"])


def test_rate_limit_is_a_retry_with_the_delay_the_service_asks_for() -> None:
    body = {"error": {"message": "slow down", "details": [{"retryDelay": "34s"}]}}
    with pytest.raises(EmbedRetry) as caught:
        gemini(FakeSession(FakeResponse(429, body))).embed(["x"])
    assert caught.value.retry_after == 34.0


def test_retry_after_header_wins_when_present() -> None:
    reply = FakeResponse(503, {"error": {"message": "busy"}}, headers={"Retry-After": "7"})
    with pytest.raises(EmbedRetry) as caught:
        gemini(FakeSession(reply)).embed(["x"])
    assert caught.value.retry_after == 7.0


def test_daily_quota_is_not_retried() -> None:
    body = {
        "error": {
            "message": "quota exceeded",
            "details": [{"violations": [{"quotaId": "EmbedContentRequestsPerDayPerProject"}]}],
        }
    }
    with pytest.raises(EmbedDailyLimit):
        gemini(FakeSession(FakeResponse(429, body))).embed(["x"])


def test_server_errors_and_network_failures_are_retries() -> None:
    with pytest.raises(EmbedRetry):
        gemini(FakeSession(FakeResponse(500, {"error": {"message": "oops"}}))).embed(["x"])
    with pytest.raises(EmbedRetry, match="ConnectionError"):
        gemini(FakeSession(requests.ConnectionError("secret-key in here"))).embed(["x"])


def test_network_error_text_never_carries_the_key() -> None:
    with pytest.raises(EmbedRetry) as caught:
        gemini(FakeSession(requests.ConnectionError("boom secret-key"))).embed(["x"])
    assert "secret-key" not in str(caught.value)


def test_a_bad_request_is_permanent() -> None:
    with pytest.raises(EmbedError) as caught:
        gemini(FakeSession(FakeResponse(400, {"error": {"message": "bad dims"}}))).embed(["x"])
    assert not isinstance(caught.value, EmbedRetry)


@pytest.mark.parametrize(
    "payload",
    [
        {"embeddings": []},
        {"embeddings": [{"values": []}]},
        {"embeddings": [{}]},
        {"nothing": 1},
        [],
    ],
)
def test_malformed_success_bodies_are_errors(payload: object) -> None:
    with pytest.raises(EmbedError):
        gemini(FakeSession(FakeResponse(200, payload))).embed(["x"])


def test_non_json_success_is_a_retry() -> None:
    with pytest.raises(EmbedRetry):
        gemini(FakeSession(FakeResponse(200, None, text="<html>"))).embed(["x"])
