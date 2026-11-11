"""Database access for Interview Prep. No audio is ever stored: only text and numbers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from .questions import COMPANY_ROLE, GENERAL_ROLE, Question

IN_PROGRESS = "In progress"
PREPARING = "Preparing"
READY = "Ready"
INCOMPLETE = "Incomplete"
FAILED = "Failed"
STATUSES = (IN_PROGRESS, PREPARING, READY, INCOMPLETE, FAILED)

# answer statuses
ANSWERED = "answered"
SKIPPED = "skipped"
TOO_SHORT = "too_short"
NO_SPEECH = "no_speech"
NOT_SCORED = "not_scored"

PROMPT_VERSION = "1"
STALE_AFTER_MINUTES = 30
PAGE_SIZE = 25


@dataclass(frozen=True)
class InterviewRow:
    id: int
    kind: str
    role_portal: str
    role_job_id: str
    role_company: str
    role_title: str
    general_title: str
    status: str
    started_on: str
    updated_on: str
    cv_name: str
    cv_uploaded_on: str
    read_aloud: bool
    question_model: str
    feedback_model: str
    prompt_version: str
    stage: str
    error: str
    note: str
    stats: dict[str, float]
    questions: int
    answered: int  # answers that were scored or are waiting to be
    spoken_score: int | None
    answer_score: int | None

    @property
    def compare_key(self) -> str:
        """What makes two interviews comparable: same kind and same role or title."""
        if self.kind == COMPANY_ROLE:
            return f"{self.kind}|{self.role_portal}|{self.role_job_id}"
        if self.kind == GENERAL_ROLE:
            return f"{self.kind}|{self.general_title.casefold()}"
        return ""  # My questions are never compared


@dataclass(frozen=True)
class QuestionRow:
    idx: int
    slot: int
    kind: str
    text: str


@dataclass(frozen=True)
class AnswerRow:
    idx: int
    status: str
    transcript: str
    duration_s: float
    word_count: int
    measure: dict[str, object]
    note: str


@dataclass(frozen=True)
class FeedbackRow:
    spoken: dict[str, object]
    answers: list[dict[str, object]]
    spoken_score: int | None
    answer_score: int | None
    tips: list[str]
    missing: list[int]
    created_on: str


_SELECT = """
SELECT i.*,
  (SELECT COUNT(*) FROM interview_question q WHERE q.interview_id = i.id) AS questions,
  (SELECT COUNT(*) FROM interview_answer a
     WHERE a.interview_id = i.id
       AND a.status IN ('answered', 'too_short', 'not_scored')) AS answered,
  f.spoken_score AS spoken_score, f.answer_score AS answer_score
FROM interview i LEFT JOIN interview_feedback f ON f.interview_id = i.id
"""


def _row(r: sqlite3.Row) -> InterviewRow:
    return InterviewRow(
        id=r["id"],
        kind=r["kind"],
        role_portal=r["role_portal"],
        role_job_id=r["role_job_id"],
        role_company=r["role_company"],
        role_title=r["role_title"],
        general_title=r["general_title"],
        status=r["status"],
        started_on=r["started_on"],
        updated_on=r["updated_on"],
        cv_name=r["cv_name"],
        cv_uploaded_on=r["cv_uploaded_on"],
        read_aloud=bool(r["read_aloud"]),
        question_model=r["question_model"],
        feedback_model=r["feedback_model"],
        prompt_version=r["prompt_version"],
        stage=r["stage"],
        error=r["error"],
        note=r["note"],
        stats=json.loads(r["stats"] or "{}"),
        questions=r["questions"],
        answered=r["answered"],
        spoken_score=r["spoken_score"],
        answer_score=r["answer_score"],
    )


class InterviewStoreMixin:
    _conn: sqlite3.Connection

    # ---- creating and reading -------------------------------------------------------------

    def create_interview(
        self,
        *,
        kind: str,
        questions: Sequence[Question],
        now: str,
        role_portal: str = "",
        role_job_id: str = "",
        role_company: str = "",
        role_title: str = "",
        general_title: str = "",
        cv_name: str = "",
        cv_uploaded_on: str = "",
        read_aloud: bool = True,
        question_model: str = "",
        note: str = "",
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO interview (kind, role_portal, role_job_id, role_company, role_title, "
            "general_title, status, started_on, updated_on, cv_name, cv_uploaded_on, read_aloud, "
            "question_model, prompt_version, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                role_portal,
                role_job_id,
                role_company,
                role_title,
                general_title,
                IN_PROGRESS,
                now,
                now,
                cv_name,
                cv_uploaded_on,
                int(read_aloud),
                question_model,
                PROMPT_VERSION,
                note,
            ),
        )
        interview_id = int(cursor.lastrowid or 0)
        self.set_questions(interview_id, questions)
        return interview_id

    def set_questions(self, interview_id: int, questions: Sequence[Question]) -> None:
        self._conn.execute("DELETE FROM interview_question WHERE interview_id = ?", (interview_id,))
        self._conn.executemany(
            "INSERT INTO interview_question (interview_id, idx, slot, kind, text) "
            "VALUES (?, ?, ?, ?, ?)",
            [(interview_id, i, q.slot, q.kind, q.text) for i, q in enumerate(questions, start=1)],
        )

    def add_stats(self, interview_id: int, **deltas: float) -> None:
        """Add to the interview's counters (calls, retries, milliseconds, tokens). No content."""
        row = self._conn.execute(
            "SELECT stats FROM interview WHERE id = ?", (interview_id,)
        ).fetchone()
        if row is None:
            return
        stats: dict[str, float] = json.loads(row["stats"] or "{}")
        for key, value in deltas.items():
            stats[key] = stats.get(key, 0) + value
        self._conn.execute(
            "UPDATE interview SET stats = ? WHERE id = ?", (json.dumps(stats), interview_id)
        )

    def get_interview(self, interview_id: int) -> InterviewRow | None:
        row = self._conn.execute(_SELECT + " WHERE i.id = ?", (interview_id,)).fetchone()
        return _row(row) if row is not None else None

    def list_interviews(
        self, kind: str = "", *, page: int = 1, page_size: int = PAGE_SIZE
    ) -> tuple[list[InterviewRow], int]:
        where, params = ("WHERE i.kind = ?", [kind]) if kind else ("", [])
        total = int(
            self._conn.execute(f"SELECT COUNT(*) FROM interview i {where}", params).fetchone()[0]
        )
        rows = self._conn.execute(
            _SELECT + f" {where} ORDER BY i.started_on DESC, i.id DESC LIMIT ? OFFSET ?",
            [*params, page_size, max(0, page - 1) * page_size],
        ).fetchall()
        return [_row(r) for r in rows], total

    def running_interview(self) -> InterviewRow | None:
        row = self._conn.execute(
            _SELECT + " WHERE i.status = ? ORDER BY i.id DESC LIMIT 1", (IN_PROGRESS,)
        ).fetchone()
        return _row(row) if row is not None else None

    def get_questions(self, interview_id: int) -> list[QuestionRow]:
        rows = self._conn.execute(
            "SELECT idx, slot, kind, text FROM interview_question WHERE interview_id = ? "
            "ORDER BY idx",
            (interview_id,),
        ).fetchall()
        return [QuestionRow(r["idx"], r["slot"], r["kind"], r["text"]) for r in rows]

    # ---- answers --------------------------------------------------------------------------

    def save_answer(
        self,
        interview_id: int,
        idx: int,
        *,
        status: str,
        now: str,
        transcript: str = "",
        duration_s: float = 0.0,
        word_count: int = 0,
        measure: dict[str, object] | None = None,
        note: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO interview_answer (interview_id, idx, status, transcript, duration_s, "
            "word_count, measure, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(interview_id, idx) DO UPDATE SET status = excluded.status, "
            "transcript = excluded.transcript, duration_s = excluded.duration_s, "
            "word_count = excluded.word_count, measure = excluded.measure, note = excluded.note",
            (
                interview_id,
                idx,
                status,
                transcript,
                duration_s,
                word_count,
                json.dumps(measure) if measure is not None else "",
                note,
            ),
        )
        self._conn.execute("UPDATE interview SET updated_on = ? WHERE id = ?", (now, interview_id))

    def get_answers(self, interview_id: int) -> list[AnswerRow]:
        rows = self._conn.execute(
            "SELECT * FROM interview_answer WHERE interview_id = ? ORDER BY idx", (interview_id,)
        ).fetchall()
        return [
            AnswerRow(
                idx=r["idx"],
                status=r["status"],
                transcript=r["transcript"],
                duration_s=r["duration_s"],
                word_count=r["word_count"],
                measure=json.loads(r["measure"]) if r["measure"] else {},
                note=r["note"],
            )
            for r in rows
        ]

    # ---- status ---------------------------------------------------------------------------

    def set_interview_status(
        self, interview_id: int, status: str, *, now: str, stage: str = "", error: str = ""
    ) -> None:
        self._conn.execute(
            "UPDATE interview SET status = ?, stage = ?, error = ?, updated_on = ? WHERE id = ?",
            (status, stage, error, now, interview_id),
        )

    def set_interview_models(self, interview_id: int, feedback_model: str) -> None:
        self._conn.execute(
            "UPDATE interview SET feedback_model = ? WHERE id = ?", (feedback_model, interview_id)
        )

    def expire_stale_interviews(self, cutoff: str, now: str) -> int:
        """An interview still In progress with no activity since `cutoff` becomes Incomplete."""
        cursor = self._conn.execute(
            "UPDATE interview SET status = ?, updated_on = ? WHERE status = ? AND updated_on < ?",
            (INCOMPLETE, now, IN_PROGRESS, cutoff),
        )
        return cursor.rowcount

    def fail_stale_interviews(self, now: str) -> int:
        """At start-up: one still Preparing belonged to a run that no longer exists."""
        cursor = self._conn.execute(
            "UPDATE interview SET status = ?, stage = '', error = ?, updated_on = ? "
            "WHERE status = ?",
            (
                FAILED,
                "The app was closed while this was being prepared. Try again.",
                now,
                PREPARING,
            ),
        )
        return cursor.rowcount

    # ---- feedback -------------------------------------------------------------------------

    def save_feedback(
        self,
        interview_id: int,
        *,
        spoken: dict[str, object],
        answers: list[dict[str, object]],
        spoken_score: int | None,
        answer_score: int | None,
        tips: Sequence[str],
        missing: Sequence[int],
        now: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO interview_feedback (interview_id, spoken, answers, spoken_score, "
            "answer_score, tips, missing, created_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(interview_id) DO UPDATE SET spoken = excluded.spoken, "
            "answers = excluded.answers, spoken_score = excluded.spoken_score, "
            "answer_score = excluded.answer_score, tips = excluded.tips, "
            "missing = excluded.missing, created_on = excluded.created_on",
            (
                interview_id,
                json.dumps(spoken),
                json.dumps(answers),
                spoken_score,
                answer_score,
                json.dumps(list(tips)),
                json.dumps(list(missing)),
                now,
            ),
        )

    def get_feedback(self, interview_id: int) -> FeedbackRow | None:
        r = self._conn.execute(
            "SELECT * FROM interview_feedback WHERE interview_id = ?", (interview_id,)
        ).fetchone()
        if r is None:
            return None
        return FeedbackRow(
            spoken=json.loads(r["spoken"]),
            answers=json.loads(r["answers"]),
            spoken_score=r["spoken_score"],
            answer_score=r["answer_score"],
            tips=json.loads(r["tips"]),
            missing=json.loads(r["missing"]),
            created_on=r["created_on"],
        )

    def previous_interview(self, row: InterviewRow) -> InterviewRow | None:
        """The latest earlier Ready interview of the same kind and role or title."""
        key = row.compare_key
        if not key:
            return None
        candidates = self._conn.execute(
            _SELECT + " WHERE i.status = ? AND i.kind = ? AND i.id != ? AND "
            "(i.started_on < ? OR (i.started_on = ? AND i.id < ?)) "
            "ORDER BY i.started_on DESC, i.id DESC",
            (READY, row.kind, row.id, row.started_on, row.started_on, row.id),
        ).fetchall()
        for r in candidates:
            candidate = _row(r)
            if candidate.compare_key == key:
                return candidate
        return None

    def recent_questions(self, row_kind: str, compare_key: str, limit: int = 5) -> list[str]:
        """Questions of the last `limit` interviews of the same kind, so they are not repeated."""
        rows = self._conn.execute(
            _SELECT + " WHERE i.kind = ? ORDER BY i.started_on DESC, i.id DESC", (row_kind,)
        ).fetchall()
        texts: list[str] = []
        taken = 0
        for r in rows:
            candidate = _row(r)
            if candidate.compare_key != compare_key:
                continue
            texts += [q.text for q in self.get_questions(candidate.id)]
            taken += 1
            if taken >= limit:
                break
        return texts

    # ---- thumbs and deleting --------------------------------------------------------------

    def set_thumb(self, interview_id: int, idx: int, value: int) -> None:
        if value == 0:
            self._conn.execute(
                "DELETE FROM interview_thumb WHERE interview_id = ? AND idx = ?",
                (interview_id, idx),
            )
            return
        self._conn.execute(
            "INSERT INTO interview_thumb (interview_id, idx, value) VALUES (?, ?, ?) "
            "ON CONFLICT(interview_id, idx) DO UPDATE SET value = excluded.value",
            (interview_id, idx, value),
        )

    def get_thumbs(self, interview_id: int) -> dict[int, int]:
        rows = self._conn.execute(
            "SELECT idx, value FROM interview_thumb WHERE interview_id = ?", (interview_id,)
        ).fetchall()
        return {r["idx"]: r["value"] for r in rows}

    def delete_interview(self, interview_id: int) -> bool:
        """Remove the interview with its questions, answers, feedback and thumbs."""
        cursor = self._conn.execute("DELETE FROM interview WHERE id = ?", (interview_id,))
        return cursor.rowcount > 0
