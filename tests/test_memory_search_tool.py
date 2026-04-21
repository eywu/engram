"""Tests for the Engram memory_search MCP tool."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from engram import memory, telemetry
from engram.cli import app as cli_app
from engram.tools import (
    MEMORY_SEARCH_CANONICAL_NAME,
    create_sdk_mcp_server,
    memory_search,
    registered_tool_names,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


@pytest.fixture(autouse=True)
def capture_telemetry(monkeypatch: pytest.MonkeyPatch):
    events: list[tuple[str, dict]] = []

    def record_event(event: str, payload: dict, **_kwargs) -> None:
        events.append((event, payload))

    monkeypatch.setattr(telemetry, "record_event", record_event)
    return events


@pytest.mark.asyncio
async def test_memory_search_returns_fts_matches(db_path: Path):
    words = ["octopus", "cobalt", "saffron", "zircon", "velvet"]
    for i, word in enumerate(words):
        memory.insert_transcript(
            channel_id="C07TEST123",
            message_uuid=f"msg-{i}",
            ts_iso=f"2026-04-21T00:00:0{i}+00:00",
            text=f"distinct keyword {word}",
            db_path=db_path,
        )

    for i, word in enumerate(words):
        results = await memory_search(
            word,
            caller_channel_id="C07TEST123",
            db_path=db_path,
        )
        assert results
        assert results[0]["message_uuid_or_summary_id"] == f"msg-{i}"
        assert word in results[0]["snippet"]


@pytest.mark.asyncio
async def test_scope_this_channel_filters_by_caller_channel(db_path: Path):
    memory.insert_transcript(
        channel_id="C07A",
        message_uuid="a",
        text="shared keyword papaya from channel A",
        db_path=db_path,
    )
    memory.insert_transcript(
        channel_id="C07B",
        message_uuid="b",
        text="shared keyword papaya from channel B",
        db_path=db_path,
    )

    results = await memory_search(
        "papaya",
        scope="this_channel",
        caller_channel_id="C07A",
        db_path=db_path,
    )

    assert {row["channel_id"] for row in results} == {"C07A"}


@pytest.mark.asyncio
async def test_scope_all_channels_aggregates(db_path: Path):
    memory.insert_transcript(
        channel_id="C07A",
        message_uuid="a",
        text="GRO-322 status is green",
        db_path=db_path,
    )
    memory.insert_transcript(
        channel_id="C07B",
        message_uuid="b",
        text="GRO-322 status is blocked",
        db_path=db_path,
    )

    results = await memory_search(
        "GRO-322",
        scope="all_channels",
        caller_channel_id="C07B",
        db_path=db_path,
    )

    assert {row["channel_id"] for row in results} == {"C07A", "C07B"}


@pytest.mark.asyncio
async def test_kind_filter_transcripts_only(db_path: Path):
    memory.insert_transcript(
        channel_id="C07TEST123",
        message_uuid="msg",
        text="keyword mango in transcript",
        db_path=db_path,
    )
    memory.insert_summary(
        channel_id="C07TEST123",
        summary_id="sum",
        text="keyword mango in summary",
        db_path=db_path,
    )

    results = await memory_search(
        "mango",
        kind="transcripts",
        caller_channel_id="C07TEST123",
        db_path=db_path,
    )

    assert len(results) == 1
    assert results[0]["kind"] == "transcripts"
    assert results[0]["message_uuid_or_summary_id"] == "msg"


@pytest.mark.asyncio
async def test_limit_is_respected(db_path: Path):
    for i in range(10):
        memory.insert_transcript(
            channel_id="C07TEST123",
            message_uuid=f"msg-{i}",
            text=f"limit keyword lychee row {i}",
            db_path=db_path,
        )

    results = await memory_search(
        "lychee",
        limit=3,
        caller_channel_id="C07TEST123",
        db_path=db_path,
    )

    assert len(results) == 3


@pytest.mark.asyncio
async def test_empty_query_returns_empty_list(db_path: Path):
    memory.insert_transcript(
        channel_id="C07TEST123",
        message_uuid="msg",
        text="empty query should not matter",
        db_path=db_path,
    )

    assert (
        await memory_search(
            "",
            caller_channel_id="C07TEST123",
            db_path=db_path,
        )
        == []
    )


@pytest.mark.asyncio
async def test_snippet_includes_query_term(db_path: Path):
    memory.insert_transcript(
        channel_id="C07TEST123",
        message_uuid="msg",
        text="the secret word is octopus",
        db_path=db_path,
    )

    results = await memory_search(
        "octopus",
        caller_channel_id="C07TEST123",
        db_path=db_path,
    )

    assert "octopus" in results[0]["snippet"]


@pytest.mark.asyncio
async def test_tool_registered_with_correct_name():
    server = create_sdk_mcp_server(channel_id="C07TEST123")

    assert server["name"] == "engram"
    tool_def = await server["instance"]._get_cached_tool_definition("memory_search")
    assert tool_def.name == "memory_search"
    assert registered_tool_names() == [MEMORY_SEARCH_CANONICAL_NAME]


@pytest.mark.asyncio
async def test_memory_search_logs_telemetry(db_path: Path, capture_telemetry):
    memory.insert_transcript(
        channel_id="C07TEST123",
        message_uuid="msg",
        text="telemetry keyword guava",
        db_path=db_path,
    )

    await memory_search(
        "guava",
        caller_channel_id="C07TEST123",
        db_path=db_path,
    )

    event, payload = capture_telemetry[-1]
    assert event == "memory_search"
    assert payload["channel_id"] == "C07TEST123"
    assert payload["query"] == "guava"
    assert payload["scope"] == "this_channel"
    assert payload["kind"] == "both"
    assert payload["limit"] == 5
    assert payload["result_count"] == 1
    assert payload["latency_ms"] >= 0


def test_status_json_lists_registered_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))

    result = CliRunner().invoke(cli_app, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert MEMORY_SEARCH_CANONICAL_NAME in payload["tools"]["registered"]
