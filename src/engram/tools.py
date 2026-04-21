"""In-process memory tools exposed to Claude through an SDK MCP server."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from engram import memory

log = logging.getLogger(__name__)

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "scope": {"type": "string", "default": "channel"},
        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        "kind": {"type": "string", "default": "both"},
        "channel_id": {"type": "string"},
    },
    "required": ["query"],
}

_SEMANTIC_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "scope": {"type": "string", "default": "channel"},
        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        "kind": {"type": "string", "default": "both"},
        "channel_id": {"type": "string"},
    },
    "required": ["query"],
}


async def memory_search_semantic(
    query: str,
    *,
    scope: str = "channel",
    limit: int = 10,
    kind: str = "both",
    channel_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Embed query and search stored memory embeddings."""
    started = time.perf_counter()
    embedding_latency_ms: int | None = None
    result = await memory.embedding_service().embed_text(query)
    embedding_latency_ms = result.latency_ms if result is not None else None

    if result is None:
        results: list[dict[str, Any]] = []
    else:
        with memory.connect(db_path) as conn:
            results = memory.search_semantic(
                conn,
                query_vec=result.vector,
                scope=scope,
                channel_id=channel_id,
                kind=kind,
                limit=_clamp_limit(limit),
            )
    _log_tool_call(
        "memory_search_semantic",
        started,
        embedding_latency_ms=embedding_latency_ms,
        result_count=len(results),
    )
    return results


async def memory_search(
    query: str,
    *,
    scope: str = "channel",
    limit: int = 10,
    kind: str = "both",
    channel_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Search memory by keyword, or hybrid keyword+semantic when kind='hybrid'."""
    started = time.perf_counter()
    embedding_latency_ms: int | None = None
    limit = _clamp_limit(limit)

    with memory.connect(db_path) as conn:
        if kind == "hybrid":
            embed_result = await memory.embedding_service().embed_text(query)
            embedding_latency_ms = (
                embed_result.latency_ms if embed_result is not None else None
            )
            keyword_results = memory.search_keyword(
                conn,
                query=query,
                scope=scope,
                channel_id=channel_id,
                kind="both",
                limit=limit,
            )
            results = memory.search_hybrid(
                conn,
                query=query,
                query_vec=embed_result.vector if embed_result is not None else None,
                scope=scope,
                channel_id=channel_id,
                kind="both",
                limit=limit,
            )
            if not keyword_results:
                memory.record_daily_metric(conn, "fts5_empty_result")
            if not keyword_results and results:
                memory.record_daily_metric(conn, "fallback_to_semantic")
        else:
            results = memory.search_keyword(
                conn,
                query=query,
                scope=scope,
                channel_id=channel_id,
                kind=kind,
                limit=limit,
            )
            if not results:
                memory.record_daily_metric(conn, "fts5_empty_result")

    _log_tool_call(
        "memory_search",
        started,
        embedding_latency_ms=embedding_latency_ms,
        result_count=len(results),
    )
    return results


def build_memory_mcp_server(
    *,
    default_channel_id: str | None = None,
    db_path: Path | str | None = None,
):
    @tool(
        "memory_search",
        "Search Engram memory with FTS5 keywords. Use kind='hybrid' for keyword plus semantic recall.",
        _SEARCH_SCHEMA,
    )
    async def _memory_search_tool(args: dict[str, Any]) -> dict[str, Any]:
        results = await memory_search(
            str(args["query"]),
            scope=str(args.get("scope") or "channel"),
            limit=int(args.get("limit") or 10),
            kind=str(args.get("kind") or "both"),
            channel_id=args.get("channel_id") or default_channel_id,
            db_path=db_path,
        )
        return _tool_result(results)

    @tool(
        "memory_search_semantic",
        "Search Engram memory by embedding the query and ranking stored embeddings by cosine similarity.",
        _SEMANTIC_SCHEMA,
    )
    async def _memory_search_semantic_tool(args: dict[str, Any]) -> dict[str, Any]:
        results = await memory_search_semantic(
            str(args["query"]),
            scope=str(args.get("scope") or "channel"),
            limit=int(args.get("limit") or 10),
            kind=str(args.get("kind") or "both"),
            channel_id=args.get("channel_id") or default_channel_id,
            db_path=db_path,
        )
        return _tool_result(results)

    return create_sdk_mcp_server(
        "engram_memory",
        version="0.1.0",
        tools=[_memory_search_tool, _memory_search_semantic_tool],
    )


def _tool_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(results, ensure_ascii=False, separators=(",", ":")),
            }
        ]
    }


def _log_tool_call(
    tool_name: str,
    started: float,
    *,
    embedding_latency_ms: int | None,
    result_count: int,
) -> None:
    latency_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "memory.tool_call tool=%s latency_ms=%d embedding_api_latency_ms=%s "
        "result_count=%d empty_result=%s",
        tool_name,
        latency_ms,
        embedding_latency_ms,
        result_count,
        result_count == 0,
    )


def _clamp_limit(limit: int) -> int:
    return max(1, min(50, int(limit)))
