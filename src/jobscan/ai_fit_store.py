"""Database access for AI fit verdicts and the log of AI calls."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class AiFitRow:
    """What the AI said about one role. The band is worked out from these on read."""

    portal: str
    job_id: str
    checks: tuple[tuple[str, str, str], ...]  # (must-have, Met/Partly/Missing, CV evidence)
    role_type_ok: bool
    domain: str  # match, close or different
    reason: str
    trimmed: bool
    inputs: str
    model: str
    scored_on: str

    @property
    def key(self) -> str:
        return f"{self.portal}|{self.job_id}"


class AiFitStoreMixin:
    _conn: sqlite3.Connection

    def get_ai_fits(self) -> dict[str, AiFitRow]:
        rows = self._conn.execute("SELECT * FROM ai_fit").fetchall()
        found: dict[str, AiFitRow] = {}
        for r in rows:
            row = AiFitRow(
                portal=r["portal"],
                job_id=r["job_id"],
                checks=tuple((c[0], c[1], c[2]) for c in json.loads(r["checks"])),
                role_type_ok=bool(r["role_type_ok"]),
                domain=r["domain"],
                reason=r["reason"],
                trimmed=bool(r["trimmed"]),
                inputs=r["inputs"],
                model=r["model"],
                scored_on=r["scored_on"],
            )
            found[row.key] = row
        return found

    def save_ai_fit(self, row: AiFitRow) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO ai_fit (portal, job_id, checks, role_type_ok, domain, reason, "
            "trimmed, inputs, model, scored_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.portal,
                row.job_id,
                json.dumps([list(c) for c in row.checks]),
                int(row.role_type_ok),
                row.domain,
                row.reason,
                int(row.trimmed),
                row.inputs,
                row.model,
                row.scored_on,
            ),
        )

    def log_ai_call(self, portal: str, job_id: str, *, when: str, ok: bool, note: str = "") -> None:
        self._conn.execute(
            "INSERT INTO ai_calls (portal, job_id, called_on, ok, note) VALUES (?, ?, ?, ?, ?)",
            (portal, job_id, when, int(ok), note),
        )

    def ai_calls_since(self, since: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM ai_calls WHERE called_on >= ?", (since,)
        ).fetchone()
        return int(row["n"])

    def last_ai_errors(self) -> dict[str, str]:
        """The latest note of a failed call per role, for roles whose latest call failed."""
        rows = self._conn.execute(
            "SELECT portal, job_id, ok, note FROM ai_calls WHERE id IN "
            "(SELECT MAX(id) FROM ai_calls GROUP BY portal, job_id)"
        ).fetchall()
        return {f"{r['portal']}|{r['job_id']}": r["note"] for r in rows if not r["ok"]}
