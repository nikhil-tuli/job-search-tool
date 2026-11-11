"""Speech to text with Groq's Whisper. The audio is sent, never saved.

One call per answer. The reply has the text, the time of each word and the time of each
sentence. The audio bytes are held in memory only: nothing is written to disk and nothing about
the audio is logged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from .metrics import TimedWord

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Groq's limit on the free plan
REQUEST_TIMEOUT_SECONDS = 60
ALLOWED_AUDIO_TYPES = ("audio/", "video/webm", "video/mp4", "application/octet-stream")


class TranscribeError(Exception):
    """A transcription failed. `status` is the HTTP status when the service answered."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Transcription:
    text: str
    words: tuple[TimedWord, ...]  # empty when the service gave no word times
    sentences: tuple[TimedWord, ...]  # empty when the service gave no sentence times
    duration_s: float | None
    ms: int = 0


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcription: ...


def parse_reply(data: dict[str, Any], *, ms: int = 0) -> Transcription:
    """Read a verbose_json reply. Missing word or sentence times are allowed."""
    text = data.get("text")
    if not isinstance(text, str):
        raise TranscribeError("The speech service reply was not in the expected form.")

    def times(key: str) -> tuple[TimedWord, ...]:
        items = data.get(key)
        out: list[TimedWord] = []
        if isinstance(items, list):
            for item in items:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("start"), int | float)
                    and isinstance(item.get("end"), int | float)
                ):
                    out.append(TimedWord(float(item["start"]), float(item["end"])))
        return tuple(out)

    duration = data.get("duration")
    return Transcription(
        text=text.strip(),
        words=times("words"),
        sentences=times("segments"),
        duration_s=float(duration) if isinstance(duration, int | float) else None,
        ms=ms,
    )


class GroqTranscriber:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        session: requests.Session | None = None,
        where: str = "Config, Interview Prep",
        prompt: str = "",
        response_format: str = "verbose_json",
    ) -> None:
        self._url = base_url.rstrip("/") + "/audio/transcriptions"
        self._model = model
        self._key = api_key
        self._session = session or requests.Session()
        self._where = where
        self._prompt = prompt
        self._format = response_format

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcription:
        if len(audio) > MAX_AUDIO_BYTES:
            raise TranscribeError("The recording is too large to send (over 25 MB).")
        data: list[tuple[str, str]] = [
            ("model", self._model),
            ("language", "en"),
            ("temperature", "0"),
            ("response_format", self._format),
            ("timestamp_granularities[]", "word"),
            ("timestamp_granularities[]", "segment"),
        ]
        if self._prompt:
            data.append(("prompt", self._prompt))
        started = time.monotonic()
        try:
            response = self._session.post(
                self._url,
                data=data,
                files={"file": (filename, audio, content_type)},
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.Timeout:
            raise TranscribeError("The speech service took too long to answer.") from None
        except requests.RequestException:
            raise TranscribeError(
                "The speech service could not be reached. Check your internet."
            ) from None
        status = response.status_code
        if status in (401, 403):
            raise TranscribeError(
                f"The speech service refused the key (HTTP {status}). Check {self._where}.",
                status=status,
            )
        if status == 404:
            raise TranscribeError(
                f"The speech service address or model was not found. Check {self._where}.",
                status=status,
            )
        if status == 429:
            raise TranscribeError(
                "The speech service is busy (rate limit).",
                status=status,
                retry_after=_retry_after(response),
            )
        if status >= 500:
            raise TranscribeError(
                f"The speech service had a problem (HTTP {status}).", status=status
            )
        if status != 200:
            raise TranscribeError(
                f"The speech service refused the recording (HTTP {status}).", status=status
            )
        try:
            body = response.json()
        except ValueError:
            raise TranscribeError(
                "The speech service reply was not in the expected form."
            ) from None
        if not isinstance(body, dict):
            raise TranscribeError("The speech service reply was not in the expected form.")
        return parse_reply(body, ms=int((time.monotonic() - started) * 1000))


def _retry_after(response: requests.Response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


def make_transcriber(
    base_url: str, model: str, key_variable: str, environ: dict[str, str]
) -> GroqTranscriber:
    key = environ.get(key_variable, "").strip()
    if not key:
        raise TranscribeError(
            f"The speech key is missing. Set {key_variable} in the terminal, then start "
            "jobscan serve from that terminal."
        )
    return GroqTranscriber(base_url, model, key)
