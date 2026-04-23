from __future__ import annotations

import json
import plistlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from engram.budget import Budget, BudgetConfig
from engram.smoketest import (
    AnthropicRuntime,
    CliResolution,
    SMOKE_CHANNEL_ID,
    SMOKE_PROMPT,
    run_smoke,
)


class _FakeClient:
    options_seen: ClaudeAgentOptions | None = None
    prompt_seen: str | None = None
    session_seen: str | None = None
    disconnected = False

    def __init__(self, options: ClaudeAgentOptions):
        type(self).options_seen = options

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str, *, session_id: str | None = None) -> None:
        type(self).prompt_seen = prompt
        type(self).session_seen = session_id

    async def receive_response(self):
        yield AssistantMessage(
            content=[TextBlock("smoke-test-ok")],
            model="claude-test-model",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=type(self).session_seen or "session-test",
            total_cost_usd=0.012345,
            usage={
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 0,
            },
            model_usage={"claude-test-model": {"input_tokens": 10}},
        )

    async def disconnect(self) -> None:
        type(self).disconnected = True


@pytest.mark.asyncio
async def test_run_smoke_records_budget_and_structured_success_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    smoke_cwd = tmp_path / ".engram" / "contexts" / "owner-dm"
    (smoke_cwd / ".claude").mkdir(parents=True)
    log_path = tmp_path / ".engram" / "logs" / "smoketest-test.jsonl"
    budget = Budget(BudgetConfig(), db_path=tmp_path / ".engram" / "cost.db")

    _FakeClient.options_seen = None
    _FakeClient.prompt_seen = None
    _FakeClient.session_seen = None
    _FakeClient.disconnected = False

    code = await run_smoke(
        cwd=smoke_cwd,
        log_path=log_path,
        client_factory=_FakeClient,
        budget=budget,
        cli_resolver=lambda _path: CliResolution(
            resolved=True,
            cli_path="/tmp/claude",
            source="path",
            path_cli="/tmp/claude",
        ),
        anthropic_loader=lambda: AnthropicRuntime(
            api_key="sk-test",
            model="claude-test-model",
        ),
    )

    assert code == 0
    assert _FakeClient.prompt_seen == SMOKE_PROMPT
    assert _FakeClient.disconnected is True
    assert _FakeClient.options_seen is not None
    assert _FakeClient.options_seen.setting_sources == ["project"]
    assert _FakeClient.options_seen.cwd == smoke_cwd
    assert _FakeClient.options_seen.can_use_tool is None
    assert _FakeClient.options_seen.hooks == {}
    assert _FakeClient.options_seen.env == {"ANTHROPIC_API_KEY": "sk-test"}

    with sqlite3.connect(budget.db_path) as conn:
        row = conn.execute(
            "SELECT channel_id, cost_usd, cache_creation_input_tokens "
            "FROM turns"
        ).fetchone()
    assert row == (SMOKE_CHANNEL_ID, "0.012345", 7)

    events = _read_jsonl(log_path)
    success = _event(events, "smoketest.success")
    assert success["hitl_disabled"] is True
    assert success["cli_resolved"] is True
    assert success["project_found"] is True
    assert success["budget_recorded"] is True
    assert success["budget_channel_id"] == SMOKE_CHANNEL_ID
    assert success["prompt_cache_status"] == "created"
    assert success["write_edit_hitl_guard_fired"] is False
    assert success["hitl_guard_invocations"] == 0


@pytest.mark.asyncio
async def test_run_smoke_fails_before_sdk_when_project_context_missing(tmp_path):
    log_path = tmp_path / "smoketest.jsonl"

    code = await run_smoke(
        cwd=tmp_path / ".engram" / "contexts" / "owner-dm",
        log_path=log_path,
        client_factory=_FakeClient,
        budget=Budget(BudgetConfig(), db_path=tmp_path / "cost.db"),
        cli_resolver=lambda _path: CliResolution(
            resolved=True,
            cli_path="/tmp/claude",
            source="path",
            path_cli="/tmp/claude",
        ),
        anthropic_loader=lambda: AnthropicRuntime(api_key=None, model=None),
    )

    assert code == 1
    failure = _event(_read_jsonl(log_path), "smoketest.failure")
    assert failure["reason"] == "project_not_found"
    assert failure["project_found"] is False


def test_launchd_smoketest_plist_is_manual_one_shot_and_copies_bridge_env():
    bridge = _plist(Path("launchd/com.engram.bridge.plist"))
    smoke = _plist(Path("launchd/com.engram.v3.smoketest.plist"))

    assert smoke["Label"] == "com.engram.v3.smoketest"
    assert smoke["RunAtLoad"] is False
    assert "StartInterval" not in smoke
    assert "StartCalendarInterval" not in smoke
    assert "KeepAlive" not in smoke
    assert smoke["ProgramArguments"][-2:] == ["-m", "engram.smoketest"]

    bridge_env = bridge["EnvironmentVariables"]
    smoke_env = smoke["EnvironmentVariables"]
    assert smoke_env["PATH"] == bridge_env["PATH"]
    assert smoke_env["LANG"] == bridge_env["LANG"]
    assert smoke_env["HOME"] == "/REPLACE/WITH/HOME"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _event(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for event in events:
        if event["event"] == name:
            return event
    raise AssertionError(f"missing event: {name}")


def _plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return plistlib.load(fh)
