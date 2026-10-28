"""All reads and writes against the SQLite database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from .fit_store import FitStoreMixin
from .models import (
    DEFAULT_STATUS,
    FOLLOW_UP_DUE,
    FOLLOW_UP_NO_DATE,
    FOLLOW_UP_OVERDUE,
    FOLLOW_UP_UPCOMING,
    PORTAL_LINKEDIN,
    PORTALS,
    STATUSES,
    Cv,
    DepthStat,
    FetchDay,
    JobDetail,
    KeywordRecord,
    NewRole,
    PageStat,
    Phrase,
    Role,
    RoleFilter,
    RoleScore,
    RunSummary,
    ScoringRun,
    SkippedTitle,
)
from .sites_store import SitesStoreMixin
from .sync_store import SyncStoreMixin
from .tailor_store import TailorStoreMixin
from .timeutil import is_day, now_ist, to_iso

RUN_RUNNING = "running"
RUN_SUCCESS = "success"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_STATUSES = (RUN_RUNNING, RUN_SUCCESS, RUN_PARTIAL, RUN_FAILED)


class StoreError(ValueError):
    """Invalid input to a store operation. Nothing was written."""


MAX_DETAIL_ATTEMPTS = 3
SCORING_COUNTERS = frozenset(
    {
        "considered",
        "excluded",
        "no_description",
        "selected",
        "scored",
        "strong",
        "partial",
        "weak",
        "failed",
        "texts_embedded",
        "texts_cached",
        "requests",
        "tokens",
        "tokens_reported",
    }
)
SCORING_STATUSES = ("running", "success", "partial", "failed")
_IN_CHUNK = 500  # stays well under SQLite's limit on bound variables


def _scoring_run(row: sqlite3.Row) -> ScoringRun:
    return ScoringRun(
        id=row["id"],
        started_on=row["started_on"],
        finished_on=row["finished_on"],
        trigger=row["trigger"],
        status=row["status"],
        considered=row["considered"],
        excluded=row["excluded"],
        excluded_by_reason=json.loads(row["excluded_by_reason"]),
        no_description=row["no_description"],
        selected=row["selected"],
        scored=row["scored"],
        strong=row["strong"],
        partial=row["partial"],
        weak=row["weak"],
        failed=row["failed"],
        texts_embedded=row["texts_embedded"],
        texts_cached=row["texts_cached"],
        requests=row["requests"],
        tokens=row["tokens"],
        tokens_reported=bool(row["tokens_reported"]),
        note=row["note"],
        source=row["source"],
    )


def _role_score(row: sqlite3.Row) -> RoleScore:
    return RoleScore(
        portal=row["portal"],
        job_id=row["job_id"],
        keyword_raw=row["keyword_raw"],
        semantic_raw=row["semantic_raw"],
        chunks=row["chunks"],
        inputs=row["inputs"],
        scored_on=row["scored_on"],
        run_id=row["run_id"],
    )


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _split_key(key: str) -> tuple[str, str]:
    portal, sep, job_id = key.partition("|")
    if not sep or not portal or not job_id:
        raise StoreError(f"invalid role key: {key!r}")
    return portal, job_id


def _role(row: sqlite3.Row) -> Role:
    return Role(
        portal=row["portal"],
        job_id=row["job_id"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        link=row["link"],
        posted_on=row["posted_on"],
        posted_precision=row["posted_precision"],
        fetched_on=row["fetched_on"],
        actively_hiring=bool(row["actively_hiring"]),
        matched_phrase=row["matched_phrase"],
        possible_duplicate_of=row["possible_duplicate_of"],
        status=row["status"],
        applied_on=row["applied_on"],
        contact_person=row["contact_person"],
        comments=row["comments"],
        raw_datetime=row["raw_datetime"],
        raw_relative=row["raw_relative"],
        first_run_id=row["first_run_id"],
        description=row["description"],
        seniority=row["seniority"],
        employment_type=row["employment_type"],
        industries=row["industries"],
        job_function=row["job_function"],
        fit_rating=row["fit_rating"],
        follow_up_needed=bool(row["follow_up_needed"]),
        follow_up_on=row["follow_up_on"],
        contact_email=row["contact_email"],
        contact_phone=row["contact_phone"],
        contact_linkedin=row["contact_linkedin"],
    )


def _run(row: sqlite3.Row) -> RunSummary:
    return RunSummary(
        id=row["id"],
        started_on=row["started_on"],
        finished_on=row["finished_on"],
        status=row["status"],
        window_seconds=row["window_seconds"],
        keywords_total=row["keywords_total"],
        keywords_done=row["keywords_done"],
        pages=row["pages"],
        cards_seen=row["cards_seen"],
        matched=row["matched"],
        new_roles=row["new_roles"],
        possible_dups=row["possible_dups"],
        throttled=row["throttled"],
        retries=row["retries"],
        failed_pages=row["failed_pages"],
        malformed_cards=row["malformed_cards"],
        note=row["note"],
        portal=row["portal"],
    )


class Store(TailorStoreMixin, FitStoreMixin, SyncStoreMixin, SitesStoreMixin):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._depth = 0

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic block. Nested use joins the outer transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        finally:
            self._depth = 0

    # ---- config -------------------------------------------------------------------------

    def get_config_raw(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM config").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_config_raw(self, values: dict[str, str]) -> None:
        with self.transaction():
            self._conn.executemany(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                list(values.items()),
            )

    # ---- phrases ------------------------------------------------------------------------

    def list_phrases(self) -> list[Phrase]:
        rows = self._conn.execute(
            "SELECT id, phrase, use_as_keyword, use_for_match FROM phrases ORDER BY position, id"
        ).fetchall()
        return [
            Phrase(r["id"], r["phrase"], bool(r["use_as_keyword"]), bool(r["use_for_match"]))
            for r in rows
        ]

    def replace_phrases(self, phrases: Sequence[tuple[str, bool, bool]]) -> None:
        """Replace the whole phrase list atomically. Blank or duplicate phrases are rejected."""
        cleaned: list[tuple[str, bool, bool]] = []
        seen: set[str] = set()
        for text, as_keyword, for_match in phrases:
            text = " ".join(text.split())
            if not text:
                raise StoreError("phrase must not be blank")
            if text.casefold() in seen:
                raise StoreError(f"duplicate phrase: {text}")
            seen.add(text.casefold())
            cleaned.append((text, as_keyword, for_match))
        with self.transaction():
            self._conn.execute("DELETE FROM phrases")
            self._conn.executemany(
                "INSERT INTO phrases (phrase, use_as_keyword, use_for_match, position) "
                "VALUES (?, ?, ?, ?)",
                [(t, int(k), int(m), i) for i, (t, k, m) in enumerate(cleaned)],
            )

    # ---- roles --------------------------------------------------------------------------

    def role_exists(self, portal: str, job_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM roles WHERE portal = ? AND job_id = ?", (portal, job_id)
        ).fetchone()
        return row is not None

    def find_duplicate(
        self, dup_key: str | None, *, exclude_job_id: str | None = None
    ) -> str | None:
        """Key ('portal|job_id') of the earliest stored role with the same dup key."""
        if dup_key is None:
            return None
        row = self._conn.execute(
            "SELECT portal, job_id FROM roles WHERE dup_key = ? AND job_id IS NOT ? "
            "ORDER BY fetched_on, job_id LIMIT 1",
            (dup_key, exclude_job_id),
        ).fetchone()
        return None if row is None else f"{row['portal']}|{row['job_id']}"

    def insert_role(self, role: NewRole) -> bool:
        """Insert a role. Returns False, leaving the stored row untouched, if the key exists."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO roles (portal, job_id, title, company, location, link, "
            "posted_on, posted_precision, fetched_on, actively_hiring, matched_phrase, "
            "possible_duplicate_of, dup_key, raw_datetime, raw_relative, first_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                role.portal,
                role.job_id,
                role.title,
                role.company,
                role.location,
                role.link,
                role.posted_on,
                role.posted_precision,
                role.fetched_on,
                int(role.actively_hiring),
                role.matched_phrase,
                role.possible_duplicate_of,
                role.dup_key,
                role.raw_datetime,
                role.raw_relative,
                role.run_id,
            ),
        )
        return cursor.rowcount == 1

    def get_role(self, portal: str, job_id: str) -> Role | None:
        row = self._conn.execute(
            "SELECT * FROM roles WHERE portal = ? AND job_id = ?", (portal, job_id)
        ).fetchone()
        return None if row is None else _role(row)

    @staticmethod
    def _where(flt: RoleFilter) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        query = flt.query.strip()
        if query:
            like = f"%{_like_escape(query)}%"
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\' "
                "OR location LIKE ? ESCAPE '\\' OR comments LIKE ? ESCAPE '\\' "
                "OR contact_person LIKE ? ESCAPE '\\' OR contact_email LIKE ? ESCAPE '\\' "
                "OR contact_phone LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
            )
            params.extend([like] * 8)
        statuses = [*flt.statuses, *([flt.status] if flt.status else [])]
        if statuses:
            for status in statuses:
                if status not in STATUSES:
                    raise StoreError(f"unknown status: {status!r}")
            clauses.append(f"status IN ({', '.join('?' * len(statuses))})")
            params.extend(statuses)
        if flt.portals:
            for portal in flt.portals:
                if portal not in PORTALS:
                    raise StoreError(f"unknown portal: {portal!r}")
            clauses.append(f"portal IN ({', '.join('?' * len(flt.portals))})")
            params.extend(flt.portals)
        if flt.duplicates_only:
            clauses.append("possible_duplicate_of IS NOT NULL")
        days = [*flt.fetched_days, *([flt.fetched_on] if flt.fetched_on else [])]
        if days:
            for day in days:
                if not is_day(day):
                    raise StoreError(f"invalid fetch date: {day!r}")
            # Timestamps are stored in IST, so the first ten characters are the IST day.
            clauses.append(f"substr(fetched_on, 1, 10) IN ({', '.join('?' * len(days))})")
            params.extend(days)
        if flt.follow_ups:
            today = flt.today or to_iso(now_ist())[:10]
            if not is_day(today):
                raise StoreError(f"invalid date: {today!r}")
            states = {
                FOLLOW_UP_DUE: ("follow_up_on = ?", [today]),
                FOLLOW_UP_UPCOMING: ("follow_up_on > ?", [today]),
                FOLLOW_UP_OVERDUE: ("follow_up_on < ?", [today]),
                FOLLOW_UP_NO_DATE: ("follow_up_on IS NULL", []),
            }
            parts: list[str] = []
            for state in flt.follow_ups:
                if state not in states:
                    raise StoreError(f"unknown follow-up state: {state!r}")
                sql, values = states[state]
                parts.append(sql)
                params.extend(values)
            clauses.append(f"follow_up_needed = 1 AND ({' OR '.join(parts)})")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def count_roles(self, flt: RoleFilter | None = None) -> int:
        where, params = self._where(flt or RoleFilter())
        row = self._conn.execute(f"SELECT COUNT(*) FROM roles{where}", params).fetchone()
        return int(row[0])

    def list_roles(
        self, flt: RoleFilter | None = None, *, limit: int | None = None, offset: int = 0
    ) -> list[Role]:
        """Newest fetched first; ties ordered by posted time (newest first), then job id.

        With a follow-up filter, the earliest follow-up date comes first and roles without a
        date last.
        """
        flt = flt or RoleFilter()
        where, params = self._where(flt)
        order = "fetched_on DESC, COALESCE(posted_on, '') DESC, job_id DESC"
        if flt.follow_ups:
            order = "follow_up_on IS NULL, follow_up_on ASC, " + order
        sql = f"SELECT * FROM roles{where} ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = [*params, limit, max(offset, 0)]
        return [_role(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- job descriptions -----------------------------------------------------------------

    def pending_details(
        self, since: str, limit: int, portal: str = PORTAL_LINKEDIN
    ) -> list[tuple[str, str]]:
        """(portal, job_id) of one portal's roles still without a description, newest first.

        Only roles fetched at or after `since` are considered, and a role that has failed
        MAX_DETAIL_ATTEMPTS times is given up on.
        """
        if limit <= 0:
            return []
        rows = self._conn.execute(
            "SELECT portal, job_id FROM roles "
            "WHERE portal = ? AND description IS NULL AND detail_attempts < ? "
            "AND fetched_on >= ? ORDER BY fetched_on DESC, job_id DESC LIMIT ?",
            (portal, MAX_DETAIL_ATTEMPTS, since, limit),
        ).fetchall()
        return [(r["portal"], r["job_id"]) for r in rows]

    def save_detail(self, portal: str, job_id: str, detail: JobDetail, *, when: str) -> None:
        self._conn.execute(
            "UPDATE roles SET description = ?, seniority = ?, employment_type = ?, "
            "industries = ?, job_function = ?, detail_fetched_on = ? "
            "WHERE portal = ? AND job_id = ?",
            (
                detail.description,
                detail.seniority,
                detail.employment_type,
                detail.industries,
                detail.job_function,
                when,
                portal,
                job_id,
            ),
        )

    def close_if_untouched(self, portal: str, job_id: str) -> bool:
        """Set a role to Closed when its status is still the default. Returns whether it changed.

        A role you have already moved to another status is never overridden.
        """
        cursor = self._conn.execute(
            "UPDATE roles SET status = 'Closed' WHERE portal = ? AND job_id = ? AND status = ?",
            (portal, job_id, DEFAULT_STATUS),
        )
        return cursor.rowcount == 1

    def record_detail_failure(self, portal: str, job_id: str) -> None:
        self._conn.execute(
            "UPDATE roles SET detail_attempts = detail_attempts + 1 "
            "WHERE portal = ? AND job_id = ?",
            (portal, job_id),
        )

    def recent_fetch_days(self, today: str, days: int = 14) -> list[tuple[str, int]]:
        """(IST day, roles first fetched that day) for the last `days` days, newest first.

        Only days with roles are listed.
        """
        first = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
        rows = self._conn.execute(
            "SELECT substr(fetched_on, 1, 10) AS day, COUNT(*) AS n FROM roles "
            "WHERE substr(fetched_on, 1, 10) >= ? GROUP BY day ORDER BY day DESC",
            (first,),
        ).fetchall()
        return [(row["day"], int(row["n"])) for row in rows]

    def fetch_summary(self, portal: str | None = None) -> list[FetchDay]:
        """Roles per IST fetch day and current status, newest day first.

        Days between the first and last fetch with no new roles are included with zeros, so a
        missed scan is visible. Every status in STATUSES has an entry on every day. Every portal
        is counted unless `portal` names one.
        """
        counts: dict[str, dict[str, int]] = {}
        where, params = ("", []) if portal is None else (" WHERE portal = ?", [portal])
        for row in self._conn.execute(
            "SELECT substr(fetched_on, 1, 10) AS day, status, COUNT(*) AS n "
            f"FROM roles{where} GROUP BY day, status",
            params,
        ):
            counts.setdefault(row["day"], {})[row["status"]] = int(row["n"])
        if not counts:
            return []
        first = date.fromisoformat(min(counts))
        last = date.fromisoformat(max(counts))
        days: list[FetchDay] = []
        current = last
        while current >= first:
            day = current.isoformat()
            found = counts.get(day, {})
            by_status = {status: found.get(status, 0) for status in STATUSES}
            days.append(FetchDay(day, sum(by_status.values()), by_status))
            current -= timedelta(days=1)
        return days

    def set_fit_rating(self, portal: str, job_id: str, rating: str | None) -> None:
        """Record the user's verdict on a role: "good", "bad", or None to clear it."""
        if rating not in (None, "good", "bad"):
            raise StoreError(f"unknown rating: {rating!r}")
        if not self.role_exists(portal, job_id):
            raise StoreError(f"unknown role: {portal}|{job_id}")
        self._conn.execute(
            "UPDATE roles SET fit_rating = ? WHERE portal = ? AND job_id = ?",
            (rating, portal, job_id),
        )

    def list_labelled_roles(self, portals: Sequence[str] = ()) -> list[Role]:
        """Roles the user has rated good or bad, from the named portals (all when none named)."""
        for portal in portals:
            if portal not in PORTALS:
                raise StoreError(f"unknown portal: {portal!r}")
        extra = f" AND portal IN ({', '.join('?' * len(portals))})" if portals else ""
        rows = self._conn.execute(
            f"SELECT * FROM roles WHERE fit_rating IS NOT NULL{extra} "
            "ORDER BY fetched_on DESC, job_id DESC",
            list(portals),
        ).fetchall()
        return [_role(r) for r in rows]

    def update_status(
        self, keys: Iterable[str], status: str, *, today: datetime | None = None
    ) -> int:
        """Set a status on many roles atomically. Any invalid input changes nothing.

        Applied sets Applied On to today (IST) when empty; Not Applied clears it; other
        statuses leave it as it is.
        """
        if status not in STATUSES:
            raise StoreError(f"unknown status: {status!r}")
        unique = list(dict.fromkeys(keys))
        if not unique:
            raise StoreError("no roles selected")
        pairs = [_split_key(k) for k in unique]
        today_iso = to_iso(today or now_ist())
        with self.transaction():
            missing = [
                k for k, (p, j) in zip(unique, pairs, strict=True) if not self.role_exists(p, j)
            ]
            if missing:
                raise StoreError(f"unknown role: {missing[0]}")
            for portal, job_id in pairs:
                if status == "Applied":
                    self._conn.execute(
                        "UPDATE roles SET status = ?, applied_on = COALESCE(applied_on, ?) "
                        "WHERE portal = ? AND job_id = ?",
                        (status, today_iso, portal, job_id),
                    )
                elif status == DEFAULT_STATUS:
                    self._conn.execute(
                        "UPDATE roles SET status = ?, applied_on = NULL "
                        "WHERE portal = ? AND job_id = ?",
                        (status, portal, job_id),
                    )
                else:
                    self._conn.execute(
                        "UPDATE roles SET status = ? WHERE portal = ? AND job_id = ?",
                        (status, portal, job_id),
                    )
        return len(pairs)

    def update_details(
        self,
        portal: str,
        job_id: str,
        *,
        status: str,
        applied_on: str | None,
        contact_person: str,
        comments: str,
        follow_up_needed: bool = False,
        follow_up_on: str | None = None,
        contact_email: str = "",
        contact_phone: str = "",
        contact_linkedin: str = "",
    ) -> None:
        """Save the editable fields of one role. applied_on is an ISO timestamp or None."""
        if status not in STATUSES:
            raise StoreError(f"unknown status: {status!r}")
        if follow_up_on is not None and not is_day(follow_up_on):
            raise StoreError(f"invalid follow-up date: {follow_up_on!r}")
        cursor = self._conn.execute(
            "UPDATE roles SET status = ?, applied_on = ?, contact_person = ?, comments = ?, "
            "follow_up_needed = ?, follow_up_on = ?, contact_email = ?, contact_phone = ?, "
            "contact_linkedin = ? WHERE portal = ? AND job_id = ?",
            (
                status,
                applied_on,
                contact_person.strip(),
                comments.strip(),
                1 if follow_up_needed else 0,
                follow_up_on,
                contact_email.strip(),
                contact_phone.strip(),
                contact_linkedin.strip(),
                portal,
                job_id,
            ),
        )
        if cursor.rowcount != 1:
            raise StoreError(f"unknown role: {portal}|{job_id}")

    # ---- runs ---------------------------------------------------------------------------

    def start_run(
        self, *, window_seconds: int, keywords_total: int, portal: str = PORTAL_LINKEDIN
    ) -> int:
        if portal not in PORTALS:
            raise StoreError(f"unknown portal: {portal!r}")
        cursor = self._conn.execute(
            "INSERT INTO runs (started_on, status, window_seconds, keywords_total, portal) "
            "VALUES (?, ?, ?, ?, ?)",
            (to_iso(now_ist()), RUN_RUNNING, window_seconds, keywords_total, portal),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def finish_run(self, run_id: int, status: str, note: str = "") -> None:
        if status not in RUN_STATUSES or status == RUN_RUNNING:
            raise StoreError(f"invalid final run status: {status!r}")
        cursor = self._conn.execute(
            "UPDATE runs SET status = ?, finished_on = ?, note = ? WHERE id = ?",
            (status, to_iso(now_ist()), note, run_id),
        )
        if cursor.rowcount != 1:
            raise StoreError(f"unknown run: {run_id}")

    def fail_stale_runs(self, note: str = "Interrupted: process ended without finishing") -> int:
        """Mark runs still 'running' as failed. Call only while holding the run lock."""
        cursor = self._conn.execute(
            "UPDATE runs SET status = ?, finished_on = ?, note = ? WHERE status = ?",
            (RUN_FAILED, to_iso(now_ist()), note, RUN_RUNNING),
        )
        return cursor.rowcount

    def record_keyword(self, run_id: int, record: KeywordRecord) -> None:
        self._conn.execute(
            "INSERT INTO run_keywords (run_id, keyword, pages, cards, matched, new_roles, "
            "possible_dups, throttled, retries, failed_pages, malformed, completed, stopped_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                record.keyword,
                record.pages,
                record.cards,
                record.matched,
                record.new_roles,
                record.possible_dups,
                record.throttled,
                record.retries,
                record.failed_pages,
                record.malformed,
                int(record.completed),
                record.stopped_by,
            ),
        )

    def record_pages(self, run_id: int, keyword: str, pages: Iterable[PageStat]) -> None:
        self._conn.executemany(
            "INSERT INTO run_pages (run_id, keyword, page_index, cards, matched) "
            "VALUES (?, ?, ?, ?, ?)",
            [(run_id, keyword, p.page_index, p.cards, p.matched) for p in pages],
        )

    def add_phrase_matches(self, run_id: int, counts: dict[str, int]) -> None:
        self._conn.executemany(
            "INSERT INTO run_phrase_matches (run_id, phrase, matches) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id, phrase) DO UPDATE SET matches = matches + excluded.matches",
            [(run_id, phrase, n) for phrase, n in counts.items() if n > 0],
        )

    def get_run(self, run_id: int) -> RunSummary | None:
        row = self._conn.execute("SELECT * FROM run_summary WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else _run(row)

    def list_runs(self, limit: int = 50, portal: str | None = None) -> list[RunSummary]:
        """Newest first. Every portal's runs unless `portal` names one."""
        where, params = ("", []) if portal is None else (" WHERE portal = ?", [portal])
        rows = self._conn.execute(
            f"SELECT * FROM run_summary{where} ORDER BY id DESC LIMIT ?", [*params, limit]
        ).fetchall()
        return [_run(r) for r in rows]

    def latest_run(self, portal: str = PORTAL_LINKEDIN) -> RunSummary | None:
        runs = self.list_runs(1, portal)
        return runs[0] if runs else None

    def last_successful_run(self, portal: str = PORTAL_LINKEDIN) -> RunSummary | None:
        row = self._conn.execute(
            "SELECT * FROM run_summary WHERE status = ? AND portal = ? ORDER BY id DESC LIMIT 1",
            (RUN_SUCCESS, portal),
        ).fetchone()
        return None if row is None else _run(row)

    def keyword_records(self, run_id: int) -> list[KeywordRecord]:
        rows = self._conn.execute(
            "SELECT * FROM run_keywords WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [
            KeywordRecord(
                keyword=r["keyword"],
                pages=r["pages"],
                cards=r["cards"],
                matched=r["matched"],
                new_roles=r["new_roles"],
                possible_dups=r["possible_dups"],
                throttled=r["throttled"],
                retries=r["retries"],
                failed_pages=r["failed_pages"],
                malformed=r["malformed"],
                completed=bool(r["completed"]),
                stopped_by=r["stopped_by"],
            )
            for r in rows
        ]

    def depth_stats(self, run_id: int) -> list[DepthStat]:
        rows = self._conn.execute(
            "SELECT page_index, SUM(cards) AS cards, SUM(matched) AS matched FROM run_pages "
            "WHERE run_id = ? GROUP BY page_index ORDER BY page_index",
            (run_id,),
        ).fetchall()
        return [DepthStat(r["page_index"], r["cards"], r["matched"]) for r in rows]

    def phrase_matches(self, run_id: int) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT phrase, matches FROM run_phrase_matches WHERE run_id = ? "
            "ORDER BY matches DESC, phrase",
            (run_id,),
        ).fetchall()
        return [(r["phrase"], r["matches"]) for r in rows]

    # ---- skipped titles -----------------------------------------------------------------

    def add_skipped(self, run_id: int, rows: Iterable[tuple[str, str, str, str, str]]) -> None:
        """Rows are (portal, job_id, title, company, fetched_on). Repeats in a run are ignored."""
        self._conn.executemany(
            "INSERT OR IGNORE INTO skipped_titles (run_id, portal, job_id, title, company, "
            "fetched_on) VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, *row) for row in rows],
        )

    def list_skipped(self, run_id: int | None = None, limit: int = 500) -> list[SkippedTitle]:
        sql = "SELECT * FROM skipped_titles"
        params: list[object] = []
        if run_id is not None:
            sql += " WHERE run_id = ?"
            params.append(run_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [
            SkippedTitle(
                r["run_id"], r["portal"], r["job_id"], r["title"], r["company"], r["fetched_on"]
            )
            for r in self._conn.execute(sql, params).fetchall()
        ]

    def recent_skipped(
        self, since: str, limit: int, portal: str | None = None
    ) -> tuple[list[SkippedTitle], int]:
        """Titles skipped since `since` across all runs, each role once, newest first.

        Returns the first `limit` of them and how many there are in all.
        """
        extra, more = ("", []) if portal is None else (" AND portal = ?", [portal])
        rows = self._conn.execute(
            "SELECT run_id, portal, job_id, title, company, MAX(fetched_on) AS fetched_on "
            f"FROM skipped_titles WHERE fetched_on >= ?{extra} GROUP BY portal, job_id "
            "ORDER BY fetched_on DESC, id DESC LIMIT ?",
            [since, *more, limit],
        ).fetchall()
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM skipped_titles WHERE fetched_on >= ?{extra} "
            "GROUP BY portal, job_id)",
            [since, *more],
        ).fetchone()[0]
        titles = [
            SkippedTitle(
                r["run_id"], r["portal"], r["job_id"], r["title"], r["company"], r["fetched_on"]
            )
            for r in rows
        ]
        return titles, total

    def purge_skipped(self, retention_days: int, *, now: datetime | None = None) -> int:
        cutoff = to_iso((now or now_ist()) - timedelta(days=retention_days))
        cursor = self._conn.execute("DELETE FROM skipped_titles WHERE fetched_on < ?", (cutoff,))
        return cursor.rowcount

    # ---- CV, embeddings and scores -----------------------------------------------------------

    def get_cv(self) -> Cv | None:
        row = self._conn.execute("SELECT * FROM cv WHERE id = 1").fetchone()
        if row is None:
            return None
        return Cv(row["filename"], row["text"], row["sha"], row["uploaded_on"])

    def get_cv_chunks(self) -> list[tuple[str, str]]:
        """(text, text hash) of the CV's chunks, in CV order."""
        rows = self._conn.execute("SELECT text, text_hash FROM cv_chunks ORDER BY position")
        return [(r["text"], r["text_hash"]) for r in rows]

    def save_cv(self, cv: Cv, chunks: Sequence[tuple[str, str]]) -> None:
        """Replace the CV and its chunks. Vectors of chunks that no longer exist are dropped."""
        with self.transaction():
            old = {text_hash for _, text_hash in self.get_cv_chunks()}
            self._conn.execute("DELETE FROM cv_chunks")
            self._conn.execute(
                "INSERT INTO cv (id, filename, text, sha, uploaded_on) VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET filename = excluded.filename, "
                "text = excluded.text, sha = excluded.sha, uploaded_on = excluded.uploaded_on",
                (cv.filename, cv.text, cv.sha, cv.uploaded_on),
            )
            self._conn.executemany(
                "INSERT INTO cv_chunks (position, text, text_hash) VALUES (?, ?, ?)",
                [(i, text, text_hash) for i, (text, text_hash) in enumerate(chunks)],
            )
            gone = sorted(old - {text_hash for _, text_hash in chunks})
            for start in range(0, len(gone), _IN_CHUNK):
                part = gone[start : start + _IN_CHUNK]
                marks = ",".join("?" * len(part))
                self._conn.execute(f"DELETE FROM embeddings WHERE text_hash IN ({marks})", part)

    def get_embeddings(self, hashes: Iterable[str], space: str) -> dict[str, bytes]:
        wanted = list(dict.fromkeys(hashes))
        found: dict[str, bytes] = {}
        for start in range(0, len(wanted), _IN_CHUNK):
            part = wanted[start : start + _IN_CHUNK]
            marks = ",".join("?" * len(part))
            rows = self._conn.execute(
                f"SELECT text_hash, vector FROM embeddings WHERE space = ? "
                f"AND text_hash IN ({marks})",
                [space, *part],
            )
            found.update({r["text_hash"]: bytes(r["vector"]) for r in rows})
        return found

    def save_embeddings(self, space: str, vectors: Mapping[str, bytes]) -> None:
        with self.transaction():
            self._conn.executemany(
                "INSERT OR REPLACE INTO embeddings (text_hash, space, vector) VALUES (?, ?, ?)",
                [(h, space, blob) for h, blob in vectors.items()],
            )

    def get_scores(self) -> dict[str, RoleScore]:
        rows = self._conn.execute("SELECT * FROM role_scores").fetchall()
        return {score.key: score for score in map(_role_score, rows)}

    def save_score(self, score: RoleScore) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO role_scores (portal, job_id, keyword_raw, semantic_raw, "
            "chunks, inputs, scored_on, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                score.portal,
                score.job_id,
                score.keyword_raw,
                score.semantic_raw,
                score.chunks,
                score.inputs,
                score.scored_on,
                score.run_id,
            ),
        )

    # ---- scoring runs -------------------------------------------------------------------------

    def start_scoring_run(self, trigger: str, *, when: str, source: str = "") -> int:
        cursor = self._conn.execute(
            "INSERT INTO scoring_runs (started_on, trigger, status, source) "
            "VALUES (?, ?, 'running', ?)",
            (when, trigger, source),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def update_scoring_run(
        self,
        run_id: int,
        counters: Mapping[str, int],
        *,
        excluded_by_reason: Mapping[str, int] | None = None,
    ) -> None:
        unknown = set(counters) - SCORING_COUNTERS
        if unknown:
            raise StoreError(f"unknown scoring counter: {sorted(unknown)[0]}")
        assignments = [f"{name} = ?" for name in counters]
        params: list[object] = list(counters.values())
        if excluded_by_reason is not None:
            assignments.append("excluded_by_reason = ?")
            params.append(json.dumps(dict(excluded_by_reason), sort_keys=True))
        if assignments:
            self._conn.execute(
                f"UPDATE scoring_runs SET {', '.join(assignments)} WHERE id = ?", [*params, run_id]
            )

    def finish_scoring_run(self, run_id: int, status: str, note: str, *, when: str) -> None:
        if status not in SCORING_STATUSES or status == "running":
            raise StoreError(f"invalid scoring run status: {status!r}")
        self._conn.execute(
            "UPDATE scoring_runs SET status = ?, note = ?, finished_on = ? WHERE id = ?",
            (status, note, when, run_id),
        )

    def fail_stale_scoring_runs(self, *, when: str) -> int:
        cursor = self._conn.execute(
            "UPDATE scoring_runs SET status = 'failed', finished_on = ?, "
            "note = 'Interrupted: process ended without finishing' WHERE status = 'running'",
            (when,),
        )
        return cursor.rowcount

    def get_scoring_run(self, run_id: int) -> ScoringRun | None:
        row = self._conn.execute("SELECT * FROM scoring_runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else _scoring_run(row)

    def list_scoring_runs(self, limit: int = 10) -> list[ScoringRun]:
        rows = self._conn.execute(
            "SELECT * FROM scoring_runs ORDER BY started_on DESC, id DESC LIMIT ?", (limit,)
        )
        return [_scoring_run(r) for r in rows]

    def texts_embedded_since(self, since: str) -> int:
        """Texts sent to the embedding service in runs that started at or after `since`."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(texts_embedded), 0) FROM scoring_runs WHERE started_on >= ?",
            (since,),
        ).fetchone()
        return int(row[0])
