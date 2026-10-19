"""Scripted stand-ins for the LinkedIn client, shared by scanner and CLI tests."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from jobscan.client import DetailOutcome, DetailResult, FetchResult, Outcome
from jobscan.embeddings import EmbedBatch
from jobscan.models import Card, JobDetail
from jobscan.parser import ParsedPage

TPM = "Technical Program Manager"
DM = "Delivery Manager"


def card(
    job_id: str,
    title: str = TPM,
    company: str = "Acme",
    location: str = "Pune, India",
    relative: str | None = "5 hours ago",
) -> Card:
    return Card(
        job_id=job_id,
        title=title,
        company=company,
        location=location,
        link=f"https://www.linkedin.com/jobs/view/{job_id}",
        actively_hiring=False,
        raw_datetime="2026-10-15",
        raw_relative=relative,
    )


def ok(*cards: Card, malformed: int = 0) -> FetchResult:
    return FetchResult(Outcome.OK, page=ParsedPage(list(cards), malformed))


def end(warning: str = "") -> FetchResult:
    return FetchResult(Outcome.END, detail="HTTP 400", warning=warning)


def throttled() -> FetchResult:
    return FetchResult(Outcome.THROTTLED, detail="HTTP 429")


def layout() -> FetchResult:
    return FetchResult(Outcome.LAYOUT_CHANGED, detail="no cards parsed")


class ScriptedFetcher:
    """Replies from a per-(keyword, page) queue; an unscripted request is a test failure."""

    def __init__(self, script: dict[tuple[str, int], list[FetchResult | BaseException]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, int]] = []

    def fetch_page(self, keyword: str, page_index: int) -> FetchResult:
        self.calls.append((keyword, page_index))
        queue = self.script.get((keyword, page_index))
        assert queue, f"unscripted request: {keyword!r} page {page_index}"
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def detail_ok(text: str = "Lead delivery of platform programs.", **criteria: str) -> DetailResult:
    return DetailResult(DetailOutcome.OK, detail=JobDetail(description=text, **criteria))


def detail_none(note: str = "HTTP 404") -> DetailResult:
    return DetailResult(DetailOutcome.NO_DESCRIPTION, note=note)


def detail_throttled() -> DetailResult:
    return DetailResult(DetailOutcome.THROTTLED, note="HTTP 429")


class ScriptedDetails:
    """Replies per job ID. A job with no script gets a plain description."""

    def __init__(self, script: dict[str, list[DetailResult]] | None = None) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[str] = []

    def fetch_detail(self, job_id: str) -> DetailResult:
        self.calls.append(job_id)
        queue = self.script.get(job_id)
        return queue.pop(0) if queue else detail_ok(f"Description of {job_id}")


class BagEmbedder:
    """Deterministic stand-in for the embedding service.

    Each word lands in a hashed slot, so texts sharing words are similar, which is enough to see
    whether scoring ranks a close match above a distant one. Failures are scripted per call.
    """

    def __init__(
        self,
        dims: int = 768,
        *,
        tokens: int | None = None,
        failures: list[BaseException] | None = None,
    ) -> None:
        self.dims = dims
        self.tokens = tokens
        self.failures = list(failures or [])
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> EmbedBatch:
        if self.failures:
            raise self.failures.pop(0)
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            vector = [0.0] * self.dims
            for word in re.findall(r"[a-z]+", text.lower()):
                vector[int(hashlib.sha1(word.encode()).hexdigest(), 16) % self.dims] += 1.0
            vectors.append(vector)
        return EmbedBatch(vectors, self.tokens)

    @property
    def texts_sent(self) -> int:
        return sum(len(call) for call in self.calls)
