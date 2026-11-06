"""Database access for AI fit verdicts and the log of AI calls."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

CALL_NOTE_MAX = 300


@dataclass(frozen=True)
class AiCallRow:
    """One logged AI call. No request or reply text is kept, only how the call went."""

    id: int
    portal: str
    job_id: str
    called_on: str
    ok: bool
    note: str
    model: str
    status: int | None
    error_text: str
    ms: int | None
    tokens_in: int | None
    tokens_out: int | None

    @property
    def key(self) -> str:
        return f"{self.portal}|{self.job_id}"


def _call_row(r: sqlite3.Row) -> AiCallRow:
    return AiCallRow(
        id=r["id"],
        portal=r["portal"],
        job_id=r["job_id"],
        called_on=r["called_on"],
        ok=bool(r["ok"]),
        note=r["note"],
        model=r["model"],
        status=r["status"],
        error_text=r["error_text"],
        ms=r["ms"],
        tokens_in=r["tokens_in"],
        tokens_out=r["tokens_out"],
    )


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

    def log_ai_call(
        self,
        portal: str,
        job_id: str,
        *,
        when: str,
        ok: bool,
        note: str = "",
        model: str = "",
        status: int | None = None,
        error_text: str = "",
        ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO ai_calls (portal, job_id, called_on, ok, note, model, status, "
            "error_text, ms, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                portal,
                job_id,
                when,
                int(ok),
                note,
                model,
                status,
                error_text[:CALL_NOTE_MAX],
                ms,
                tokens_in,
                tokens_out,
            ),
        )

    def ai_calls_between(self, since: str) -> list[AiCallRow]:
        """Every logged call at or after `since`, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM ai_calls WHERE called_on >= ? ORDER BY id", (since,)
        ).fetchall()
        return [_call_row(r) for r in rows]

    def unpushed_ai_calls(self, limit: int) -> list[AiCallRow]:
        """Calls not yet sent to Langfuse, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM ai_calls WHERE pushed_on IS NULL ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [_call_row(r) for r in rows]

    def mark_ai_calls_pushed(self, ids: list[int], when: str) -> None:
        self._conn.executemany(
            "UPDATE ai_calls SET pushed_on = ? WHERE id = ?", [(when, i) for i in ids]
        )

    def pushed_ai_call_ids(self) -> set[int]:
        rows = self._conn.execute("SELECT id FROM ai_calls WHERE pushed_on IS NOT NULL").fetchall()
        return {int(r["id"]) for r in rows}

    def get_langfuse_scores(self) -> dict[str, tuple[str, int, int]]:
        """Per role: (trace id, human rating 1 or 0, agreement 1, 0 or -1) last sent."""
        rows = self._conn.execute("SELECT * FROM langfuse_scores").fetchall()
        return {
            f"{r['portal']}|{r['job_id']}": (r["trace_id"], int(r["human"]), int(r["agree"]))
            for r in rows
        }

    def save_langfuse_score(
        self, portal: str, job_id: str, *, trace_id: str, human: int, agree: int, when: str
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO langfuse_scores (portal, job_id, trace_id, human, agree, "
            "pushed_on) VALUES (?, ?, ?, ?, ?, ?)",
            (portal, job_id, trace_id, human, agree, when),
        )

    def get_push_state(self) -> tuple[str, int, int, str] | None:
        """(when, traces sent, ratings sent, error) of the last push, or None if never run."""
        row = self._conn.execute("SELECT * FROM langfuse_push WHERE id = 1").fetchone()
        if row is None:
            return None
        return row["pushed_on"], int(row["traces"]), int(row["ratings"]), row["error"]

    def save_push_state(self, when: str, traces: int, ratings: int, error: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO langfuse_push (id, pushed_on, traces, ratings, error) "
            "VALUES (1, ?, ?, ?, ?)",
            (when, traces, ratings, error[:300]),
        )

    def latest_ok_ai_calls(self) -> dict[str, AiCallRow]:
        """The latest successful call per role, for the 'scored by' line."""
        rows = self._conn.execute(
            "SELECT * FROM ai_calls WHERE id IN "
            "(SELECT MAX(id) FROM ai_calls WHERE ok = 1 GROUP BY portal, job_id)"
        ).fetchall()
        return {f"{r['portal']}|{r['job_id']}": _call_row(r) for r in rows}

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
