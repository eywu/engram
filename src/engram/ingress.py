"""Ingress — Slack Bolt listeners (DMs + allowed channels).

M1: minimal. DMs always answered; team channels answered only if explicitly
in `allowed_channels` from config. Threads are preserved (we reply in-thread
if the incoming message was in a thread).

M2 replaces the allowlist with manifest-driven provisioning.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
from decimal import Decimal

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from slack_bolt.async_app import AsyncApp

from engram.agent import Agent
from engram.config import EngramConfig
from engram.costs import CostLedger, TurnCost
from engram.egress import _suggestion_label, post_reply, update_question_resolved
from engram.router import Router

log = logging.getLogger(__name__)
hitl_log = logging.getLogger("engram.hitl")
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
HITL_ACTION_ID_PATTERN = re.compile(r"^hitl_choice_(?:\d+|deny)$")


def register_listeners(
    app: AsyncApp,
    config: EngramConfig,
    router: Router,
    agent: Agent,
    cost_ledger: CostLedger | None = None,
) -> None:
    """Attach message/app_mention handlers to a Bolt AsyncApp."""

    @app.action(HITL_ACTION_ID_PATTERN)
    async def on_hitl_action(ack, body, client):
        await ack()
        try:
            result = await handle_block_action(body, router, client)
            if not result.get("ok"):
                log.warning(
                    "ingress.hitl_action_failed error=%s",
                    result.get("error", "unknown"),
                )
        except Exception:
            log.exception("ingress.hitl_action_handler_failed")

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

        if event.get("thread_ts") and any(
            q.slack_thread_ts == event.get("thread_ts")
            for q in router.hitl.pending_for_channel(channel_id)
        ):
            await handle_thread_reply(event, router, client)
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


async def handle_block_action(payload: dict, router, slack_client) -> dict:
    """Handle Slack block_actions event. Must return ACK within 3 seconds."""
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "error": "no actions"}

    action = actions[0]
    value = action.get("value", "")
    if "|" not in value:
        return {"ok": False, "error": "malformed value"}

    permission_request_id, choice_key = value.split("|", 1)
    q = router.hitl.get_by_id(permission_request_id)
    if q is None:
        return {"ok": False, "error": "question not found (may be resolved)"}
    if q.future.done():
        return {"ok": True, "info": "already resolved"}

    clicker_user_id = payload.get("user", {}).get("id")
    if q.who_can_answer and clicker_user_id != q.who_can_answer:
        return {"ok": False, "error": "not authorized"}

    task = asyncio.create_task(
        _resolve_block_action(q, choice_key, router, slack_client)
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"ok": True}


async def _resolve_block_action(q, choice_key, router, slack_client) -> None:
    try:
        if choice_key == "deny":
            result = PermissionResultDeny(message="user denied")
            answer_text = "Deny"
        else:
            try:
                idx = int(choice_key)
                suggestion = q.suggestions[idx]
                result = PermissionResultAllow(
                    updated_input=q.tool_input,
                    updated_permissions=(
                        [suggestion] if hasattr(suggestion, "to_dict") else None
                    ),
                )
                answer_text = _suggestion_label(suggestion)
            except (ValueError, IndexError):
                result = PermissionResultAllow()
                answer_text = "Allow"

        resolved = router.hitl.resolve(q.permission_request_id, result)
        if resolved:
            hitl_log.info(
                "hitl.answer_received",
                extra={
                    "permission_request_id": q.permission_request_id,
                    "choice": choice_key,
                    "decision": _hitl_decision_label(result),
                },
            )
        await update_question_resolved(q, answer_text, slack_client)
    except Exception:
        log.exception("resolve_block_action failed")


async def handle_thread_reply(event: dict, router, slack_client) -> None:
    """Resolve a pending question when a Slack thread reply provides an answer."""
    thread_ts = event.get("thread_ts")
    channel_id = event.get("channel")
    reply_text = event.get("text", "")
    if not thread_ts or not channel_id or not reply_text:
        return

    candidates = [
        q
        for q in router.hitl.pending_for_channel(channel_id)
        if q.slack_thread_ts == thread_ts
    ]
    if not candidates:
        return

    q = candidates[0]
    if q.future.done():
        return

    replier_user_id = event.get("user")
    if q.who_can_answer and replier_user_id != q.who_can_answer:
        return

    result = PermissionResultAllow(
        updated_input={**q.tool_input, "_user_answer": reply_text}
    )
    resolved = router.hitl.resolve(q.permission_request_id, result)
    if resolved:
        hitl_log.info(
            "hitl.answer_received",
            extra={
                "permission_request_id": q.permission_request_id,
                "choice": "thread_reply",
                "decision": "allow",
            },
        )
    await update_question_resolved(q, reply_text[:80], slack_client)


def _hitl_decision_label(result) -> str:
    if isinstance(result, PermissionResultAllow):
        return "allow"
    if isinstance(result, PermissionResultDeny):
        return "deny"
    raise TypeError(f"Unknown PermissionResult type: {type(result)}")


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
