"""In-process MCP tools exposed to Claude."""
from __future__ import annotations

import json
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from engram.memory import open_memory_db, search_keyword
from engram.telemetry import logger as telemetry_logger

MEMORY_SEARCH_SERVER_NAME = "engram-memory"
MEMORY_SEARCH_TOOL_NAME = "memory_search"
MEMORY_SEARCH_FULL_TOOL_NAME = (
    f"mcp__{MEMORY_SEARCH_SERVER_NAME}__{MEMORY_SEARCH_TOOL_NAME}"
)

_MEMORY_SEARCH_DESCRIPTION = (
    "Search Engram's own transcripts and summaries via full-text keyword match. "
    "Use when the user asks about something they told you earlier, or when you "
    "need to recall context from a prior conversation in this or another channel. "
    "Returns up to `limit` snippets with channel_id and timestamp."
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
            "enum": ["transcripts", "summaries", "both"],
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


def make_memory_search_server(
    caller_channel_id: str,
    memory_db_path: Path | None = None,
) -> McpSdkServerConfig:
    """Return a per-channel SDK MCP server exposing `memory_search`."""

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
        try:
            with closing(open_memory_db(memory_db_path)) as conn:
                effective_channel = (
                    caller_channel_id if scope == "this_channel" else None
                )
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
        telemetry_logger.info(
            "memory_search.invoked",
            extra={
                "channel_id": caller_channel_id,
                "query_len": len(query),
                "scope": scope,
                "kind": kind,
                "limit": limit,
                "result_count": len(normalized_rows),
                "latency_ms": int((time.monotonic() - started_at) * 1000),
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
        tools=[memory_search],
    )


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
