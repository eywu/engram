"""Tests that Agent._build_options produces correct SDK options.

No network — we don't call query(). We just inspect the ClaudeAgentOptions
the agent would construct for various sessions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultDeny
from claude_agent_sdk.types import ToolPermissionContext

from engram.agent import Agent
from engram.config import AnthropicConfig, EngramConfig, HITLConfig, SlackConfig
from engram.manifest import (
    OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES,
    Behavior,
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    MemoryScope,
    PermissionTier,
    PermissionsRules,
    ScopeList,
)
from engram.mcp import resolve_team_mcp_servers
from engram.mcp_tools import MEMORY_SEARCH_FULL_TOOL_NAMES
from engram.router import Router, SessionState


def _cfg() -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-test"),
        max_turns_per_message=8,
    )


def _session(manifest: ChannelManifest | None, cwd: Path | None = None) -> SessionState:
    return SessionState(
        channel_id="C1",
        is_dm=False,
        cwd=cwd,
        manifest=manifest,
    )


def _write_mcp_config(tmp_path: Path, servers: dict) -> None:
    mcp_dir = tmp_path / ".claude"
    mcp_dir.mkdir()
    (mcp_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": servers}),
        encoding="utf-8",
    )


# ── Legacy mode (no manifest) ──────────────────────────────────────────


def test_legacy_mode_uses_user_setting_source():
    a = Agent(_cfg())
    opts = a._build_options(_session(None))
    assert opts.setting_sources == ["user"]
    assert opts.max_turns == 8
    assert opts.allowed_tools == []
    assert opts.disallowed_tools == []
    assert opts.can_use_tool is None


@pytest.mark.asyncio
async def test_hitl_tool_guard_uses_router_hitl_timeout():
    router = Router(hitl=HITLConfig(timeout_s=0))
    session = _session(None)
    questions = []
    a = Agent(_cfg(), router=router)

    async def on_new_question(q):
        questions.append(q)

    a._on_new_question = on_new_question
    opts = a._build_options(session)
    assert "PermissionRequest" not in opts.hooks
    assert opts.can_use_tool is not None

    result = await opts.can_use_tool(
        "Bash",
        {"cmd": "pytest"},
        ToolPermissionContext(tool_use_id="tool-1"),
    )

    assert questions[0].timeout_s == 0
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "question timed out after 0s"
    assert result.interrupt is True


def test_hitl_disabled_skips_permission_request_hook():
    router = Router(hitl=HITLConfig(enabled=False))
    a = Agent(_cfg(), router=router)

    opts = a._build_options(_session(None))

    assert "PermissionRequest" not in opts.hooks


# ── Owner-DM manifest ──────────────────────────────────────────────────


def test_owner_dm_manifest_full_inheritance():
    m = ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        status=ChannelStatus.ACTIVE,
        setting_sources=["user"],
        permissions=PermissionsRules(
            allow=list(OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES)
        ),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    expected_tools = set(OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES) | set(
        MEMORY_SEARCH_FULL_TOOL_NAMES
    )
    assert opts.setting_sources == ["user"]
    assert opts.disallowed_tools == []
    assert set(opts.allowed_tools) == expected_tools
    assert len(opts.allowed_tools) == len(expected_tools)
    assert set(opts.mcp_servers) == {"engram-memory"}
    assert getattr(opts, "strict_mcp_config", False) is False
    assert "strict-mcp-config" not in opts.extra_args
    # Runtime guard is always wired when a manifest is present — even
    # for full-inheritance. It's a no-op in that case.
    assert opts.can_use_tool is not None


# ── Team-channel manifest ──────────────────────────────────────────────


def test_team_channel_manifest_excludes_tools():
    m = ChannelManifest(
        channel_id="C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
        setting_sources=["project"],
        tools=ScopeList(disallowed=["Bash", "Write", "Edit"]),
        behavior=Behavior(max_turns=6),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert opts.setting_sources == ["project"]
    assert opts.disallowed_tools == ["Bash", "Write", "Edit"]
    assert opts.max_turns == 6
    assert opts.can_use_tool is not None
    assert getattr(opts, "strict_mcp_config", False) is True
    assert opts.extra_args["strict-mcp-config"] is None


def test_team_channel_escape_hatch_allow_list():
    m = ChannelManifest(
        channel_id="C07LOCKED",
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
        tools=ScopeList(allowed=["Read", "Grep"]),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert opts.allowed_tools == ["Read", "Grep"]
    assert opts.disallowed_tools == []


def test_team_channel_gets_strict_mcp_flag():
    m = ChannelManifest(
        channel_id="C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert getattr(opts, "strict_mcp_config", False) is True
    assert opts.extra_args["strict-mcp-config"] is None
    assert opts.mcp_servers == {}
    assert json.loads(opts.extra_args["mcp-config"]) == {"mcpServers": {}}


def test_team_channel_mcp_servers_matches_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_mcp_config(
        tmp_path,
        {
            "linear": {"type": "http", "url": "https://linear.example/mcp"},
            "slack-internal": {"command": "slack-mcp"},
            "figma": {"type": "http", "url": "https://figma.example/mcp"},
        },
    )
    m = ChannelManifest(
        channel_id="C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
        mcp_servers=ScopeList(allowed=["linear", "slack-internal"]),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert set(opts.mcp_servers) == {"linear", "slack-internal"}
    assert "mcp-config" not in opts.extra_args


def test_owner_dm_does_not_use_strict_mode():
    m = ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        status=ChannelStatus.ACTIVE,
        setting_sources=["user"],
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert getattr(opts, "strict_mcp_config", False) is False
    assert "strict-mcp-config" not in opts.extra_args


def test_owner_dm_memory_exclusions_flow_to_mcp_server(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    def fake_make_memory_search_server(
        caller_channel_id: str,
        memory_db_path: Path | None = None,
        embedder: object | None = None,
        *,
        excluded_channels: list[str] | None = None,
    ) -> dict[str, object]:
        captured["caller_channel_id"] = caller_channel_id
        captured["memory_db_path"] = memory_db_path
        captured["embedder"] = embedder
        captured["excluded_channels"] = excluded_channels
        return {"name": "engram-memory"}

    monkeypatch.setattr(
        "engram.agent.make_memory_search_server",
        fake_make_memory_search_server,
    )
    manifest = ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        status=ChannelStatus.ACTIVE,
        setting_sources=["user"],
        memory=MemoryScope(excluded_channels=["C07DENIED"]),
    )
    a = Agent(_cfg())

    opts = a._build_options(_session(manifest))

    assert set(opts.mcp_servers) == {"engram-memory"}
    assert captured["caller_channel_id"] == "C1"
    assert captured["excluded_channels"] == ["C07DENIED"]


def test_team_channel_memory_mcp_server_matches_manifest():
    m = ChannelManifest(
        channel_id="C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
        mcp_servers=ScopeList(allowed=["engram-memory"]),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert set(opts.mcp_servers) == {"engram-memory"}
    assert opts.allowed_tools == MEMORY_SEARCH_FULL_TOOL_NAMES
    assert "mcp-config" not in opts.extra_args


def test_team_channel_memory_exclusions_flow_to_mcp_server(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    def fake_make_memory_search_server(
        caller_channel_id: str,
        memory_db_path: Path | None = None,
        embedder: object | None = None,
        *,
        excluded_channels: list[str] | None = None,
    ) -> dict[str, object]:
        captured["caller_channel_id"] = caller_channel_id
        captured["memory_db_path"] = memory_db_path
        captured["embedder"] = embedder
        captured["excluded_channels"] = excluded_channels
        return {"name": "engram-memory"}

    monkeypatch.setattr(
        "engram.mcp.make_memory_search_server",
        fake_make_memory_search_server,
    )
    manifest = ChannelManifest(
        channel_id="C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
        mcp_servers=ScopeList(allowed=["engram-memory"]),
        memory=MemoryScope(excluded_channels=["C07DENIED"]),
    )

    servers, allowed, missing = resolve_team_mcp_servers(
        manifest,
        configured_servers={},
    )

    assert servers == {"engram-memory": {"name": "engram-memory"}}
    assert allowed == ["engram-memory"]
    assert missing == []
    assert captured["caller_channel_id"] == "C07TEAM"
    assert captured["excluded_channels"] == ["C07DENIED"]


# ── Behavior overrides ─────────────────────────────────────────────────


def test_manifest_max_turns_overrides_config():
    m = ChannelManifest(
        channel_id="C1",
        identity=IdentityTemplate.TASK_ASSISTANT,
        behavior=Behavior(max_turns=3),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert opts.max_turns == 3


def test_missing_manifest_max_turns_falls_back_to_config():
    m = ChannelManifest(
        channel_id="C1", identity=IdentityTemplate.TASK_ASSISTANT
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert opts.max_turns == 8  # from _cfg()


def test_cwd_threaded_through():
    m = ChannelManifest(
        channel_id="C1", identity=IdentityTemplate.TASK_ASSISTANT
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m, cwd=Path("/tmp/engram-project")))
    assert opts.cwd == "/tmp/engram-project"


def test_permission_mode_plumbed():
    m = ChannelManifest(
        channel_id="C1",
        identity=IdentityTemplate.TASK_ASSISTANT,
        behavior=Behavior(permission_mode="plan"),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert opts.permission_mode == "plan"


def test_yolo_tier_uses_bypass_permissions():
    m = ChannelManifest(
        channel_id="C1",
        identity=IdentityTemplate.TASK_ASSISTANT,
        permission_tier=PermissionTier.YOLO,
        behavior=Behavior(permission_mode="plan"),
    )
    a = Agent(_cfg())

    opts = a._build_options(_session(m))

    assert opts.permission_mode == "bypassPermissions"


# ── Runtime guard behavior via a full dispatch ─────────────────────────


@pytest.mark.asyncio
async def test_runtime_guard_integrates_with_agent():
    """End-to-end: pull can_use_tool off the options and call it."""
    from unittest.mock import Mock

    from claude_agent_sdk import PermissionResultDeny

    m = ChannelManifest(
        channel_id="C1",
        identity=IdentityTemplate.TASK_ASSISTANT,
        tools=ScopeList(disallowed=["Bash"]),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert opts.can_use_tool is not None
    r = await opts.can_use_tool("Bash", {"cmd": "ls"}, Mock())
    assert isinstance(r, PermissionResultDeny)
