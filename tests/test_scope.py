"""Tests for manifest → SDK scope translation + runtime tool guard."""
from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from engram.manifest import ChannelManifest, IdentityTemplate, ScopeList
from engram.scope import build_scope_decision, build_tool_guard

# ── Helpers ─────────────────────────────────────────────────────────────


def _manifest(
    *,
    tools: ScopeList | None = None,
    mcp: ScopeList | None = None,
    skills: ScopeList | None = None,
) -> ChannelManifest:
    return ChannelManifest(
        channel_id="C1",
        identity=IdentityTemplate.TASK_ASSISTANT,
        tools=tools or ScopeList(),
        mcp_servers=mcp or ScopeList(),
        skills=skills or ScopeList(),
    )


async def _ask(guard, tool_name: str, input_: dict[str, Any] | None = None):
    ctx = Mock()  # ToolPermissionContext — contents unused by our guard
    return await guard(tool_name, input_ or {}, ctx)


# ── Static scope decision ──────────────────────────────────────────────


def test_decision_full_inheritance():
    m = _manifest()  # all ScopeList()s unrestricted
    d = build_scope_decision(m)
    assert d.allowed_tools == []
    assert d.disallowed_tools == []
    assert d.skills == "all"
    assert d.mcp_allowed is None
    assert d.mcp_disallowed == []


def test_decision_team_channel_exclusions():
    m = _manifest(
        tools=ScopeList(disallowed=["Bash", "Write", "Edit"]),
        mcp=ScopeList(disallowed=["personal-notes"]),
    )
    d = build_scope_decision(m)
    assert d.disallowed_tools == ["Bash", "Write", "Edit"]
    assert d.mcp_disallowed == ["personal-notes"]
    assert d.skills == "all"


def test_decision_escape_hatch_allow_list():
    m = _manifest(tools=ScopeList(allowed=["Read", "Grep"]))
    d = build_scope_decision(m)
    assert d.allowed_tools == ["Read", "Grep"]
    assert d.disallowed_tools == []


def test_decision_skills_allow_list():
    m = _manifest(skills=ScopeList(allowed=["search-the-web"]))
    d = build_scope_decision(m)
    assert d.skills == ["search-the-web"]


# ── Runtime guard: tools ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_allows_by_default():
    guard = build_tool_guard(_manifest())
    r = await _ask(guard, "Read", {"path": "/x"})
    assert isinstance(r, PermissionResultAllow)


@pytest.mark.asyncio
async def test_guard_denies_disallowed_tool():
    guard = build_tool_guard(
        _manifest(tools=ScopeList(disallowed=["Bash"]))
    )
    r = await _ask(guard, "Bash", {"command": "ls"})
    assert isinstance(r, PermissionResultDeny)
    assert "Bash" in r.message
    assert "disallowed" in r.message


@pytest.mark.asyncio
async def test_guard_denies_tool_not_in_allowed_list():
    guard = build_tool_guard(
        _manifest(tools=ScopeList(allowed=["Read", "Grep"]))
    )
    r = await _ask(guard, "Write", {"path": "/x"})
    assert isinstance(r, PermissionResultDeny)
    assert "not in channel manifest's allowed list" in r.message


@pytest.mark.asyncio
async def test_guard_allows_tool_in_allowed_list():
    guard = build_tool_guard(
        _manifest(tools=ScopeList(allowed=["Read", "Grep"]))
    )
    r = await _ask(guard, "Read", {"path": "/x"})
    assert isinstance(r, PermissionResultAllow)


@pytest.mark.asyncio
async def test_guard_allowed_plus_disallowed_combines():
    """allowed defines universe; disallowed further filters."""
    guard = build_tool_guard(
        _manifest(
            tools=ScopeList(
                allowed=["Read", "Grep", "Write"], disallowed=["Write"]
            )
        )
    )
    # In allowed list but also in disallowed → denied
    r = await _ask(guard, "Write", {})
    assert isinstance(r, PermissionResultDeny)
    # Not in allowed at all → denied
    r = await _ask(guard, "Bash", {})
    assert isinstance(r, PermissionResultDeny)
    # In allowed, not in disallowed → allowed
    r = await _ask(guard, "Read", {})
    assert isinstance(r, PermissionResultAllow)


# ── Runtime guard: MCPs ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_allows_mcp_by_default():
    guard = build_tool_guard(_manifest())
    r = await _ask(guard, "mcp__linear__create_issue", {})
    assert isinstance(r, PermissionResultAllow)


@pytest.mark.asyncio
async def test_guard_denies_disallowed_mcp_server():
    guard = build_tool_guard(
        _manifest(mcp=ScopeList(disallowed=["linear"]))
    )
    r = await _ask(guard, "mcp__linear__create_issue", {})
    assert isinstance(r, PermissionResultDeny)
    assert "linear" in r.message
    # Also covers when the operator also disallows the underlying tool
    # but the mcp check catches it first.


@pytest.mark.asyncio
async def test_guard_denies_mcp_not_in_allowed_list():
    guard = build_tool_guard(
        _manifest(mcp=ScopeList(allowed=["linear"]))
    )
    r = await _ask(guard, "mcp__gmail__send_email", {})
    assert isinstance(r, PermissionResultDeny)


@pytest.mark.asyncio
async def test_guard_allows_memory_search_unless_explicitly_denied():
    guard = build_tool_guard(_manifest(mcp=ScopeList(allowed=["linear"])))
    r = await _ask(guard, "mcp__engram__memory_search", {})
    assert isinstance(r, PermissionResultAllow)

    guard = build_tool_guard(_manifest(mcp=ScopeList(disallowed=["engram"])))
    r = await _ask(guard, "mcp__engram__memory_search", {})
    assert isinstance(r, PermissionResultDeny)


@pytest.mark.asyncio
async def test_guard_allows_mcp_in_allowed_list():
    guard = build_tool_guard(
        _manifest(mcp=ScopeList(allowed=["linear"]))
    )
    r = await _ask(guard, "mcp__linear__list_issues", {})
    assert isinstance(r, PermissionResultAllow)


@pytest.mark.asyncio
async def test_guard_tool_list_does_not_affect_mcp_names():
    """Adding 'Bash' to disallowed shouldn't accidentally block an MCP
    named Bash; MCP names are namespaced under mcp__."""
    guard = build_tool_guard(
        _manifest(tools=ScopeList(disallowed=["Bash"]))
    )
    r = await _ask(guard, "mcp__something__bash_like_thing", {})
    assert isinstance(r, PermissionResultAllow)


@pytest.mark.asyncio
async def test_guard_denies_malformed_mcp_name_gracefully():
    """A bare 'mcp__' with no server name shouldn't crash — allow-by-default."""
    guard = build_tool_guard(_manifest())
    r = await _ask(guard, "mcp__", {})
    assert isinstance(r, PermissionResultAllow)
