"""Database access for the Word CV file and tailored resume requests."""

from __future__ import annotations

import sqlite3

from .models import (
    TAILOR_ACTIVE,
    TAILOR_FAILED,
    TAILOR_PREPARING,
    TAILOR_READY,
    TAILOR_RESUME,
    TAILOR_REVIEW,
    TAILOR_STALE,
    CvFile,
    TailorRequest,
)
from .timeutil import now_ist, to_iso

_LIST_COLUMNS = (
    "id, portal, job_id, company, title, cv_filename, status, created_on, updated_on, "
    "proposed_count, applied_count, output_name, note, kind"
)
_ACTIVE_SQL = ",".join(f"'{s}'" for s in TAILOR_ACTIVE)


def _request(row: sqlite3.Row, *, full: bool = False) -> TailorRequest:
    return TailorRequest(
        id=row["id"],
        portal=row["portal"],
        job_id=row["job_id"],
        company=row["company"],
        title=row["title"],
        cv_filename=row["cv_filename"],
        status=row["status"],
        created_on=row["created_on"],
        updated_on=row["updated_on"],
        proposed_count=row["proposed_count"],
        applied_count=row["applied_count"],
        output_name=row["output_name"],
        note=row["note"],
        proposal=row["proposal"] if full else "",
        applied=row["applied"] if full else "",
        description=row["description"] if full else "",
        kind=row["kind"],
    )


class TailorStoreMixin:
    _conn: sqlite3.Connection

    # ---- the Word CV --------------------------------------------------------------------

    def get_cv_file(self) -> CvFile | None:
        row = self._conn.execute("SELECT * FROM cv_files WHERE id = 1").fetchone()
        if row is None:
            return None
        return CvFile(row["filename"], bytes(row["data"]), row["sha"], row["uploaded_on"])

    def save_cv_file(self, file: CvFile) -> None:
        self._conn.execute(
            "INSERT INTO cv_files (id, filename, data, sha, uploaded_on) VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET filename = excluded.filename, data = excluded.data, "
            "sha = excluded.sha, uploaded_on = excluded.uploaded_on",
            (file.filename, file.data, file.sha, file.uploaded_on),
        )

    def clear_cv_file(self) -> None:
        self._conn.execute("DELETE FROM cv_files WHERE id = 1")

    # ---- the cover letter template -------------------------------------------------------

    def get_cover_template(self) -> CvFile | None:
        row = self._conn.execute("SELECT * FROM cover_templates WHERE id = 1").fetchone()
        if row is None:
            return None
        return CvFile(row["filename"], bytes(row["data"]), row["sha"], row["uploaded_on"])

    def save_cover_template(self, file: CvFile) -> None:
        self._conn.execute(
            "INSERT INTO cover_templates (id, filename, data, sha, uploaded_on) "
            "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET filename = excluded.filename, "
            "data = excluded.data, sha = excluded.sha, uploaded_on = excluded.uploaded_on",
            (file.filename, file.data, file.sha, file.uploaded_on),
        )

    def clear_cover_template(self) -> None:
        self._conn.execute("DELETE FROM cover_templates WHERE id = 1")

    def get_tailor_template_sha(self, request_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT template_sha FROM tailor_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return row["template_sha"] if row else None

    # ---- requests -----------------------------------------------------------------------

    def create_tailor_request(
        self,
        *,
        portal: str,
        job_id: str,
        company: str,
        title: str,
        description: str,
        cv_filename: str,
        cv_sha: str,
        kind: str = TAILOR_RESUME,
        template_sha: str = "",
    ) -> int:
        when = to_iso(now_ist())
        cursor = self._conn.execute(
            "INSERT INTO tailor_requests (portal, job_id, company, title, description, "
            "cv_filename, cv_sha, status, created_on, updated_on, kind, template_sha) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                portal,
                job_id,
                company,
                title,
                description,
                cv_filename,
                cv_sha,
                TAILOR_PREPARING,
                when,
                when,
                kind,
                template_sha,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def active_tailor_request(
        self, portal: str, job_id: str, kind: str = TAILOR_RESUME
    ) -> TailorRequest | None:
        row = self._conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM tailor_requests WHERE portal = ? AND job_id = ? "
            f"AND kind = ? AND status IN ({_ACTIVE_SQL}) ORDER BY id DESC LIMIT 1",
            (portal, job_id, kind),
        ).fetchone()
        return _request(row) if row else None

    def get_tailor_request(self, request_id: int) -> TailorRequest | None:
        row = self._conn.execute(
            "SELECT * FROM tailor_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return _request(row, full=True) if row else None

    def get_tailor_cv_sha(self, request_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT cv_sha FROM tailor_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return row["cv_sha"] if row else None

    def get_tailor_output(self, request_id: int) -> tuple[str, bytes] | None:
        row = self._conn.execute(
            "SELECT output_name, output FROM tailor_requests WHERE id = ? AND status = ?",
            (request_id, TAILOR_READY),
        ).fetchone()
        if row is None or row["output"] is None:
            return None
        return row["output_name"], bytes(row["output"])

    def list_tailor_requests(self, limit: int = 200) -> list[TailorRequest]:
        rows = self._conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM tailor_requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_request(r) for r in rows]

    def count_tailor_review(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM tailor_requests WHERE status = ?", (TAILOR_REVIEW,)
            ).fetchone()[0]
        )

    def _set_tailor(self, request_id: int, sql: str, params: tuple[object, ...]) -> None:
        self._conn.execute(
            f"UPDATE tailor_requests SET {sql}, updated_on = ? WHERE id = ?",
            (*params, to_iso(now_ist()), request_id),
        )

    def set_tailor_proposal(self, request_id: int, proposal_json: str, count: int) -> None:
        self._set_tailor(
            request_id,
            "status = ?, proposal = ?, proposed_count = ?, note = ''",
            (TAILOR_REVIEW, proposal_json, count),
        )

    def set_tailor_ready(
        self, request_id: int, applied_json: str, count: int, name: str, data: bytes
    ) -> None:
        self._set_tailor(
            request_id,
            "status = ?, applied = ?, applied_count = ?, output_name = ?, output = ?, note = ''",
            (TAILOR_READY, applied_json, count, name, data),
        )

    def set_tailor_status(self, request_id: int, status: str, note: str = "") -> None:
        self._set_tailor(request_id, "status = ?, note = ?", (status, note))

    def set_tailor_failed(self, request_id: int, note: str) -> None:
        self.set_tailor_status(request_id, TAILOR_FAILED, note)

    def fail_stale_tailor(self) -> int:
        """At start-up: a request still 'preparing' belonged to a run that no longer exists."""
        cursor = self._conn.execute(
            "UPDATE tailor_requests SET status = ?, note = ?, updated_on = ? WHERE status = ?",
            (
                TAILOR_FAILED,
                "The app was closed while this was being prepared. Try again.",
                to_iso(now_ist()),
                TAILOR_PREPARING,
            ),
        )
        return cursor.rowcount

    def mark_tailor_stale(self, kind: str | None = None, note: str = "") -> None:
        """Requests waiting on a replaced file can no longer be applied.

        A new CV stales both kinds; a new cover letter template stales only cover letters.
        """
        where, params = (
            "status IN (?, ?)",
            [
                TAILOR_STALE,
                note or "Your CV was replaced. Start again.",
                to_iso(now_ist()),
                TAILOR_PREPARING,
                TAILOR_REVIEW,
            ],
        )
        if kind is not None:
            where += " AND kind = ?"
            params.append(kind)
        self._conn.execute(
            f"UPDATE tailor_requests SET status = ?, note = ?, updated_on = ? WHERE {where}",
            params,
        )

    def db_path(self) -> str | None:
        """The database file, or None for an in-memory database."""
        row = self._conn.execute("PRAGMA database_list").fetchone()
        return row["file"] or None if row else None


__all__ = ["TAILOR_FAILED", "TAILOR_REVIEW", "TAILOR_STALE", "TailorStoreMixin"]
