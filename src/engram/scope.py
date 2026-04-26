"""Translate a ChannelManifest into concrete Claude SDK enforcement.

Builds three artifacts from a manifest:

1. A `ToolGuard` — a `can_use_tool` callback that runs on every tool call
   from the Claude runtime and consults the manifest's scope lists. This
   is the authoritative enforcement point for tools AND MCP-prefixed tool
   names (e.g. `mcp__linear__create_issue`) since MCP calls flow through
   the same gate.

2. A `ScopeDecision` — the fields to pass into `ClaudeAgentOptions`:
   `allowed_tools`, `disallowed_tools`, `skills`, and filtered
   `mcp_servers`. These give the SDK enough information to avoid
   advertising tools/skills/MCPs that would be denied anyway, keeping
   priming cost down.

Resolution rule (agrees with the schema docs):
- If `scope.allowed` is None → inherit all; apply only `disallowed`.
- If `scope.allowed` is a list → only those entries are exposed; if
  `disallowed` is also set, further filter within the allowed list.

The `can_use_tool` callback is redundant with the static lists for simple
tool names, but remains the final safety net for:
- MCP tool names that the SDK discovers at runtime (we can't always
  enumerate them in advance)
- Bugs or mismatches in the static-list filtering
- Future dynamic policies we may want to layer in (e.g. budget gates)
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import ToolPermissionContext

from engram.manifest import ChannelManifest, ScopeList

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Scope decision (static fields for ClaudeAgentOptions)
# ─────────────────────────────────────────────────────────────────


@dataclass
class ScopeDecision:
    """What the SDK will be told about scope up front.

    Mirrors the relevant `ClaudeAgentOptions` fields. Any of these may be
    `None` meaning "don't override the SDK default" — the `can_use_tool`
    callback still enforces at call time.
    """

    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    skills: list[str] | str | None = None  # list, "all", or None
    # mcp_servers is a *filter instruction*, not a config — the agent.py
    # layer applies it against the user inventory from ~/.claude.json.
    mcp_allowed: list[str] | None = None
    mcp_disallowed: list[str] = field(default_factory=list)


def build_scope_decision(manifest: ChannelManifest) -> ScopeDecision:
    """Translate a manifest into the static SDK-facing scope."""
    d = ScopeDecision()

    # Tools
    t = manifest.tools
    if t.allowed is not None:
        d.allowed_tools = list(t.allowed)
    d.disallowed_tools = list(t.disallowed)

    # Permission rules (native Claude Code `Tool(specifier)` syntax).
    # These merge with the tool names above and flow through to the
    # CLI's --allowedTools / --disallowedTools flags, which natively
    # handle globbing + path matching. Deny wins per Claude Code docs.
    p = manifest.permissions
    if p.deny:
        d.disallowed_tools.extend(p.deny)
    if p.allow:
        d.allowed_tools.extend(p.allow)

    # Skills
    s = manifest.skills
    if s.allowed is not None:
        # Explicit allow-list replaces inheritance.
        d.skills = list(s.allowed)
    elif s.disallowed:
        # Inherit "all" but filter via can_use_tool. The SDK doesn't
        # accept a "disallow-list" for skills, so we pass "all" here and
        # let the agent (via a PreToolUse hook or naming convention)
        # enforce. M2 scope is names-only; skill filtering at invocation
        # time is a future tightening.
        d.skills = "all"
    else:
        # Full inheritance with no restrictions.
        d.skills = "all"

    # MCP servers — the agent layer does the filtering against an
    # inherited map; we just carry the instructions through.
    m = manifest.mcp_servers
    d.mcp_allowed = list(m.allowed) if m.allowed is not None else None
    d.mcp_disallowed = list(m.disallowed)

    return d


# ─────────────────────────────────────────────────────────────────
# Runtime guard (can_use_tool callback)
# ─────────────────────────────────────────────────────────────────


CanUseToolFn = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]


def build_tool_guard(manifest: ChannelManifest) -> CanUseToolFn:
    """Return a `can_use_tool` callback enforcing manifest scope.

    The callback denies requests that:
    - Use a tool in `tools.disallowed`.
    - Use a tool NOT in `tools.allowed` (when `allowed` is set).
    - Call an MCP tool whose server is in `mcp_servers.disallowed`.
    - Call an MCP tool whose server is NOT in `mcp_servers.allowed`
      (when that list is set).

    Names follow Claude Code's conventions:
    - Plain tools: `Read`, `Write`, `Bash`, etc.
    - MCP tools: `mcp__<server>__<tool>`
    - Skills: currently routed through the `Skill` tool family, not
      enforced here in M2. (See note in `build_scope_decision`.)
    """
    tools = manifest.tools
    mcp = manifest.mcp_servers
    channel_id = manifest.channel_id

    async def can_use_tool(
        tool_name: str,
        _input: dict[str, Any],
        _ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        deny = _check_tool(tool_name, tools) or _check_mcp(tool_name, mcp)
        if deny:
            log.info(
                "scope.denied channel=%s tool=%s reason=%s",
                channel_id,
                tool_name,
                deny,
            )
            return PermissionResultDeny(
                message=deny,
                interrupt=False,
            )
        return PermissionResultAllow()

    return can_use_tool


def _check_tool(tool_name: str, tools: ScopeList) -> str | None:
    """Return deny reason or None.

    This checks only plain tool NAMES. Specifier-format rules
    (e.g. `Read(~/.ssh/**)`) belong in `manifest.permissions.deny` and
    are handled by the SDK's CLI — not here.
    """
    # Skip MCP names — they're checked in `_check_mcp`.
    if tool_name.startswith("mcp__"):
        return None
    # Only match bare tool names in `tools.disallowed`. Entries with
    # parens are specifier rules meant for the CLI; ignoring them here
    # is correct — the CLI side enforces path/arg matching.
    bare_disallowed = [t for t in tools.disallowed if "(" not in t]
    if tool_name in bare_disallowed:
        return f"tool '{tool_name}' disallowed by channel manifest"
    if tools.allowed is not None:
        bare_allowed = [t for t in tools.allowed if "(" not in t]
        # If there are ONLY specifier-format allows, treat as "inherit
        # all" from this guard's perspective — CLI will filter.
        if bare_allowed and tool_name not in bare_allowed:
            return (
                f"tool '{tool_name}' not in channel manifest's allowed list"
            )
    return None


def _check_mcp(tool_name: str, mcp: ScopeList) -> str | None:
    """Return deny reason or None for MCP-prefixed tool names.

    Name format: `mcp__<server>__<tool>`. We parse the server name and
    check it against the manifest's mcp_servers scope list.
    """
    if not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__", 2)
    if len(parts) < 2:
        return None
    server = parts[1]
    if server in mcp.disallowed:
        return f"MCP server '{server}' disallowed by channel manifest"
    if mcp.allowed is not None and server not in mcp.allowed:
        return (
            f"MCP server '{server}' not in channel manifest's allowed list"
        )
    return None
