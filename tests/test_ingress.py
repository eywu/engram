"""Ingress tests for pending-channel discoverability."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from engram.agent import AgentTurn
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.ingress import register_listeners
from engram.manifest import ChannelStatus, load_manifest
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
        self.conversations_info_calls: list[str] = []
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
        self.conversations_info_calls.append(channel)
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


def message_handler(app: DecoratorApp):
    return next(handler for event, handler in app.events if event == "message")


@pytest.mark.asyncio
async def test_pending_channel_posts_ack_exactly_once(tmp_path: Path):
    home = tmp_path / ".engram"
    app = DecoratorApp()
    agent = FakeAgent()
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()
    register_listeners(app, make_config(), router, agent)
    handler = message_handler(app)
    say_calls = []

    async def say(*, text, thread_ts=None):
        say_calls.append({"text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": "1713800000.999999"}

    async def fire(ts: str) -> None:
        await handler(
            event={
                "channel": "C07TEST123",
                "channel_type": "channel",
                "user": "U07REQUESTER",
                "text": "<@B07TEST> hey engram",
                "ts": ts,
            },
            say=say,
            client=slack,
        )

    await asyncio.gather(
        fire("1713800000.000100"),
        fire("1713800001.000100"),
        fire("1713800002.000100"),
    )

    manifest = load_manifest(channel_manifest_path("C07TEST123", home))

    assert manifest.status == ChannelStatus.PENDING
    assert manifest.acknowledged_pending is True
    assert len(slack.ephemeral_calls) == 1
    assert slack.ephemeral_calls[0]["channel"] == "C07TEST123"
    assert slack.ephemeral_calls[0]["user"] == "U07REQUESTER"
    assert (
        slack.ephemeral_calls[0]["text"]
        == "👋 I've been added to this channel but I'm waiting for my operator to approve me.\n"
        "An approval request has been sent to the owner. I'll respond once they approve."
    )
    assert len(slack.post_calls) == 1
    assert slack.post_calls[0]["channel"] == "D07OWNER"
    assert agent.calls == []
    assert say_calls == []
