"""SQLite-backed transcript and summary memory storage."""
from __future__ import annotations

import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Any

DEFAULT_MEMORY_DB = Path("~/.engram/memory.db")
VALID_SUMMARY_TRIGGERS = ("compact", "nightly", "nightly-weekly", "manual")
VALID_SEARCH_KINDS = ("both", "transcripts", "summaries")
ALL_CHANNEL_SCOPES = ("all", "all_channels")


def open_memory_db(path: Path | None = None) -> sqlite3.Connection:
    """Open the Engram memory database, creating and migrating it if needed."""
    db_path = Path(path) if path is not None else DEFAULT_MEMORY_DB.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create the first-run memory schema.

    The migration is intentionally idempotent because tests and local first-run
    paths both call it defensively.
    """
    try:
        conn.executescript(
            """
            BEGIN;

            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                role TEXT NOT NULL,
                message_uuid TEXT NOT NULL UNIQUE,
                parent_uuid TEXT,
                text TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                "trigger" TEXT NOT NULL CHECK (
                    "trigger" IN ('compact', 'nightly', 'nightly-weekly', 'manual')
                ),
                day TEXT NOT NULL,
                custom_instructions TEXT,
                summary_text TEXT NOT NULL,
                embedding BLOB
            );

            CREATE TABLE IF NOT EXISTS ingest_state (
                session_id TEXT PRIMARY KEY,
                last_ingested_uuid TEXT NOT NULL,
                last_ingested_ts TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
                text,
                content='transcripts',
                content_rowid='id'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
                summary_text,
                content='summaries',
                content_rowid='id'
            );

            CREATE INDEX IF NOT EXISTS idx_transcripts_session_ts
                ON transcripts(session_id, ts);
            CREATE INDEX IF NOT EXISTS idx_transcripts_channel_ts
                ON transcripts(channel_id, ts);
            CREATE INDEX IF NOT EXISTS idx_summaries_session_ts
                ON summaries(session_id, ts);
            CREATE INDEX IF NOT EXISTS idx_summaries_channel_day
                ON summaries(channel_id, day);
            CREATE INDEX IF NOT EXISTS idx_summaries_channel_ts
                ON summaries(channel_id, ts);

            CREATE TRIGGER IF NOT EXISTS transcripts_ai
            AFTER INSERT ON transcripts
            BEGIN
                INSERT INTO transcripts_fts(rowid, text)
                VALUES (new.id, new.text);
            END;

            CREATE TRIGGER IF NOT EXISTS summaries_ai
            AFTER INSERT ON summaries
            BEGIN
                INSERT INTO summaries_fts(rowid, summary_text)
                VALUES (new.id, new.summary_text);
            END;

            COMMIT;
            """
        )
    except Exception:
        with suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise


def insert_transcript(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    channel_id: str,
    ts: str,
    role: str,
    message_uuid: str,
    parent_uuid: str | None,
    text: str,
) -> None:
    """Insert one transcript message, ignoring duplicate message UUIDs."""
    conn.execute(
        """
        INSERT OR IGNORE INTO transcripts (
            session_id,
            channel_id,
            ts,
            role,
            message_uuid,
            parent_uuid,
            text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, channel_id, ts, role, message_uuid, parent_uuid, text),
    )


def insert_summary(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    channel_id: str,
    ts: str,
    trigger: str,
    day: str,
    custom_instructions: str | None,
    summary_text: str,
    embedding: Any = None,
) -> None:
    """Insert one generated memory summary."""
    conn.execute(
        """
        INSERT INTO summaries (
            session_id,
            channel_id,
            ts,
            "trigger",
            day,
            custom_instructions,
            summary_text,
            embedding
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            channel_id,
            ts,
            trigger,
            day,
            custom_instructions,
            summary_text,
            embedding,
        ),
    )


def get_watermark(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Return the last ingested message UUID for a session."""
    row = conn.execute(
        """
        SELECT last_ingested_uuid
        FROM ingest_state
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row["last_ingested_uuid"])


def set_watermark(
    conn: sqlite3.Connection,
    session_id: str,
    message_uuid: str,
    ts: str,
) -> None:
    """Upsert the last ingested message UUID for a session."""
    conn.execute(
        """
        INSERT INTO ingest_state (session_id, last_ingested_uuid, last_ingested_ts)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            last_ingested_uuid = excluded.last_ingested_uuid,
            last_ingested_ts = excluded.last_ingested_ts
        """,
        (session_id, message_uuid, ts),
    )


def search_keyword(
    conn: sqlite3.Connection,
    *,
    query: str,
    scope: str = "this_channel",
    channel_id: str | None = None,
    kind: str = "both",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search transcript and summary full-text indexes."""
    if kind not in VALID_SEARCH_KINDS:
        raise ValueError(f"kind must be one of {VALID_SEARCH_KINDS}")
    if scope == "this_channel" and channel_id is None:
        raise ValueError("channel_id is required when scope='this_channel'")
    if scope != "this_channel" and scope not in ALL_CHANNEL_SCOPES:
        raise ValueError("scope must be 'this_channel', 'all', or 'all_channels'")

    limit = max(0, limit)
    if limit == 0:
        return []

    results: list[dict[str, Any]] = []
    if kind in ("both", "transcripts"):
        results.extend(_search_transcripts(conn, query, scope, channel_id, limit))
    if kind in ("both", "summaries"):
        results.extend(_search_summaries(conn, query, scope, channel_id, limit))

    results.sort(key=lambda row: row["ts"], reverse=True)
    return results[:limit]


def _channel_clause(scope: str) -> str:
    if scope == "this_channel":
        return "AND channel_id = ?"
    return ""


def _channel_params(scope: str, channel_id: str | None) -> tuple[str, ...]:
    if scope == "this_channel":
        if channel_id is None:
            raise ValueError("channel_id is required when scope='this_channel'")
        return (channel_id,)
    return ()


def _search_transcripts(
    conn: sqlite3.Connection,
    query: str,
    scope: str,
    channel_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            'transcript' AS kind,
            transcripts.channel_id,
            transcripts.ts,
            snippet(transcripts_fts, 0, '', '', '...', 24) AS snippet,
            transcripts.message_uuid
        FROM transcripts_fts
        JOIN transcripts ON transcripts.id = transcripts_fts.rowid
        WHERE transcripts_fts MATCH ?
        {_channel_clause(scope)}
        ORDER BY bm25(transcripts_fts), transcripts.ts DESC
        LIMIT ?
        """,
        (query, *_channel_params(scope, channel_id), limit),
    ).fetchall()
    return [
        {
            "kind": row["kind"],
            "channel_id": row["channel_id"],
            "ts": row["ts"],
            "snippet": row["snippet"],
            "message_uuid": row["message_uuid"],
        }
        for row in rows
    ]


def _search_summaries(
    conn: sqlite3.Connection,
    query: str,
    scope: str,
    channel_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            'summary' AS kind,
            summaries.channel_id,
            summaries.ts,
            snippet(summaries_fts, 0, '', '', '...', 24) AS snippet,
            summaries.id AS summary_id
        FROM summaries_fts
        JOIN summaries ON summaries.id = summaries_fts.rowid
        WHERE summaries_fts MATCH ?
        {_channel_clause(scope)}
        ORDER BY bm25(summaries_fts), summaries.ts DESC
        LIMIT ?
        """,
        (query, *_channel_params(scope, channel_id), limit),
    ).fetchall()
    return [
        {
            "kind": row["kind"],
            "channel_id": row["channel_id"],
            "ts": row["ts"],
            "snippet": row["snippet"],
            "summary_id": row["summary_id"],
        }
        for row in rows
    ]
