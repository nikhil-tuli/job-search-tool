"""Fetching job descriptions: shared by the scan and by the details command."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .client import DetailFetcher, DetailOutcome
from .config import Config
from .models import PORTAL_LINKEDIN
from .store import Store
from .timeutil import now_ist, to_iso


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

    Throttling follows the same Config settings as the listing pages. A throttled role is
    tried again up to "Retries per page or description" times, after the retry wait. It is not
    counted against the role, so a role still throttled is left for the next run. The batch
    stops once "Abort after throttled responses in a row" is reached, because more requests
    would only make things worse. Any success starts the count again.
    """
    result = DetailsResult()
    throttled_in_row = 0
    for portal, job_id in pending:
        wait(config.delay_seconds_min, config.delay_seconds_max, None)
        attempt = 0
        while True:
            fetched = fetcher.fetch_detail(job_id)
            if fetched.outcome is not DetailOutcome.THROTTLED:
                break
            throttled_in_row += 1
            emit(f"  description {job_id}: throttled ({fetched.note})")
            if throttled_in_row >= config.abort_after_consecutive_throttles:
                result.stopped = True
                return result
            if attempt >= config.retry_count:
                break
            attempt += 1
            wait(
                config.retry_wait_min,
                config.retry_wait_max,
                f"retry {attempt}/{config.retry_count}",
            )
        if fetched.outcome is DetailOutcome.THROTTLED:
            continue  # left for the next run
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


def recent_since(config: Config) -> str:
    """Roles fetched at or after this time are the ones a scan or `jobscan details` covers."""
    return to_iso(now_ist() - timedelta(days=config.detail_max_age_days))


def waiting_for_description(store: Store, config: Config, portal: str = PORTAL_LINKEDIN) -> int:
    """One portal's recent roles still without a description, not counting ones given up on."""
    return len(store.pending_details(recent_since(config), 1_000_000, portal))
