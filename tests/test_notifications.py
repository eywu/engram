"""Tests for pending-channel approval notifications."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from engram.agent import AgentTurn
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.ingress import register_listeners
from engram.manifest import ChannelStatus, load_manifest
from engram.notifications import handle_pending_channel_action
from engram.paths import channel_manifest_path
from engram.router import Router


class DecoratorApp:
    def __init__(self) -> None:
        self.actions = []
        self.commands = []
        self.events = []

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

    async def conversations_info(self, *, channel):
        return {"ok": True, "channel": {"name": "engram-self"}}


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, ChannelStatus | None]] = []

    async def run_turn(self, session, text, *, user_id=None):
        self.calls.append(
            (
                session.channel_id,
                text,
                user_id,
                session.manifest.status if session.manifest is not None else None,
            )
        )
        return AgentTurn(
            text="bot reply",
            cost_usd=None,
            duration_ms=1,
            num_turns=1,
            is_error=False,
        )


def make_config(owner_dm_channel_id: str = "D07OWNER") -> EngramConfig:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )
    cfg.owner_dm_channel_id = owner_dm_channel_id
    return cfg


def pending_action_payload(
    action_id: str,
    *,
    value: str,
    owner_dm_channel_id: str = "D07OWNER",
    message_ts: str = "1713800000.000200",
    user_id: str = "U07OWNER",
) -> dict[str, Any]:
    return {
        "actions": [{"action_id": action_id, "value": value}],
        "channel": {"id": owner_dm_channel_id},
        "container": {"message_ts": message_ts},
        "message": {"ts": message_ts},
        "user": {"id": user_id},
    }


def message_handler(app: DecoratorApp):
    return next(handler for event, handler in app.events if event == "message")


def pending_action_handler(app: DecoratorApp):
    return next(
        handler
        for pattern, handler in app.actions
        if pattern.match("pending_channel_approve")
    )


def action_value(
    *,
    channel_id: str = "C07TEST123",
    source_thread_ts: str | None = "1713800000.000100",
) -> str:
    return json.dumps(
        {
            "channel_id": channel_id,
            "source_thread_ts": source_thread_ts,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


async def wait_until(predicate) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1
    while not predicate():
        if loop.time() > deadline:
            pytest.fail("condition was not met before timeout")
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_approve_button_activates_channel_and_invalidates_cache(tmp_path: Path):
    home = tmp_path / ".engram"
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()
    cached = await router.get("C07TEST123", channel_name="#engram-self", is_dm=False)

    result = await handle_pending_channel_action(
        pending_action_payload(
            "pending_channel_approve",
            value=action_value(),
        ),
        router,
        slack,
    )

    reloaded = await router.get("C07TEST123", channel_name="#engram-self", is_dm=False)
    manifest = load_manifest(channel_manifest_path("C07TEST123", home))

    assert result["ok"] is True
    assert manifest.status == ChannelStatus.ACTIVE
    assert cached is not reloaded
    assert reloaded.manifest.status == ChannelStatus.ACTIVE
    assert len(slack.post_calls) == 1
    assert slack.post_calls[0]["channel"] == "C07TEST123"
    assert slack.post_calls[0]["thread_ts"] == "1713800000.000100"
    assert slack.post_calls[0]["text"] == "✅ Approved by <@U07OWNER>. Standing by."
    assert len(slack.update_calls) == 1
    assert slack.update_calls[0]["channel"] == "D07OWNER"


@pytest.mark.asyncio
async def test_deny_button_denies_channel_without_posting_in_channel(tmp_path: Path):
    home = tmp_path / ".engram"
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()
    cached = await router.get("C07TEST123", channel_name="#engram-self", is_dm=False)

    result = await handle_pending_channel_action(
        pending_action_payload(
            "pending_channel_deny",
            value=action_value(),
        ),
        router,
        slack,
    )

    reloaded = await router.get("C07TEST123", channel_name="#engram-self", is_dm=False)
    manifest = load_manifest(channel_manifest_path("C07TEST123", home))

    assert result["ok"] is True
    assert manifest.status == ChannelStatus.DENIED
    assert cached is not reloaded
    assert reloaded.manifest.status == ChannelStatus.DENIED
    assert slack.post_calls == []
    assert len(slack.update_calls) == 1
    assert slack.update_calls[0]["channel"] == "D07OWNER"


@pytest.mark.asyncio
async def test_pending_channel_full_bounce_without_restart(tmp_path: Path):
    home = tmp_path / ".engram"
    app = DecoratorApp()
    agent = FakeAgent()
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()
    register_listeners(app, make_config(), router, agent)
    on_message = message_handler(app)
    on_pending_action = pending_action_handler(app)
    say_calls = []
    ack_calls = 0

    async def say(*, text, thread_ts=None):
        say_calls.append({"text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": "1713800000.999999"}

    async def ack():
        nonlocal ack_calls
        ack_calls += 1

    await on_message(
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

    assert len(slack.ephemeral_calls) == 1
    assert len(slack.post_calls) == 1
    assert slack.post_calls[0]["channel"] == "D07OWNER"
    assert agent.calls == []

    approve_value = slack.post_calls[0]["blocks"][2]["elements"][0]["value"]
    owner_dm_ts = slack.post_calls[0]["_ts"]
    await on_pending_action(
        ack=ack,
        body=pending_action_payload(
            "pending_channel_approve",
            value=approve_value,
            message_ts=owner_dm_ts,
        ),
        client=slack,
    )
    await wait_until(
        lambda: (
            load_manifest(channel_manifest_path("C07TEST123", home)).status
            == ChannelStatus.ACTIVE
            and len(slack.post_calls) == 2
            and len(slack.update_calls) == 1
        )
    )

    await on_message(
        event={
            "channel": "C07TEST123",
            "channel_type": "channel",
            "user": "U07REQUESTER",
            "text": "<@B07TEST> what changed?",
            "ts": "1713800001.000100",
        },
        say=say,
        client=slack,
    )

    assert ack_calls == 1
    assert len(slack.ephemeral_calls) == 1
    assert len(agent.calls) == 1
    assert agent.calls[0] == (
        "C07TEST123",
        "<@B07TEST> what changed?",
        "U07REQUESTER",
        ChannelStatus.ACTIVE,
    )
    assert say_calls == []
    assert len(slack.post_calls) == 3
    agent_post = slack.post_calls[2]
    assert agent_post["channel"] == "C07TEST123"
    assert agent_post["blocks"] == [{"type": "markdown", "text": "bot reply"}]
    assert agent_post["text"] == "bot reply"
