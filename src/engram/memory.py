"""SQLite + FTS5 memory storage with optional Gemini embeddings."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import random
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from engram import paths
from engram.config import EmbeddingsConfig

log = logging.getLogger(__name__)

DEFAULT_MEMORY_DB_PATH = paths.engram_home() / "memory.db"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class Embedder(Protocol):
    async def embed(self, text: str) -> Sequence[float]:
        """Return an embedding vector for text."""


@dataclass
class EmbeddingResult:
    vector: list[float]
    latency_ms: int


@dataclass
class _EmbeddingJob:
    kind: str
    row_id: int
    text: str
    db_path: Path | None
    conn: sqlite3.Connection | None


class GeminiEmbedder:
    """Small async wrapper around google-genai's sync embed_content call."""

    def __init__(self, *, api_key: str, model: str, dimensions: int):
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self._client: Any | None = None

    async def embed(self, text: str) -> Sequence[float]:
        return await asyncio.to_thread(self._embed_sync, text)

    def _embed_sync(self, text: str) -> Sequence[float]:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "contents": text,
        }
        if self.model != "text-embedding-004" and self.dimensions:
            from google.genai import types

            kwargs["config"] = types.EmbedContentConfig(
                output_dimensionality=self.dimensions
            )

        result = self._client.models.embed_content(**kwargs)
        embeddings = getattr(result, "embeddings", None)
        embedding = embeddings[0] if embeddings else getattr(result, "embedding", result)
        values = getattr(embedding, "values", embedding)
        return list(values)


class EmbeddingService:
    """Bounded background embedding queue used by memory ingestion."""

    def __init__(
        self,
        config: EmbeddingsConfig | None = None,
        *,
        api_key: str | None = None,
        embedder: Embedder | None = None,
        rng: random.Random | None = None,
    ):
        self.config = config or EmbeddingsConfig()
        self.rng = rng or random.Random()
        self._queue: asyncio.Queue[_EmbeddingJob] | None = None
        self._worker_task: asyncio.Task[None] | None = None

        self.enabled = bool(self.config.enabled)
        if self.enabled and self.config.provider != "gemini":
            log.warning(
                "memory.embeddings_disabled unsupported_provider=%s",
                self.config.provider,
            )
            self.enabled = False

        self._embedder: Embedder | None = embedder
        if self.enabled and self._embedder is None:
            resolved_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
            if not resolved_key:
                log.warning(
                    "memory.embeddings_disabled missing_env=GEMINI_API_KEY"
                )
                self.enabled = False
            else:
                self._embedder = GeminiEmbedder(
                    api_key=resolved_key,
                    model=self.config.model,
                    dimensions=self.config.dimensions,
                )

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            log.warning("memory.embedding_queue_not_started no_running_loop=True")
            return

        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.config.queue_size)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    def enqueue(self, job: _EmbeddingJob) -> bool:
        if not self.enabled or not job.text.strip():
            return False
        self.start()
        if self._queue is None:
            return False
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            log.warning(
                "memory.embedding_queue_full kind=%s row_id=%s",
                job.kind,
                job.row_id,
            )
            return False
        return True

    async def embed_text(self, text: str) -> EmbeddingResult | None:
        if not self.enabled or self._embedder is None or not text.strip():
            return None
        started = time.perf_counter()
        try:
            values = await asyncio.wait_for(
                self._embedder.embed(text),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError:
            latency_ms = _elapsed_ms(started)
            log.warning(
                "memory.embedding_timeout model=%s timeout_seconds=%.3f latency_ms=%d",
                self.config.model,
                self.config.timeout_seconds,
                latency_ms,
            )
            return None
        except Exception as e:
            latency_ms = _elapsed_ms(started)
            log.warning(
                "memory.embedding_failed model=%s error=%s latency_ms=%d",
                self.config.model,
                type(e).__name__,
                latency_ms,
                exc_info=True,
            )
            return None
        return EmbeddingResult(vector=[float(v) for v in values], latency_ms=_elapsed_ms(started))

    async def drain(self, timeout: float = 5.0) -> None:
        if self._queue is None:
            return
        await asyncio.wait_for(self._queue.join(), timeout=timeout)

    async def shutdown(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                result = await self.embed_text(job.text)
                if result is None:
                    continue
                try:
                    blob = embedding_to_blob(
                        result.vector,
                        dimensions=self.config.dimensions,
                    )
                except ValueError as e:
                    log.warning(
                        "memory.embedding_invalid kind=%s row_id=%s error=%s",
                        job.kind,
                        job.row_id,
                        e,
                    )
                    continue
                _write_embedding(job, blob)
                log.info(
                    "memory.embedding_updated kind=%s row_id=%s "
                    "embedding_api_latency_ms=%d",
                    job.kind,
                    job.row_id,
                    result.latency_ms,
                )
            finally:
                self._queue.task_done()


_embedding_service = EmbeddingService(EmbeddingsConfig(enabled=False))


def configure_embeddings(
    config: EmbeddingsConfig | None = None,
    *,
    api_key: str | None = None,
    embedder: Embedder | None = None,
    rng: random.Random | None = None,
) -> EmbeddingService:
    """Configure the process-global embedding service."""
    global _embedding_service
    _embedding_service = EmbeddingService(
        config,
        api_key=api_key,
        embedder=embedder,
        rng=rng,
    )
    _embedding_service.start()
    return _embedding_service


def embedding_service() -> EmbeddingService:
    return _embedding_service


async def shutdown_embeddings() -> None:
    await _embedding_service.shutdown()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path).expanduser() if db_path is not None else DEFAULT_MEMORY_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT 'channel',
            channel_id TEXT,
            summary_text TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT 'channel',
            channel_id TEXT,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS memory_daily_metrics (
            day TEXT NOT NULL,
            metric TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, metric)
        );
        """
    )
    _ensure_column(conn, "summaries", "embedding", "embedding BLOB")
    _ensure_column(conn, "transcripts", "embedding", "embedding BLOB")
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts
        USING fts5(summary_text, content='summaries', content_rowid='id');

        CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts
        USING fts5(text, content='transcripts', content_rowid='id');

        CREATE TRIGGER IF NOT EXISTS summaries_ai AFTER INSERT ON summaries BEGIN
            INSERT INTO summaries_fts(rowid, summary_text)
            VALUES (new.id, new.summary_text);
        END;
        CREATE TRIGGER IF NOT EXISTS summaries_ad AFTER DELETE ON summaries BEGIN
            INSERT INTO summaries_fts(summaries_fts, rowid, summary_text)
            VALUES ('delete', old.id, old.summary_text);
        END;
        CREATE TRIGGER IF NOT EXISTS summaries_au AFTER UPDATE OF summary_text ON summaries BEGIN
            INSERT INTO summaries_fts(summaries_fts, rowid, summary_text)
            VALUES ('delete', old.id, old.summary_text);
            INSERT INTO summaries_fts(rowid, summary_text)
            VALUES (new.id, new.summary_text);
        END;

        CREATE TRIGGER IF NOT EXISTS transcripts_ai AFTER INSERT ON transcripts BEGIN
            INSERT INTO transcripts_fts(rowid, text)
            VALUES (new.id, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS transcripts_ad AFTER DELETE ON transcripts BEGIN
            INSERT INTO transcripts_fts(transcripts_fts, rowid, text)
            VALUES ('delete', old.id, old.text);
        END;
        CREATE TRIGGER IF NOT EXISTS transcripts_au AFTER UPDATE OF text ON transcripts BEGIN
            INSERT INTO transcripts_fts(transcripts_fts, rowid, text)
            VALUES ('delete', old.id, old.text);
            INSERT INTO transcripts_fts(rowid, text)
            VALUES (new.id, new.text);
        END;
        """
    )
    conn.commit()


def insert_summary(
    conn: sqlite3.Connection,
    *,
    summary_text: str,
    scope: str = "channel",
    channel_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> int:
    init_db(conn)
    cursor = conn.execute(
        """
        INSERT INTO summaries (scope, channel_id, summary_text, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            scope,
            channel_id,
            summary_text,
            _json_dumps(metadata),
            created_at or _utc_now(),
        ),
    )
    conn.commit()
    row_id = int(cursor.lastrowid)
    _embedding_service.enqueue(
        _EmbeddingJob(
            kind="summary",
            row_id=row_id,
            text=summary_text,
            db_path=_db_path_for(conn),
            conn=None if _db_path_for(conn) else conn,
        )
    )
    return row_id


def insert_transcript(
    conn: sqlite3.Connection,
    *,
    text: str,
    role: str,
    scope: str = "channel",
    channel_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> int:
    init_db(conn)
    cursor = conn.execute(
        """
        INSERT INTO transcripts (scope, channel_id, role, text, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            scope,
            channel_id,
            role,
            text,
            _json_dumps(metadata),
            created_at or _utc_now(),
        ),
    )
    conn.commit()
    row_id = int(cursor.lastrowid)
    if _should_embed_transcript(text=text, role=role):
        db_path = _db_path_for(conn)
        _embedding_service.enqueue(
            _EmbeddingJob(
                kind="transcript",
                row_id=row_id,
                text=text,
                db_path=db_path,
                conn=None if db_path else conn,
            )
        )
    return row_id


def search_keyword(
    conn: sqlite3.Connection,
    *,
    query: str,
    scope: str | None = None,
    channel_id: str | None = None,
    kind: str = "both",
    limit: int = 10,
) -> list[dict[str, Any]]:
    init_db(conn)
    if not query.strip() or limit <= 0:
        return []

    results: list[dict[str, Any]] = []
    for table in _selected_tables(kind):
        results.extend(
            _search_keyword_table(
                conn,
                table=table,
                query=query,
                scope=scope,
                channel_id=channel_id,
                limit=limit,
            )
        )

    results.sort(key=lambda r: float(r["score"]))
    return results[:limit]


def search_semantic(
    conn: sqlite3.Connection,
    *,
    query_vec: Sequence[float] | np.ndarray | None,
    scope: str | None = None,
    channel_id: str | None = None,
    kind: str = "both",
    limit: int = 10,
) -> list[dict[str, Any]]:
    init_db(conn)
    if query_vec is None or limit <= 0:
        return []

    query_arr = np.asarray(query_vec, dtype=np.float32)
    query_norm = float(np.linalg.norm(query_arr))
    if query_arr.size == 0 or query_norm == 0:
        return []

    results: list[dict[str, Any]] = []
    for table in _selected_tables(kind):
        rows = _select_embedding_rows(
            conn,
            table=table,
            scope=scope,
            channel_id=channel_id,
        )
        for row in rows:
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            if vec.shape != query_arr.shape:
                continue
            denom = query_norm * float(np.linalg.norm(vec))
            if denom == 0:
                continue
            score = float(np.dot(query_arr, vec) / denom)
            results.append(
                {
                    "kind": table.result_kind,
                    "row_id": int(row["id"]),
                    "scope": row["scope"],
                    "channel_id": row["channel_id"],
                    "text": row[table.text_column],
                    "score": score,
                    "source": "semantic",
                }
            )

    results.sort(key=lambda r: float(r["score"]), reverse=True)
    return results[:limit]


def search_hybrid(
    conn: sqlite3.Connection,
    *,
    query: str,
    query_vec: Sequence[float] | np.ndarray | None,
    scope: str | None = None,
    channel_id: str | None = None,
    kind: str = "both",
    limit: int = 10,
) -> list[dict[str, Any]]:
    keyword = search_keyword(
        conn,
        query=query,
        scope=scope,
        channel_id=channel_id,
        kind=kind,
        limit=max(limit * 2, limit),
    )
    semantic = search_semantic(
        conn,
        query_vec=query_vec,
        scope=scope,
        channel_id=channel_id,
        kind=kind,
        limit=max(limit * 2, limit),
    )
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    _merge_rrf(merged, keyword, source="keyword")
    _merge_rrf(merged, semantic, source="semantic")
    results = list(merged.values())
    results.sort(key=lambda r: float(r["score"]), reverse=True)
    return results[:limit]


def record_daily_metric(
    conn: sqlite3.Connection,
    metric: str,
    *,
    amount: int = 1,
    day: dt.date | None = None,
) -> None:
    init_db(conn)
    day_s = (day or dt.datetime.now(dt.UTC).date()).isoformat()
    conn.execute(
        """
        INSERT INTO memory_daily_metrics (day, metric, count)
        VALUES (?, ?, ?)
        ON CONFLICT(day, metric) DO UPDATE SET count = count + excluded.count
        """,
        (day_s, metric, amount),
    )
    conn.commit()


def monthly_embedding_counts_by_channel(
    conn: sqlite3.Connection,
    *,
    now: dt.datetime | None = None,
) -> dict[str, int]:
    init_db(conn)
    now = now or dt.datetime.now(dt.UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    counts: dict[str, int] = {}
    for table_name in ("summaries", "transcripts"):
        for row in conn.execute(
            f"""
            SELECT COALESCE(channel_id, '(none)') AS channel_id, COUNT(*) AS n
            FROM {table_name}
            WHERE embedding IS NOT NULL AND created_at >= ?
            GROUP BY COALESCE(channel_id, '(none)')
            """,
            (month_start.isoformat(),),
        ):
            channel = str(row["channel_id"])
            counts[channel] = counts.get(channel, 0) + int(row["n"])
    return counts


def embedding_to_blob(
    values: Sequence[float] | np.ndarray,
    *,
    dimensions: int | None = None,
) -> bytes:
    arr = np.asarray(values, dtype=np.float32)
    if dimensions is not None:
        if arr.size < dimensions:
            raise ValueError(f"embedding has {arr.size} dimensions, expected {dimensions}")
        if arr.size > dimensions:
            arr = arr[:dimensions]
    return arr.tobytes()


def blob_to_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


@dataclass(frozen=True)
class _MemoryTable:
    table_name: str
    fts_name: str
    text_column: str
    result_kind: str


_SUMMARIES = _MemoryTable("summaries", "summaries_fts", "summary_text", "summary")
_TRANSCRIPTS = _MemoryTable("transcripts", "transcripts_fts", "text", "transcript")


def _selected_tables(kind: str | None) -> list[_MemoryTable]:
    normalized = (kind or "both").lower()
    if normalized in {"summary", "summaries"}:
        return [_SUMMARIES]
    if normalized in {"transcript", "transcripts"}:
        return [_TRANSCRIPTS]
    if normalized in {"both", "hybrid", "all"}:
        return [_SUMMARIES, _TRANSCRIPTS]
    raise ValueError(f"unsupported memory kind: {kind}")


def _search_keyword_table(
    conn: sqlite3.Connection,
    *,
    table: _MemoryTable,
    query: str,
    scope: str | None,
    channel_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    sql, params = _keyword_sql(table, query, scope, channel_id, limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        safe_query = _safe_fts_query(query)
        if not safe_query:
            return []
        sql, params = _keyword_sql(table, safe_query, scope, channel_id, limit)
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "kind": table.result_kind,
            "row_id": int(row["id"]),
            "scope": row["scope"],
            "channel_id": row["channel_id"],
            "text": row[table.text_column],
            "score": float(row["score"]),
            "source": "keyword",
        }
        for row in rows
    ]


def _keyword_sql(
    table: _MemoryTable,
    query: str,
    scope: str | None,
    channel_id: str | None,
    limit: int,
) -> tuple[str, list[Any]]:
    filters, params = _filters_sql(scope, channel_id)
    params = [query, *params, limit]
    return (
        f"""
        SELECT m.id,
               m.scope,
               m.channel_id,
               m.{table.text_column},
               bm25({table.fts_name}) AS score
        FROM {table.fts_name}
        JOIN {table.table_name} m ON m.id = {table.fts_name}.rowid
        WHERE {table.fts_name} MATCH ?
        {filters}
        ORDER BY score
        LIMIT ?
        """,
        params,
    )


def _select_embedding_rows(
    conn: sqlite3.Connection,
    *,
    table: _MemoryTable,
    scope: str | None,
    channel_id: str | None,
) -> list[sqlite3.Row]:
    filters, params = _filters_sql(scope, channel_id)
    return conn.execute(
        f"""
        SELECT id, scope, channel_id, {table.text_column}, embedding
        FROM {table.table_name} m
        WHERE embedding IS NOT NULL
        {filters}
        """,
        params,
    ).fetchall()


def _filters_sql(scope: str | None, channel_id: str | None) -> tuple[str, list[Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if scope and scope != "all":
        filters.append("AND m.scope = ?")
        params.append(scope)
    if channel_id:
        filters.append("AND m.channel_id = ?")
        params.append(channel_id)
    return ("\n        ".join(filters), params)


def _safe_fts_query(query: str) -> str:
    tokens = _TOKEN_RE.findall(query)
    return " OR ".join(tokens)


def _merge_rrf(
    merged: dict[tuple[str, int], dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    source: str,
    k: int = 60,
) -> None:
    for rank, row in enumerate(rows, start=1):
        key = (str(row["kind"]), int(row["row_id"]))
        entry = merged.get(key)
        if entry is None:
            entry = dict(row)
            entry["score"] = 0.0
            entry["sources"] = []
            merged[key] = entry
        entry["score"] = float(entry["score"]) + 1.0 / (k + rank)
        if source not in entry["sources"]:
            entry["sources"].append(source)
        entry[f"{source}_score"] = row.get("score")
        entry["source"] = "hybrid"


def _should_embed_transcript(*, text: str, role: str) -> bool:
    if not _embedding_service.enabled:
        return False
    if role.lower() != "user":
        return False
    if len(text.split()) <= 30:
        return False
    return _embedding_service.rng.random() < _embedding_service.config.sample_rate_transcripts


def _write_embedding(job: _EmbeddingJob, blob: bytes) -> None:
    table = "summaries" if job.kind == "summary" else "transcripts"
    if job.db_path is not None:
        with sqlite3.connect(job.db_path) as conn:
            conn.execute(
                f"UPDATE {table} SET embedding = ? WHERE id = ?",
                (blob, job.row_id),
            )
            conn.commit()
        return

    if job.conn is None:
        return
    job.conn.execute(
        f"UPDATE {table} SET embedding = ? WHERE id = ?",
        (blob, job.row_id),
    )
    job.conn.commit()


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    ddl: str,
) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _db_path_for(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        return None
    return Path(row[2])


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
