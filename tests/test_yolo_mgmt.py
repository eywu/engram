from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from engram.cli import app as cli_app
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.ingress import handle_engram_command, handle_yolo_action
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
        self.ephemeral_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.chat_postMessage = self._chat_post_message
        self.chat_postEphemeral = self._chat_post_ephemeral

    async def _chat_post_message(self, **kwargs):
        ts = f"1713800000.{len(self.post_calls) + 300:06d}"
        self.post_calls.append({**kwargs, "_ts": ts})
        return {"ok": True, "ts": ts}

    async def _chat_post_ephemeral(self, **kwargs):
        self.ephemeral_calls.append(kwargs)
        return {"ok": True}

    async def chat_update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"ok": True}


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("HOME", str(tmp_path))
    return CliRunner()


def make_config() -> EngramConfig:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )
    cfg.owner_dm_channel_id = "D07OWNER"
    cfg.owner_user_id = "U07OWNER"
    return cfg


def write_active_yolo_manifest(
    home: Path,
    *,
    channel_id: str = "C07TEAM",
    label: str = "#growth",
    remaining_hours: int = 24,
    pre_yolo_tier: PermissionTier = PermissionTier.OWNER_SCOPED,
) -> Path:
    now = datetime.now(UTC)
    path = channel_manifest_path(channel_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_manifest(
        ChannelManifest(
            channel_id=channel_id,
            identity=IdentityTemplate.TASK_ASSISTANT,
            status=ChannelStatus.ACTIVE,
            label=label,
            permission_tier=PermissionTier.YOLO,
            yolo_granted_at=now - timedelta(hours=1),
            yolo_until=now + timedelta(hours=remaining_hours),
            pre_yolo_tier=pre_yolo_tier,
        ),
        path,
    )
    return path


def yolo_action_payload(
    *,
    action_id: str,
    channel_id: str = "C07OPS",
    user_id: str = "U07OWNER",
    value: str = "C07TEAM",
) -> dict[str, Any]:
    return {
        "actions": [{"action_id": action_id, "value": value}],
        "channel": {"id": channel_id},
        "user": {"id": user_id},
    }


@pytest.mark.asyncio
async def test_yolo_list_empty_posts_no_active_message(tmp_path: Path) -> None:
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
        command_text="yolo list",
    )

    assert result == {"ok": True, "count": 0}
    assert len(slack.ephemeral_calls) == 1
    assert slack.ephemeral_calls[0]["text"] == "No active yolo grants."
    assert slack.ephemeral_calls[0]["blocks"][0]["text"]["text"] == "No active yolo grants."


@pytest.mark.asyncio
async def test_yolo_list_renders_active_grants_with_buttons(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    write_active_yolo_manifest(home)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07OPS",
        source_channel_name="ops",
        user_id="U07OWNER",
        command_text="yolo list",
    )

    assert result == {"ok": True, "count": 1}
    call = slack.ephemeral_calls[0]
    assert call["text"] == "Active yolo grants"
    assert "*#growth* (`C07TEAM`)" in call["blocks"][1]["text"]["text"]
    assert "Restores to: `owner-scoped`" in call["blocks"][1]["text"]["text"]
    buttons = call["blocks"][2]["elements"]
    assert [button["text"]["text"] for button in buttons] == ["Extend 6h", "Revoke"]
    assert [button["action_id"] for button in buttons] == [
        "yolo_extend_C07TEAM",
        "yolo_revoke_C07TEAM",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_text",
    [
        "yolo list",
        "yolo off C07TEAM",
        "yolo extend C07TEAM 6h",
    ],
)
async def test_non_owner_yolo_subcommands_are_rejected_without_state_change(
    tmp_path: Path,
    command_text: str,
) -> None:
    home = tmp_path / ".engram"
    write_active_yolo_manifest(home)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()
    before = load_manifest(channel_manifest_path("C07TEAM", home))

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07OPS",
        source_channel_name="ops",
        user_id="U07OTHER",
        command_text=command_text,
    )

    after = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result == {"ok": False, "error": "not owner"}
    assert slack.ephemeral_calls == [
        {"channel": "C07OPS", "user": "U07OTHER", "text": "Owner-only."}
    ]
    assert after == before
    assert slack.post_calls == []


@pytest.mark.asyncio
async def test_yolo_off_command_revokes_and_notifies_channel_and_owner_dm(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_yolo_manifest(home)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07OPS",
        source_channel_name="ops",
        user_id="U07OWNER",
        command_text="yolo off #growth",
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED
    assert manifest.yolo_until is None
    assert manifest.pre_yolo_tier is None
    assert slack.post_calls[0] == {
        "channel": "C07TEAM",
        "text": "YOLO ended by owner",
        "_ts": slack.post_calls[0]["_ts"],
    }
    assert slack.post_calls[1]["channel"] == "D07OWNER"
    assert "YOLO ended on #growth" in slack.post_calls[1]["text"]
    assert "restored to owner-scoped" in slack.post_calls[1]["text"]
    assert slack.ephemeral_calls[-1]["text"] == "Revoked yolo for #growth."


@pytest.mark.asyncio
async def test_yolo_extend_command_rejects_when_total_remaining_would_exceed_cap(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    write_active_yolo_manifest(home, remaining_hours=70)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()
    before = load_manifest(channel_manifest_path("C07TEAM", home))

    result = await handle_engram_command(
        router=router,
        config=make_config(),
        slack_client=slack,
        source_channel_id="C07OPS",
        source_channel_name="ops",
        user_id="U07OWNER",
        command_text="yolo extend C07TEAM 6h",
    )

    after = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is False
    assert result["error"] == "yolo cap exceeded"
    assert slack.ephemeral_calls[-1]["text"] == "Cannot extend beyond 72h total remaining."
    assert after == before
    assert slack.post_calls == []


@pytest.mark.asyncio
async def test_yolo_extend_action_extends_and_confirms(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    write_active_yolo_manifest(home, remaining_hours=24)
    initial = load_manifest(channel_manifest_path("C07TEAM", home))
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_yolo_action(
        payload=yolo_action_payload(action_id="yolo_extend_C07TEAM"),
        router=router,
        config=make_config(),
        slack_client=slack,
        action_kind="extend",
    )

    updated = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert updated.yolo_until == initial.yolo_until + timedelta(hours=6)
    assert slack.post_calls[0]["channel"] == "D07OWNER"
    assert "YOLO extended on #growth by 6h" in slack.post_calls[0]["text"]
    assert slack.ephemeral_calls[0]["text"].startswith("Extended #growth by 6h.")


@pytest.mark.asyncio
async def test_yolo_revoke_action_revokes_and_confirms(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    write_active_yolo_manifest(home)
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_yolo_action(
        payload=yolo_action_payload(action_id="yolo_revoke_C07TEAM"),
        router=router,
        config=make_config(),
        slack_client=slack,
        action_kind="revoke",
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is True
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED
    assert manifest.yolo_until is None
    assert [call["channel"] for call in slack.post_calls] == ["C07TEAM", "D07OWNER"]
    assert slack.ephemeral_calls[0]["text"] == "Revoked yolo for #growth."


def test_cli_yolo_list_empty(cli: CliRunner) -> None:
    result = cli.invoke(cli_app, ["yolo", "list"])

    assert result.exit_code == 0
    assert "No active yolo grants." in result.output


def test_cli_yolo_list_extend_and_off(cli: CliRunner, tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    write_active_yolo_manifest(home)
    initial = load_manifest(channel_manifest_path("C07TEAM", home))

    list_result = cli.invoke(cli_app, ["yolo", "list"])
    extend_result = cli.invoke(cli_app, ["yolo", "extend", "C07TEAM", "6h"])
    updated = load_manifest(channel_manifest_path("C07TEAM", home))
    off_result = cli.invoke(cli_app, ["yolo", "off", "C07TEAM"])
    revoked = load_manifest(channel_manifest_path("C07TEAM", home))

    assert list_result.exit_code == 0
    assert "C07TEAM" in list_result.output
    assert "owner-scoped" in list_result.output
    assert extend_result.exit_code == 0
    assert updated.yolo_until == initial.yolo_until + timedelta(hours=6)
    assert off_result.exit_code == 0
    assert revoked.permission_tier == PermissionTier.OWNER_SCOPED
    assert revoked.yolo_until is None


def test_cli_yolo_extend_rejects_cap_overflow(cli: CliRunner, tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    write_active_yolo_manifest(home, remaining_hours=70)
    before = load_manifest(channel_manifest_path("C07TEAM", home))

    result = cli.invoke(cli_app, ["yolo", "extend", "C07TEAM", "6h"])

    after = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result.exit_code == 2
    assert "Cannot extend beyond 72h total remaining." in result.output
    assert after == before
