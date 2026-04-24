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
import uuid
from dataclasses import dataclass
from decimal import Decimal

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from slack_bolt.async_app import AsyncApp

from engram import paths
from engram.agent import Agent
from engram.config import EngramConfig
from engram.costs import CostLedger, TurnCost
from engram.egress import (
    _always_allow_label,
    _suggestion_label,
    post_meta_eligibility_question,
    post_reply,
    update_question_resolved,
)
from engram.hitl import PendingQuestion, _resolve_question
from engram.manifest import (
    ChannelStatus,
    ManifestError,
    dump_manifest,
    load_manifest,
)
from engram.notifications import (
    PENDING_CHANNEL_ACTION_ID_PATTERN,
    handle_pending_channel_action,
    notify_pending_channel,
    post_pending_channel_ack,
)
from engram.router import Router

log = logging.getLogger(__name__)
hitl_log = logging.getLogger("engram.hitl")
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
HITL_ACTION_ID_PATTERN = re.compile(r"^hitl_choice_(?:\d+|always_\d+|deny)$")
CHANNEL_REF_PATTERN = re.compile(r"<#(?P<id>[A-Z0-9]+)(?:\|[^>]+)?>")
CHANNEL_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+$")


@dataclass(frozen=True)
class MetaEligibilityCommand:
    eligible: bool
    target: str | None


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

    @app.action(PENDING_CHANNEL_ACTION_ID_PATTERN)
    async def on_pending_channel_action(ack, body, client):
        await ack()

        async def _run() -> None:
            try:
                result = await handle_pending_channel_action(
                    body,
                    router,
                    client,
                )
                if not result.get("ok"):
                    log.warning(
                        "ingress.pending_channel_action_failed error=%s",
                        result.get("error", "unknown"),
                    )
            except Exception:
                log.exception("ingress.pending_channel_action_handler_failed")

        task = asyncio.create_task(_run())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    @app.command("/exclude-from-nightly")
    async def on_exclude_from_nightly(ack, body, client):
        await ack()
        try:
            await handle_meta_eligibility_command(
                router=router,
                config=config,
                slack_client=client,
                source_channel_id=str(body.get("channel_id") or ""),
                source_channel_name=body.get("channel_name"),
                user_id=body.get("user_id"),
                eligible=False,
                target_text=body.get("text"),
            )
        except Exception:
            log.exception("ingress.meta_exclusion_command_failed")

    @app.command("/include-in-nightly")
    async def on_include_in_nightly(ack, body, client):
        await ack()
        try:
            await handle_meta_eligibility_command(
                router=router,
                config=config,
                slack_client=client,
                source_channel_id=str(body.get("channel_id") or ""),
                source_channel_name=body.get("channel_name"),
                user_id=body.get("user_id"),
                eligible=True,
                target_text=body.get("text"),
            )
        except Exception:
            log.exception("ingress.meta_inclusion_command_failed")

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

        parsed_meta_command = parse_meta_eligibility_command(text)
        if parsed_meta_command is not None:
            await handle_meta_eligibility_command(
                router=router,
                config=config,
                slack_client=client,
                source_channel_id=channel_id,
                source_channel_name=session.channel_name,
                user_id=user_id,
                eligible=parsed_meta_command.eligible,
                target_text=parsed_meta_command.target,
            )
            return

        # M2: manifest-driven gating replaces the M1 allowlist.
        # If the router produced a manifest, use its status. Otherwise
        # fall back to the M1 allowlist behavior so legacy configs keep
        # working.
        if session.manifest is not None:
            if session.manifest.status == ChannelStatus.PENDING:
                async with session.agent_lock:
                    manifest = session.manifest
                    if (
                        manifest is not None
                        and manifest.status == ChannelStatus.PENDING
                        and not manifest.acknowledged_pending
                    ):
                        channel_label = await _resolve_pending_channel_label(
                            client,
                            channel_id=channel_id,
                            is_dm=is_dm,
                            fallback=manifest.label or session.channel_name or channel_id,
                        )
                        if channel_label:
                            session.channel_name = channel_label
                        await post_pending_channel_ack(
                            client,
                            channel_id=channel_id,
                            user_id=user_id,
                            thread_ts=thread_ts,
                            owner_dm_channel_id=(
                                config.owner_dm_channel_id
                                or router.owner_dm_channel_id
                            ),
                        )
                        await notify_pending_channel(
                            slack_client=client,
                            owner_dm_channel_id=(
                                config.owner_dm_channel_id
                                or router.owner_dm_channel_id
                            ),
                            channel_id=channel_id,
                            channel_label=channel_label or channel_id,
                            invited_by_user_id=user_id,
                            template=manifest.identity.value,
                            first_message=text,
                            source_thread_ts=thread_ts,
                        )
                        updated_manifest = manifest.model_copy(
                            update={"acknowledged_pending": True}
                        )
                        if router.home is not None:
                            dump_manifest(
                                updated_manifest,
                                paths.channel_manifest_path(channel_id, router.home),
                            )
                        router.replace_cached_manifest(updated_manifest)
                log.info(
                    "ingress.skip session=%s reason=manifest_status_pending",
                    session.label(),
                )
                return
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
            client,
            session.channel_id,
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
    parts = value.split("|", 2)
    if len(parts) < 2:
        return {"ok": False, "error": "malformed value"}

    permission_request_id, choice_key = parts[0], parts[1]
    tool_name = parts[2] if len(parts) == 3 else None
    q = router.hitl.get_by_id(permission_request_id)
    if q is None:
        return {"ok": False, "error": "question not found (may be resolved)"}
    if q.future.done():
        return {"ok": True, "info": "already resolved"}

    clicker_user_id = payload.get("user", {}).get("id")
    if q.who_can_answer and clicker_user_id != q.who_can_answer:
        return {"ok": False, "error": "not authorized"}

    task = asyncio.create_task(
        _resolve_block_action(
            q,
            choice_key,
            router,
            slack_client,
            tool_name=tool_name,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"ok": True}


async def handle_meta_eligibility_command(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    source_channel_id: str,
    source_channel_name: str | None,
    user_id: str | None,
    eligible: bool,
    target_text: str | None,
) -> dict[str, object]:
    """Create an owner-DM HITL card for OQ31 nightly meta eligibility changes."""
    if not source_channel_id:
        return {"ok": False, "error": "missing source channel"}

    home = router.home or paths.engram_home()
    target_channel_id = _resolve_meta_target(
        target_text,
        source_channel_id=source_channel_id,
        home=home,
    )
    if target_channel_id is None:
        return {"ok": False, "error": "target channel not found"}

    if target_channel_id == source_channel_id:
        await router.get(
            target_channel_id,
            channel_name=_channel_label_from_name(source_channel_name),
            is_dm=target_channel_id.startswith("D"),
        )

    manifest_path = paths.channel_manifest_path(target_channel_id, home)
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        log.warning(
            "ingress.meta_eligibility_manifest_missing channel=%s error=%s",
            target_channel_id,
            exc,
        )
        return {"ok": False, "error": "manifest not found"}

    owner_dm_channel_id = config.owner_dm_channel_id or router.owner_dm_channel_id
    if not owner_dm_channel_id:
        log.warning(
            "ingress.meta_eligibility_no_owner_dm source=%s target=%s",
            source_channel_id,
            target_channel_id,
        )
        return {"ok": False, "error": "owner DM channel is not configured"}

    async def apply_confirmed_decision(result) -> None:
        if not isinstance(result, PermissionResultAllow):
            return
        latest = load_manifest(manifest_path)
        updated = latest.model_copy(update={"meta_eligible": eligible})
        dump_manifest(updated, manifest_path)
        router.replace_cached_manifest(updated)
        log.info(
            "ingress.meta_eligibility_updated channel=%s meta_eligible=%s",
            target_channel_id,
            eligible,
        )

    command_name = "include-in-nightly" if eligible else "exclude-from-nightly"
    q = PendingQuestion(
        permission_request_id=str(uuid.uuid4()),
        channel_id=owner_dm_channel_id,
        session_id=f"meta-eligibility:{target_channel_id}",
        turn_id=str(uuid.uuid4()),
        tool_name=command_name,
        tool_input={
            "channel_id": target_channel_id,
            "source_channel_id": source_channel_id,
            "requested_by": user_id,
            "meta_eligible": eligible,
        },
        suggestions=[{"name": "Confirm"}],
        who_can_answer=None,
        posted_at=datetime.datetime.now(datetime.UTC),
        timeout_s=config.hitl.timeout_s,
        on_resolve=apply_confirmed_decision,
    )
    router.hitl.register(q)

    # OQ31 locked decision: cards are always sent to the owner DM, even when
    # the command originates in a channel, so approvals have one consistent
    # operator-facing surface.
    channel_ts, thread_ts = await post_meta_eligibility_question(
        q,
        slack_client,
        channel_label=_manifest_display_label(manifest),
        eligible=eligible,
    )
    q.slack_channel_ts = channel_ts
    q.slack_thread_ts = thread_ts
    log.info(
        "ingress.meta_eligibility_question_posted source=%s target=%s owner_dm=%s eligible=%s",
        source_channel_id,
        target_channel_id,
        owner_dm_channel_id,
        eligible,
    )
    return {"ok": True, "permission_request_id": q.permission_request_id}


def parse_meta_eligibility_command(text: str) -> MetaEligibilityCommand | None:
    stripped = text.strip()
    if not stripped:
        return None

    parts = stripped.split(maxsplit=1)
    command = parts[0].lstrip("/").lower()
    if command == "exclude-from-nightly":
        return MetaEligibilityCommand(False, _normalize_target_text(parts[1] if len(parts) > 1 else None))
    if command == "include-in-nightly":
        return MetaEligibilityCommand(True, _normalize_target_text(parts[1] if len(parts) > 1 else None))

    natural_patterns = (
        (False, r"\bexclude(?:\s+(?P<target><#[^>]+>|[A-Z][A-Z0-9]+|#[A-Za-z0-9_-]+|this channel))?\s+from\s+nightly\b"),
        (True, r"\binclude(?:\s+(?P<target><#[^>]+>|[A-Z][A-Z0-9]+|#[A-Za-z0-9_-]+|this channel))?\s+in\s+nightly\b"),
    )
    for eligible, pattern in natural_patterns:
        match = re.search(pattern, stripped, flags=re.IGNORECASE)
        if match:
            return MetaEligibilityCommand(
                eligible,
                _normalize_target_text(match.groupdict().get("target")),
            )
    return None


def _normalize_target_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    target = raw.strip()
    if not target or target.lower() in {"this", "this channel"}:
        return None
    return target


def _resolve_meta_target(
    raw_target: str | None,
    *,
    source_channel_id: str,
    home,
) -> str | None:
    target = _normalize_target_text(raw_target)
    if target is None:
        return source_channel_id

    mention = CHANNEL_REF_PATTERN.search(target)
    if mention:
        return mention.group("id")

    first_token = target.split()[0]
    if CHANNEL_ID_PATTERN.match(first_token):
        return first_token

    if first_token.startswith("#"):
        wanted = first_token.lower()
        for manifest_path in sorted(paths.contexts_dir(home).glob("*/.claude/channel-manifest.yaml")):
            try:
                manifest = load_manifest(manifest_path)
            except ManifestError:
                continue
            labels = {
                str(manifest.label or "").lower(),
                f"#{manifest.channel_id}".lower(),
                manifest.channel_id.lower(),
            }
            if wanted in labels:
                return manifest.channel_id
    return None


def _manifest_display_label(manifest) -> str:
    return manifest.label or manifest.channel_id


def _channel_label_from_name(channel_name: str | None) -> str | None:
    if not channel_name:
        return None
    return channel_name if channel_name.startswith("#") else f"#{channel_name}"


async def _resolve_pending_channel_label(
    slack_client,
    *,
    channel_id: str,
    is_dm: bool,
    fallback: str,
) -> str:
    if is_dm:
        return fallback
    try:
        info = await slack_client.conversations_info(channel=channel_id)
    except Exception:
        log.info(
            "ingress.pending_channel_label_lookup_failed channel=%s",
            channel_id,
            exc_info=True,
        )
        return fallback

    name = info.get("channel", {}).get("name")
    if not name:
        return fallback
    return name if str(name).startswith("#") else f"#{name}"


async def _resolve_block_action(
    q,
    choice_key,
    router,
    slack_client,
    *,
    tool_name: str | None = None,
) -> None:
    try:
        if choice_key == "always":
            result = _resolve_question(
                q,
                choice="always",
                tool_name=tool_name or q.tool_name,
                router=router,
            )
            answer_text = (
                f"{_always_allow_label(tool_name or q.tool_name)} "
                "(will not ask again in this channel)"
            )
        elif choice_key == "deny":
            result = _resolve_question(q, choice="deny")
            answer_text = "Deny"
        else:
            try:
                idx = int(choice_key)
                suggestion = q.suggestions[idx]
                result = _resolve_question(
                    q,
                    choice="allow",
                    suggestion=suggestion,
                )
                answer_text = _suggestion_label(
                    suggestion,
                    tool_name=q.tool_name,
                )
            except (ValueError, IndexError):
                result = _resolve_question(q, choice="allow")
                answer_text = _suggestion_label(None, tool_name=q.tool_name)

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
            if q.on_resolve is not None:
                await q.on_resolve(result)
        await update_question_resolved(
            q,
            answer_text,
            slack_client,
            allowed=isinstance(result, PermissionResultAllow),
        )
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
