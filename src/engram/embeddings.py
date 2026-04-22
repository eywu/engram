"""Gemini embeddings and nonblocking memory embedding queue."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from engram.config import EmbeddingsConfig
from engram.memory import open_memory_db

log = logging.getLogger(__name__)


class GeminiEmbedder:
    """Small async wrapper around Gemini ``text-embedding-004``."""

    def __init__(
        self,
        config: EmbeddingsConfig,
        *,
        client: Any | None = None,
    ):
        self.config = config
        self.enabled = False
        self.client = client
        self.last_latency_ms: int | None = None

        if not config.enabled:
            log.warning("embeddings.disabled reason=config_disabled")
            return
        if config.provider != "gemini":
            log.warning(
                "embeddings.disabled reason=unsupported_provider provider=%s",
                config.provider,
            )
            return
        if client is None and not config.api_key:
            log.warning("embeddings.disabled reason=missing_api_key")
            return

        if self.client is None:
            try:
                from google import genai
            except Exception:
                log.warning("embeddings.disabled reason=client_import_failed", exc_info=True)
                return
            self.client = genai.Client(api_key=config.api_key)

        self.enabled = True

    async def embed_one(self, text: str) -> bytes | None:
        """Return one float32 embedding packed as bytes, or ``None`` on failure."""
        if not self.enabled:
            return None
        if not text.strip():
            return None

        started_at = time.monotonic()
        try:
            values = await asyncio.wait_for(
                asyncio.to_thread(self._embed_sync, text),
                timeout=self.config.api_timeout_s,
            )
        except TimeoutError:
            self.last_latency_ms = int((time.monotonic() - started_at) * 1000)
            log.warning(
                "embeddings.embed_failed reason=timeout timeout_s=%s",
                self.config.api_timeout_s,
            )
            return None
        except Exception:
            self.last_latency_ms = int((time.monotonic() - started_at) * 1000)
            log.warning("embeddings.embed_failed reason=api_error", exc_info=True)
            return None

        self.last_latency_ms = int((time.monotonic() - started_at) * 1000)
        vector = np.asarray(values, dtype=np.float32)
        if vector.size != self.config.dimensions:
            log.warning(
                "embeddings.embed_failed reason=dimension_mismatch expected=%d actual=%d",
                self.config.dimensions,
                vector.size,
            )
            return None
        return vector.tobytes()

    async def embed_batch(self, texts: list[str]) -> list[bytes | None]:
        """Embed a batch without retries. M3 keeps this intentionally simple."""
        return [await self.embed_one(text) for text in texts]

    def _embed_sync(self, text: str) -> list[float]:
        response = self.client.models.embed_content(  # type: ignore[union-attr]
            model=self.config.model,
            contents=text,
        )
        return _extract_embedding_values(response)


@dataclass(frozen=True)
class _EmbeddingWorkItem:
    table: str
    row_id: int
    text: str


class EmbeddingQueue:
    """Bounded asyncio queue that embeds rows out-of-band after insert."""

    def __init__(
        self,
        embedder: GeminiEmbedder,
        *,
        db_path: Path | None = None,
        max_size: int = 1000,
        rng: random.Random | None = None,
    ):
        self.embedder = embedder
        self.db_path = db_path
        self._queue: asyncio.Queue[_EmbeddingWorkItem] = asyncio.Queue(maxsize=max_size)
        self._rng = rng
        self._running = False
        self.drop_count = 0

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    async def enqueue_summary(self, summary_id: int, text: str) -> None:
        if not self.embedder.enabled:
            return
        self._enqueue(_EmbeddingWorkItem("summaries", summary_id, text))

    async def enqueue_transcript_if_sampled(self, transcript_id: int, text: str) -> None:
        if not self.embedder.enabled:
            return
        if _token_count(text) < self.embedder.config.min_transcript_tokens:
            return
        random_value = self._rng.random() if self._rng is not None else random.random()
        if random_value >= self.embedder.config.sample_rate_transcripts:
            return
        self._enqueue(_EmbeddingWorkItem("transcripts", transcript_id, text))

    async def run(self) -> None:
        """Run the worker loop until its task is cancelled."""
        if not self.embedder.enabled:
            return
        self._running = True
        try:
            while True:
                item = await self._queue.get()
                try:
                    await self._process(item)
                finally:
                    self._queue.task_done()
        finally:
            self._running = False

    async def drain(self) -> None:
        """Wait for pending queue work to be persisted."""
        if not self.embedder.enabled:
            return
        if self._running:
            await self._queue.join()
            return

        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self._process(item)
            finally:
                self._queue.task_done()

    def _enqueue(self, item: _EmbeddingWorkItem) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.drop_count += 1
            log.warning(
                "embedding.queue_full kind=%s row_id=%d drop_count=%d",
                "summary" if item.table == "summaries" else "transcript",
                item.row_id,
                self.drop_count,
            )

    async def _process(self, item: _EmbeddingWorkItem) -> None:
        embedding = await self.embedder.embed_one(item.text)
        if embedding is None:
            return
        await asyncio.to_thread(self._store_embedding, item, embedding)

    def _store_embedding(self, item: _EmbeddingWorkItem, embedding: bytes) -> None:
        if item.table not in {"summaries", "transcripts"}:
            raise ValueError(f"invalid embedding table: {item.table}")
        with closing(open_memory_db(self.db_path)) as conn:
            conn.execute(
                f"UPDATE {item.table} SET embedding = ? WHERE id = ?",
                (embedding, item.row_id),
            )


def _extract_embedding_values(response: Any) -> list[float]:
    embeddings = _get_attr_or_key(response, "embeddings")
    if embeddings:
        first = embeddings[0]
        values = _get_attr_or_key(first, "values") or _get_attr_or_key(first, "embedding")
        if values is not None:
            return list(values)

    embedding = _get_attr_or_key(response, "embedding")
    if embedding is not None:
        values = _get_attr_or_key(embedding, "values") or embedding
        return list(values)

    values = _get_attr_or_key(response, "values")
    if values is not None:
        return list(values)

    raise ValueError("Gemini embedding response did not contain embedding values")


def _get_attr_or_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _token_count(text: str) -> int:
    return len(text.split())
