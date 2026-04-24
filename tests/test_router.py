"""Router tests — pure in-memory and manifest-driven modes."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engram import paths
from engram.config import HITLConfig
from engram.manifest import (
    OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES,
    ChannelStatus,
    IdentityTemplate,
    dump_manifest,
    load_manifest,
)
from engram.router import Router


@pytest.mark.asyncio
async def test_creates_session_on_first_get():
    r = Router()
    s = await r.get("C1", is_dm=False)
    assert s.channel_id == "C1"
    assert s.turn_count == 0
    assert not s.is_dm


@pytest.mark.asyncio
async def test_caches_session_across_gets():
    r = Router()
    s1 = await r.get("C1")
    s2 = await r.get("C1")
    assert s1 is s2


@pytest.mark.asyncio
async def test_separates_channels():
    r = Router()
    a = await r.get("C1", is_dm=False)
    b = await r.get("D1", is_dm=True)
    assert a is not b
    assert a.is_dm is False
    assert b.is_dm is True
    assert r.session_count() == 2


@pytest.mark.asyncio
async def test_concurrent_get_no_duplicate():
    r = Router()
    results = await asyncio.gather(*(r.get("C1") for _ in range(20)))
    assert all(s is results[0] for s in results)
    assert r.session_count() == 1


@pytest.mark.asyncio
async def test_session_label():
    r = Router()
    s_named = await r.get("C1", channel_name="growth", is_dm=False)
    assert "growth" in s_named.label()
    s_dm = await r.get("D1", is_dm=True)
    assert "dm" in s_dm.label()


# ── M2: manifest-driven mode ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manifest_mode_auto_provisions_team_channel(tmp_path: Path):
    r = Router(home=tmp_path)
    s = await r.get("C07TEAM", channel_name="#growth", is_dm=False)
    assert s.manifest is not None
    assert s.manifest.channel_id == "C07TEAM"
    assert s.manifest.identity == IdentityTemplate.TASK_ASSISTANT
    assert s.manifest.status == ChannelStatus.PENDING  # needs approval
    assert s.manifest.nightly_included is False
    assert not s.is_active()
    # cwd should be project root so Claude inherits .claude/
    assert s.cwd == paths.project_root(tmp_path)


@pytest.mark.asyncio
async def test_manifest_mode_auto_provisions_owner_dm(tmp_path: Path):
    r = Router(home=tmp_path, owner_dm_channel_id="D07OWNER")
    s = await r.get("D07OWNER", is_dm=True)
    assert s.manifest is not None
    assert s.manifest.identity == IdentityTemplate.OWNER_DM_FULL
    assert s.manifest.status == ChannelStatus.ACTIVE
    assert s.manifest.nightly_included is True
    assert s.is_active()


@pytest.mark.asyncio
async def test_manifest_mode_non_owner_dm_is_task_assistant(tmp_path: Path):
    """Random DMs from non-owners get task-assistant identity, not owner-DM."""
    r = Router(home=tmp_path, owner_dm_channel_id="D07OWNER")
    s = await r.get("D07STRANGER", is_dm=True)
    assert s.manifest.identity == IdentityTemplate.TASK_ASSISTANT


@pytest.mark.asyncio
async def test_manifest_mode_loads_existing_manifest(tmp_path: Path):
    """On second boot, router must use existing manifest, not re-provision."""
    r1 = Router(home=tmp_path)
    s1 = await r1.get("C07TEAM", is_dm=False)
    # Simulate operator approving the channel by editing the manifest
    approved = s1.manifest.model_copy(update={"status": ChannelStatus.ACTIVE})
    dump_manifest(
        approved,
        paths.channel_manifest_path("C07TEAM", tmp_path),
    )

    # Second router instance (simulating a fresh boot)
    r2 = Router(home=tmp_path)
    s2 = await r2.get("C07TEAM", is_dm=False)
    assert s2.manifest.status == ChannelStatus.ACTIVE
    assert s2.is_active()


@pytest.mark.asyncio
async def test_manifest_mode_migrates_existing_owner_dm_empty_allow_list(
    tmp_path: Path,
):
    r1 = Router(home=tmp_path, owner_dm_channel_id="D07OWNER")
    await r1.get("D07OWNER", is_dm=True)
    manifest_path = paths.channel_manifest_path("D07OWNER", tmp_path)
    manifest = load_manifest(manifest_path)
    dump_manifest(
        manifest.model_copy(
            update={
                "permissions": manifest.permissions.model_copy(update={"allow": []})
            }
        ),
        manifest_path,
    )

    r2 = Router(home=tmp_path, owner_dm_channel_id="D07OWNER")
    s2 = await r2.get("D07OWNER", is_dm=True)

    assert s2.manifest.permissions.allow == list(
        OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES
    )
    assert load_manifest(manifest_path).permissions.allow == list(
        OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES
    )


@pytest.mark.asyncio
async def test_legacy_mode_sessions_are_always_active():
    """No home = no manifest. is_active() must default True so old behavior works."""
    r = Router()
    s = await r.get("C1")
    assert s.manifest is None
    assert s.is_active()


@pytest.mark.asyncio
async def test_template_vars_flow_into_claude_md(tmp_path: Path):
    """owner_display_name + slack_workspace_name must reach the rendered CLAUDE.md."""
    r = Router(
        home=tmp_path,
        owner_dm_channel_id="D07OWNER",
        template_vars={
            "owner_display_name": "Alice",
            "slack_workspace_name": "acme-corp",
        },
    )
    await r.get("D07OWNER", is_dm=True)
    body = paths.channel_claude_md_path("D07OWNER", tmp_path).read_text()
    assert "Alice" in body
    assert "acme-corp" in body
    # The workspace default (which is ONLY used in the workspace slot) must
    # not appear when we've supplied a real workspace name.
    assert "this workspace" not in body
    # Raw {{var}} markers must be fully resolved.
    assert "{{owner_display_name}}" not in body
    assert "{{slack_workspace_name}}" not in body


@pytest.mark.asyncio
async def test_template_vars_optional(tmp_path: Path):
    """Omitting template_vars falls back to defaults (no crash)."""
    r = Router(home=tmp_path)
    await r.get("D07ANY", is_dm=True)
    body = paths.channel_claude_md_path("D07ANY", tmp_path).read_text()
    # Generic defaults are used in the slots — workspace slot shows "this workspace".
    assert "this workspace" in body
    assert "{{owner_display_name}}" not in body


def test_hitl_config_sets_limiter_daily_cap():
    r = Router(hitl=HITLConfig(max_per_day=2))
    for _ in range(2):
        r.hitl_limiter.reserve("C07TEST123")

    allowed, reason = r.hitl_limiter.check("C07TEST123")

    assert allowed is False
    assert reason == "daily question budget exhausted (2/day)"


@pytest.mark.asyncio
async def test_manifest_hitl_overrides_router_default(tmp_path: Path):
    r = Router(home=tmp_path, hitl=HITLConfig(max_per_day=9))
    await r.get("C07TEAM", is_dm=False)

    cfg = r.hitl_config_for_channel("C07TEAM")

    assert cfg.max_per_day == 1000
    assert cfg.timeout_s == 300


@pytest.mark.asyncio
async def test_invalidate_drops_cached_session_and_reloads_manifest(tmp_path: Path):
    r = Router(home=tmp_path)
    first = await r.get("C07TEAM", channel_name="#growth", is_dm=False)
    manifest_path = paths.channel_manifest_path("C07TEAM", tmp_path)
    dump_manifest(
        first.manifest.model_copy(update={"status": ChannelStatus.ACTIVE}),
        manifest_path,
    )

    invalidated = await r.invalidate("C07TEAM")
    second = await r.get("C07TEAM", channel_name="#growth", is_dm=False)

    assert invalidated is True
    assert first is not second
    assert second.manifest.status == ChannelStatus.ACTIVE
