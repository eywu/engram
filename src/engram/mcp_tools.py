"""In-process MCP tools exposed to Claude."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from engram.memory import open_memory_db, search_hybrid, search_keyword, search_semantic
from engram.telemetry import logger as telemetry_logger

MEMORY_SEARCH_SERVER_NAME = "engram-memory"
MEMORY_SEARCH_TOOL_NAME = "memory_search"
MEMORY_SEARCH_SEMANTIC_TOOL_NAME = "memory_search_semantic"
MEMORY_SEARCH_FULL_TOOL_NAME = (
    f"mcp__{MEMORY_SEARCH_SERVER_NAME}__{MEMORY_SEARCH_TOOL_NAME}"
)
MEMORY_SEARCH_SEMANTIC_FULL_TOOL_NAME = (
    f"mcp__{MEMORY_SEARCH_SERVER_NAME}__{MEMORY_SEARCH_SEMANTIC_TOOL_NAME}"
)
MEMORY_SEARCH_FULL_TOOL_NAMES = [
    MEMORY_SEARCH_FULL_TOOL_NAME,
    MEMORY_SEARCH_SEMANTIC_FULL_TOOL_NAME,
]

_MEMORY_SEARCH_DESCRIPTION = (
    "Search Engram's own transcripts and summaries via full-text keyword or hybrid "
    "semantic match. "
    "Use when the user asks about something they told you earlier, or when you "
    "need to recall context from a prior conversation in this or another channel. "
    "Returns up to `limit` snippets with channel_id and timestamp."
)

_MEMORY_SEARCH_SEMANTIC_DESCRIPTION = (
    "Search Engram's own transcripts and summaries by semantic similarity. "
    "Use for paraphrase recall when exact keywords may not match prior memory."
)

_MEMORY_SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "FTS5 keyword or phrase to search for.",
        },
        "scope": {
            "type": "string",
            "enum": ["this_channel", "all_channels"],
            "default": "this_channel",
        },
        "kind": {
            "type": "string",
            "enum": ["transcripts", "summaries", "both", "hybrid"],
            "default": "both",
        },
        "limit": {
            "type": "integer",
            "default": 5,
            "description": "Maximum number of results to return; clamped to 1-20.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_MEMORY_SEARCH_SEMANTIC_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural-language query to embed for semantic recall.",
        },
        "scope": {
            "type": "string",
            "enum": ["this_channel", "all_channels"],
            "default": "this_channel",
        },
        "limit": {
            "type": "integer",
            "default": 5,
            "description": "Maximum number of results to return; clamped to 1-20.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


_FTS5_INVOCATIONS = 0
_FTS5_EMPTY_RESULTS = 0


@dataclass
class _DisabledEmbedder:
    enabled: bool = False
    last_latency_ms: int | None = None

    async def embed_one(self, _text: str) -> bytes | None:
        return None


def make_memory_search_server(
    caller_channel_id: str,
    memory_db_path: Path | None = None,
    embedder: Any | None = None,
) -> McpSdkServerConfig:
    """Return a per-channel SDK MCP server exposing Engram memory search tools."""
    active_embedder = embedder or _DisabledEmbedder()

    @tool(
        name=MEMORY_SEARCH_TOOL_NAME,
        description=_MEMORY_SEARCH_DESCRIPTION,
        input_schema=_MEMORY_SEARCH_INPUT_SCHEMA,
    )
    async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        scope = str(args.get("scope") or "this_channel")
        kind = str(args.get("kind") or "both")
        limit = _clamp_limit(args.get("limit", 5))

        if not query:
            return {"content": [{"type": "text", "text": "[]"}]}

        started_at = time.monotonic()
        embedding_api_latency_ms: int | None = None
        try:
            effective_channel = caller_channel_id if scope == "this_channel" else None
            if kind == "hybrid":
                query_vec, embedding_api_latency_ms = await _embed_query(
                    active_embedder,
                    query,
                )
                if query_vec is None:
                    with closing(open_memory_db(memory_db_path)) as conn:
                        rows = search_keyword(
                            conn,
                            query=query,
                            scope=scope,  # type: ignore[arg-type]
                            channel_id=effective_channel,
                            kind="both",
                            limit=limit,
                        )
                else:
                    rows = await _run_hybrid_search(
                        memory_db_path=memory_db_path,
                        query=query,
                        query_vec=query_vec,
                        scope=scope,
                        channel_id=effective_channel,
                        limit=limit,
                    )
            else:
                with closing(open_memory_db(memory_db_path)) as conn:
                    rows = search_keyword(
                        conn,
                        query=query,
                        scope=scope,  # type: ignore[arg-type]
                        channel_id=effective_channel,
                        kind=kind,  # type: ignore[arg-type]
                        limit=limit,
                    )
        except Exception as exc:
            telemetry_logger.error(
                "memory_search.failed",
                extra={
                    "channel_id": caller_channel_id,
                    "error": str(exc),
                },
            )
            return {
                "content": [{"type": "text", "text": "[]"}],
                "isError": True,
                "is_error": True,
            }

        normalized_rows = [_normalize_row(row) for row in rows]
        if kind != "hybrid":
            _record_fts5_invocation(empty_result=not normalized_rows)
        telemetry_logger.info(
            "memory_search.invoked",
            extra={
                "tool": MEMORY_SEARCH_TOOL_NAME,
                "channel_id": caller_channel_id,
                "query_len": len(query),
                "scope": scope,
                "kind": kind,
                "limit": limit,
                "result_count": len(normalized_rows),
                "empty_result": not normalized_rows,
                "latency_ms": int((time.monotonic() - started_at) * 1000),
                "embedding_api_latency_ms": embedding_api_latency_ms,
            },
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(normalized_rows, default=str),
                }
            ]
        }

    @tool(
        name=MEMORY_SEARCH_SEMANTIC_TOOL_NAME,
        description=_MEMORY_SEARCH_SEMANTIC_DESCRIPTION,
        input_schema=_MEMORY_SEARCH_SEMANTIC_INPUT_SCHEMA,
    )
    async def memory_search_semantic(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        scope = str(args.get("scope") or "this_channel")
        limit = _clamp_limit(args.get("limit", 5))

        if not query:
            return {"content": [{"type": "text", "text": "[]"}]}

        started_at = time.monotonic()
        rows: list[dict[str, Any]] = []
        query_vec, embedding_api_latency_ms = await _embed_query(active_embedder, query)
        if query_vec is not None:
            try:
                rows = await _run_semantic_search(
                    memory_db_path=memory_db_path,
                    query_vec=query_vec,
                    scope=scope,
                    channel_id=caller_channel_id if scope == "this_channel" else None,
                    limit=limit,
                )
            except Exception as exc:
                telemetry_logger.error(
                    "memory_search_semantic.failed",
                    extra={
                        "channel_id": caller_channel_id,
                        "error": str(exc),
                    },
                )
                return {
                    "content": [{"type": "text", "text": "[]"}],
                    "isError": True,
                    "is_error": True,
                }

        normalized_rows = [_normalize_row(row) for row in rows]
        telemetry_logger.info(
            "memory_search_semantic.invoked",
            extra={
                "tool": MEMORY_SEARCH_SEMANTIC_TOOL_NAME,
                "channel_id": caller_channel_id,
                "query_len": len(query),
                "scope": scope,
                "limit": limit,
                "result_count": len(normalized_rows),
                "empty_result": not normalized_rows,
                "latency_ms": int((time.monotonic() - started_at) * 1000),
                "embedding_api_latency_ms": embedding_api_latency_ms,
            },
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(normalized_rows, default=str),
                }
            ]
        }

    return create_sdk_mcp_server(
        name=MEMORY_SEARCH_SERVER_NAME,
        tools=[memory_search, memory_search_semantic],
    )


def memory_tool_metrics() -> dict[str, Any]:
    empty_rate = (
        _FTS5_EMPTY_RESULTS / _FTS5_INVOCATIONS
        if _FTS5_INVOCATIONS
        else 0.0
    )
    return {
        "fts5_only": {
            "invocations": _FTS5_INVOCATIONS,
            "empty_results": _FTS5_EMPTY_RESULTS,
            "empty_result_rate": empty_rate,
        },
        "fallback_to_semantic": {
            "invocations": 0,
            "rate": 0.0,
        },
    }


def _clamp_limit(raw_limit: object) -> int:
    try:
        limit = int(raw_limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        limit = 5
    return max(1, min(20, limit))


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    if "ts_iso" not in normalized and "ts" in normalized:
        normalized["ts_iso"] = normalized.pop("ts")
    return normalized


async def _embed_query(embedder: Any, query: str) -> tuple[bytes | None, int | None]:
    started_at = time.monotonic()
    query_vec = await embedder.embed_one(query)
    return query_vec, int((time.monotonic() - started_at) * 1000)


async def _run_semantic_search(
    *,
    memory_db_path: Path | None,
    query_vec: bytes,
    scope: str,
    channel_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        with closing(open_memory_db(memory_db_path)) as conn:
            return search_semantic(
                conn,
                query_vec=query_vec,
                scope=scope,  # type: ignore[arg-type]
                channel_id=channel_id,
                kind="both",
                limit=limit,
            )

    return await asyncio.to_thread(_run)


async def _run_hybrid_search(
    *,
    memory_db_path: Path | None,
    query: str,
    query_vec: bytes,
    scope: str,
    channel_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        with closing(open_memory_db(memory_db_path)) as conn:
            return search_hybrid(
                conn,
                query=query,
                query_vec=query_vec,
                scope=scope,  # type: ignore[arg-type]
                channel_id=channel_id,
                kind="both",
                limit=limit,
            )

    return await asyncio.to_thread(_run)


def _record_fts5_invocation(*, empty_result: bool) -> None:
    global _FTS5_INVOCATIONS, _FTS5_EMPTY_RESULTS
    _FTS5_INVOCATIONS += 1
    if empty_result:
        _FTS5_EMPTY_RESULTS += 1
