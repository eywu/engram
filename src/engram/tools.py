"""Engram's in-process MCP tools."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from claude_agent_sdk import create_sdk_mcp_server as sdk_create_mcp_server
from claude_agent_sdk import tool

from engram import memory, telemetry

ENGRAM_MCP_SERVER_NAME = "engram"
MEMORY_SEARCH_TOOL_NAME = "memory_search"
MEMORY_SEARCH_CANONICAL_NAME = (
    f"mcp__{ENGRAM_MCP_SERVER_NAME}__{MEMORY_SEARCH_TOOL_NAME}"
)

_MEMORY_SEARCH_DESCRIPTION = (
    "Search Engram's own transcripts and summaries via full-text keyword "
    "match. Use this for in-context recall of prior Slack conversation "
    "content."
)

_MEMORY_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Keyword query to search for.",
        },
        "scope": {
            "type": "string",
            "enum": ["this_channel", "all_channels"],
            "default": "this_channel",
            "description": "Search only the caller's channel or all channels.",
        },
        "kind": {
            "type": "string",
            "enum": ["transcripts", "summaries", "both"],
            "default": "both",
            "description": "Which memory row types to search.",
        },
        "limit": {
            "type": "integer",
            "default": 5,
            "minimum": 1,
            "maximum": 50,
            "description": "Maximum number of matches to return.",
        },
    },
    "required": ["query"],
}


async def memory_search(
    query: str,
    scope: Literal["this_channel", "all_channels"] = "this_channel",
    kind: Literal["transcripts", "summaries", "both"] = "both",
    limit: int = 5,
    *,
    caller_channel_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict]:
    """Search transcripts and summaries through the keyword memory index."""
    start = time.perf_counter()
    results: list[dict] = []
    try:
        results = memory.search_keyword(
            query,
            channel_id=caller_channel_id,
            scope=scope,
            kind=kind,
            limit=limit,
            db_path=db_path,
        )
        return results
    finally:
        telemetry.record_event(
            "memory_search",
            {
                "channel_id": caller_channel_id,
                "query": query,
                "scope": scope,
                "kind": kind,
                "limit": limit,
                "result_count": len(results),
                "latency_ms": round((time.perf_counter() - start) * 1000, 3),
            },
        )


def create_sdk_mcp_server(
    *,
    channel_id: str | None = None,
    db_path: Path | str | None = None,
):
    """Create Engram's in-process SDK MCP server for one channel."""

    @tool(
        MEMORY_SEARCH_TOOL_NAME,
        _MEMORY_SEARCH_DESCRIPTION,
        _MEMORY_SEARCH_SCHEMA,
    )
    async def memory_search_tool(args: dict) -> dict:
        results = await memory_search(
            query=str(args.get("query") or ""),
            scope=args.get("scope") or "this_channel",
            kind=args.get("kind") or "both",
            limit=int(args.get("limit") or 5),
            caller_channel_id=channel_id,
            db_path=db_path,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(results, separators=(",", ":")),
                }
            ]
        }

    return sdk_create_mcp_server(
        name=ENGRAM_MCP_SERVER_NAME,
        version="1.0.0",
        tools=[memory_search_tool],
    )


def registered_tool_names() -> list[str]:
    """Return canonical SDK tool names registered by Engram."""
    return [MEMORY_SEARCH_CANONICAL_NAME]
