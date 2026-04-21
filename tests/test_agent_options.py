"""Tests that Agent._build_options produces correct SDK options.

No network — we don't call query(). We just inspect the ClaudeAgentOptions
the agent would construct for various sessions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from engram.agent import Agent
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.manifest import (
    Behavior,
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    ScopeList,
)
from engram.router import SessionState
from engram.tools import ENGRAM_MCP_SERVER_NAME, MEMORY_SEARCH_CANONICAL_NAME


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


# ── Legacy mode (no manifest) ──────────────────────────────────────────


def test_legacy_mode_uses_user_setting_source():
    a = Agent(_cfg())
    opts = a._build_options(_session(None))
    assert opts.setting_sources == ["user"]
    assert opts.max_turns == 8
    assert opts.allowed_tools == [MEMORY_SEARCH_CANONICAL_NAME]
    assert opts.disallowed_tools == []
    assert ENGRAM_MCP_SERVER_NAME in opts.mcp_servers
    assert opts.can_use_tool is None


# ── Owner-DM manifest ──────────────────────────────────────────────────


def test_owner_dm_manifest_full_inheritance():
    m = ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        status=ChannelStatus.ACTIVE,
        setting_sources=["user"],
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert opts.setting_sources == ["user"]
    assert opts.disallowed_tools == []
    assert opts.allowed_tools == [MEMORY_SEARCH_CANONICAL_NAME]
    assert ENGRAM_MCP_SERVER_NAME in opts.mcp_servers
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


def test_team_channel_escape_hatch_allow_list():
    m = ChannelManifest(
        channel_id="C07LOCKED",
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
        tools=ScopeList(allowed=["Read", "Grep"]),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert opts.allowed_tools == ["Read", "Grep", MEMORY_SEARCH_CANONICAL_NAME]
    assert opts.disallowed_tools == []


def test_memory_search_can_be_denied_by_mcp_manifest():
    m = ChannelManifest(
        channel_id="C07LOCKED",
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
        mcp_servers=ScopeList(disallowed=[ENGRAM_MCP_SERVER_NAME]),
    )
    a = Agent(_cfg())
    opts = a._build_options(_session(m))
    assert MEMORY_SEARCH_CANONICAL_NAME not in opts.allowed_tools
    assert ENGRAM_MCP_SERVER_NAME not in opts.mcp_servers


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
