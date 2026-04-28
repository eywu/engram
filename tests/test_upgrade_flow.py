"""Tests for GRO-511 button-driven tier changes."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.ingress import (
    ACTION_ID_TIER_PICK,
    handle_block_action,
    handle_engram_command,
    handle_tier_pick_action,
)
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
    ScopeList,
    build_mcp_manifest_change_plan,
    dump_manifest,
    load_manifest,
    persist_approved_mcp_manifest_change,
    set_channel_mcp_server_access,
)
from engram.mcp_trust import MCPTrustDecision, MCPTrustTier
from engram.paths import channel_manifest_path, state_dir
from engram.router import Router


class FakeSlackClient:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.ephemeral_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.chat_postMessage = self._chat_post_message
        self.chat_postEphemeral = self._chat_post_ephemeral

    async def _chat_post_message(self, **kwargs):
        ts = f"1713800000.{len(self.post_calls) + 200:06d}"
        self.post_calls.append({**kwargs, "_ts": ts})
        return {"ok": True, "ts": ts}

    async def _chat_post_ephemeral(self, **kwargs):
        self.ephemeral_calls.append(kwargs)
        return {"ok": True}

    async def chat_update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"ok": True}


def make_config() -> EngramConfig:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )
    cfg.owner_dm_channel_id = "D07OWNER"
    cfg.owner_user_id = "U07OWNER"
    return cfg


def write_active_manifest(
    home: Path,
    channel_id: str,
    *,
    label: str = "#growth",
    tier: PermissionTier = PermissionTier.TASK_ASSISTANT,
    nightly_included: bool | None = None,
) -> None:
    path = channel_manifest_path(channel_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "identity": IdentityTemplate.TASK_ASSISTANT,
        "status": ChannelStatus.ACTIVE,
        "label": label,
        "permission_tier": tier,
        "mcp_servers": (
            ScopeList(allowed=["engram-memory"])
            if channel_id.startswith("C")
            else ScopeList()
        ),
    }
    if nightly_included is not None:
        payload["nightly_included"] = nightly_included
    dump_manifest(ChannelManifest(**payload), path)


def write_mcp_inventory(tmp_path: Path, payload: dict[str, object]) -> None:
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": payload}),
        encoding="utf-8",
    )


def picker_payload(
    *,
    value: str,
    user_id: str,
    channel_id: str = "C07TEAM",
) -> dict[str, Any]:
    return {
        "actions": [
            {
                "action_id": ACTION_ID_TIER_PICK,
                "block_id": f"engram_upgrade_picker:{channel_id}",
                "value": value,
            }
        ],
        "channel": {"id": channel_id},
        "user": {"id": user_id},
    }


def action_texts(blocks: list[dict[str, Any]]) -> list[str]:
    return [str(element["text"]["text"]) for element in blocks[1]["elements"]]


def _block_action_payload(value: str, *, user_id: str = "U07OWNER") -> dict[str, Any]:
    choice = value.split("|", 2)[1]
    return {
        "type": "block_actions",
        "actions": [
            {
                "action_id": f"hitl_choice_{choice}",
                "block_id": "hitl_actions",
                "value": value,
            }
        ],
        "user": {"id": user_id},
    }


def allow_mcp_for_test(home: Path, channel_id: str, server_name: str) -> None:
    manifest_path = channel_manifest_path(channel_id, home)
    manifest = load_manifest(manifest_path)
    updated = manifest.model_copy(
        update={
            "mcp_servers": manifest.mcp_servers.model_copy(
                update={
                    "allowed": list(
                        dict.fromkeys([*(manifest.mcp_servers.allowed or []), server_name])
                    )
                }
            )
        }
    )
    plan = build_mcp_manifest_change_plan(manifest_path, updated)
    assert plan is not None
    persist_approved_mcp_manifest_change(plan)


async def _wait_until(predicate, *, timeout_s: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


@pytest.mark.asyncio
async def test_bare_upgrade_command_shows_owner_picker_with_all_tiers(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OWNER",
        command_text="upgrade",
    )

    assert result == {"ok": True, "picker": True, "tier": "safe", "is_owner": True}
    assert slack.post_calls == []
    assert slack.ephemeral_calls[0]["text"] == "Current tier: safe\nPick a new tier:"
    assert action_texts(slack.ephemeral_calls[0]["blocks"]) == [
        "🔒 Safe (current)",
        "✨ Trusted",
        "🚀 YOLO",
    ]
    assert [element.get("style") for element in slack.ephemeral_calls[0]["blocks"][1]["elements"]] == [
        None,
        "primary",
        "primary",
    ]


@pytest.mark.asyncio
async def test_bare_upgrade_command_shows_non_owner_only_downgrade_enabled(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.OWNER_SCOPED)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OTHER",
        command_text="upgrade",
    )

    assert result == {"ok": True, "picker": True, "tier": "trusted", "is_owner": False}
    assert (
        slack.ephemeral_calls[0]["text"]
        == "Current tier: trusted\nOnly the channel owner can upgrade. You can downgrade:"
    )
    assert action_texts(slack.ephemeral_calls[0]["blocks"]) == [
        "🔒 Safe",
        "✨ Trusted (current)",
        "🚀 YOLO (owner only)",
    ]
    assert [element.get("style") for element in slack.ephemeral_calls[0]["blocks"][1]["elements"]] == [
        "primary",
        None,
        None,
    ]


@pytest.mark.asyncio
async def test_tier_pick_action_reverifies_invoker_identity(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_tier_pick_action(
        payload=picker_payload(
            value="C07TEAM|trusted|U07OWNER",
            user_id="U07OTHER",
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is False
    assert result["error"] == "identity mismatch"
    assert (
        result["response"]["text"]
        == "This picker was opened for a different user. Run `/engram upgrade` yourself."
    )
    assert manifest.permission_tier == PermissionTier.TASK_ASSISTANT
    assert slack.post_calls == []


@pytest.mark.asyncio
async def test_owner_tier_pick_updates_manifest_and_posts_public_notice(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_tier_pick_action(
        payload=picker_payload(
            value="C07TEAM|trusted|U07OWNER",
            user_id="U07OWNER",
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED
    assert result["response"]["text"] == "Tier set to `trusted`. Read-only tools now auto-allow."
    assert slack.post_calls == [
        {
            "channel": "C07TEAM",
            "text": (
                "✨ <@U07OWNER> upgraded this channel to `trusted`. "
                "Read-only tools now auto-allow. Type `/engram` to see current settings."
            ),
            "_ts": slack.post_calls[0]["_ts"],
        }
    ]


@pytest.mark.asyncio
async def test_mcp_list_command_renders_effective_servers(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_mcp_inventory(
        tmp_path,
        {
            "camoufox": {"command": "uvx", "args": ["camoufox-browser[mcp]==0.1.1"]},
        },
    )
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    allow_mcp_for_test(home, "C07TEAM", "camoufox")
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OWNER",
        command_text="mcp list",
    )

    assert result["ok"] is True
    assert "MCP access for #growth (C07TEAM)" in slack.ephemeral_calls[0]["text"]
    assert "Effective: engram-memory, camoufox" in slack.ephemeral_calls[0]["text"]


@pytest.mark.asyncio
async def test_mcp_allow_in_inherit_mode_is_noop_via_slash_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".engram"
    monkeypatch.setenv("HOME", str(tmp_path))
    write_mcp_inventory(
        tmp_path,
        {
            "camoufox": {"command": "uvx", "args": ["camoufox-browser[mcp]==0.1.1"]},
        },
    )
    write_active_manifest(
        home,
        "D07OWNER",
        label="Owner DM",
        tier=PermissionTier.OWNER_SCOPED,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="D07OWNER",
        source_channel_name=None,
        user_id="U07OTHER",
        command_text="mcp allow camoufox",
    )

    manifest = load_manifest(channel_manifest_path("D07OWNER", home))
    assert result["ok"] is True
    assert manifest.mcp_servers.allowed is None
    assert manifest.mcp_servers.disallowed == []
    assert slack.ephemeral_calls == [
        {
            "channel": "D07OWNER",
            "user": "U07OTHER",
            "text": (
                "MCP server `camoufox` already inherits here.\n\n"
                "MCP access for Owner DM (D07OWNER)\n"
                "Tier: trusted\n"
                "Mode: inherit-all\n"
                "Allowed: inherit-all\n"
                "Denied: (none)\n"
                "Effective: camoufox, engram-memory"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_non_owner_cannot_grant_mcp_access_via_slash_command(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OTHER",
        command_text="mcp allow camoufox",
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result == {"ok": False, "error": "not owner"}
    assert manifest.mcp_servers.allowed == ["engram-memory"]
    assert slack.ephemeral_calls == [
        {
            "channel": "C07TEAM",
            "user": "U07OTHER",
            "text": (
                "Only the channel owner can grant MCP access to `camoufox`. "
                "Ask owner to run `/engram mcp allow camoufox`."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_owner_can_add_trusted_publisher_via_slash_command(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="D07OWNER",
        source_channel_name=None,
        user_id="U07OWNER",
        command_text="trust add camoufox-labs",
    )

    assert result == {"ok": True, "action": "trust add", "changed": True}
    overlay = (state_dir(home) / "trusted_publishers.yaml").read_text(encoding="utf-8")
    assert "npm:" in overlay
    assert "pypi:" in overlay
    assert "camoufox-labs" in overlay
    assert slack.ephemeral_calls == [
        {
            "channel": "D07OWNER",
            "user": "U07OWNER",
            "text": "Trusted publishers updated: npm:camoufox-labs, pypi:camoufox-labs",
        }
    ]


@pytest.mark.asyncio
async def test_trust_add_is_idempotent_for_already_trusted_publisher(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()
    config = make_config()

    first = await handle_engram_command(
        router=router,
        config=config,
        slack_client=slack,
        source_channel_id="D07OWNER",
        source_channel_name=None,
        user_id="U07OWNER",
        command_text="trust add pypi:camoufox-labs",
    )
    second = await handle_engram_command(
        router=router,
        config=config,
        slack_client=slack,
        source_channel_id="D07OWNER",
        source_channel_name=None,
        user_id="U07OWNER",
        command_text="trust add pypi:camoufox-labs",
    )

    assert first == {"ok": True, "action": "trust add", "changed": True}
    assert second == {"ok": True, "action": "trust add", "changed": False}
    assert slack.ephemeral_calls[-1]["text"] == (
        "Publishers already trusted: pypi:camoufox-labs"
    )


@pytest.mark.asyncio
async def test_non_owner_cannot_add_trusted_publisher_via_slash_command(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OTHER",
        command_text="trust add pypi:camoufox-labs",
    )

    assert result == {"ok": False, "error": "not owner"}
    assert not (state_dir(home) / "trusted_publishers.yaml").exists()
    assert slack.ephemeral_calls == [
        {"channel": "C07TEAM", "user": "U07OTHER", "text": "Owner-only."}
    ]


@pytest.mark.asyncio
async def test_owner_can_allow_mcp_access_via_slash_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".engram"
    write_mcp_inventory(
        tmp_path,
        {
            "camoufox": {"command": "uvx", "args": ["camoufox-browser[mcp]==0.1.1"]},
        },
    )
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    async def trust_fake(_server_name, _server_config, *, home=None):
        return MCPTrustDecision(
            server_name="camoufox",
            tier=MCPTrustTier.UNKNOWN,
            registry="pypi",
            package_name="camoufox-browser[mcp]",
            version="0.1.1",
            publisher="camoufox-labs",
            publishers=["camoufox-labs"],
            trust_summary="metadata lookup failed",
            reason="metadata lookup failed",
        )

    monkeypatch.setattr("engram.mcp_manifest_gate.resolve_mcp_server_trust", trust_fake)

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OWNER",
        command_text="mcp allow camoufox",
    )

    assert result == {"ok": True, "action": "allow", "changed": False, "pending": True}
    assert len(slack.post_calls) == 1
    assert slack.post_calls[0]["channel"] == "D07OWNER"
    assert "Owner approval required for MCP addition" in slack.post_calls[0]["blocks"][0]["text"]["text"]
    assert "Owner approval requested in the owner DM." in slack.ephemeral_calls[0]["text"]

    pending = router.hitl.pending_for_channel("C07TEAM")
    assert len(pending) == 1
    q = pending[0]
    ack = await handle_block_action(
        _block_action_payload(f"{q.permission_request_id}|0"),
        router,
        slack,
    )

    assert ack == {"ok": True}
    await _wait_until(
        lambda: load_manifest(channel_manifest_path("C07TEAM", home)).mcp_servers.allowed
        == ["engram-memory", "camoufox"]
    )
    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert manifest.mcp_servers.allowed == ["engram-memory", "camoufox"]


@pytest.mark.asyncio
async def test_owner_can_reject_unknown_mcp_access_via_slash_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".engram"
    write_mcp_inventory(
        tmp_path,
        {
            "camoufox": {"command": "uvx", "args": ["camoufox-browser[mcp]==0.1.1"]},
        },
    )
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    async def trust_fake(_server_name, _server_config, *, home=None):
        return MCPTrustDecision(
            server_name="camoufox",
            tier=MCPTrustTier.UNKNOWN,
            registry="pypi",
            package_name="camoufox-browser[mcp]",
            version="0.1.1",
            publisher="camoufox-labs",
            publishers=["camoufox-labs"],
            trust_summary="metadata lookup failed",
            reason="metadata lookup failed",
        )

    monkeypatch.setattr("engram.mcp_manifest_gate.resolve_mcp_server_trust", trust_fake)

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OWNER",
        command_text="mcp allow camoufox",
    )

    assert result == {"ok": True, "action": "allow", "changed": False, "pending": True}
    pending = router.hitl.pending_for_channel("C07TEAM")
    assert len(pending) == 1
    q = pending[0]
    ack = await handle_block_action(
        _block_action_payload(f"{q.permission_request_id}|deny"),
        router,
        slack,
    )

    assert ack == {"ok": True}
    await _wait_until(lambda: q.future.done() and bool(slack.update_calls))
    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert manifest.mcp_servers.allowed == ["engram-memory"]


@pytest.mark.asyncio
async def test_anyone_can_deny_mcp_access_via_slash_command(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_mcp_inventory(
        tmp_path,
        {
            "camoufox": {"command": "uvx", "args": ["camoufox-browser[mcp]==0.1.1"]},
        },
    )
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    allow_mcp_for_test(home, "C07TEAM", "camoufox")
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OTHER",
        command_text="mcp deny camoufox",
    )

    updated = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert updated.mcp_servers.disallowed == ["camoufox"]
    assert "Denied MCP server `camoufox`." in slack.ephemeral_calls[0]["text"]
    assert "Effective: engram-memory" in slack.ephemeral_calls[0]["text"]


@pytest.mark.asyncio
async def test_mcp_command_authorizes_against_fresh_disk_manifest(
    tmp_path: Path,
) -> None:
    """GRO-531 regression: TOCTOU between cached manifest and disk.

    The slash-command must reload the manifest from disk before running
    `can_change_mcp_access`, otherwise an on-host CLI edit between
    `get(channel)` and the auth check creates a window where authorization
    decisions are made against stale state but writes happen against fresh
    disk state.

    Setup: cached manifest has NO allow list (inherit-all mode). On-disk
    manifest has been edited by a CLI to ADD an allow list. The slash
    command must see the disk version and refuse to allow into a fresh
    explicit allow list when the requester isn't the owner.
    """
    home = tmp_path / ".engram"
    write_mcp_inventory(
        tmp_path,
        {
            "camoufox": {"command": "uvx", "args": ["camoufox-browser[mcp]==0.1.1"]},
        },
    )
    # Initial state: explicit allow list with engram-memory only.
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    # Prime the router cache by loading the channel session BEFORE the
    # CLI edit happens. Now `session.manifest` reflects the original state.
    await router.get("C07TEAM", channel_name="growth", is_dm=False)

    # Simulate a CLI edit between cache prime and slash-command processing:
    # an on-host CLI run (or another slash-command) explicitly DENIES
    # camoufox in the on-disk manifest.
    set_channel_mcp_server_access(
        "C07TEAM", "camoufox", action="deny", home=home
    )

    # Now a non-owner asks to allow camoufox. With the bug, the cached
    # manifest doesn't know about the deny, so `is_disallowed=False` is
    # passed to can_change_mcp_access; without the trust gate the request
    # would route to set_channel_mcp_server_access against fresh disk
    # state and silently undo the deny. With the fix, the disk reload
    # reveals camoufox is explicitly disallowed.
    slack = FakeSlackClient()
    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OTHER",  # not the owner
        command_text="mcp allow camoufox",
    )

    # Non-owner cannot change access. The auth check must be against
    # fresh disk state, so it sees the explicit disallowed entry.
    assert result["ok"] is False
    # On-disk state must be unchanged: camoufox still disallowed, NOT in allowed.
    on_disk = load_manifest(channel_manifest_path("C07TEAM", home))
    assert "camoufox" in on_disk.mcp_servers.disallowed
    assert "camoufox" not in (on_disk.mcp_servers.allowed or [])


@pytest.mark.asyncio
async def test_mcp_command_invalidates_session_for_immediate_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRO-531 regression: MCP changes must take effect immediately.

    Previously the slash-command called `router.replace_cached_manifest`
    which only updates the cached manifest reference but does NOT
    disconnect the live ClaudeSDKClient. The running SDK keeps the OLD
    MCP set until idle timeout (~15 min).

    The fix calls `router.invalidate(channel_id)` which disconnects the
    agent client and removes the session from the cache.
    """
    home = tmp_path / ".engram"
    write_mcp_inventory(
        tmp_path,
        {
            "camoufox": {"command": "uvx", "args": ["camoufox-browser[mcp]==0.1.1"]},
        },
    )
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    # Prime the session cache.
    await router.get("C07TEAM", channel_name="growth", is_dm=False)
    assert router.session_count() == 1

    async def trust_fake(_server_name, _server_config, *, home=None):
        return MCPTrustDecision(
            server_name="camoufox",
            tier=MCPTrustTier.OFFICIAL,
            registry="pypi",
            package_name="camoufox-browser[mcp]",
            version="0.1.1",
            trust_summary="official server",
            reason="official package",
        )

    monkeypatch.setattr("engram.mcp_manifest_gate.resolve_mcp_server_trust", trust_fake)

    slack = FakeSlackClient()
    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OWNER",
        command_text="mcp allow camoufox",
    )

    assert result["ok"] is True
    assert result["changed"] is True
    # Session must be evicted from the cache so the next request
    # rebuilds the SDK client with the new MCP set. Before the fix the
    # session would still be cached with the old client.
    assert router.session_count() == 0


@pytest.mark.asyncio
async def test_non_owner_downgrade_via_picker_is_allowed(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(
        home,
        "C07TEAM",
        tier=PermissionTier.OWNER_SCOPED,
        nightly_included=True,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_tier_pick_action(
        payload=picker_payload(
            value="C07TEAM|safe|U07OTHER",
            user_id="U07OTHER",
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert manifest.permission_tier == PermissionTier.TASK_ASSISTANT
    assert manifest.nightly_included is False
    assert result["response"]["text"] == "Tier set to `safe`. Read-only tools no longer auto-allow."
    assert slack.post_calls == [
        {
            "channel": "C07TEAM",
            "text": (
                "🔒 <@U07OTHER> downgraded this channel to `safe`. "
                "Read-only tools no longer auto-allow. Type `/engram` to see current settings."
            ),
            "_ts": slack.post_calls[0]["_ts"],
        }
    ]


@pytest.mark.asyncio
async def test_non_owner_upgrade_via_picker_is_rejected(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_tier_pick_action(
        payload=picker_payload(
            value="C07TEAM|trusted|U07OTHER",
            user_id="U07OTHER",
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is False
    assert result["error"] == "not owner"
    assert result["response"]["text"] == (
        "Only the channel owner can upgrade to `trusted`. "
        "Ask owner to run `/engram upgrade`."
    )
    assert manifest.permission_tier == PermissionTier.TASK_ASSISTANT
    assert slack.post_calls == []


@pytest.mark.asyncio
async def test_yolo_picker_action_returns_duration_picker(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.OWNER_SCOPED)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_tier_pick_action(
        payload=picker_payload(
            value="C07TEAM|yolo|U07OWNER",
            user_id="U07OWNER",
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED
    assert result["response"]["text"].startswith("YOLO mode will bypass HITL gates")
    assert [element["text"]["text"] for element in result["response"]["blocks"][1]["elements"]] == [
        "⏱️ 6h",
        "⏱️ 24h",
        "⏱️ 72h",
        "✕ Cancel",
    ]
    assert slack.post_calls == []


@pytest.mark.asyncio
async def test_current_tier_click_is_noop_without_public_notice(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_tier_pick_action(
        payload=picker_payload(
            value="C07TEAM|safe|U07OWNER",
            user_id="U07OWNER",
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert manifest.permission_tier == PermissionTier.TASK_ASSISTANT
    assert result["changed"] is False
    assert result["response"]["text"] == "Already on `safe`."
    assert slack.post_calls == []


@pytest.mark.asyncio
async def test_arg_upgrade_yolo_to_current_tier_is_noop(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.YOLO)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OWNER",
        command_text="upgrade yolo working on docs",
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert result["changed"] is False
    assert manifest.permission_tier == PermissionTier.YOLO
    assert slack.ephemeral_calls == [
        {
            "channel": "C07TEAM",
            "user": "U07OWNER",
            "text": "Already on `yolo`.",
        }
    ]
    assert slack.post_calls == []


@pytest.mark.asyncio
async def test_arg_upgrade_shortcut_executes_immediately(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM", tier=PermissionTier.TASK_ASSISTANT)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OWNER",
        command_text="upgrade trusted Working on docs",
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED
    assert slack.ephemeral_calls == [
        {
            "channel": "C07TEAM",
            "user": "U07OWNER",
            "text": "Tier set to `trusted`. Read-only tools now auto-allow.",
        }
    ]
    assert slack.post_calls == [
        {
            "channel": "C07TEAM",
            "text": (
                "✨ <@U07OWNER> upgraded this channel to `trusted`. "
                "Read-only tools now auto-allow. Type `/engram` to see current settings."
            ),
            "_ts": slack.post_calls[0]["_ts"],
        }
    ]
