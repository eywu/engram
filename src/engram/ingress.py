"""Ingress — Slack Bolt listeners (DMs + allowed channels).

M1: minimal. DMs always answered; team channels answered only if explicitly
in `allowed_channels` from config. Threads are preserved (we reply in-thread
if the incoming message was in a thread).

M2 replaces the allowlist with manifest-driven provisioning.
"""
from __future__ import annotations

import datetime
import logging
from decimal import Decimal

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
            turn = await agent.run_turn(session, text, user_id=user_id)
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

        await _send_budget_warnings(
            client,
            config,
            turn,
            channel_id=session.channel_id,
        )

        if cost_ledger is not None and turn.cost_usd is not None:
            cost_ledger.record(
                TurnCost(
                    timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
                    session_label=session.label(),
                    session_id=session.session_id,
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


async def _send_budget_warnings(client, config: EngramConfig, turn, *, channel_id: str) -> None:
    if not turn.budget_warnings:
        return
    if not config.owner_dm_channel_id:
        log.warning(
            "budget.warning_no_owner_dm channel=%s thresholds=%s",
            channel_id,
            ",".join(_format_threshold(t) for t in turn.budget_warnings),
        )
        return

    for threshold in turn.budget_warnings:
        text = _budget_warning_text(
            threshold,
            month_to_date=turn.budget_month_to_date_usd,
            monthly_cap=turn.budget_monthly_cap_usd,
            channel_id=channel_id,
        )
        try:
            await client.chat_postMessage(
                channel=config.owner_dm_channel_id,
                text=text,
            )
        except Exception:
            log.warning(
                "budget.warning_dm_failed owner_dm=%s channel=%s threshold=%s",
                config.owner_dm_channel_id,
                channel_id,
                _format_threshold(threshold),
                exc_info=True,
            )


def _budget_warning_text(
    threshold: Decimal,
    *,
    month_to_date: Decimal | None,
    monthly_cap: Decimal | None,
    channel_id: str,
) -> str:
    pct = _format_threshold(threshold)
    mtd = _format_money(month_to_date) if month_to_date is not None else "unknown"
    cap = _format_money(monthly_cap) if monthly_cap is not None else "unknown"
    return (
        f"Budget warning: Engram monthly spend crossed {pct} "
        f"({mtd} of {cap}). Channel: {channel_id}. Service is still running."
    )


def _format_threshold(threshold: Decimal) -> str:
    pct = (threshold * Decimal("100")).quantize(Decimal("1"))
    return f"{pct}%"


def _format_money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01'))}"
