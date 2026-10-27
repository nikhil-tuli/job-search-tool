"""Database access for Email Sync."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .models import (
    SYNC_APPLIED,
    SYNC_REVIEW,
    SyncState,
    SyncUpdate,
)

_FIELDS = (
    "id, portal, job_id, company, title, previous_status, new_status, subject, sender, "
    "email_date, message_id, confidence, state, created_on, changed_on, note"
)


def _update(row: sqlite3.Row) -> SyncUpdate:
    return SyncUpdate(**{name: row[name] for name in SyncUpdate.__dataclass_fields__})


class SyncStoreMixin:
    _conn: sqlite3.Connection

    def create_sync_update(
        self,
        *,
        portal: str,
        job_id: str,
        company: str,
        title: str,
        previous_status: str,
        new_status: str,
        subject: str,
        sender: str,
        email_date: str,
        message_id: str,
        confidence: str,
        state: str,
        when: str,
        note: str = "",
    ) -> int | None:
        """Save one update. None when this email already has one."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO sync_updates (portal, job_id, company, title, previous_status, "
            "new_status, subject, sender, email_date, message_id, confidence, state, created_on, "
            "changed_on, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                portal,
                job_id,
                company,
                title,
                previous_status,
                new_status,
                subject[:300],
                sender[:200],
                email_date,
                message_id,
                confidence,
                state,
                when,
                when if state == SYNC_APPLIED else "",
                note,
            ),
        )
        return cursor.lastrowid if cursor.rowcount else None

    def get_sync_update(self, update_id: int) -> SyncUpdate | None:
        row = self._conn.execute(
            f"SELECT {_FIELDS} FROM sync_updates WHERE id = ?", (update_id,)
        ).fetchone()
        return None if row is None else _update(row)

    def list_sync_updates(self, state: str = "", portal: str = "") -> list[SyncUpdate]:
        """Newest first; review rows come first when no state is chosen."""
        where, args = [], []
        if state:
            where.append("state = ?")
            args.append(state)
        if portal:
            where.append("portal = ?")
            args.append(portal)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._conn.execute(
            f"SELECT {_FIELDS} FROM sync_updates {clause} "
            "ORDER BY (state = 'review') DESC, email_date DESC, id DESC",
            args,
        ).fetchall()
        return [_update(r) for r in rows]

    def count_sync_by_state(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM sync_updates GROUP BY state")
        return {r["state"]: r["n"] for r in rows.fetchall()}

    def count_sync_review(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM sync_updates WHERE state = ?", (SYNC_REVIEW,)
        ).fetchone()
        return int(row[0])

    def has_pending_sync(self, portal: str, job_id: str, new_status: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sync_updates WHERE portal = ? AND job_id = ? AND new_status = ? "
            "AND state = ? LIMIT 1",
            (portal, job_id, new_status, SYNC_REVIEW),
        ).fetchone()
        return row is not None

    def set_sync_state_of(self, update_id: int, state: str, *, when: str, note: str = "") -> None:
        self._conn.execute(
            "UPDATE sync_updates SET state = ?, changed_on = ?, note = ? WHERE id = ?",
            (state, when, note, update_id),
        )

    def latest_sync_change(self, portal: str, job_id: str) -> SyncUpdate | None:
        """The newest update that really changed this role's status (automatic or accepted)."""
        row = self._conn.execute(
            f"SELECT {_FIELDS} FROM sync_updates WHERE portal = ? AND job_id = ? "
            "AND state IN ('applied', 'accepted') ORDER BY changed_on DESC, id DESC LIMIT 1",
            (portal, job_id),
        ).fetchone()
        return None if row is None else _update(row)

    # ---- mail already read -------------------------------------------------------------------

    def seen_messages(self, ids: Iterable[str]) -> set[str]:
        wanted = list(ids)
        found: set[str] = set()
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT message_id FROM sync_seen WHERE message_id IN ({marks})", chunk
            )
            found.update(r[0] for r in rows.fetchall())
        return found

    def mark_messages_seen(self, ids: Iterable[str], *, when: str) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO sync_seen (message_id, seen_on) VALUES (?, ?)",
            [(i, when) for i in ids],
        )

    # ---- the sync itself ---------------------------------------------------------------------

    def get_sync_state(self) -> SyncState:
        row = self._conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
        return SyncState(
            last_synced_on=row["last_synced_on"],
            running_since=row["running_since"],
            error=row["error"],
            checked=row["checked"],
            ignored=row["ignored"],
            phase=row["phase"],
            done=row["done"],
            total=row["total"],
            available=row["available"],
        )

    def set_sync_progress(self, phase: str, done: int, total: int, available: int = 0) -> None:
        self._conn.execute(
            "UPDATE sync_state SET phase = ?, done = ?, total = ?, available = ? WHERE id = 1",
            (phase, done, total, available or total),
        )

    def begin_sync(self, *, now: str, stale_before: str) -> bool:
        """Claim the sync. False when another one is running (and not stuck)."""
        cursor = self._conn.execute(
            "UPDATE sync_state SET running_since = ? WHERE id = 1 "
            "AND (running_since = '' OR running_since < ?)",
            (now, stale_before),
        )
        return cursor.rowcount == 1

    def finish_sync(self, *, now: str, error: str, checked: int, ignored: int) -> None:
        """End a sync. Only a good one moves the last-synced time."""
        if error:
            self._conn.execute(
                "UPDATE sync_state SET running_since = '', error = ?, phase = '', done = 0, "
                "total = 0, available = 0 WHERE id = 1",
                (error,),
            )
            return
        self._conn.execute(
            "UPDATE sync_state SET running_since = '', error = '', last_synced_on = ?, "
            "checked = ?, ignored = ?, phase = '', done = 0, total = 0, available = 0 "
            "WHERE id = 1",
            (now, checked, ignored),
        )
