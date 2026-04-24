from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from engram.agent import AgentTurn
from engram.bootstrap import provision_channel
from engram.cli_channels import app as channels_app
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.ingress import register_listeners
from engram.manifest import (
    YOLO_DEFAULT_DURATION,
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
    dump_manifest,
    load_manifest,
)
from engram.nightly.apply import ApplyResult
from engram.nightly.harvest import HarvestResult
from engram.nightly.pipeline import run_nightly_pipeline
from engram.nightly.synthesize import SynthesisResult
from engram.nightly.yolo import sweep_expired_yolo
from engram.paths import channel_manifest_path
from engram.router import Router
from engram.telemetry import write_json


class DecoratorApp:
    def __init__(self) -> None:
        self.actions = []
        self.commands = []
        self.events = []
        self.views = []
        self.view_closed_handlers = []

    def action(self, pattern):
        def decorator(func):
            self.actions.append((pattern, func))
            return func

        return decorator

    def command(self, command_name):
        def decorator(func):
            self.commands.append((command_name, func))
            return func

        return decorator

    def event(self, event_name):
        def decorator(func):
            self.events.append((event_name, func))
            return func

        return decorator

    def view(self, callback_id):
        def decorator(func):
            self.views.append((callback_id, func))
            return func

        return decorator

    def view_closed(self, callback_id):
        def decorator(func):
            self.view_closed_handlers.append((callback_id, func))
            return func

        return decorator


class FakeSlackClient:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.chat_postMessage = self._chat_post_message

    async def _chat_post_message(self, **kwargs):
        ts = f"1713800000.{len(self.post_calls) + 100:06d}"
        self.post_calls.append({**kwargs, "_ts": ts})
        return {"ok": True, "ts": ts}

    async def chat_update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"ok": True}


class FakeAgent:
    def __init__(self) -> None:
        self.permission_tiers_seen: list[PermissionTier | None] = []

    async def run_turn(self, session, text, *, user_id=None):
        tier = session.manifest.permission_tier if session.manifest is not None else None
        self.permission_tiers_seen.append(tier)
        return AgentTurn(
            text="bot reply",
            cost_usd=None,
            duration_ms=1,
            num_turns=1,
            is_error=False,
        )


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("HOME", str(tmp_path))
    return CliRunner()


def _config(owner_dm_channel_id: str = "D07OWNER") -> EngramConfig:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )
    cfg.owner_dm_channel_id = owner_dm_channel_id
    return cfg


def _message_handler(app: DecoratorApp):
    return next(handler for event, handler in app.events if event == "message")


def _seed_expired_yolo_manifest(
    *,
    home: Path,
    channel_id: str = "C07TEST123",
    label: str = "growth",
    now: datetime,
) -> Path:
    provision_channel(
        channel_id,
        identity=IdentityTemplate.TASK_ASSISTANT,
        label=label,
        home=home,
    )
    manifest_path = channel_manifest_path(channel_id, home)
    manifest = load_manifest(manifest_path).model_copy(
        update={
            "status": ChannelStatus.ACTIVE,
            "permission_tier": PermissionTier.YOLO,
            "yolo_granted_at": now - YOLO_DEFAULT_DURATION - timedelta(minutes=5),
            "yolo_until": now - timedelta(minutes=5),
            "pre_yolo_tier": PermissionTier.OWNER_SCOPED,
        }
    )
    dump_manifest(manifest, manifest_path)
    return manifest_path


@pytest.mark.parametrize("duration_text, expected_hours", [("6h", 6), ("24h", 24), ("72h", 72)])
def test_cli_upgrade_yolo_sets_fields_for_supported_durations(
    cli: CliRunner,
    tmp_path: Path,
    duration_text: str,
    expected_hours: int,
) -> None:
    home = tmp_path / ".engram"
    provision_channel(
        "C07TEST123",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="growth",
        home=home,
    )

    result = cli.invoke(
        channels_app,
        ["upgrade", "C07TEST123", "yolo", "--until", duration_text],
    )

    assert result.exit_code == 0
    manifest = load_manifest(channel_manifest_path("C07TEST123", home))
    assert manifest.permission_tier == PermissionTier.YOLO
    assert manifest.pre_yolo_tier == PermissionTier.TASK_ASSISTANT
    assert manifest.yolo_granted_at is not None
    assert manifest.yolo_until is not None
    assert manifest.yolo_until - manifest.yolo_granted_at == timedelta(hours=expected_hours)


def test_cli_upgrade_yolo_extends_active_window_without_resetting_pre_yolo_tier(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    provision_channel(
        "C07TEST123",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="growth",
        home=home,
    )

    first = cli.invoke(channels_app, ["upgrade", "C07TEST123", "yolo"])
    assert first.exit_code == 0
    initial = load_manifest(channel_manifest_path("C07TEST123", home))
    initial_granted_at = initial.yolo_granted_at
    initial_until = initial.yolo_until
    assert initial_granted_at is not None
    assert initial_until is not None
    assert initial_until - initial_granted_at == timedelta(hours=24)

    second = cli.invoke(
        channels_app,
        ["upgrade", "C07TEST123", "yolo", "--until", "6h"],
    )
    assert second.exit_code == 0

    updated = load_manifest(channel_manifest_path("C07TEST123", home))
    assert updated.permission_tier == PermissionTier.YOLO
    assert updated.pre_yolo_tier == PermissionTier.OWNER_SCOPED
    assert updated.yolo_granted_at == initial_granted_at
    assert updated.yolo_until == initial_until + timedelta(hours=6)


def test_cli_upgrade_yolo_rejects_out_of_range_duration(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    provision_channel(
        "C07TEST123",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="growth",
        home=home,
    )

    result = cli.invoke(
        channels_app,
        ["upgrade", "C07TEST123", "yolo", "--until", "30d"],
    )

    assert result.exit_code == 2
    assert "yolo upgrades must use a bounded duration of 6h, 24h, or 72h" in result.output


@pytest.mark.asyncio
async def test_lazy_yolo_expiry_demotes_notifies_and_sweep_becomes_noop(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / ".engram"
    now = datetime(2026, 4, 23, 15, 0, tzinfo=UTC)
    _seed_expired_yolo_manifest(home=home, now=now)

    app = DecoratorApp()
    slack = FakeSlackClient()
    agent = FakeAgent()
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    register_listeners(app, _config(), router, agent)
    handler = _message_handler(app)

    say_calls: list[dict[str, Any]] = []

    async def say(*, text, thread_ts=None):
        say_calls.append({"text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": "1713800000.999999"}

    with caplog.at_level("INFO"):
        await handler(
            event={
                "channel": "C07TEST123",
                "channel_type": "channel",
                "user": "U07REQUESTER",
                "text": "<@B07TEST> hey engram",
                "ts": "1713800000.000100",
            },
            say=say,
            client=slack,
        )

    manifest = load_manifest(channel_manifest_path("C07TEST123", home))
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED
    assert manifest.yolo_granted_at is None
    assert manifest.yolo_until is None
    assert manifest.pre_yolo_tier is None
    assert agent.permission_tiers_seen == [PermissionTier.OWNER_SCOPED]
    assert say_calls == []

    dm_call = next(call for call in slack.post_calls if call["channel"] == "D07OWNER")
    assert "YOLO expired on #growth" in dm_call["blocks"][0]["text"]["text"]
    assert "reverted to trusted" in dm_call["blocks"][0]["text"]["text"]
    assert "Duration used: 24h 0m." in dm_call["blocks"][0]["text"]["text"]
    assert dm_call["blocks"][1]["type"] == "context"
    assert "/engram upgrade C07TEST123 yolo --until 24h" in dm_call["blocks"][1]["elements"][0]["text"]

    reply_call = next(call for call in slack.post_calls if call["channel"] == "C07TEST123")
    assert reply_call["blocks"][0]["text"] == "bot reply"
    assert "channel.yolo_expired" in caplog.text
    assert "trigger=lazy" in caplog.text
    assert "channel.yolo_demoted" in caplog.text

    results = await sweep_expired_yolo(
        home=home,
        now=now,
        slack_client=slack,
        owner_dm_channel_id="D07OWNER",
    )
    assert results == []
    assert len(slack.post_calls) == 2


@pytest.mark.asyncio
async def test_nightly_sweep_demotes_idle_channel_and_posts_dm(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / ".engram"
    now = datetime(2026, 4, 23, 15, 0, tzinfo=UTC)
    _seed_expired_yolo_manifest(home=home, now=now)
    slack = FakeSlackClient()

    with caplog.at_level("INFO"):
        results = await sweep_expired_yolo(
            home=home,
            now=now,
            slack_client=slack,
            owner_dm_channel_id="D07OWNER",
        )

    assert len(results) == 1
    manifest = load_manifest(channel_manifest_path("C07TEST123", home))
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED
    assert manifest.yolo_until is None
    assert len(slack.post_calls) == 1
    assert slack.post_calls[0]["channel"] == "D07OWNER"
    assert "Duration used: 24h 0m." in slack.post_calls[0]["blocks"][0]["text"]["text"]
    assert "/engram upgrade C07TEST123 yolo --until 24h" in slack.post_calls[0]["blocks"][1]["elements"][0]["text"]
    assert "trigger=sweep" in caplog.text


@pytest.mark.asyncio
async def test_run_nightly_pipeline_calls_yolo_sweep_before_harvest(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("nightly:\n  min_evidence: 1\n", encoding="utf-8")
    events: list[str] = []

    async def fake_yolo_sweep(**kwargs: Any) -> list[object]:
        events.append("sweep")
        return []

    def fake_harvest(**kwargs: Any) -> HarvestResult:
        events.append("harvest")
        payload = {
            "date": "2026-04-23",
            "channels": [],
            "skipped_channels": [],
        }
        path = kwargs["output_root"] / "2026-04-23" / "harvest.json"
        write_json(path, payload)
        return HarvestResult(output_path=path, payload=payload)

    async def fake_synthesize(
        harvest_json: Path,
        *,
        output_root: Path,
        **_: Any,
    ) -> SynthesisResult:
        events.append("synthesize")
        payload = {
            "schema_version": 1,
            "date": "2026-04-23",
            "channels": [],
            "skipped_channels": [],
            "totals": {"cost_usd": "0.000000"},
        }
        path = output_root / "2026-04-23" / "synthesis.json"
        write_json(path, payload)
        return SynthesisResult(output_path=path, payload=payload)

    async def fake_apply(synthesis_json: Path, **_: Any) -> ApplyResult:
        events.append("apply")
        payload = json.loads(synthesis_json.read_text(encoding="utf-8"))
        return ApplyResult(
            output_path=None,
            rows_written=0,
            rows_queued=0,
            dry_run=False,
            payload=payload,
        )

    await run_nightly_pipeline(
        target_date=date(2026, 4, 23),
        db_path=tmp_path / "memory.db",
        output_root=tmp_path / "nightly",
        config_path=config_path,
        clock=lambda: datetime(2026, 4, 23, 23, tzinfo=UTC),
        yolo_sweep_func=fake_yolo_sweep,
        harvest_func=fake_harvest,
        synthesize_func=fake_synthesize,
        apply_func=fake_apply,
    )

    assert events == ["sweep", "harvest", "synthesize", "apply"]
