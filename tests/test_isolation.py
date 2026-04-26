"""§07 integration tests — prove per-channel isolation holds end-to-end.

Each test walks the full pipeline:
  router.get(channel_id) → provisions/loads manifest
  agent._build_options(session) → produces ClaudeAgentOptions
  options.can_use_tool(...) → denies or allows

We don't call the SDK's `query()` here — that would be a live-wire test.
The contract M2 is responsible for enforcing is: "given a manifest, the
SDK options produced are correctly constrained." That contract is fully
testable at option-build time plus the runtime guard.

These tests are the gate for completing M2, per §07 milestones doc.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from engram.agent import Agent
from engram.bootstrap import ensure_project_root
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.manifest import ChannelStatus, dump_manifest
from engram.paths import channel_manifest_path
from engram.router import Router

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def cfg() -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-test"),
        max_turns_per_message=8,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A clean engram home with project root seeded."""
    ensure_project_root(home=tmp_path)
    return tmp_path


async def _approve_team_channel(home: Path, channel_id: str) -> None:
    """Team channels start PENDING; bump to ACTIVE so the agent runs."""
    from engram.manifest import load_manifest

    p = channel_manifest_path(channel_id, home)
    m = load_manifest(p)
    dump_manifest(
        m.model_copy(update={"status": ChannelStatus.ACTIVE}),
        p,
    )


# ── TEST 1: tool isolation (Bash) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_isolation_bash_denied_in_team_allowed_in_owner_dm(
    cfg: EngramConfig, home: Path
):
    """Owner-DM can Bash. Team channel cannot. §07 test #1."""
    owner_dm_id = "D07OWNER"
    team_id = "C07TEAM"

    router = Router(home=home, owner_dm_channel_id=owner_dm_id)
    agent = Agent(cfg)

    # Resolve both sessions
    dm_session = await router.get(owner_dm_id, is_dm=True)
    team_session = await router.get(
        team_id, channel_name="#growth", is_dm=False
    )
    await _approve_team_channel(home, team_id)
    # Refresh the team session's manifest to pick up the approval
    team_session = await Router(
        home=home, owner_dm_channel_id=owner_dm_id
    ).get(team_id, is_dm=False)

    dm_opts = agent._build_options(dm_session)
    team_opts = agent._build_options(team_session)

    ctx = Mock()

    # Owner-DM: Bash allowed (no manifest-level restriction).
    dm_result = await dm_opts.can_use_tool("Bash", {"cmd": "ls"}, ctx)
    assert isinstance(
        dm_result, PermissionResultAllow
    ), "Owner-DM should allow Bash; manifest is unrestricted"

    # Team channel: Bash in disallowed list by default.
    team_result = await team_opts.can_use_tool("Bash", {"cmd": "ls"}, ctx)
    assert isinstance(
        team_result, PermissionResultDeny
    ), "Team channel default manifest must deny Bash"
    assert "Bash" in team_result.message


# ── TEST 2: MCP isolation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_isolation_mcp_denied_in_team_when_excluded(
    cfg: EngramConfig, home: Path
):
    """A team channel with linear-mcp in disallowed list cannot invoke it;
    owner-DM can (§07 test #4, adapted for the actually-enforceable surface)."""
    from engram.manifest import ScopeList, load_manifest

    owner_dm_id = "D07OWNER"
    team_id = "C07TEAM"

    router = Router(home=home, owner_dm_channel_id=owner_dm_id)
    agent = Agent(cfg)

    # Provision + approve team channel, then tighten its MCP scope
    await router.get(team_id, is_dm=False)
    team_path = channel_manifest_path(team_id, home)
    m = load_manifest(team_path)
    m = m.model_copy(
        update={
            "status": ChannelStatus.ACTIVE,
            "mcp_servers": ScopeList(disallowed=["linear"]),
        }
    )
    dump_manifest(m, team_path)

    # Provision owner-DM
    await router.get(owner_dm_id, is_dm=True)

    # Re-resolve both with fresh router so manifests are loaded from disk
    router2 = Router(home=home, owner_dm_channel_id=owner_dm_id)
    dm = await router2.get(owner_dm_id, is_dm=True)
    team = await router2.get(team_id, is_dm=False)

    dm_opts = agent._build_options(dm)
    team_opts = agent._build_options(team)

    ctx = Mock()

    dm_result = await dm_opts.can_use_tool(
        "mcp__linear__create_issue", {"title": "x"}, ctx
    )
    assert isinstance(dm_result, PermissionResultAllow)

    team_result = await team_opts.can_use_tool(
        "mcp__linear__create_issue", {"title": "x"}, ctx
    )
    assert isinstance(team_result, PermissionResultDeny)
    assert "linear" in team_result.message


# ── TEST 3: memory / cwd isolation (channel-separated memory dir) ──────


@pytest.mark.asyncio
async def test_isolation_memory_directories_are_separate(
    cfg: EngramConfig, home: Path
):
    """Each channel gets its own `.claude/memory/` directory, so any skill
    that writes to $CLAUDE_PROJECT_DIR/memory won't bleed across channels.

    §07 test #3: DM writes 'favorite color: blue'; team channel shouldn't
    see it. The isolation mechanism is the per-channel directory tree, not
    a path-filter policy. This test verifies the directory layout."""
    from engram import paths

    owner_dm_id = "D07OWNER"
    team_id = "C07TEAM"
    router = Router(home=home, owner_dm_channel_id=owner_dm_id)
    await router.get(owner_dm_id, is_dm=True)
    await router.get(team_id, is_dm=False)

    dm_mem = paths.channel_memory_dir(owner_dm_id, home)
    team_mem = paths.channel_memory_dir(team_id, home)

    assert dm_mem.is_dir()
    assert team_mem.is_dir()
    assert dm_mem != team_mem
    # Simulate DM writing a memory
    (dm_mem / "favorite-color.md").write_text("blue\n")

    # Team channel's memory dir must NOT see this file
    team_files = list(team_mem.glob("*"))
    assert all(f.name != "favorite-color.md" for f in team_files)


# ── TEST 4: setting_sources isolation ───────────────────────────────────


@pytest.mark.asyncio
async def test_isolation_setting_sources_differ(
    cfg: EngramConfig, home: Path
):
    """Owner-DM uses user-level settings; team channel uses project-level
    only, so personal MCPs in ~/.claude.json don't leak into team
    channels. §07 test #4, mechanism check."""
    owner_dm_id = "D07OWNER"
    team_id = "C07TEAM"

    router = Router(home=home, owner_dm_channel_id=owner_dm_id)
    agent = Agent(cfg)

    dm = await router.get(owner_dm_id, is_dm=True)
    team = await router.get(team_id, is_dm=False)
    await _approve_team_channel(home, team_id)
    team = await Router(home=home, owner_dm_channel_id=owner_dm_id).get(
        team_id, is_dm=False
    )

    dm_opts = agent._build_options(dm)
    team_opts = agent._build_options(team)

    assert dm_opts.setting_sources == ["user"]
    assert team_opts.setting_sources == ["project"]
    assert dm_opts.setting_sources != team_opts.setting_sources


# ── Meta: pending channels don't run ────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_channel_is_not_active(
    cfg: EngramConfig, home: Path
):
    """Freshly-provisioned team channels are pending; ingress must skip them."""
    router = Router(home=home)
    s = await router.get("C07TEAM", is_dm=False)
    assert s.manifest is not None
    assert s.manifest.status == ChannelStatus.PENDING
    assert not s.is_active()


# ── Path-argument isolation (via native Claude Code permission rules) ──


@pytest.mark.asyncio
async def test_isolation_path_filters_flow_into_sdk_options(
    cfg: EngramConfig, home: Path
):
    """Post-M2 addendum: path filtering IS in scope, enforced by the SDK
    via `Tool(specifier)` rules merged into disallowed_tools. This
    verifies rules arrive verbatim in the options the CLI sees.

    We don't exec the CLI here; the CLI's own test suite covers glob
    semantics. Our contract is: rules round-trip intact.
    """
    owner_dm_id = "D07OWNER"
    team_id = "C07TEAM"
    router = Router(home=home, owner_dm_channel_id=owner_dm_id)
    agent = Agent(cfg)

    dm_session = await router.get(owner_dm_id, is_dm=True)
    team_session = await router.get(team_id, is_dm=False)

    dm_opts = agent._build_options(dm_session)
    team_opts = agent._build_options(team_session)

    # Team channel ships with the aggressive default deny list.
    assert any("Read(~/.ssh/" in t for t in team_opts.disallowed_tools), (
        "team channel should block Read on ~/.ssh/** via native rule"
    )
    assert any("Read(**/.env" in t for t in team_opts.disallowed_tools), (
        "team channel should block Read on .env files"
    )
    # Grep/Glob variants also covered (searching a secret == reading it).
    assert any("Grep(~/.ssh/" in t for t in team_opts.disallowed_tools)

    # Owner-DM ships with a lighter but non-empty deny list.
    assert any("Read(~/.ssh/" in t for t in dm_opts.disallowed_tools)
    assert any("Read(**/.env" in t for t in dm_opts.disallowed_tools)
