"""SQLite-backed memory ingestion primitives."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    """Open and initialize the Engram memory database."""
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create memory tables if they do not already exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            ts TEXT,
            role TEXT NOT NULL,
            message_uuid TEXT NOT NULL UNIQUE,
            parent_uuid TEXT,
            text TEXT NOT NULL DEFAULT '',
            inserted_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
        );

        CREATE INDEX IF NOT EXISTS idx_transcripts_session_id_id
            ON transcripts(session_id, id);
        CREATE INDEX IF NOT EXISTS idx_transcripts_channel_id_id
            ON transcripts(channel_id, id);

        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            ts TEXT,
            trigger TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            custom_instructions TEXT,
            source_message_uuid TEXT,
            inserted_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
        );

        CREATE INDEX IF NOT EXISTS idx_summaries_session_id_id
            ON summaries(session_id, id);
        CREATE INDEX IF NOT EXISTS idx_summaries_channel_id_id
            ON summaries(channel_id, id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_source_message_uuid
            ON summaries(source_message_uuid)
            WHERE source_message_uuid IS NOT NULL;

        CREATE TABLE IF NOT EXISTS watermarks (
            session_id TEXT PRIMARY KEY,
            message_uuid TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
        );
        """
    )
    conn.commit()


def get_watermark(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute(
        "SELECT message_uuid FROM watermarks WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return str(row["message_uuid"])
    return str(row[0])


def set_watermark(
    conn: sqlite3.Connection,
    session_id: str,
    message_uuid: str,
) -> None:
    conn.execute(
        """
        INSERT INTO watermarks(session_id, message_uuid)
        VALUES (?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            message_uuid = excluded.message_uuid,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (session_id, message_uuid),
    )
    conn.commit()


def insert_transcript(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    channel_id: str,
    ts: str | None,
    role: str,
    message_uuid: str,
    parent_uuid: str | None,
    text: str,
) -> bool:
    """Insert a transcript row, ignoring duplicate message UUIDs."""
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO transcripts(
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
    conn.commit()
    return cursor.rowcount > 0


def insert_summary(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    channel_id: str,
    ts: str | None,
    trigger: str,
    summary_text: str,
    custom_instructions: str | None,
    source_message_uuid: str | None = None,
) -> bool:
    """Insert a summary row, ignoring duplicates by source message UUID."""
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO summaries(
            session_id,
            channel_id,
            ts,
            trigger,
            summary_text,
            custom_instructions,
            source_message_uuid
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            channel_id,
            ts,
            trigger,
            summary_text,
            custom_instructions,
            source_message_uuid,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0
