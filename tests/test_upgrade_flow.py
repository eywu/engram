"""Tests for GRO-501 tier-upgrade request and approval flows."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.ingress import (
    _PENDING_UPGRADE_REQUESTS,
    _PENDING_UPGRADE_REQUESTS_BY_CHANNEL,
    handle_upgrade_action,
    handle_upgrade_command,
)
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
    dump_manifest,
    load_manifest,
)
from engram.paths import channel_manifest_path
from engram.router import Router


class FakeSlackClient:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.ephemeral_calls: list[dict[str, Any]] = []
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


@pytest.fixture(autouse=True)
def clear_pending_requests() -> None:
    _PENDING_UPGRADE_REQUESTS.clear()
    _PENDING_UPGRADE_REQUESTS_BY_CHANNEL.clear()
    yield
    _PENDING_UPGRADE_REQUESTS.clear()
    _PENDING_UPGRADE_REQUESTS_BY_CHANNEL.clear()


def make_config() -> EngramConfig:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )
    cfg.owner_dm_channel_id = "D07OWNER"
    cfg.owner_user_id = "U07OWNER"
    return cfg


def write_active_manifest(home: Path, channel_id: str, *, label: str = "#growth") -> None:
    path = channel_manifest_path(channel_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_manifest(
        ChannelManifest(
            channel_id=channel_id,
            identity=IdentityTemplate.TASK_ASSISTANT,
            status=ChannelStatus.ACTIVE,
            label=label,
        ),
        path,
    )


def owner_action_payload(*, action_id: str, value: str, user_id: str = "U07OWNER") -> dict[str, Any]:
    return {
        "actions": [{"action_id": action_id, "value": value}],
        "channel": {"id": "D07OWNER"},
        "user": {"id": user_id},
    }


@pytest.mark.asyncio
async def test_upgrade_command_posts_waiting_message_and_owner_dm(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM")
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    with caplog.at_level(logging.INFO, logger="engram.ingress"):
        result = await handle_upgrade_command(
            router=router,
            config=make_config(),
            slack_client=slack,
            source_channel_id="C07TEAM",
            source_channel_name="growth",
            user_id="U07REQUESTER",
            requested_tier=PermissionTier.OWNER_SCOPED,
            reason="private workspace",
        )

    assert result["ok"] is True
    assert len(slack.post_calls) == 2
    assert slack.post_calls[0]["channel"] == "C07TEAM"
    assert slack.post_calls[0]["text"] == (
        "⏳ Permission upgrade requested — waiting for owner approval."
    )
    assert slack.post_calls[1]["channel"] == "D07OWNER"
    assert slack.post_calls[1]["text"] == "Permission upgrade request"
    button_texts = [
        element["text"]["text"]
        for element in slack.post_calls[1]["blocks"][1]["elements"]
    ]
    assert button_texts == [
        "Approve until revoked",
        "Approve 30d",
        "Deny",
    ]
    record = next(
        item for item in caplog.records if item.getMessage() == "permission.upgrade_requested"
    )
    assert record.channel == "C07TEAM"
    assert record.user == "U07REQUESTER"
    assert record.from_tier == "task-assistant"
    assert record.to_tier == "owner-scoped"
    assert record.reason == "private workspace"


@pytest.mark.asyncio
async def test_upgrade_approve_permanent_updates_manifest_and_messages(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM")
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    await handle_upgrade_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07REQUESTER",
        requested_tier=PermissionTier.OWNER_SCOPED,
        reason="private workspace",
    )
    approve_value = slack.post_calls[1]["blocks"][1]["elements"][0]["value"]

    with caplog.at_level(logging.INFO, logger="engram.ingress"):
        result = await handle_upgrade_action(
            payload=owner_action_payload(
                action_id="upgrade_decision_approve_permanent",
                value=approve_value,
            ),
            router=router,
            slack_client=slack,
            owner_user_id="U07OWNER",
        )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result == {"ok": True, "decision": "approve", "duration": "permanent"}
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED
    assert manifest.yolo_until is None
    assert manifest.pre_yolo_tier is None
    assert [call["channel"] for call in slack.update_calls] == ["C07TEAM", "D07OWNER"]
    assert slack.update_calls[0]["text"] == "✅ Upgraded to owner-scoped by <@U07OWNER>."
    assert slack.update_calls[1]["text"] == "✅ Approved by <@U07OWNER>."
    record = next(
        item for item in caplog.records if item.getMessage() == "permission.upgrade_granted"
    )
    assert record.channel == "C07TEAM"
    assert record.approver == "U07OWNER"
    assert record.duration == "permanent"


@pytest.mark.asyncio
async def test_upgrade_approve_bounded_yolo_sets_expiry(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM")
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    await handle_upgrade_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07REQUESTER",
        requested_tier=PermissionTier.YOLO,
        reason="need fast iteration",
    )

    dm_buttons = slack.post_calls[1]["blocks"][1]["elements"]
    assert [button["text"]["text"] for button in dm_buttons] == [
        "Approve 24h",
        "Approve 6h",
        "Deny",
    ]

    result = await handle_upgrade_action(
        payload=owner_action_payload(
            action_id="upgrade_decision_approve_24h",
            value=dm_buttons[0]["value"],
        ),
        router=router,
        slack_client=slack,
        owner_user_id="U07OWNER",
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result == {"ok": True, "decision": "approve", "duration": "24h"}
    assert manifest.permission_tier == PermissionTier.YOLO
    assert manifest.yolo_until is not None
    assert manifest.pre_yolo_tier == PermissionTier.TASK_ASSISTANT
    assert slack.update_calls[0]["text"] == "✅ Upgraded to yolo by <@U07OWNER>."
    assert slack.update_calls[1]["text"] == "✅ Approved by <@U07OWNER>."


@pytest.mark.asyncio
async def test_upgrade_deny_updates_messages_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM")
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    await handle_upgrade_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07REQUESTER",
        requested_tier=PermissionTier.OWNER_SCOPED,
        reason=None,
    )
    deny_value = slack.post_calls[1]["blocks"][1]["elements"][2]["value"]

    with caplog.at_level(logging.INFO, logger="engram.ingress"):
        result = await handle_upgrade_action(
            payload=owner_action_payload(
                action_id="upgrade_decision_deny",
                value=deny_value,
            ),
            router=router,
            slack_client=slack,
            owner_user_id="U07OWNER",
        )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result == {"ok": True, "decision": "deny"}
    assert manifest.permission_tier == PermissionTier.TASK_ASSISTANT
    assert slack.update_calls[0]["text"] == "❌ Request denied."
    assert slack.update_calls[1]["text"] == "❌ Denied by <@U07OWNER>."
    record = next(
        item for item in caplog.records if item.getMessage() == "permission.upgrade_denied"
    )
    assert record.channel == "C07TEAM"
    assert record.approver == "U07OWNER"


@pytest.mark.asyncio
async def test_non_owner_upgrade_click_gets_ephemeral_denial(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM")
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    await handle_upgrade_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07REQUESTER",
        requested_tier=PermissionTier.OWNER_SCOPED,
        reason=None,
    )
    approve_value = slack.post_calls[1]["blocks"][1]["elements"][0]["value"]

    result = await handle_upgrade_action(
        payload=owner_action_payload(
            action_id="upgrade_decision_approve_permanent",
            value=approve_value,
            user_id="U07OTHER",
        ),
        router=router,
        slack_client=slack,
        owner_user_id="U07OWNER",
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result == {"ok": False, "error": "not owner"}
    assert manifest.permission_tier == PermissionTier.TASK_ASSISTANT
    assert slack.update_calls == []
    assert slack.ephemeral_calls == [
        {
            "channel": "D07OWNER",
            "user": "U07OTHER",
            "text": "Only the owner can approve upgrades.",
        }
    ]


@pytest.mark.asyncio
async def test_new_request_supersedes_old_dm_card(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    write_active_manifest(home, "C07TEAM")
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    first = await handle_upgrade_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07REQUESTER",
        requested_tier=PermissionTier.OWNER_SCOPED,
        reason="first",
    )
    second = await handle_upgrade_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07REQUESTER",
        requested_tier=PermissionTier.YOLO,
        reason="second",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(slack.post_calls) == 4
    assert len(slack.update_calls) == 1
    assert slack.update_calls[0]["channel"] == "D07OWNER"
    assert slack.update_calls[0]["text"] == "⚪ Superseded by newer request."
