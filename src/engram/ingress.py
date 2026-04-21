"""Ingress — Slack Bolt listeners (DMs + allowed channels).

M1: minimal. DMs always answered; team channels answered only if explicitly
in `allowed_channels` from config. Threads are preserved (we reply in-thread
if the incoming message was in a thread).

M2 replaces the allowlist with manifest-driven provisioning.
"""
from __future__ import annotations

import datetime
import logging

from slack_bolt.async_app import AsyncApp

from engram.agent import Agent
from engram.config import EngramConfig
from engram.costs import CostLedger, TurnCost
from engram.egress import post_reply
from engram.router import Router

log = logging.getLogger(__name__)


def register_listeners(
    app: AsyncApp,
    config: EngramConfig,
    router: Router,
    agent: Agent,
    cost_ledger: CostLedger | None = None,
) -> None:
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

        session = await router.get(
            channel_id,
            channel_name=None,  # resolve lazily later
            is_dm=is_dm,
        )

        # M2: manifest-driven gating replaces the M1 allowlist.
        # If the router produced a manifest, use its status. Otherwise
        # fall back to the M1 allowlist behavior so legacy configs keep
        # working.
        if session.manifest is not None:
            if not session.is_active():
                log.info(
                    "ingress.skip session=%s reason=manifest_status_%s",
                    session.label(),
                    session.manifest.status,
                )
                return
        else:
            if not is_dm and channel_id not in config.allowed_channels:
                log.debug(
                    "ingress.skip channel=%s reason=not-allowed", channel_id
                )
                return

        log.info(
            "ingress.received session=%s user=%s len=%d",
            session.label(),
            user_id,
            len(text),
        )

        try:
            turn = await agent.run_turn(session, text)
        except Exception:
            log.exception("ingress.agent_failure session=%s", session.label())
            await say(
                text="Something went wrong on my side. I've logged it.",
                thread_ts=thread_ts,
            )
            return

        egress_result = await post_reply(
            say,
            turn,
            thread_ts=thread_ts if not is_dm else None,
            session_label=session.label(),
        )

        if cost_ledger is not None and turn.cost_usd is not None:
            cost_ledger.record(
                TurnCost(
                    timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
                    session_label=session.label(),
                    channel_id=session.channel_id,
                    is_dm=session.is_dm,
                    cost_usd=turn.cost_usd,
                    duration_ms=turn.duration_ms,
                    num_turns=turn.num_turns,
                    user_text_len=len(text),
                    chunks_posted=egress_result.chunks_posted,
                    is_error=turn.is_error,
                )
            )

    @app.event("app_mention")
    async def on_mention(event, say):
        # Treat mentions in allowed channels the same as regular messages.
        # Bolt will also fire on_message for these, so we dedupe here by
        # just logging; the message handler above does the real work.
        log.debug("ingress.app_mention channel=%s user=%s", event.get("channel"), event.get("user"))
