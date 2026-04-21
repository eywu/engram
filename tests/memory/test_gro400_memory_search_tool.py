from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import types

import engram.mcp_tools as mcp_tools
from engram import paths
from engram.bootstrap import provision_channel
from engram.manifest import IdentityTemplate, load_manifest
from engram.mcp_tools import MEMORY_SEARCH_FULL_TOOL_NAMES, make_memory_search_server
from engram.memory import insert_summary, insert_transcript, open_memory_db

BASE_TS = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
CHANNEL_A = "C07TESTA"
CHANNEL_B = "C07TESTB"


async def _call_memory_search(
    server_config: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    server = server_config["instance"]
    handler = server.request_handlers[types.CallToolRequest]
    result = await handler(
        types.CallToolRequest(
            params=types.CallToolRequestParams(
                name="memory_search",
                arguments=args,
            )
        )
    )
    root = result.root
    response: dict[str, Any] = {
        "content": [
            {"type": item.type, "text": item.text}
            for item in root.content
        ]
    }
    if root.isError:
        response["isError"] = True
    return response


async def _list_tools(server_config: dict[str, Any]) -> list[str]:
    server = server_config["instance"]
    handler = server.request_handlers[types.ListToolsRequest]
    result = await handler(types.ListToolsRequest())
    return [tool.name for tool in result.root.tools]


def _rows_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    return json.loads(response["content"][0]["text"])


def _seed_transcript(
    db_path: Path,
    *,
    channel_id: str = CHANNEL_A,
    message_uuid: str = "msg-1",
    text: str = "memory text",
    ts: datetime = BASE_TS,
) -> None:
    with closing(open_memory_db(db_path)) as conn:
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


def _seed_summary(
    db_path: Path,
    *,
    channel_id: str = CHANNEL_A,
    summary_text: str = "memory summary",
    ts: datetime = BASE_TS,
) -> int:
    with closing(open_memory_db(db_path)) as conn:
        return insert_summary(
            conn,
            session_id=f"session-{channel_id}",
            channel_id=channel_id,
            ts=ts,
            trigger="manual",
            day=date(2026, 4, 21),
            custom_instructions=None,
            summary_text=summary_text,
            embedding=None,
        )


@pytest.mark.asyncio
async def test_tool_registered_with_canonical_name(tmp_path: Path):
    server = make_memory_search_server(CHANNEL_A, tmp_path / "memory.db")

    assert server["name"] == "engram-memory"
    assert await _list_tools(server) == ["memory_search", "memory_search_semantic"]


@pytest.mark.asyncio
async def test_memory_search_returns_fts_matches(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    for index, word in enumerate(["octopus", "narwhal", "quartz", "saffron", "zenith"]):
        _seed_transcript(
            db_path,
            message_uuid=f"distinct-{word}",
            text=f"the distinct keyword is {word}",
            ts=BASE_TS + timedelta(seconds=index),
        )

    server = make_memory_search_server(CHANNEL_A, db_path)

    for word in ["octopus", "narwhal", "quartz", "saffron", "zenith"]:
        response = await _call_memory_search(server, {"query": word})
        rows = _rows_from_response(response)
        assert len(rows) == 1
        assert rows[0]["message_uuid"] == f"distinct-{word}"
        assert rows[0]["kind"] == "transcript"
        assert rows[0]["ts_iso"]


@pytest.mark.asyncio
async def test_scope_this_channel_filters_by_caller_channel(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    _seed_transcript(
        db_path,
        channel_id=CHANNEL_A,
        message_uuid="a-shared",
        text="sharedtoken from channel a",
    )
    _seed_transcript(
        db_path,
        channel_id=CHANNEL_B,
        message_uuid="b-shared",
        text="sharedtoken from channel b",
        ts=BASE_TS + timedelta(seconds=1),
    )
    server = make_memory_search_server(CHANNEL_A, db_path)

    response = await _call_memory_search(server, {"query": "sharedtoken"})

    rows = _rows_from_response(response)
    assert {row["channel_id"] for row in rows} == {CHANNEL_A}
    assert [row["message_uuid"] for row in rows] == ["a-shared"]


@pytest.mark.asyncio
async def test_scope_all_channels_aggregates(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    _seed_transcript(
        db_path,
        channel_id=CHANNEL_A,
        message_uuid="a-all",
        text="globaltoken from channel a",
    )
    _seed_transcript(
        db_path,
        channel_id=CHANNEL_B,
        message_uuid="b-all",
        text="globaltoken from channel b",
        ts=BASE_TS + timedelta(seconds=1),
    )
    server = make_memory_search_server(CHANNEL_A, db_path)

    response = await _call_memory_search(
        server,
        {"query": "globaltoken", "scope": "all_channels"},
    )

    rows = _rows_from_response(response)
    assert {row["channel_id"] for row in rows} == {CHANNEL_A, CHANNEL_B}


@pytest.mark.asyncio
async def test_kind_filter_transcripts_only(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    _seed_transcript(db_path, message_uuid="transcript-mixed", text="mixedtoken transcript")
    _seed_summary(db_path, summary_text="mixedtoken summary")
    server = make_memory_search_server(CHANNEL_A, db_path)

    response = await _call_memory_search(
        server,
        {"query": "mixedtoken", "kind": "transcripts"},
    )

    rows = _rows_from_response(response)
    assert len(rows) == 1
    assert rows[0]["kind"] == "transcript"
    assert rows[0]["message_uuid"] == "transcript-mixed"
    assert rows[0]["summary_id"] is None


@pytest.mark.asyncio
async def test_kind_filter_summaries_only(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    _seed_transcript(db_path, message_uuid="transcript-mixed", text="summaryonly transcript")
    summary_id = _seed_summary(db_path, summary_text="summaryonly summary")
    server = make_memory_search_server(CHANNEL_A, db_path)

    response = await _call_memory_search(
        server,
        {"query": "summaryonly", "kind": "summaries"},
    )

    rows = _rows_from_response(response)
    assert len(rows) == 1
    assert rows[0]["kind"] == "summary"
    assert rows[0]["message_uuid"] is None
    assert rows[0]["summary_id"] == summary_id


@pytest.mark.asyncio
async def test_limit_respected(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    for index in range(10):
        _seed_transcript(
            db_path,
            message_uuid=f"limit-{index}",
            text=f"limittoken row {index}",
            ts=BASE_TS + timedelta(seconds=index),
        )
    server = make_memory_search_server(CHANNEL_A, db_path)

    response = await _call_memory_search(server, {"query": "limittoken", "limit": 3})

    assert len(_rows_from_response(response)) == 3


@pytest.mark.asyncio
async def test_limit_clamped_to_max_20(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    for index in range(25):
        _seed_transcript(
            db_path,
            message_uuid=f"clamp-{index}",
            text=f"clamptoken row {index}",
            ts=BASE_TS + timedelta(seconds=index),
        )
    server = make_memory_search_server(CHANNEL_A, db_path)

    response = await _call_memory_search(server, {"query": "clamptoken", "limit": 999})

    assert len(_rows_from_response(response)) <= 20


@pytest.mark.asyncio
async def test_empty_query_returns_empty_list(tmp_path: Path):
    server = make_memory_search_server(CHANNEL_A, tmp_path / "memory.db")

    response = await _call_memory_search(server, {"query": ""})

    assert response == {"content": [{"type": "text", "text": "[]"}]}


@pytest.mark.asyncio
async def test_snippet_includes_query_term(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    _seed_transcript(db_path, message_uuid="snippet", text="the hidden needle is here")
    server = make_memory_search_server(CHANNEL_A, db_path)

    response = await _call_memory_search(server, {"query": "needle"})

    rows = _rows_from_response(response)
    assert "needle" in rows[0]["snippet"].lower()


@pytest.mark.asyncio
async def test_db_error_returns_is_error_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def boom(_path: Path | None = None):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(mcp_tools, "open_memory_db", boom)
    server = make_memory_search_server(CHANNEL_A, tmp_path / "memory.db")

    response = await _call_memory_search(server, {"query": "needle"})

    assert response["isError"] is True
    assert response["content"] == [{"type": "text", "text": "[]"}]


@pytest.mark.asyncio
async def test_invocation_logs_telemetry(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    db_path = tmp_path / "memory.db"
    _seed_transcript(db_path, message_uuid="telemetry", text="telemetrytoken row")
    server = make_memory_search_server(CHANNEL_A, db_path)

    with caplog.at_level(logging.INFO, logger="engram.telemetry"):
        await _call_memory_search(server, {"query": "telemetrytoken", "limit": 7})

    records = [r for r in caplog.records if r.getMessage() == "memory_search.invoked"]
    assert len(records) == 1
    record = records[0]
    assert record.channel_id == CHANNEL_A
    assert record.query_len == len("telemetrytoken")
    assert record.scope == "this_channel"
    assert record.kind == "both"
    assert record.limit == 7
    assert record.result_count == 1
    assert isinstance(record.latency_ms, int)


@pytest.mark.asyncio
async def test_tool_output_is_valid_json(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    _seed_transcript(db_path, message_uuid="json", text="jsontoken row")
    server = make_memory_search_server(CHANNEL_A, db_path)

    response = await _call_memory_search(server, {"query": "jsontoken"})

    assert isinstance(json.loads(response["content"][0]["text"]), list)


def test_manifest_allows_memory_by_default(tmp_path: Path):
    result = provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path,
    )

    manifest = load_manifest(result.manifest_path)

    assert "engram-memory" in (manifest.mcp_servers.allowed or [])


def test_status_json_reports_memory_search_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from engram.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "ENGRAM_SLACK_BOT_TOKEN",
        "ENGRAM_SLACK_APP_TOKEN",
        "ENGRAM_ANTHROPIC_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("engram.cli._bridge_pid", lambda: None)
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=paths.engram_home(),
    )

    result = CliRunner().invoke(app, ["status", "--json"])

    payload = json.loads(result.output)
    channel = next(c for c in payload["channels"] if c["channel_id"] == "C07TEAM")
    assert channel["tools"]["registered"] == MEMORY_SEARCH_FULL_TOOL_NAMES
