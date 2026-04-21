"""SQLite FTS5 memory search for transcripts and summaries."""
from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from pathlib import Path
from typing import Literal

from engram import paths

MemoryKind = Literal["transcripts", "summaries", "both"]
MemoryScope = Literal["this_channel", "all_channels"]

VALID_KINDS = {"transcripts", "summaries", "both"}
VALID_SCOPES = {"this_channel", "all_channels"}


def memory_db_path(home: Path | None = None) -> Path:
    """Return the default Engram memory database path."""
    return paths.engram_home(home) / "memory.db"


def ensure_schema(db_path: Path | str | None = None) -> None:
    """Create the FTS5 table if it does not already exist."""
    with _connect(db_path) as con:
        _ensure_schema(con)


def insert_transcript(
    *,
    channel_id: str,
    text: str,
    ts_iso: str | None = None,
    message_uuid: str | None = None,
    db_path: Path | str | None = None,
) -> str:
    """Insert a transcript row for tests and ingestion code."""
    identifier = message_uuid or str(uuid.uuid4())
    _insert_memory_item(
        kind="transcripts",
        channel_id=channel_id,
        ts_iso=ts_iso,
        identifier=identifier,
        body=text,
        db_path=db_path,
    )
    return identifier


def insert_summary(
    *,
    channel_id: str,
    text: str,
    ts_iso: str | None = None,
    summary_id: str | None = None,
    db_path: Path | str | None = None,
) -> str:
    """Insert a summary row for tests and summarization code."""
    identifier = summary_id or str(uuid.uuid4())
    _insert_memory_item(
        kind="summaries",
        channel_id=channel_id,
        ts_iso=ts_iso,
        identifier=identifier,
        body=text,
        db_path=db_path,
    )
    return identifier


def search_keyword(
    query: str,
    *,
    channel_id: str | None = None,
    scope: MemoryScope = "this_channel",
    kind: MemoryKind = "both",
    limit: int = 5,
    db_path: Path | str | None = None,
) -> list[dict]:
    """Search memory via SQLite FTS5 keyword matching.

    ``scope='this_channel'`` requires ``channel_id``; without it, the safe
    result is an empty list rather than accidentally widening to all channels.
    """
    query = (query or "").strip()
    if not query:
        return []
    if limit <= 0:
        return []
    if scope not in VALID_SCOPES:
        raise ValueError(f"invalid memory search scope: {scope}")
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid memory search kind: {kind}")
    if scope == "this_channel" and not channel_id:
        return []

    fts_query = _build_fts_query(query)
    if not fts_query:
        return []

    where = ["memory_fts MATCH ?"]
    params: list[object] = [fts_query]

    if scope == "this_channel":
        where.append("channel_id = ?")
        params.append(channel_id)
    if kind != "both":
        where.append("kind = ?")
        params.append(kind)
    params.append(int(limit))

    sql = f"""
        SELECT
            kind,
            channel_id,
            ts_iso,
            snippet(memory_fts, 4, '', '', '...', 20) AS snippet,
            identifier AS message_uuid_or_summary_id,
            bm25(memory_fts) AS rank
        FROM memory_fts
        WHERE {" AND ".join(where)}
        ORDER BY rank ASC, ts_iso DESC
        LIMIT ?
    """

    with _connect(db_path) as con:
        rows = con.execute(sql, params).fetchall()
    return [
        {
            "kind": row["kind"],
            "channel_id": row["channel_id"],
            "ts_iso": row["ts_iso"],
            "snippet": row["snippet"],
            "message_uuid_or_summary_id": row["message_uuid_or_summary_id"],
        }
        for row in rows
    ]


def _insert_memory_item(
    *,
    kind: Literal["transcripts", "summaries"],
    channel_id: str,
    ts_iso: str | None,
    identifier: str,
    body: str,
    db_path: Path | str | None,
) -> None:
    timestamp = ts_iso or dt.datetime.now(dt.UTC).isoformat()
    with _connect(db_path) as con:
        con.execute(
            """
            INSERT INTO memory_fts(kind, channel_id, ts_iso, identifier, body)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kind, channel_id, timestamp, identifier, body),
        )


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = memory_db_path() if db_path is None else Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    _ensure_schema(con)
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            kind UNINDEXED,
            channel_id UNINDEXED,
            ts_iso UNINDEXED,
            identifier UNINDEXED,
            body,
            tokenize = "unicode61 tokenchars '-_'"
        )
        """
    )


def _build_fts_query(query: str) -> str:
    """Escape user text into a literal-term FTS5 query.

    This intentionally does not implement query expansion; it only prevents
    punctuation such as ``GRO-322`` from being interpreted as FTS operators.
    """
    terms = [term for term in query.split() if term]
    return " AND ".join(f'"{term.replace("\"", "\"\"")}"' for term in terms)
