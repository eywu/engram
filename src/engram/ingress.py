"""Ingress — Slack Bolt listeners (DMs + allowed channels).

M1: minimal. DMs always answered; team channels answered only if explicitly
in `allowed_channels` from config. Threads are preserved (we reply in-thread
if the incoming message was in a thread).

M2 replaces the allowlist with manifest-driven provisioning.
"""
from __future__ import annotations

import logging

from slack_bolt.async_app import AsyncApp

from engram.agent import Agent
from engram.config import EngramConfig
from engram.egress import post_reply
from engram.router import Router

log = logging.getLogger(__name__)


def register_listeners(app: AsyncApp, config: EngramConfig, router: Router, agent: Agent) -> None:
    """Attach message/app_mention handlers to a Bolt AsyncApp."""

    @app.event("message")
    async def on_message(event, say, client):
        # Ignore bot messages, message_changed, etc.
        if event.get("subtype") is not None:
            return
        if event.get("bot_id"):
            return

        channel_id = event.get("channel")
        channel_type = event.get("channel_type")  # "im" for DMs
        user_id = event.get("user")
        text = (event.get("text") or "").strip()
        ts = event.get("ts")
        thread_ts = event.get("thread_ts") or ts

        if not channel_id or not text:
            return

        is_dm = channel_type == "im"
        if not is_dm and channel_id not in config.allowed_channels:
            # Not a DM and not in our allowlist — stay quiet.
            log.debug("ingress.skip channel=%s reason=not-allowed", channel_id)
            return

        session = await router.get(
            channel_id,
            channel_name=None,  # resolve lazily later
            is_dm=is_dm,
        )

        log.info(
            "ingress.received session=%s user=%s len=%d",
            session.label(),
            user_id,
            len(text),
        )

        # Serialize concurrent messages per-channel.
        async with session.lock:
            try:
                turn = await agent.run_turn(session, text)
            except Exception:
                log.exception("ingress.agent_failure session=%s", session.label())
                await say(
                    text="Something went wrong on my side. I've logged it.",
                    thread_ts=thread_ts,
                )
                return

            await post_reply(
                say,
                turn,
                thread_ts=thread_ts if not is_dm else None,
                session_label=session.label(),
            )

    @app.event("app_mention")
    async def on_mention(event, say):
        # Treat mentions in allowed channels the same as regular messages.
        # Bolt will also fire on_message for these, so we dedupe here by
        # just logging; the message handler above does the real work.
        log.debug("ingress.app_mention channel=%s user=%s", event.get("channel"), event.get("user"))
