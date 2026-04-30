from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from mcp import types

import engram.mcp_tools as mcp_tools
from engram.config import EmbeddingsConfig
from engram.embeddings import EmbeddingQueue, GeminiEmbedder
from engram.mcp_tools import make_memory_search_server
from engram.memory import (
    insert_summary,
    insert_transcript,
    migrate,
    open_memory_db,
    search_hybrid,
    search_keyword,
    search_semantic,
)

BASE_TS = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
CHANNEL_A = "C07TESTA"
CHANNEL_B = "C07TESTB"


class _FakeModels:
    def __init__(self, values: list[float] | None = None, delay_s: float = 0.0):
        self.values = values or [0.0] * 768
        self.delay_s = delay_s
        self.calls = 0

    def embed_content(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        return {"embeddings": [{"values": self.values}]}


class _FakeClient:
    def __init__(self, values: list[float] | None = None, delay_s: float = 0.0):
        self.models = _FakeModels(values=values, delay_s=delay_s)


class _FakeEmbedder:
    def __init__(
        self,
        *,
        vector: list[float] | None = None,
        config: EmbeddingsConfig | None = None,
        delay_s: float = 0.0,
    ):
        self.config = config or EmbeddingsConfig(api_key="fake")
        self.enabled = True
        self.vector = vector or [1.0] * self.config.dimensions
        self.delay_s = delay_s
        self.calls = 0

    async def embed_one(self, _text: str) -> bytes | None:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return _pack(self.vector)


def _pack(values: list[float]) -> bytes:
    return np.asarray(values, dtype=np.float32).tobytes()


def _seed_summary(
    conn: sqlite3.Connection,
    *,
    channel_id: str = CHANNEL_A,
    text: str = "summary text",
    embedding: bytes | None = None,
    ts: datetime = BASE_TS,
) -> int:
    return insert_summary(
        conn,
        session_id=f"session-{channel_id}",
        channel_id=channel_id,
        ts=ts,
        trigger="manual",
        day=None,
        custom_instructions=None,
        summary_text=text,
        embedding=embedding,
    )


def _seed_transcript(
    conn: sqlite3.Connection,
    *,
    channel_id: str = CHANNEL_A,
    message_uuid: str = "msg-1",
    text: str = "transcript text",
    embedding: bytes | None = None,
    ts: datetime = BASE_TS,
) -> int:
    insert_transcript(
        conn,
        session_id=f"session-{channel_id}",
        channel_id=channel_id,
        ts=ts,
        role="user",
        message_uuid=message_uuid,
        parent_uuid=None,
        text=text,
    )
    row = conn.execute(
        "SELECT id FROM transcripts WHERE message_uuid = ?",
        (message_uuid,),
    ).fetchone()
    assert row is not None
    transcript_id = int(row["id"])
    if embedding is not None:
        conn.execute(
            "UPDATE transcripts SET embedding = ? WHERE id = ?",
            (embedding, transcript_id),
        )
    return transcript_id


async def _call_tool(
    server_config: dict[str, Any],
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    server = server_config["instance"]
    handler = server.request_handlers[types.CallToolRequest]
    result = await handler(
        types.CallToolRequest(
            params=types.CallToolRequestParams(name=name, arguments=args)
        )
    )
    root = result.root
    response: dict[str, Any] = {
        "content": [{"type": item.type, "text": item.text} for item in root.content]
    }
    if root.isError:
        response["isError"] = True
    return response


def _rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    return json.loads(response["content"][0]["text"])


@pytest.mark.asyncio
async def test_embedder_returns_bytes_of_correct_dimension():
    config = EmbeddingsConfig(dimensions=3, api_key="fake")
    embedder = GeminiEmbedder(config, client=_FakeClient(values=[1.0, 2.0, 3.0]))

    payload = await embedder.embed_one("hello")

    assert payload is not None
    assert np.frombuffer(payload, dtype=np.float32).size == config.dimensions


@pytest.mark.asyncio
async def test_missing_gemini_key_disables_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = EmbeddingsConfig.from_mapping({"enabled": True})

    with caplog.at_level(logging.WARNING):
        embedder = GeminiEmbedder(config)
        result = await embedder.embed_one("text")

    assert embedder.enabled is False
    assert result is None
    assert "embeddings.disabled reason=missing_api_key" in caplog.text


@pytest.mark.asyncio
async def test_embedding_api_timeout_returns_none(caplog: pytest.LogCaptureFixture):
    config = EmbeddingsConfig(dimensions=3, api_timeout_s=0.01, api_key="fake")
    embedder = GeminiEmbedder(
        config,
        client=_FakeClient(values=[1.0, 2.0, 3.0], delay_s=0.05),
    )

    with caplog.at_level(logging.WARNING):
        result = await embedder.embed_one("slow")

    assert result is None
    assert "embeddings.embed_failed reason=timeout" in caplog.text


@pytest.mark.asyncio
async def test_queue_enqueue_summary_populates_embedding_column(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with closing(open_memory_db(db_path)) as conn:
        summary_id = _seed_summary(conn, text="summary to embed")
    queue = EmbeddingQueue(
        _FakeEmbedder(vector=[1.0, 0.0, 0.0], config=EmbeddingsConfig(dimensions=3, api_key="fake")),
        db_path=db_path,
    )

    worker = asyncio.create_task(queue.run())
    try:
        await queue.enqueue_summary(summary_id, "summary to embed")
        await queue.drain()
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    with closing(open_memory_db(db_path)) as conn:
        row = conn.execute(
            "SELECT embedding FROM summaries WHERE id = ?",
            (summary_id,),
        ).fetchone()
    assert row["embedding"] is not None


@pytest.mark.asyncio
async def test_queue_sample_rate_respected_for_transcripts(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    transcript_ids = []
    with closing(open_memory_db(db_path)) as conn:
        for index in range(100):
            transcript_ids.append(
                _seed_transcript(
                    conn,
                    message_uuid=f"sample-{index}",
                    text="this transcript has enough tokens for semantic embedding",
                    ts=BASE_TS + timedelta(seconds=index),
                )
            )
    config = EmbeddingsConfig(
        dimensions=3,
        sample_rate_transcripts=0.3,
        min_transcript_tokens=3,
        api_key="fake",
    )
    queue = EmbeddingQueue(
        _FakeEmbedder(vector=[1.0, 0.0, 0.0], config=config),
        db_path=db_path,
        rng=random.Random(7),
    )

    for transcript_id in transcript_ids:
        await queue.enqueue_transcript_if_sampled(
            transcript_id,
            "this transcript has enough tokens for semantic embedding",
        )
    await queue.drain()

    with closing(open_memory_db(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM transcripts WHERE embedding IS NOT NULL"
        ).fetchone()[0]
    assert 20 <= count <= 40


@pytest.mark.asyncio
async def test_queue_drop_on_full_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    db_path = tmp_path / "memory.db"
    queue = EmbeddingQueue(
        _FakeEmbedder(vector=[1.0, 0.0, 0.0], config=EmbeddingsConfig(dimensions=3, api_key="fake")),
        db_path=db_path,
        max_size=1,
    )

    with caplog.at_level(logging.WARNING):
        await queue.enqueue_summary(1, "first")
        await queue.enqueue_summary(2, "second")

    assert queue.drop_count == 1
    assert "embedding.queue_full kind=summary row_id=2" in caplog.text


@pytest.mark.asyncio
async def test_queue_drain_awaits_pending(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    summary_ids = []
    with closing(open_memory_db(db_path)) as conn:
        for index in range(5):
            summary_ids.append(_seed_summary(conn, text=f"summary {index}"))
    queue = EmbeddingQueue(
        _FakeEmbedder(
            vector=[1.0, 0.0, 0.0],
            config=EmbeddingsConfig(dimensions=3, api_key="fake"),
            delay_s=0.01,
        ),
        db_path=db_path,
    )

    for summary_id in summary_ids:
        await queue.enqueue_summary(summary_id, "summary")
    await queue.drain()

    with closing(open_memory_db(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM summaries WHERE embedding IS NOT NULL"
        ).fetchone()[0]
    assert count == 5


def test_search_semantic_finds_paraphrase_fts_misses(tmp_path: Path):
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        summary_id = _seed_summary(
            conn,
            text="I use Argon2 for password hashing",
            embedding=_pack([1.0, 0.0, 0.0]),
        )

        assert search_keyword(
            conn,
            query="cryptographic library auth",
            scope="all_channels",
        ) == []
        results = search_semantic(
            conn,
            query_vec=_pack([0.95, 0.05, 0.0]),
            scope="all_channels",
        )

    assert results[0]["summary_id"] == summary_id


def test_search_semantic_scope_filters(tmp_path: Path):
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        _seed_summary(conn, channel_id=CHANNEL_A, text="channel a", embedding=_pack([1.0, 0.0]))
        _seed_summary(conn, channel_id=CHANNEL_B, text="channel b", embedding=_pack([1.0, 0.0]))

        results = search_semantic(
            conn,
            query_vec=_pack([1.0, 0.0]),
            scope="this_channel",
            channel_id=CHANNEL_A,
            limit=10,
        )

    assert {row["channel_id"] for row in results} == {CHANNEL_A}


def test_search_semantic_excludes_channels(tmp_path: Path):
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        _seed_summary(conn, channel_id=CHANNEL_A, text="channel a", embedding=_pack([1.0, 0.0]))
        _seed_summary(conn, channel_id=CHANNEL_B, text="channel b", embedding=_pack([1.0, 0.0]))

        results = search_semantic(
            conn,
            query_vec=_pack([1.0, 0.0]),
            scope="all_channels",
            excluded_channels=[CHANNEL_B],
            limit=10,
        )

    assert {row["channel_id"] for row in results} == {CHANNEL_A}


def test_search_hybrid_merges_fts_and_semantic_hits(tmp_path: Path):
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        keyword_id = _seed_summary(conn, text="exacttoken keyword row")
        semantic_id = _seed_summary(conn, text="semantic only row", embedding=_pack([1.0, 0.0]))

        results = search_hybrid(
            conn,
            query="exacttoken",
            query_vec=_pack([1.0, 0.0]),
            scope="all_channels",
            limit=5,
        )

    assert {row["summary_id"] for row in results} == {keyword_id, semantic_id}


def test_search_hybrid_excludes_channels(tmp_path: Path):
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        _seed_summary(
            conn,
            channel_id=CHANNEL_A,
            text="hybridtoken allowed",
            embedding=_pack([1.0, 0.0]),
        )
        _seed_summary(
            conn,
            channel_id=CHANNEL_B,
            text="hybridtoken excluded",
            embedding=_pack([1.0, 0.0]),
            ts=BASE_TS + timedelta(seconds=1),
        )

        results = search_hybrid(
            conn,
            query="hybridtoken",
            query_vec=_pack([1.0, 0.0]),
            scope="all_channels",
            excluded_channels=[CHANNEL_B],
            limit=5,
        )

    assert {row["channel_id"] for row in results} == {CHANNEL_A}


def test_search_hybrid_ranks_high_overlap_first(tmp_path: Path):
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        overlap_id = _seed_summary(
            conn,
            text="needle overlap row",
            embedding=_pack([1.0, 0.0]),
            ts=BASE_TS,
        )
        _seed_summary(conn, text="needle fts only", ts=BASE_TS + timedelta(seconds=1))
        _seed_summary(
            conn,
            text="semantic only",
            embedding=_pack([1.0, 0.0]),
            ts=BASE_TS + timedelta(seconds=2),
        )

        results = search_hybrid(
            conn,
            query="needle",
            query_vec=_pack([1.0, 0.0]),
            scope="all_channels",
            limit=3,
        )

    assert results[0]["summary_id"] == overlap_id


@pytest.mark.asyncio
async def test_memory_search_semantic_tool_roundtrip(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with closing(open_memory_db(db_path)) as conn:
        summary_id = _seed_summary(
            conn,
            text="I use Argon2 for password hashing",
            embedding=_pack([1.0, 0.0]),
        )
    server = make_memory_search_server(
        CHANNEL_A,
        db_path,
        embedder=_FakeEmbedder(vector=[1.0, 0.0], config=EmbeddingsConfig(dimensions=2, api_key="fake")),
    )

    response = await _call_tool(
        server,
        "memory_search_semantic",
        {"query": "what crypto library do we use for auth?"},
    )

    rows = _rows(response)
    assert rows[0]["summary_id"] == summary_id
    assert rows[0]["ts_iso"]


@pytest.mark.asyncio
async def test_memory_search_kind_hybrid_dispatches_to_search_hybrid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_path = tmp_path / "memory.db"
    with closing(open_memory_db(db_path)) as conn:
        summary_id = _seed_summary(conn, text="hybrid target")
    called: dict[str, Any] = {}

    def fake_search_hybrid(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        called["kwargs"] = kwargs
        return [
            {
                "kind": "summary",
                "channel_id": CHANNEL_A,
                "ts": BASE_TS.isoformat(),
                "snippet": "hybrid target",
                "message_uuid": None,
                "summary_id": summary_id,
            }
        ]

    monkeypatch.setattr(mcp_tools, "search_hybrid", fake_search_hybrid)
    server = make_memory_search_server(
        CHANNEL_A,
        db_path,
        embedder=_FakeEmbedder(vector=[1.0, 0.0], config=EmbeddingsConfig(dimensions=2, api_key="fake")),
    )

    response = await _call_tool(
        server,
        "memory_search",
        {"query": "hybrid", "kind": "hybrid"},
    )

    assert called["kwargs"]["query"] == "hybrid"
    assert _rows(response)[0]["summary_id"] == summary_id


def test_migration_adds_transcripts_embedding_column_idempotent():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(
            """
            CREATE TABLE transcripts (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                ts TIMESTAMP NOT NULL,
                role TEXT NOT NULL,
                message_uuid TEXT UNIQUE NOT NULL,
                parent_uuid TEXT,
                text TEXT NOT NULL
            );
            """
        )
        migrate(db)
        migrate(db)

        columns = [
            row[1]
            for row in db.execute("PRAGMA table_info(transcripts)").fetchall()
            if row[1] == "embedding"
        ]
    finally:
        db.close()

    assert columns == ["embedding"]
