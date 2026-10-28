"""Database access for the companies whose career pages are read."""

from __future__ import annotations

import sqlite3

from .models import SitesCompany
from .timeutil import now_ist, to_iso


def _company(row: sqlite3.Row) -> SitesCompany:
    return SitesCompany(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        system=row["system"],
        board=row["board"],
        tag=row["tag"],
        enabled=bool(row["enabled"]),
        last_run_on=row["last_run_on"],
        last_status=row["last_status"],
        last_note=row["last_note"],
        last_found=row["last_found"],
    )


class SitesStoreMixin:
    _conn: sqlite3.Connection

    def list_sites(self) -> list[SitesCompany]:
        rows = self._conn.execute("SELECT * FROM sites_companies ORDER BY name COLLATE NOCASE")
        return [_company(r) for r in rows.fetchall()]

    def get_site(self, site_id: int) -> SitesCompany | None:
        row = self._conn.execute(
            "SELECT * FROM sites_companies WHERE id = ?", (site_id,)
        ).fetchone()
        return _company(row) if row else None

    def upsert_site(self, name: str, url: str, system: str, board: str, tag: str) -> bool:
        """Add a company, or update the link and tag of one with the same name. True if new."""
        existing = self._conn.execute(
            "SELECT id FROM sites_companies WHERE name = ?", (name,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO sites_companies (name, url, system, board, tag, created_on) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, url, system, board, tag, to_iso(now_ist())),
            )
            return True
        self._conn.execute(
            "UPDATE sites_companies SET name = ?, url = ?, system = ?, board = ?, tag = ?, "
            "last_status = '', last_note = '', last_found = 0 WHERE id = ?",
            (name, url, system, board, tag, existing["id"]),
        )
        return False

    def set_site_system(self, site_id: int, system: str, board: str) -> None:
        self._conn.execute(
            "UPDATE sites_companies SET system = ?, board = ? WHERE id = ?",
            (system, board, site_id),
        )

    def set_site_enabled(self, site_id: int, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE sites_companies SET enabled = ? WHERE id = ?", (int(enabled), site_id)
        )

    def delete_site(self, site_id: int) -> None:
        self._conn.execute("DELETE FROM sites_companies WHERE id = ?", (site_id,))

    def record_site_result(self, site_id: int, status: str, note: str, found: int) -> None:
        self._conn.execute(
            "UPDATE sites_companies SET last_run_on = ?, last_status = ?, last_note = ?, "
            "last_found = ? WHERE id = ?",
            (to_iso(now_ist()), status, note, found, site_id),
        )
