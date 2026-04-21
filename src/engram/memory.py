"""SQLite foundation for Engram's channel memory."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Literal

VALID_SUMMARY_TRIGGERS = frozenset({"compact", "nightly", "nightly-weekly", "manual"})
MAX_SEARCH_LIMIT = 100


def open_memory_db(path: Path | None = None) -> sqlite3.Connection:
    """Open the memory database, creating and migrating it on first use."""
    db_path = (path or (Path.home() / ".engram" / "memory.db")).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create the canonical memory schema if it is not already present."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            ts TIMESTAMP NOT NULL,
            role TEXT NOT NULL,
            message_uuid TEXT UNIQUE NOT NULL,
            parent_uuid TEXT,
            text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_transcripts_channel_ts
        ON transcripts(channel_id, ts);
        CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts
        USING fts5(text, content='transcripts', content_rowid='id');

        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            channel_id TEXT NOT NULL,
            ts TIMESTAMP NOT NULL,
            trigger TEXT NOT NULL,
            day DATE,
            custom_instructions TEXT,
            summary_text TEXT NOT NULL,
            embedding BLOB,
            CHECK (trigger IN ('compact', 'nightly', 'nightly-weekly', 'manual'))
        );
        CREATE INDEX IF NOT EXISTS idx_summaries_channel_day
        ON summaries(channel_id, day);
        CREATE INDEX IF NOT EXISTS idx_summaries_channel_trigger
        ON summaries(channel_id, trigger, ts);
        CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts
        USING fts5(summary_text, content='summaries', content_rowid='id');

        CREATE TABLE IF NOT EXISTS ingest_state (
            session_id TEXT PRIMARY KEY,
            last_ingested_uuid TEXT,
            last_ingested_ts TIMESTAMP
        );

        CREATE TRIGGER IF NOT EXISTS trg_transcripts_fts_ai
        AFTER INSERT ON transcripts BEGIN
            INSERT INTO transcripts_fts(rowid, text)
            VALUES (new.id, new.text);
        END;

        CREATE TRIGGER IF NOT EXISTS trg_transcripts_fts_ad
        AFTER DELETE ON transcripts BEGIN
            INSERT INTO transcripts_fts(transcripts_fts, rowid, text)
            VALUES ('delete', old.id, old.text);
        END;

        CREATE TRIGGER IF NOT EXISTS trg_transcripts_fts_au
        AFTER UPDATE ON transcripts BEGIN
            INSERT INTO transcripts_fts(transcripts_fts, rowid, text)
            VALUES ('delete', old.id, old.text);
            INSERT INTO transcripts_fts(rowid, text)
            VALUES (new.id, new.text);
        END;

        CREATE TRIGGER IF NOT EXISTS trg_summaries_fts_ai
        AFTER INSERT ON summaries BEGIN
            INSERT INTO summaries_fts(rowid, summary_text)
            VALUES (new.id, new.summary_text);
        END;

        CREATE TRIGGER IF NOT EXISTS trg_summaries_fts_ad
        AFTER DELETE ON summaries BEGIN
            INSERT INTO summaries_fts(summaries_fts, rowid, summary_text)
            VALUES ('delete', old.id, old.summary_text);
        END;

        CREATE TRIGGER IF NOT EXISTS trg_summaries_fts_au
        AFTER UPDATE ON summaries BEGIN
            INSERT INTO summaries_fts(summaries_fts, rowid, summary_text)
            VALUES ('delete', old.id, old.summary_text);
            INSERT INTO summaries_fts(rowid, summary_text)
            VALUES (new.id, new.summary_text);
        END;
        """
    )


def insert_transcript(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    channel_id: str,
    ts: datetime,
    role: str,
    message_uuid: str,
    parent_uuid: str | None,
    text: str,
) -> bool:
    """Insert one transcript message, ignoring duplicates by message UUID."""
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO transcripts (
            session_id, channel_id, ts, role, message_uuid, parent_uuid, text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, channel_id, _datetime_to_db(ts), role, message_uuid, parent_uuid, text),
    )
    return conn.total_changes > before


def insert_summary(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    channel_id: str,
    ts: datetime,
    trigger: str,
    day: date | None = None,
    custom_instructions: str | None = None,
    summary_text: str,
    embedding: bytes | None = None,
) -> int:
    """Insert one summary row and return its generated ID."""
    if trigger not in VALID_SUMMARY_TRIGGERS:
        allowed = ", ".join(sorted(VALID_SUMMARY_TRIGGERS))
        raise ValueError(f"invalid summary trigger {trigger!r}; expected one of: {allowed}")

    cursor = conn.execute(
        """
        INSERT INTO summaries (
            session_id,
            channel_id,
            ts,
            trigger,
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
            _datetime_to_db(ts),
            trigger,
            _date_to_db(day),
            custom_instructions,
            summary_text,
            embedding,
        ),
    )
    return int(cursor.lastrowid)


def get_watermark(conn: sqlite3.Connection, session_id: str) -> tuple[str | None, datetime | None]:
    """Return the last ingested message UUID and timestamp for a session."""
    row = conn.execute(
        """
        SELECT last_ingested_uuid, last_ingested_ts
        FROM ingest_state
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None, None
    return row[0], _datetime_from_db(row[1])


def set_watermark(
    conn: sqlite3.Connection,
    session_id: str,
    message_uuid: str,
    ts: datetime,
) -> None:
    """Upsert the ingestion watermark for a session."""
    conn.execute(
        """
        INSERT INTO ingest_state (session_id, last_ingested_uuid, last_ingested_ts)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            last_ingested_uuid = excluded.last_ingested_uuid,
            last_ingested_ts = excluded.last_ingested_ts
        """,
        (session_id, message_uuid, _datetime_to_db(ts)),
    )


def search_keyword(
    conn: sqlite3.Connection,
    *,
    query: str,
    scope: Literal["this_channel", "all_channels"] = "this_channel",
    channel_id: str | None = None,
    kind: Literal["transcripts", "summaries", "both"] = "both",
    limit: int = 5,
) -> list[dict]:
    """Run an FTS5 keyword search over transcript and summary memory."""
    if not query:
        return []
    if scope == "this_channel" and channel_id is None:
        raise ValueError("channel_id is required when scope='this_channel'")
    if scope not in {"this_channel", "all_channels"}:
        raise ValueError("scope must be 'this_channel' or 'all_channels'")
    if kind not in {"transcripts", "summaries", "both"}:
        raise ValueError("kind must be 'transcripts', 'summaries', or 'both'")

    clamped_limit = min(limit, MAX_SEARCH_LIMIT)
    if clamped_limit <= 0:
        return []

    selects: list[str] = []
    params: list[object] = []
    channel_filter = " AND {table}.channel_id = ?" if scope == "this_channel" else ""

    if kind in {"transcripts", "both"}:
        selects.append(
            f"""
            SELECT
                'transcript' AS kind,
                transcripts.channel_id AS channel_id,
                transcripts.ts AS ts,
                snippet(transcripts_fts, 0, '', '', '...', 20) AS snippet,
                transcripts.message_uuid AS message_uuid,
                NULL AS summary_id
            FROM transcripts_fts
            JOIN transcripts ON transcripts_fts.rowid = transcripts.id
            WHERE transcripts_fts MATCH ?{channel_filter.format(table="transcripts")}
            """
        )
        params.append(query)
        if scope == "this_channel":
            params.append(channel_id)

    if kind in {"summaries", "both"}:
        selects.append(
            f"""
            SELECT
                'summary' AS kind,
                summaries.channel_id AS channel_id,
                summaries.ts AS ts,
                snippet(summaries_fts, 0, '', '', '...', 20) AS snippet,
                NULL AS message_uuid,
                summaries.id AS summary_id
            FROM summaries_fts
            JOIN summaries ON summaries_fts.rowid = summaries.id
            WHERE summaries_fts MATCH ?{channel_filter.format(table="summaries")}
            """
        )
        params.append(query)
        if scope == "this_channel":
            params.append(channel_id)

    sql = f"""
        SELECT kind, channel_id, ts, snippet, message_uuid, summary_id
        FROM (
            {" UNION ALL ".join(selects)}
        )
        ORDER BY ts DESC
        LIMIT ?
    """
    params.append(clamped_limit)

    results: list[dict] = []
    for kind_value, channel_value, ts_value, snippet_value, message_uuid, summary_id in conn.execute(
        sql,
        params,
    ):
        results.append(
            {
                "kind": kind_value,
                "channel_id": channel_value,
                "ts": _iso_string(ts_value),
                "snippet": str(snippet_value).replace("<mark>", "").replace("</mark>", ""),
                "message_uuid": message_uuid,
                "summary_id": summary_id,
            }
        )
    return results


def _datetime_to_db(value: datetime) -> str:
    return value.isoformat()


def _datetime_from_db(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _date_to_db(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _iso_string(value: object) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)
