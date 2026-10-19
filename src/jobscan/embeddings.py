"""Embedding vectors: the Gemini client, rate limiting, and the vector maths.

The client only sends one batch and classifies what came back. Retrying, pacing and stopping are
the scorer's job, the same split as the LinkedIn client and the scanner.
"""

from __future__ import annotations

import math
import re
import struct
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
API_KEY_ENV = "GEMINI_API_KEY"
CHARS_PER_TOKEN = 3  # a cautious estimate for English text (about 4 in practice)
_RETRY_DELAY = re.compile(r"(\d+(?:\.\d+)?)s")
_WINDOW_SECONDS = 60.0


class EmbedError(RuntimeError):
    """A request that will not succeed if repeated (bad key, bad request)."""


class EmbedAuthError(EmbedError):
    """The key is missing, wrong or not allowed to use the model."""


class EmbedDailyLimit(EmbedError):
    """The daily free quota is spent; nothing more can be embedded until it resets."""


class EmbedRetry(RuntimeError):
    """A temporary refusal or failure; the same request may work after a wait."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RateLimited(RuntimeError):
    """Pacing would need a wait longer than the caller is willing to make."""


@dataclass(frozen=True)
class EmbedBatch:
    vectors: list[list[float]]
    tokens: int | None  # as reported by the service; None when it reports nothing


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> EmbedBatch: ...


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


# ---- vector maths ------------------------------------------------------------------------------


def normalise(vector: Sequence[float]) -> list[float]:
    length = math.sqrt(sum(x * x for x in vector))
    if length == 0:
        raise EmbedError("the service returned an all-zero vector")
    return [x / length for x in vector]


def pack(vector: Sequence[float]) -> bytes:
    """Little-endian float32, so a database moves between machines."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> tuple[float, ...]:
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def _dot_fallback(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# math.sumprod (Python 3.12+) runs the loop in C.
dot: Callable[[Sequence[float], Sequence[float]], float] = getattr(math, "sumprod", _dot_fallback)


# ---- pacing ------------------------------------------------------------------------------------


class RateLimiter:
    """Keeps texts and tokens per minute under the free-tier limits, with a sliding window."""

    def __init__(
        self,
        texts_per_minute: int,
        tokens_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_wait: float | None = None,
    ) -> None:
        self.texts_per_minute = texts_per_minute
        self.tokens_per_minute = tokens_per_minute
        self._clock = clock
        self._sleep = sleep
        self._max_wait = max_wait
        self._events: deque[tuple[float, int, int]] = deque()  # (time, texts, tokens)

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= _WINDOW_SECONDS:
            self._events.popleft()

    def acquire(self, texts: int, tokens: int) -> None:
        """Wait until a batch of this size fits in the window, then count it."""
        if texts > self.texts_per_minute or tokens > self.tokens_per_minute:
            raise EmbedError("a single batch is larger than the per-minute limit")
        while True:
            now = self._clock()
            self._prune(now)
            used_texts = sum(e[1] for e in self._events)
            used_tokens = sum(e[2] for e in self._events)
            if (
                used_texts + texts <= self.texts_per_minute
                and used_tokens + tokens <= self.tokens_per_minute
            ):
                self._events.append((now, texts, tokens))
                return
            wait = max(self._events[0][0] + _WINDOW_SECONDS - now, 0.05)
            if self._max_wait is not None and wait > self._max_wait:
                raise RateLimited(f"rate limit: would need to wait {wait:.0f}s")
            self._sleep(wait)


# ---- Gemini ------------------------------------------------------------------------------------


class GeminiEmbedder:
    """gemini-embedding-001 over REST, one batch per call."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        dimensions: int,
        session: requests.Session | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise EmbedAuthError(f"no API key; set the {API_KEY_ENV} environment variable")
        self._key = api_key
        self._model = model
        self._dimensions = dimensions
        self._session = session or requests.Session()
        self._timeout = timeout

    def embed(self, texts: Sequence[str]) -> EmbedBatch:
        body = {
            "requests": [
                {
                    "model": f"models/{self._model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": "SEMANTIC_SIMILARITY",
                    "outputDimensionality": self._dimensions,
                }
                for text in texts
            ]
        }
        try:
            response = self._session.post(
                GEMINI_URL.format(model=self._model),
                json=body,
                headers={"x-goog-api-key": self._key},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            # The message names the exception type only: it must never carry the request.
            raise EmbedRetry(f"network error ({type(exc).__name__})") from None
        return self._parse(response, expected=len(texts))

    def _parse(self, response: requests.Response, *, expected: int) -> EmbedBatch:
        status = response.status_code
        if status == 200:
            return _batch_from(_json(response), expected)
        message = _error_message(response)
        if status in (401, 403):
            raise EmbedAuthError(f"HTTP {status}: {message}")
        if status == 429:
            if "PerDay" in response.text:
                raise EmbedDailyLimit(f"daily quota reached: {message}")
            raise EmbedRetry(f"rate limited: {message}", _retry_after(response))
        if status >= 500 or status == 408:
            raise EmbedRetry(f"HTTP {status}: {message}", _retry_after(response))
        raise EmbedError(f"HTTP {status}: {message}")


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise EmbedRetry("the service returned something that is not JSON") from None


def _error_message(response: requests.Response) -> str:
    try:
        error = response.json().get("error", {})
        return str(error.get("message") or error.get("status") or "")[:200]
    except (ValueError, AttributeError):
        return ""


def _retry_after(response: requests.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    match = _RETRY_DELAY.search(response.text)  # Google's body says "retryDelay": "34s"
    return float(match.group(1)) if match else None


def _batch_from(payload: Any, expected: int) -> EmbedBatch:
    embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
    if not isinstance(embeddings, list) or len(embeddings) != expected:
        raise EmbedError("the service returned a different number of vectors than texts sent")
    vectors: list[list[float]] = []
    for item in embeddings:
        values = item.get("values") if isinstance(item, dict) else None
        if not isinstance(values, list) or not values:
            raise EmbedError("the service returned a vector without values")
        vectors.append([float(x) for x in values])
    usage = payload.get("usageMetadata")
    tokens = usage.get("totalTokenCount") if isinstance(usage, dict) else None
    return EmbedBatch(vectors, tokens if isinstance(tokens, int) else None)
