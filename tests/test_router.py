"""Router tests — pure in-memory and manifest-driven modes."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engram import paths
from engram.manifest import ChannelStatus, IdentityTemplate, dump_manifest
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
async def test_legacy_mode_sessions_are_always_active():
    """No home = no manifest. is_active() must default True so old behavior works."""
    r = Router()
    s = await r.get("C1")
    assert s.manifest is None
    assert s.is_active()
