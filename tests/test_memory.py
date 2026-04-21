"""GRO-396 semantic memory tests."""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Sequence

import numpy as np
import pytest

from engram import memory
from engram.config import EmbeddingsConfig
from engram.tools import memory_search_semantic


class FakeEmbedder:
    async def embed(self, text: str) -> Sequence[float]:
        lower = text.lower()
        if "argon2" in lower or "cryptographic" in lower or "authentication" in lower:
            return [1.0, 0.0, 0.0]
        if "walrus" in lower:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


class HangingEmbedder:
    async def embed(self, text: str) -> Sequence[float]:
        await asyncio.sleep(10)
        return [1.0, 0.0, 0.0]


@pytest.fixture(autouse=True)
async def clean_embedding_service():
    await memory.shutdown_embeddings()
    memory.configure_embeddings(EmbeddingsConfig(enabled=False))
    yield
    await memory.shutdown_embeddings()
    memory.configure_embeddings(EmbeddingsConfig(enabled=False))


@pytest.mark.asyncio
async def test_insert_summary_triggers_async_embed(tmp_path):
    service = memory.configure_embeddings(
        EmbeddingsConfig(enabled=True, dimensions=3),
        embedder=FakeEmbedder(),
    )
    with memory.connect(tmp_path / "memory.db") as conn:
        row_id = memory.insert_summary(
            conn,
            summary_text="I use Argon2 for password hashing",
            scope="channel",
            channel_id="C07TEST123",
        )

        await service.drain()

        row = conn.execute(
            "SELECT embedding FROM summaries WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["embedding"] is not None
        assert memory.blob_to_embedding(row["embedding"]).shape == (3,)


@pytest.mark.asyncio
async def test_missing_gemini_key_disables_embeddings_gracefully(
    tmp_path,
    monkeypatch,
    caplog,
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    caplog.set_level("WARNING")
    service = memory.configure_embeddings(EmbeddingsConfig(enabled=True, dimensions=3))

    assert service.enabled is False
    assert "memory.embeddings_disabled" in caplog.text

    with memory.connect(tmp_path / "memory.db") as conn:
        row_id = memory.insert_summary(
            conn,
            summary_text="I use Argon2 for password hashing",
            scope="channel",
            channel_id="C07TEST123",
        )
        row = conn.execute(
            "SELECT embedding FROM summaries WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["embedding"] is None
        assert (
            memory.search_semantic(
                conn,
                query_vec=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
                scope="channel",
                channel_id="C07TEST123",
                kind="both",
                limit=5,
            )
            == []
        )


@pytest.mark.asyncio
async def test_semantic_finds_paraphrase_that_fts_misses(tmp_path):
    service = memory.configure_embeddings(
        EmbeddingsConfig(enabled=True, dimensions=3),
        embedder=FakeEmbedder(),
    )
    db_path = tmp_path / "memory.db"
    with memory.connect(db_path) as conn:
        memory.insert_summary(
            conn,
            summary_text="I use Argon2 for password hashing",
            scope="channel",
            channel_id="C07TEST123",
        )
        await service.drain()

        keyword = memory.search_keyword(
            conn,
            query="cryptographic library for authentication",
            scope="channel",
            channel_id="C07TEST123",
            kind="both",
            limit=5,
        )
        assert keyword == []

    semantic = await memory_search_semantic(
        "cryptographic library for authentication",
        scope="channel",
        channel_id="C07TEST123",
        limit=5,
        db_path=db_path,
    )
    assert semantic[0]["text"] == "I use Argon2 for password hashing"


@pytest.mark.asyncio
async def test_hybrid_includes_both_fts_and_semantic_hits(tmp_path):
    with memory.connect(tmp_path / "memory.db") as conn:
        fts_id = memory.insert_summary(
            conn,
            summary_text="walrus rollout checklist",
            scope="channel",
            channel_id="C07TEST123",
        )
        semantic_id = memory.insert_summary(
            conn,
            summary_text="I use Argon2 for password hashing",
            scope="channel",
            channel_id="C07TEST123",
        )
        conn.execute(
            "UPDATE summaries SET embedding = ? WHERE id = ?",
            (memory.embedding_to_blob([0.0, 1.0, 0.0], dimensions=3), fts_id),
        )
        conn.execute(
            "UPDATE summaries SET embedding = ? WHERE id = ?",
            (memory.embedding_to_blob([1.0, 0.0, 0.0], dimensions=3), semantic_id),
        )
        conn.commit()

        results = memory.search_hybrid(
            conn,
            query="walrus",
            query_vec=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
            scope="channel",
            channel_id="C07TEST123",
            kind="both",
            limit=5,
        )

    keys = {(r["kind"], r["row_id"]) for r in results}
    assert ("summary", fts_id) in keys
    assert ("summary", semantic_id) in keys
    assert len(keys) == len(results)


@pytest.mark.asyncio
async def test_sample_rate_respected(tmp_path):
    service = memory.configure_embeddings(
        EmbeddingsConfig(
            enabled=True,
            dimensions=3,
            sample_rate_transcripts=0.3,
        ),
        embedder=FakeEmbedder(),
        rng=random.Random(9),
    )
    with memory.connect(tmp_path / "memory.db") as conn:
        for i in range(10):
            memory.insert_transcript(
                conn,
                text=f"user message {i} " + ("token " * 31),
                role="user",
                scope="channel",
                channel_id="C07TEST123",
            )

        await service.drain()

        embedded = conn.execute(
            "SELECT COUNT(*) AS n FROM transcripts WHERE embedding IS NOT NULL"
        ).fetchone()["n"]
        assert embedded == 3


@pytest.mark.asyncio
async def test_gemini_api_timeout_does_not_block_ingestion(tmp_path, caplog):
    service = memory.configure_embeddings(
        EmbeddingsConfig(enabled=True, dimensions=3, timeout_seconds=0.05),
        embedder=HangingEmbedder(),
    )
    caplog.set_level("WARNING")
    with memory.connect(tmp_path / "memory.db") as conn:
        started = time.perf_counter()
        row_id = memory.insert_summary(
            conn,
            summary_text="I use Argon2 for password hashing",
            scope="channel",
            channel_id="C07TEST123",
        )
        assert time.perf_counter() - started < 0.2

        await service.drain(timeout=1.0)

        row = conn.execute(
            "SELECT embedding FROM summaries WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["embedding"] is None
        assert "memory.embedding_timeout" in caplog.text
