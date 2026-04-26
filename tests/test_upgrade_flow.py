"""Tests for GRO-511 button-driven tier changes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.ingress import (
    ACTION_ID_TIER_PICK,
    handle_engram_command,
    handle_tier_pick_action,
)
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
    ScopeList,
    dump_manifest,
    load_manifest,
    set_channel_mcp_server_access,
)
from engram.paths import channel_manifest_path
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
    set_channel_mcp_server_access(
        "C07TEAM",
        "camoufox",
        action="allow",
        home=home,
    )
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
async def test_owner_can_allow_mcp_access_via_slash_command(
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
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
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

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert manifest.mcp_servers.allowed == ["engram-memory", "camoufox"]
    assert "Allowed MCP server `camoufox`." in slack.ephemeral_calls[0]["text"]
    assert "Effective: engram-memory, camoufox" in slack.ephemeral_calls[0]["text"]


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
    set_channel_mcp_server_access(
        "C07TEAM",
        "camoufox",
        action="allow",
        home=home,
    )
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
