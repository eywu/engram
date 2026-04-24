from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.ingress import (
    ACTION_ID_CHANNELS_PAGE,
    ACTION_ID_NIGHTLY_TOGGLE,
    ACTION_ID_TIER_PICK,
    ACTION_ID_YOLO_DURATION,
    handle_channels_dashboard_action,
    handle_engram_command,
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
    def __init__(self, conversation_info: dict[str, dict[str, Any]] | None = None) -> None:
        self.conversation_info = conversation_info or {}
        self.post_calls: list[dict[str, Any]] = []
        self.ephemeral_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.info_calls: list[str] = []
        self.chat_postMessage = self._chat_post_message
        self.chat_postEphemeral = self._chat_post_ephemeral

    async def _chat_post_message(self, **kwargs):
        ts = f"1713800000.{len(self.post_calls) + 400:06d}"
        self.post_calls.append({**kwargs, "_ts": ts})
        return {"ok": True, "ts": ts}

    async def _chat_post_ephemeral(self, **kwargs):
        self.ephemeral_calls.append(kwargs)
        return {"ok": True}

    async def chat_update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"ok": True}

    async def conversations_info(self, *, channel: str):
        self.info_calls.append(channel)
        return {"ok": True, "channel": self.conversation_info.get(channel, {})}


def make_config() -> EngramConfig:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )
    cfg.owner_dm_channel_id = "D07OWNER"
    cfg.owner_user_id = "U07OWNER"
    return cfg


def write_manifest(
    home: Path,
    channel_id: str,
    *,
    identity: IdentityTemplate = IdentityTemplate.TASK_ASSISTANT,
    status: ChannelStatus = ChannelStatus.ACTIVE,
    label: str | None = None,
    tier: PermissionTier | None = None,
    meta_eligible: bool = True,
    yolo_hours: int | None = None,
    pre_yolo_tier: PermissionTier | None = None,
) -> None:
    now = datetime.now(UTC)
    manifest_path = channel_manifest_path(channel_id, home)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    effective_tier = (
        tier
        if tier is not None
        else (
            PermissionTier.OWNER_SCOPED
            if identity == IdentityTemplate.OWNER_DM_FULL
            else PermissionTier.TASK_ASSISTANT
        )
    )
    dump_manifest(
        ChannelManifest(
            channel_id=channel_id,
            identity=identity,
            status=status,
            label=label,
            permission_tier=effective_tier,
            meta_eligible=meta_eligible,
            yolo_granted_at=(
                now - timedelta(hours=1)
                if effective_tier == PermissionTier.YOLO and yolo_hours is not None
                else None
            ),
            yolo_until=(
                now + timedelta(hours=yolo_hours)
                if effective_tier == PermissionTier.YOLO and yolo_hours is not None
                else None
            ),
            pre_yolo_tier=pre_yolo_tier,
        ),
        manifest_path,
    )


def dashboard_row_texts(blocks: list[dict[str, Any]]) -> list[str]:
    return [
        str(block["text"]["text"])
        for block in blocks
        if block["type"] == "section"
        and str(block["text"]["text"]).startswith(("📬", "💬"))
    ]


def dashboard_action_labels(
    blocks: list[dict[str, Any]],
    channel_id: str,
) -> list[str]:
    block = next(
        block
        for block in blocks
        if block["type"] == "actions"
        and block.get("block_id") == f"engram_channels:0:channel:{channel_id}"
    )
    return [str(element["text"]["text"]) for element in block["elements"]]


def dashboard_nav_labels(blocks: list[dict[str, Any]]) -> list[str]:
    nav_blocks = [
        block
        for block in blocks
        if block["type"] == "actions" and str(block.get("block_id", "")).endswith(":nav")
    ]
    if not nav_blocks:
        return []
    return [str(element["text"]["text"]) for element in nav_blocks[0]["elements"]]


def dashboard_action_payload(
    *,
    action_id: str,
    value: str,
    page: int = 0,
    channel_id: str | None = "C07TEAM",
    source_channel_id: str = "D07OWNER",
    user_id: str = "U07OWNER",
) -> dict[str, Any]:
    kind = "nav" if channel_id is None else f"channel:{channel_id}"
    return {
        "actions": [
            {
                "action_id": action_id,
                "value": value,
                "block_id": f"engram_channels:{page}:{kind}",
            }
        ],
        "channel": {"id": source_channel_id},
        "user": {"id": user_id},
    }


@pytest.mark.asyncio
async def test_channels_dashboard_redirects_team_channel_to_owner_dm(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07OPS",
        source_channel_name="ops",
        user_id="U07OWNER",
        command_text="channels",
    )

    assert result == {"ok": False, "error": "dm_only"}
    assert slack.ephemeral_calls == [
        {
            "channel": "C07OPS",
            "user": "U07OWNER",
            "text": (
                "Channel dashboard is DM-only. "
                "Run `/engram channels` from your DM with Engram."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_channels_dashboard_empty_state(tmp_path: Path) -> None:
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
        command_text="channels",
    )

    assert result == {"ok": True, "count": 0, "page": 0}
    assert len(slack.ephemeral_calls) == 1
    assert slack.ephemeral_calls[0]["text"] == "Engram channel dashboard"
    assert (
        slack.ephemeral_calls[0]["blocks"][0]["text"]["text"]
        == "No Engram-enabled channels yet. Add Engram to a channel and re-run `/engram channels`."
    )


@pytest.mark.asyncio
async def test_channels_dashboard_one_channel_renders_owner_dm_row(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    write_manifest(
        home,
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Alice (DM)",
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient(conversation_info={"D07OWNER": {"is_im": True}})

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="D07OWNER",
        source_channel_name=None,
        user_id="U07OWNER",
        command_text="channels",
    )

    assert result == {"ok": True, "count": 1, "page": 0}
    rows = dashboard_row_texts(slack.ephemeral_calls[0]["blocks"])
    assert rows == ["📬 owner-dm (you) [✨ trusted]\nIncluded in nightly ✓"]
    assert dashboard_action_labels(slack.ephemeral_calls[0]["blocks"], "D07OWNER") == [
        "Upgrade to YOLO",
        "Downgrade to Safe",
    ]
    assert dashboard_nav_labels(slack.ephemeral_calls[0]["blocks"]) == []


@pytest.mark.asyncio
async def test_channels_dashboard_sorts_rows_and_shows_expected_buttons(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    write_manifest(
        home,
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Alice (DM)",
        tier=PermissionTier.YOLO,
        yolo_hours=24,
        pre_yolo_tier=PermissionTier.OWNER_SCOPED,
    )
    write_manifest(home, "G07PRIVATE", label="#engram-self")
    write_manifest(
        home,
        "C07TRUST",
        label="#growth-team",
        tier=PermissionTier.OWNER_SCOPED,
    )
    write_manifest(home, "C07SAFE", label="#random", meta_eligible=False)
    write_manifest(
        home,
        "C07ARCH",
        label="#archive-room",
        tier=PermissionTier.OWNER_SCOPED,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient(
        conversation_info={
            "D07OWNER": {"is_im": True},
            "G07PRIVATE": {"name": "engram-self", "is_private": True},
            "C07TRUST": {"name": "growth-team", "is_private": False},
            "C07SAFE": {"name": "random", "is_private": False},
            "C07ARCH": {
                "name": "archive-room",
                "is_private": False,
                "is_archived": True,
            },
        }
    )

    await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="D07OWNER",
        source_channel_name=None,
        user_id="U07OWNER",
        command_text="channels",
    )

    rows = dashboard_row_texts(slack.ephemeral_calls[0]["blocks"])
    assert rows[0].startswith("📬 owner-dm (you) [🚀 yolo")
    assert rows[1] == "💬 engram-self [🔒 safe]\nNightly: excluded (safe tier)"
    assert rows[2] == "💬 growth-team [✨ trusted]\nIncluded in nightly ✓"
    assert rows[3] == "💬 random [🔒 safe]\nNightly: excluded (safe tier)"
    assert rows[4] == "💬 archive-room [archived] [✨ trusted]\nIncluded in nightly ✓"
    assert dashboard_action_labels(slack.ephemeral_calls[0]["blocks"], "G07PRIVATE") == [
        "Upgrade",
    ]
    assert dashboard_action_labels(slack.ephemeral_calls[0]["blocks"], "C07TRUST") == [
        "Upgrade to YOLO",
        "Downgrade to Safe",
        "Exclude from nightly",
    ]


@pytest.mark.asyncio
async def test_channels_dashboard_page_size_boundary_at_20_has_no_pagination(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_manifest(
        home,
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Alice (DM)",
    )
    for index in range(19):
        write_manifest(
            home,
            f"C07TEAM{index:02d}",
            label=f"#team-{index:02d}",
            tier=PermissionTier.OWNER_SCOPED,
        )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient(
        conversation_info={
            "D07OWNER": {"is_im": True},
            **{
                f"C07TEAM{index:02d}": {"name": f"team-{index:02d}", "is_private": False}
                for index in range(19)
            },
        }
    )

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="D07OWNER",
        source_channel_name=None,
        user_id="U07OWNER",
        command_text="channels",
    )

    assert result == {"ok": True, "count": 20, "page": 0}
    assert len(dashboard_row_texts(slack.ephemeral_calls[0]["blocks"])) == 20
    assert dashboard_nav_labels(slack.ephemeral_calls[0]["blocks"]) == []


@pytest.mark.asyncio
async def test_channels_dashboard_paginates_25_channels_and_page_action_rerenders(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_manifest(
        home,
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Alice (DM)",
    )
    for index in range(24):
        write_manifest(
            home,
            f"C07TEAM{index:02d}",
            label=f"#team-{index:02d}",
            tier=PermissionTier.OWNER_SCOPED,
        )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient(
        conversation_info={
            "D07OWNER": {"is_im": True},
            **{
                f"C07TEAM{index:02d}": {"name": f"team-{index:02d}", "is_private": False}
                for index in range(24)
            },
        }
    )

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="D07OWNER",
        source_channel_name=None,
        user_id="U07OWNER",
        command_text="channels",
    )

    assert result == {"ok": True, "count": 25, "page": 0}
    first_blocks = slack.ephemeral_calls[0]["blocks"]
    assert len(dashboard_row_texts(first_blocks)) == 20
    assert dashboard_nav_labels(first_blocks) == ["Next"]

    page_result = await handle_channels_dashboard_action(
        payload=dashboard_action_payload(
            action_id=ACTION_ID_CHANNELS_PAGE,
            value="1",
            page=0,
            channel_id=None,
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    assert page_result["ok"] is True
    assert page_result["page"] == 1
    assert page_result["response"]["replace_original"] is True
    second_blocks = page_result["response"]["blocks"]
    assert len(dashboard_row_texts(second_blocks)) == 5
    assert dashboard_nav_labels(second_blocks) == ["Previous"]


@pytest.mark.asyncio
async def test_channels_dashboard_tier_pick_action_updates_manifest_and_rerenders(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_manifest(
        home,
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Alice (DM)",
    )
    write_manifest(
        home,
        "C07TEAM",
        label="#growth-team",
        tier=PermissionTier.OWNER_SCOPED,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient(
        conversation_info={
            "D07OWNER": {"is_im": True},
            "C07TEAM": {"name": "growth-team", "is_private": False},
        }
    )

    result = await handle_channels_dashboard_action(
        payload=dashboard_action_payload(
            action_id=ACTION_ID_TIER_PICK,
            value="C07TEAM|yolo",
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert result["response"]["replace_original"] is True
    assert manifest.permission_tier == PermissionTier.YOLO
    assert manifest.yolo_until is not None
    team_row = next(
        text
        for text in dashboard_row_texts(result["response"]["blocks"])
        if text.startswith("💬 growth-team")
    )
    assert "[🚀 yolo" in team_row
    assert "expires " in team_row


@pytest.mark.asyncio
async def test_channels_dashboard_nightly_toggle_action_rerenders(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_manifest(
        home,
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Alice (DM)",
    )
    write_manifest(
        home,
        "C07TEAM",
        label="#growth-team",
        tier=PermissionTier.OWNER_SCOPED,
        meta_eligible=True,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient(
        conversation_info={
            "D07OWNER": {"is_im": True},
            "C07TEAM": {"name": "growth-team", "is_private": False},
        }
    )

    result = await handle_channels_dashboard_action(
        payload=dashboard_action_payload(
            action_id=ACTION_ID_NIGHTLY_TOGGLE,
            value="C07TEAM|exclude",
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert manifest.meta_eligible is False
    assert result["response"]["replace_original"] is True
    assert "Nightly: excluded" in dashboard_row_texts(result["response"]["blocks"])[1]
    action_block = next(
        block
        for block in result["response"]["blocks"]
        if block["type"] == "actions"
        and block.get("block_id") == "engram_channels:0:channel:C07TEAM"
    )
    assert [element["text"]["text"] for element in action_block["elements"]] == [
        "Upgrade to YOLO",
        "Downgrade to Safe",
        "Include in nightly",
    ]


@pytest.mark.asyncio
async def test_channels_dashboard_yolo_extend_action_rerenders(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_manifest(
        home,
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Alice (DM)",
    )
    write_manifest(
        home,
        "C07TEAM",
        label="#growth-team",
        tier=PermissionTier.YOLO,
        yolo_hours=24,
        pre_yolo_tier=PermissionTier.OWNER_SCOPED,
    )
    before = load_manifest(channel_manifest_path("C07TEAM", home))
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient(
        conversation_info={
            "D07OWNER": {"is_im": True},
            "C07TEAM": {"name": "growth-team", "is_private": False},
        }
    )

    result = await handle_channels_dashboard_action(
        payload=dashboard_action_payload(
            action_id=ACTION_ID_YOLO_DURATION,
            value="C07TEAM|24h",
        ),
        router=router,
        config=make_config(),
        slack_client=slack,
    )

    after = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert result["response"]["replace_original"] is True
    assert after.yolo_until is not None
    assert before.yolo_until is not None
    assert after.yolo_until > before.yolo_until
