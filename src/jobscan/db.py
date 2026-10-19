"""SQLite connection setup and versioned schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path


class SchemaTooNewError(RuntimeError):
    """The database was created by a newer version of this tool."""


# Frozen per migration: a shipped migration must keep producing the same schema, so it never
# reads the live constants in models.py.
_STATUSES_V1 = (
    "Not Applied",
    "Applied",
    "Initial screening rejected",
    "Interview phase rejected",
    "Interview Ongoing",
    "Interview cleared",
)
_STATUSES_V2 = (_STATUSES_V1[0], "Not relevant", *_STATUSES_V1[1:])
_STATUSES_V3 = (*_STATUSES_V2[:2], "Closed", *_STATUSES_V2[2:])
_STATUSES_V4 = (*_STATUSES_V3[:3], "Skipped", *_STATUSES_V3[3:])


# Title phrases seeded by migration 1. Frozen: later phrases are added by later migrations.
_PHRASES_V1 = (
    "Technical Program Manager",
    "Senior Technical Program Manager",
    "Senior Program Manager",
    "Staff Technical Program Manager",
    "Staff Program Manager",
    "Delivery Manager",
    "TPM",
    "Technical Project Manager",
)
_PHRASES_V5 = (
    "Senior Project Manager",
    "IT Project Manager",
    "Software Program Manager",
    "Technical Project/Program Manager",
    "Technical Program Management",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Run statements one by one. executescript() would commit and break atomicity."""
    pending = ""
    for line in script.splitlines():
        pending += line + "\n"
        if sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip():
        raise ValueError("incomplete SQL statement in migration")


def _migration_1(conn: sqlite3.Connection) -> None:
    _execute_script(
        conn,
        f"""
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_on TEXT NOT NULL,
            finished_on TEXT,
            status TEXT NOT NULL
                CHECK (status IN ('running', 'success', 'partial', 'failed')),
            window_seconds INTEGER NOT NULL,
            keywords_total INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE run_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            keyword TEXT NOT NULL,
            pages INTEGER NOT NULL,
            cards INTEGER NOT NULL,
            matched INTEGER NOT NULL,
            new_roles INTEGER NOT NULL,
            possible_dups INTEGER NOT NULL,
            throttled INTEGER NOT NULL,
            retries INTEGER NOT NULL,
            failed_pages INTEGER NOT NULL,
            malformed INTEGER NOT NULL,
            completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
            stopped_by TEXT NOT NULL
        );
        CREATE INDEX run_keywords_run ON run_keywords(run_id);

        CREATE TABLE run_pages (
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            keyword TEXT NOT NULL,
            page_index INTEGER NOT NULL,
            cards INTEGER NOT NULL,
            matched INTEGER NOT NULL,
            PRIMARY KEY (run_id, keyword, page_index)
        );

        CREATE TABLE run_phrase_matches (
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            phrase TEXT NOT NULL,
            matches INTEGER NOT NULL,
            PRIMARY KEY (run_id, phrase)
        );

        CREATE TABLE roles (
            portal TEXT NOT NULL,
            job_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            link TEXT NOT NULL,
            posted_on TEXT,
            posted_precision TEXT NOT NULL,
            fetched_on TEXT NOT NULL,
            actively_hiring INTEGER NOT NULL DEFAULT 0 CHECK (actively_hiring IN (0, 1)),
            matched_phrase TEXT NOT NULL,
            possible_duplicate_of TEXT,
            dup_key TEXT,
            status TEXT NOT NULL DEFAULT 'Not Applied'
                CHECK (status IN ({_sql_list(_STATUSES_V1)})),
            applied_on TEXT,
            contact_person TEXT NOT NULL DEFAULT '',
            comments TEXT NOT NULL DEFAULT '',
            relevant TEXT CHECK (relevant IN ('Y', 'N')),
            raw_datetime TEXT,
            raw_relative TEXT,
            first_run_id INTEGER REFERENCES runs(id),
            PRIMARY KEY (portal, job_id)
        );
        CREATE INDEX roles_dup_key ON roles(dup_key);
        CREATE INDEX roles_fetched_on ON roles(fetched_on DESC);
        CREATE INDEX roles_status ON roles(status);

        CREATE TABLE phrases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase TEXT NOT NULL UNIQUE COLLATE NOCASE,
            use_as_keyword INTEGER NOT NULL CHECK (use_as_keyword IN (0, 1)),
            use_for_match INTEGER NOT NULL CHECK (use_for_match IN (0, 1)),
            position INTEGER NOT NULL
        );

        CREATE TABLE config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE skipped_titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            portal TEXT NOT NULL,
            job_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            fetched_on TEXT NOT NULL,
            UNIQUE (run_id, portal, job_id)
        );
        CREATE INDEX skipped_titles_fetched ON skipped_titles(fetched_on);

        CREATE VIEW run_summary AS
        SELECT r.id, r.started_on, r.finished_on, r.status, r.window_seconds,
               r.keywords_total, r.note,
               COALESCE(SUM(k.completed), 0) AS keywords_done,
               COALESCE(SUM(k.pages), 0) AS pages,
               COALESCE(SUM(k.cards), 0) AS cards_seen,
               COALESCE(SUM(k.matched), 0) AS matched,
               COALESCE(SUM(k.new_roles), 0) AS new_roles,
               COALESCE(SUM(k.possible_dups), 0) AS possible_dups,
               COALESCE(SUM(k.throttled), 0) AS throttled,
               COALESCE(SUM(k.retries), 0) AS retries,
               COALESCE(SUM(k.failed_pages), 0) AS failed_pages,
               COALESCE(SUM(k.malformed), 0) AS malformed_cards
        FROM runs r LEFT JOIN run_keywords k ON k.run_id = r.id
        GROUP BY r.id;
        """,
    )
    conn.executemany(
        "INSERT INTO phrases (phrase, use_as_keyword, use_for_match, position) VALUES (?, 1, 1, ?)",
        [(phrase, position) for position, phrase in enumerate(_PHRASES_V1)],
    )


_ROLE_COLUMNS = (
    "portal, job_id, title, company, location, link, posted_on, posted_precision, fetched_on, "
    "actively_hiring, matched_phrase, possible_duplicate_of, dup_key, applied_on, "
    "contact_person, comments, raw_datetime, raw_relative, first_run_id"
)


def _roles_v2_table_sql(name: str, statuses: tuple[str, ...]) -> str:
    """CREATE TABLE for the roles layout introduced in schema 2, with the given status list."""
    return f"""
        CREATE TABLE {name} (
            portal TEXT NOT NULL,
            job_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            link TEXT NOT NULL,
            posted_on TEXT,
            posted_precision TEXT NOT NULL,
            fetched_on TEXT NOT NULL,
            actively_hiring INTEGER NOT NULL DEFAULT 0 CHECK (actively_hiring IN (0, 1)),
            matched_phrase TEXT NOT NULL,
            possible_duplicate_of TEXT,
            dup_key TEXT,
            status TEXT NOT NULL DEFAULT 'Not Applied'
                CHECK (status IN ({_sql_list(statuses)})),
            applied_on TEXT,
            contact_person TEXT NOT NULL DEFAULT '',
            comments TEXT NOT NULL DEFAULT '',
            raw_datetime TEXT,
            raw_relative TEXT,
            first_run_id INTEGER REFERENCES runs(id),
            PRIMARY KEY (portal, job_id)
        );
        """


def _migration_2(conn: sqlite3.Connection) -> None:
    """Add the 'Not relevant' status and drop the separate Relevant column.

    Roles marked N that were still 'Not Applied' become 'Not relevant'. Y marks, and N marks on
    roles that already moved on (for example Applied), are dropped.
    """
    _execute_script(
        conn,
        f"""
        {_roles_v2_table_sql("roles_new", _STATUSES_V2)}
        INSERT INTO roles_new (status, {_ROLE_COLUMNS})
        SELECT CASE WHEN relevant = 'N' AND status = 'Not Applied'
                    THEN 'Not relevant' ELSE status END,
               {_ROLE_COLUMNS}
        FROM roles;
        DROP TABLE roles;
        ALTER TABLE roles_new RENAME TO roles;
        CREATE INDEX roles_dup_key ON roles(dup_key);
        CREATE INDEX roles_fetched_on ON roles(fetched_on DESC);
        CREATE INDEX roles_status ON roles(status);
        """,
    )


def _migration_3(conn: sqlite3.Connection) -> None:
    """Add the 'Closed' status. SQLite cannot alter a CHECK, so the table is rebuilt as is."""
    _execute_script(
        conn,
        f"""
        {_roles_v2_table_sql("roles_new", _STATUSES_V3)}
        INSERT INTO roles_new (status, {_ROLE_COLUMNS})
        SELECT status, {_ROLE_COLUMNS} FROM roles;
        DROP TABLE roles;
        ALTER TABLE roles_new RENAME TO roles;
        CREATE INDEX roles_dup_key ON roles(dup_key);
        CREATE INDEX roles_fetched_on ON roles(fetched_on DESC);
        CREATE INDEX roles_status ON roles(status);
        """,
    )


def _migration_4(conn: sqlite3.Connection) -> None:
    """Add the 'Skipped' status. SQLite cannot alter a CHECK, so the table is rebuilt as is."""
    _execute_script(
        conn,
        f"""
        {_roles_v2_table_sql("roles_new", _STATUSES_V4)}
        INSERT INTO roles_new (status, {_ROLE_COLUMNS})
        SELECT status, {_ROLE_COLUMNS} FROM roles;
        DROP TABLE roles;
        ALTER TABLE roles_new RENAME TO roles;
        CREATE INDEX roles_dup_key ON roles(dup_key);
        CREATE INDEX roles_fetched_on ON roles(fetched_on DESC);
        CREATE INDEX roles_status ON roles(status);
        """,
    )


def _migration_5(conn: sqlite3.Connection) -> None:
    """Add five more title phrases (search keyword and title match). Existing ones are kept."""
    start = int(conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM phrases").fetchone()[0])
    conn.executemany(
        "INSERT OR IGNORE INTO phrases (phrase, use_as_keyword, use_for_match, position) "
        "VALUES (?, 1, 1, ?)",
        [(phrase, start + offset) for offset, phrase in enumerate(_PHRASES_V5)],
    )


def _migration_6(conn: sqlite3.Connection) -> None:
    """Add the job description, LinkedIn's criteria list and the description fetch bookkeeping."""
    _execute_script(
        conn,
        """
        ALTER TABLE roles ADD COLUMN description TEXT;
        ALTER TABLE roles ADD COLUMN seniority TEXT;
        ALTER TABLE roles ADD COLUMN employment_type TEXT;
        ALTER TABLE roles ADD COLUMN industries TEXT;
        ALTER TABLE roles ADD COLUMN job_function TEXT;
        ALTER TABLE roles ADD COLUMN detail_fetched_on TEXT;
        ALTER TABLE roles ADD COLUMN detail_attempts INTEGER NOT NULL DEFAULT 0;
        """,
    )


def _migration_7(conn: sqlite3.Connection) -> None:
    """CV, its chunks, cached embeddings, per-role scores and the scoring run log."""
    _execute_script(
        conn,
        """
        CREATE TABLE cv (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            filename TEXT NOT NULL,
            text TEXT NOT NULL,
            sha TEXT NOT NULL,
            uploaded_on TEXT NOT NULL
        );

        CREATE TABLE cv_chunks (
            position INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            text_hash TEXT NOT NULL
        );

        CREATE TABLE embeddings (
            text_hash TEXT NOT NULL,
            space TEXT NOT NULL,
            vector BLOB NOT NULL,
            PRIMARY KEY (text_hash, space)
        );

        CREATE TABLE scoring_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_on TEXT NOT NULL,
            finished_on TEXT,
            trigger TEXT NOT NULL CHECK (trigger IN ('scan', 'manual', 'role')),
            status TEXT NOT NULL
                CHECK (status IN ('running', 'success', 'partial', 'failed')),
            considered INTEGER NOT NULL DEFAULT 0,
            excluded INTEGER NOT NULL DEFAULT 0,
            excluded_by_reason TEXT NOT NULL DEFAULT '{}',
            no_description INTEGER NOT NULL DEFAULT 0,
            selected INTEGER NOT NULL DEFAULT 0,
            scored INTEGER NOT NULL DEFAULT 0,
            strong INTEGER NOT NULL DEFAULT 0,
            partial INTEGER NOT NULL DEFAULT 0,
            weak INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            texts_embedded INTEGER NOT NULL DEFAULT 0,
            texts_cached INTEGER NOT NULL DEFAULT 0,
            requests INTEGER NOT NULL DEFAULT 0,
            tokens INTEGER NOT NULL DEFAULT 0,
            tokens_reported INTEGER NOT NULL DEFAULT 0 CHECK (tokens_reported IN (0, 1)),
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX scoring_runs_started ON scoring_runs(started_on DESC);

        CREATE TABLE role_scores (
            portal TEXT NOT NULL,
            job_id TEXT NOT NULL,
            keyword_raw REAL NOT NULL,
            semantic_raw REAL NOT NULL,
            chunks INTEGER NOT NULL,
            inputs TEXT NOT NULL,
            scored_on TEXT NOT NULL,
            run_id INTEGER REFERENCES scoring_runs(id) ON DELETE SET NULL,
            PRIMARY KEY (portal, job_id),
            FOREIGN KEY (portal, job_id) REFERENCES roles(portal, job_id) ON DELETE CASCADE
        );
        """,
    )


# Append new migrations to the end; never edit one that has shipped.
MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migration_1,
    _migration_2,
    _migration_3,
    _migration_4,
    _migration_5,
    _migration_6,
    _migration_7,
)
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the database up to the current schema, one atomic step per migration."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"database schema version {current} is newer than this tool supports "
            f"({SCHEMA_VERSION}); upgrade jobscan"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            MIGRATIONS[version - 1](conn)
            conn.execute(f"PRAGMA user_version = {version}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection in autocommit mode with the pragmas this tool relies on."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn
