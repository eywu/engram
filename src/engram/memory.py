"""SQLite foundation for Engram's channel memory."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import numpy as np

VALID_SUMMARY_TRIGGERS = frozenset({"compact", "nightly", "nightly-weekly", "manual"})
MAX_SEARCH_LIMIT = 100
RRF_K = 60
SQLITE_BUSY_TIMEOUT_MS = 30_000


def open_memory_db(path: Path | None = None) -> sqlite3.Connection:
    """Open the memory database, creating and migrating it on first use."""
    db_path = (path or (Path.home() / ".engram" / "memory.db")).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        db_path,
        isolation_level=None,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
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
            text TEXT NOT NULL,
            embedding BLOB
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_channel_day_trigger_unique
        ON summaries(channel_id, day, trigger);
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
    _ensure_column(conn, "transcripts", "embedding", "BLOB")


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
    assert cursor.lastrowid is not None
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
    excluded_channels: Sequence[str] | None = None,
    kind: Literal["transcripts", "summaries", "both"] = "both",
    limit: int = 5,
) -> list[dict]:
    """Run an FTS5 keyword search over transcript and summary memory."""
    if not query:
        return []
    _validate_search_scope(scope, channel_id)
    if kind not in {"transcripts", "summaries", "both"}:
        raise ValueError("kind must be 'transcripts', 'summaries', or 'both'")

    clamped_limit = min(limit, MAX_SEARCH_LIMIT)
    if clamped_limit <= 0:
        return []

    selects: list[str] = []
    params: list[object] = []

    if kind in {"transcripts", "both"}:
        channel_filter, channel_params = _channel_filter_sql(
            "transcripts",
            scope=scope,
            channel_id=channel_id,
            excluded_channels=excluded_channels,
        )
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
            WHERE transcripts_fts MATCH ?{channel_filter}
            """
        )
        params.append(query)
        params.extend(channel_params)

    if kind in {"summaries", "both"}:
        channel_filter, channel_params = _channel_filter_sql(
            "summaries",
            scope=scope,
            channel_id=channel_id,
            excluded_channels=excluded_channels,
        )
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
            WHERE summaries_fts MATCH ?{channel_filter}
            """
        )
        params.append(query)
        params.extend(channel_params)

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


def search_semantic(
    conn: sqlite3.Connection,
    *,
    query_vec: bytes,
    scope: Literal["this_channel", "all_channels"] = "this_channel",
    channel_id: str | None = None,
    excluded_channels: Sequence[str] | None = None,
    kind: Literal["transcripts", "summaries", "both"] = "both",
    limit: int = 5,
) -> list[dict]:
    """Run in-memory cosine search over stored transcript and summary embeddings."""
    _validate_search_scope(scope, channel_id)
    if kind not in {"transcripts", "summaries", "both"}:
        raise ValueError("kind must be 'transcripts', 'summaries', or 'both'")

    clamped_limit = min(limit, MAX_SEARCH_LIMIT)
    if clamped_limit <= 0 or not query_vec:
        return []

    query = _vector_from_bytes(query_vec)
    query_norm = float(np.linalg.norm(query))
    if query.size == 0 or query_norm == 0.0:
        return []

    candidates: list[tuple[float, dict]] = []
    channel_filter, channel_params = _channel_filter_sql(
        None,
        scope=scope,
        channel_id=channel_id,
        excluded_channels=excluded_channels,
    )

    if kind in {"transcripts", "both"}:
        params: list[object] = list(channel_params)
        for row in conn.execute(
            f"""
            SELECT channel_id, ts, text, message_uuid, embedding
            FROM transcripts
            WHERE embedding IS NOT NULL{channel_filter}
            """,
            params,
        ):
            score = _cosine_similarity(query, query_norm, row[4])
            if score is None:
                continue
            candidates.append(
                (
                    score,
                    {
                        "kind": "transcript",
                        "channel_id": row[0],
                        "ts": _iso_string(row[1]),
                        "snippet": _semantic_snippet(row[2]),
                        "message_uuid": row[3],
                        "summary_id": None,
                    },
                )
            )

    if kind in {"summaries", "both"}:
        params = list(channel_params)
        for row in conn.execute(
            f"""
            SELECT channel_id, ts, summary_text, id, embedding
            FROM summaries
            WHERE embedding IS NOT NULL{channel_filter}
            """,
            params,
        ):
            score = _cosine_similarity(query, query_norm, row[4])
            if score is None:
                continue
            candidates.append(
                (
                    score,
                    {
                        "kind": "summary",
                        "channel_id": row[0],
                        "ts": _iso_string(row[1]),
                        "snippet": _semantic_snippet(row[2]),
                        "message_uuid": None,
                        "summary_id": row[3],
                    },
                )
            )

    candidates.sort(key=lambda item: (item[0], item[1]["ts"]), reverse=True)
    return [row for _score, row in candidates[:clamped_limit]]


def search_hybrid(
    conn: sqlite3.Connection,
    *,
    query: str,
    query_vec: bytes,
    scope: Literal["this_channel", "all_channels"] = "this_channel",
    channel_id: str | None = None,
    excluded_channels: Sequence[str] | None = None,
    kind: Literal["transcripts", "summaries", "both"] = "both",
    limit: int = 5,
) -> list[dict]:
    """Merge FTS5 and semantic recall using reciprocal rank fusion.

    RRF uses ``k=60`` to keep a row that appears in both paths ahead of
    single-path hits without making small rank differences too sharp.
    """
    if not query:
        return []
    _validate_search_scope(scope, channel_id)
    if kind not in {"transcripts", "summaries", "both"}:
        raise ValueError("kind must be 'transcripts', 'summaries', or 'both'")

    clamped_limit = min(limit, MAX_SEARCH_LIMIT)
    if clamped_limit <= 0:
        return []

    db_file = _database_file(conn)
    if db_file is not None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            keyword_future = executor.submit(
                _search_keyword_from_path,
                db_file,
                query,
                scope,
                channel_id,
                excluded_channels,
                kind,
                clamped_limit,
            )
            semantic_future = executor.submit(
                _search_semantic_from_path,
                db_file,
                query_vec,
                scope,
                channel_id,
                excluded_channels,
                kind,
                clamped_limit,
            )
            keyword_rows = keyword_future.result()
            semantic_rows = semantic_future.result()
    else:
        keyword_rows = search_keyword(
            conn,
            query=query,
            scope=scope,
            channel_id=channel_id,
            excluded_channels=excluded_channels,
            kind=kind,
            limit=clamped_limit,
        )
        semantic_rows = search_semantic(
            conn,
            query_vec=query_vec,
            scope=scope,
            channel_id=channel_id,
            excluded_channels=excluded_channels,
            kind=kind,
            limit=clamped_limit,
        )

    return _merge_rrf(keyword_rows, semantic_rows, clamped_limit)


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


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _validate_search_scope(scope: str, channel_id: str | None) -> None:
    if scope == "this_channel" and channel_id is None:
        raise ValueError("channel_id is required when scope='this_channel'")
    if scope not in {"this_channel", "all_channels"}:
        raise ValueError("scope must be 'this_channel' or 'all_channels'")


def _channel_filter_sql(
    table: str | None,
    *,
    scope: str,
    channel_id: str | None,
    excluded_channels: Sequence[str] | None,
) -> tuple[str, list[object]]:
    column = f"{table}.channel_id" if table else "channel_id"
    clauses: list[str] = []
    params: list[object] = []

    if scope == "this_channel":
        clauses.append(f" AND {column} = ?")
        params.append(channel_id)

    exclusions = _normalize_channel_ids(excluded_channels)
    if exclusions:
        placeholders = ", ".join("?" for _ in exclusions)
        clauses.append(f" AND {column} NOT IN ({placeholders})")
        params.extend(exclusions)

    return "".join(clauses), params


def _normalize_channel_ids(channel_ids: Sequence[str] | None) -> list[str]:
    if not channel_ids:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in channel_ids:
        channel_id = str(raw).strip()
        if not channel_id or channel_id in seen:
            continue
        normalized.append(channel_id)
        seen.add(channel_id)
    return normalized


def _vector_from_bytes(value: bytes) -> np.ndarray:
    if len(value) % np.dtype(np.float32).itemsize:
        return np.asarray([], dtype=np.float32)
    return np.frombuffer(value, dtype=np.float32)


def _cosine_similarity(
    query: np.ndarray,
    query_norm: float,
    candidate_blob: bytes,
) -> float | None:
    candidate = _vector_from_bytes(candidate_blob)
    if candidate.size != query.size:
        return None
    candidate_norm = float(np.linalg.norm(candidate))
    if candidate_norm == 0.0:
        return None
    return float(np.dot(query, candidate) / (query_norm * candidate_norm))


def _semantic_snippet(text: object, max_chars: int = 240) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 3].rstrip()}..."


def _database_file(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return None
    db_file = row[2]
    return Path(db_file) if db_file else None


def _search_keyword_from_path(
    db_file: Path,
    query: str,
    scope: str,
    channel_id: str | None,
    excluded_channels: Sequence[str] | None,
    kind: str,
    limit: int,
) -> list[dict]:
    with closing(open_memory_db(db_file)) as conn:
        return search_keyword(
            conn,
            query=query,
            scope=scope,  # type: ignore[arg-type]
            channel_id=channel_id,
            excluded_channels=excluded_channels,
            kind=kind,  # type: ignore[arg-type]
            limit=limit,
        )


def _search_semantic_from_path(
    db_file: Path,
    query_vec: bytes,
    scope: str,
    channel_id: str | None,
    excluded_channels: Sequence[str] | None,
    kind: str,
    limit: int,
) -> list[dict]:
    with closing(open_memory_db(db_file)) as conn:
        return search_semantic(
            conn,
            query_vec=query_vec,
            scope=scope,  # type: ignore[arg-type]
            channel_id=channel_id,
            excluded_channels=excluded_channels,
            kind=kind,  # type: ignore[arg-type]
            limit=limit,
        )


def _merge_rrf(
    keyword_rows: list[dict],
    semantic_rows: list[dict],
    limit: int,
) -> list[dict]:
    rows_by_key: dict[tuple[str, object], dict] = {}
    scores: dict[tuple[str, object], float] = {}

    for rows in (keyword_rows, semantic_rows):
        for rank, row in enumerate(rows, start=1):
            key = _result_key(row)
            if key is None:
                continue
            rows_by_key.setdefault(key, row)
            scores[key] = scores.get(key, 0.0) + (1.0 / (RRF_K + rank))

    ordered_keys = sorted(
        rows_by_key,
        key=lambda key: (scores[key], rows_by_key[key].get("ts") or ""),
        reverse=True,
    )
    return [rows_by_key[key] for key in ordered_keys[:limit]]


def _result_key(row: dict) -> tuple[str, object] | None:
    row_kind = row.get("kind")
    identifier = row.get("message_uuid") or row.get("summary_id")
    if row_kind is None or identifier is None:
        return None
    return str(row_kind), identifier
