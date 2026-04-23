from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from engram.config import EmbeddingsConfig
from engram.embeddings import EmbeddingQueue
from engram.memory import open_memory_db
from engram.nightly.apply import apply_synthesis


class _FakeEmbedder:
    def __init__(self) -> None:
        self.config = EmbeddingsConfig(dimensions=3, api_key="fake")
        self.enabled = True
        self.calls: list[str] = []

    async def embed_one(self, text: str) -> bytes:
        self.calls.append(text)
        return np.asarray([1.0, 0.0, 0.0], dtype=np.float32).tobytes()


def _clock() -> datetime:
    return datetime(2026, 4, 22, 23, 45, tzinfo=UTC)


def _write_synthesis(tmp_path: Path, summaries: dict[str, str]) -> Path:
    path = tmp_path / "synthesis.json"
    path.write_text(json.dumps(_payload(summaries), indent=2), encoding="utf-8")
    return path


def _payload(summaries: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "date": "2026-04-22",
        "channels": [
            {
                "channel_id": channel_id,
                "status": "synthesized",
                "synthesis": {
                    "schema_version": 1,
                    "date": "2026-04-22",
                    "channel_id": channel_id,
                    "summary": summary,
                    "highlights": [],
                    "decisions": [],
                    "action_items": [],
                    "open_questions": [],
                    "source_row_ids": [1],
                },
            }
            for channel_id, summary in summaries.items()
        ],
        "skipped_channels": [],
    }


@pytest.mark.asyncio
async def test_apply_writes_three_nightly_rows_and_flushes_embeddings(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    embedder = _FakeEmbedder()
    queue = EmbeddingQueue(embedder, db_path=db_path)
    synthesis = _write_synthesis(
        tmp_path,
        {
            "C07TESTA": "alpha durable summary",
            "C07TESTB": "beta durable summary",
            "C07TESTC": "gamma durable summary",
        },
    )

    result = await apply_synthesis(
        synthesis,
        db_path=db_path,
        embedding_queue=queue,
        clock=_clock,
    )

    assert result.rows_written == 3
    assert result.rows_queued == 3
    assert queue.depth == 0
    assert embedder.calls == [
        "alpha durable summary",
        "beta durable summary",
        "gamma durable summary",
    ]
    with closing(open_memory_db(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT channel_id, day, trigger, summary_text, embedding
            FROM summaries
            ORDER BY channel_id
            """
        ).fetchall()
    assert len(rows) == 3
    assert {row["trigger"] for row in rows} == {"nightly"}
    assert {row["day"] for row in rows} == {"2026-04-22"}
    assert all(row["embedding"] is not None for row in rows)


@pytest.mark.asyncio
async def test_apply_rerun_upserts_without_duplicates_and_logs_overwrite(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="engram.nightly.apply")
    db_path = tmp_path / "memory.db"
    first = _write_synthesis(
        tmp_path,
        {
            "C07TESTA": "alpha first",
            "C07TESTB": "beta first",
            "C07TESTC": "gamma first",
        },
    )
    queue = EmbeddingQueue(_FakeEmbedder(), db_path=db_path)
    await apply_synthesis(first, db_path=db_path, embedding_queue=queue, clock=_clock)

    second = _write_synthesis(
        tmp_path,
        {
            "C07TESTA": "alpha overwritten",
            "C07TESTB": "beta overwritten",
            "C07TESTC": "gamma overwritten",
        },
    )
    caplog.clear()
    await apply_synthesis(
        second,
        db_path=db_path,
        embedding_queue=EmbeddingQueue(_FakeEmbedder(), db_path=db_path),
        clock=_clock,
    )

    with closing(open_memory_db(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        rows = conn.execute(
            """
            SELECT channel_id, summary_text, embedding
            FROM summaries
            WHERE trigger = 'nightly' AND day = '2026-04-22'
            ORDER BY channel_id
            """
        ).fetchall()
    assert count == 3
    assert [row["summary_text"] for row in rows] == [
        "alpha overwritten",
        "beta overwritten",
        "gamma overwritten",
    ]
    assert all(row["embedding"] is not None for row in rows)
    overwrite_logs = [
        record for record in caplog.records if record.getMessage() == "apply.upsert_overwrite"
    ]
    assert [record.channel_id for record in overwrite_logs] == [
        "C07TESTA",
        "C07TESTB",
        "C07TESTC",
    ]


@pytest.mark.asyncio
async def test_apply_dry_run_copies_identical_artifact_without_touching_db(
    tmp_path: Path,
) -> None:
    source = '{"date":"2099-01-02","channels":[],"schema_version":1}\n'
    synthesis = tmp_path / "synthesis.json"
    synthesis.write_text(source, encoding="utf-8")
    db_path = tmp_path / "memory.db"

    result = await apply_synthesis(
        synthesis,
        db_path=db_path,
        dry_run=True,
        clock=_clock,
    )

    assert result.output_path == Path("/tmp/engram-nightly-dryrun-2099-01-02/synthesis.json")
    assert result.output_path.read_text(encoding="utf-8") == source
    assert not db_path.exists()
