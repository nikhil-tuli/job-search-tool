"""Fetching job descriptions: shared by the scan and by the details command."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .client import DetailFetcher, DetailOutcome
from .config import Config
from .store import Store
from .timeutil import to_iso

THROTTLES_BEFORE_STOP = 2


@dataclass
class DetailsResult:
    saved: int = 0
    failed: int = 0
    failed_ids: set[str] = field(default_factory=set)
    stopped: bool = False  # repeated throttling cut the batch short


def fetch_descriptions(
    *,
    store: Store,
    fetcher: DetailFetcher,
    pending: Sequence[tuple[str, str]],
    config: Config,
    emit: Callable[[str], None],
    wait: Callable[[float, float, str | None], None],
    clock: Callable[[], datetime],
) -> DetailsResult:
    """Fetch the description of each (portal, job_id), one request at a time.

    A throttled response is not counted against a role, so it is tried again later. Two in a row
    stop the batch, because more requests would only make things worse.
    """
    result = DetailsResult()
    throttled_in_row = 0
    for portal, job_id in pending:
        wait(config.delay_seconds_min, config.delay_seconds_max, None)
        fetched = fetcher.fetch_detail(job_id)
        if fetched.outcome is DetailOutcome.THROTTLED:
            throttled_in_row += 1
            emit(f"  description {job_id}: throttled ({fetched.note})")
            if throttled_in_row >= THROTTLES_BEFORE_STOP:
                result.stopped = True
                break
            wait(config.retry_wait_min, config.retry_wait_max, "next description")
            continue
        throttled_in_row = 0
        if fetched.outcome is DetailOutcome.OK and fetched.detail is not None:
            store.save_detail(portal, job_id, fetched.detail, when=to_iso(clock()))
            result.saved += 1
        else:
            store.record_detail_failure(portal, job_id)
            result.failed += 1
            result.failed_ids.add(job_id)
            emit(f"  description {job_id}: none ({fetched.note})")
    return result
