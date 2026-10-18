"""Shared domain types and constants."""

from __future__ import annotations

from dataclasses import dataclass

PORTAL_LINKEDIN = "LinkedIn"

STATUSES: tuple[str, ...] = (
    "Not Applied",
    "Not relevant",
    "Closed",
    "Skipped",
    "Applied",
    "Initial screening rejected",
    "Interview phase rejected",
    "Interview Ongoing",
    "Interview cleared",
)
DEFAULT_STATUS = STATUSES[0]
# Order shown in dropdowns and filters (A to Z). STATUSES keeps the workflow order.
SORTED_STATUSES: tuple[str, ...] = tuple(sorted(STATUSES, key=str.casefold))

DEFAULT_PHRASES: tuple[str, ...] = (
    "Technical Program Manager",
    "Senior Technical Program Manager",
    "Senior Program Manager",
    "Staff Technical Program Manager",
    "Staff Program Manager",
    "Delivery Manager",
    "TPM",
    "Technical Project Manager",
    "Senior Project Manager",
    "IT Project Manager",
    "Software Program Manager",
    "Technical Project/Program Manager",
    "Technical Program Management",
)


@dataclass(frozen=True)
class Card:
    """One job card parsed from a LinkedIn search results page."""

    job_id: str
    title: str
    company: str
    location: str
    link: str
    actively_hiring: bool
    raw_datetime: str | None
    raw_relative: str | None


@dataclass(frozen=True)
class Phrase:
    """A configured title phrase with its two switches."""

    id: int
    phrase: str
    use_as_keyword: bool
    use_for_match: bool


@dataclass(frozen=True)
class NewRole:
    """A role ready to be inserted into storage."""

    portal: str
    job_id: str
    title: str
    company: str
    location: str
    link: str
    posted_on: str | None
    posted_precision: str
    fetched_on: str
    actively_hiring: bool
    matched_phrase: str
    possible_duplicate_of: str | None
    dup_key: str | None
    raw_datetime: str | None
    raw_relative: str | None
    run_id: int


@dataclass
class KeywordRecord:
    """Counters for one keyword search within a run (mutable while the search runs)."""

    keyword: str
    pages: int = 0
    cards: int = 0
    matched: int = 0
    new_roles: int = 0
    possible_dups: int = 0
    throttled: int = 0
    retries: int = 0
    failed_pages: int = 0
    malformed: int = 0
    completed: bool = False
    stopped_by: str = ""

    @property
    def dropped(self) -> int:
        return self.cards - self.matched


@dataclass(frozen=True)
class PageStat:
    """Cards and title matches on one results page, for the noise-by-depth view."""

    page_index: int
    cards: int
    matched: int


@dataclass(frozen=True)
class DepthStat:
    page_index: int
    cards: int
    matched: int

    @property
    def match_rate(self) -> float:
        return self.matched / self.cards if self.cards else 0.0


@dataclass(frozen=True)
class Role:
    """A stored role, exactly as it appears in the roles table."""

    portal: str
    job_id: str
    title: str
    company: str
    location: str
    link: str
    posted_on: str | None
    posted_precision: str
    fetched_on: str
    actively_hiring: bool
    matched_phrase: str
    possible_duplicate_of: str | None
    status: str
    applied_on: str | None
    contact_person: str
    comments: str
    raw_datetime: str | None
    raw_relative: str | None
    first_run_id: int | None

    @property
    def key(self) -> str:
        return f"{self.portal}|{self.job_id}"


@dataclass(frozen=True)
class RoleFilter:
    query: str = ""
    status: str | None = None
    duplicates_only: bool = False
    fetched_on: str | None = None  # YYYY-MM-DD, the IST day the role was first fetched


@dataclass(frozen=True)
class FetchDay:
    """Roles first fetched on one IST day, split by their current status."""

    day: str  # YYYY-MM-DD
    total: int
    by_status: dict[str, int]


@dataclass(frozen=True)
class RunSummary:
    id: int
    started_on: str
    finished_on: str | None
    status: str
    window_seconds: int
    keywords_total: int
    keywords_done: int
    pages: int
    cards_seen: int
    matched: int
    new_roles: int
    possible_dups: int
    throttled: int
    retries: int
    failed_pages: int
    malformed_cards: int
    note: str


@dataclass(frozen=True)
class SkippedTitle:
    run_id: int
    portal: str
    job_id: str
    title: str
    company: str
    fetched_on: str
