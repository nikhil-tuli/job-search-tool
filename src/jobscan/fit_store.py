"""Database access for the Fit result saved when a role was considered for scoring."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from .fit_filters import FitResult, Reason


class FitStoreMixin:
    _conn: sqlite3.Connection

    def get_fits(self) -> dict[str, tuple[FitResult, str]]:
        """Saved Fit by role key, with the scoring inputs it was worked out from."""
        rows = self._conn.execute("SELECT portal, job_id, reasons, inputs FROM role_fit")
        found: dict[str, tuple[FitResult, str]] = {}
        for row in rows.fetchall():
            reasons = tuple(Reason(kind, detail) for kind, detail in json.loads(row["reasons"]))
            found[f"{row['portal']}|{row['job_id']}"] = (FitResult(reasons), row["inputs"])
        return found

    def save_fits(self, items: Iterable[tuple[str, str, FitResult, str]], *, when: str) -> None:
        """Save or replace Fit for (portal, job id, result, inputs) items."""
        self._conn.executemany(
            "INSERT OR REPLACE INTO role_fit (portal, job_id, reasons, inputs, decided_on) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    portal,
                    job_id,
                    json.dumps([[r.kind, r.detail] for r in result.reasons]),
                    inputs,
                    when,
                )
                for portal, job_id, result, inputs in items
            ],
        )
