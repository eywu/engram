"""Ingress — Slack Bolt listeners (DMs + allowed channels).

M1: minimal. DMs always answered; team channels answered only if explicitly
in `allowed_channels` from config. Threads are preserved (we reply in-thread
if the incoming message was in a thread).

M2 replaces the allowlist with manifest-driven provisioning.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from slack_bolt.async_app import AsyncApp

from engram import paths
from engram.agent import Agent, archive_session_transcript
from engram.config import EngramConfig
from engram.costs import CostLedger, TurnCost
from engram.egress import (
    ActiveYoloGrantRow,
    _always_allow_label,
    _suggestion_label,
    build_footgun_confirmation_modal,
    build_session_greeting,
    post_reply,
    post_upgrade_result_in_channel,
    post_yolo_expired_notification,
    render_active_yolo_grants,
    update_question_resolved,
    update_upgrade_request_dm,
)
from engram.hitl import PendingQuestion, _resolve_question
from engram.manifest import (
    YOLO_DURATION_CHOICES,
    YOLO_MAX_DURATION,
    ChannelManifest,
    ChannelStatus,
    ManifestError,
    PermissionTier,
    dump_manifest,
    load_manifest,
    parse_permission_tier,
    permission_tier_choices_text,
    persist_yolo_demotion,
    set_channel_nightly_included,
    set_channel_permission_tier,
    validate_upgrade_duration,
)
from engram.notifications import (
    PENDING_CHANNEL_ACTION_ID_PATTERN,
    handle_pending_channel_action,
    notify_pending_channel,
    post_pending_channel_ack,
)
from engram.permissions.authorization import can_change_tier, classify_transition
from engram.router import Router

log = logging.getLogger(__name__)
hitl_log = logging.getLogger("engram.hitl")
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
HITL_ACTION_ID_PATTERN = re.compile(r"^hitl_choice_(?:\d+|always_\d+|deny)$")
UPGRADE_ACTION_ID_PATTERN = re.compile(
    r"^upgrade_decision_(approve_(?:permanent|30d|24h|6h)|deny)$"
)
YOLO_EXTEND_ACTION_ID_PATTERN = re.compile(r"^yolo_extend_[A-Z0-9]+$")
YOLO_REVOKE_ACTION_ID_PATTERN = re.compile(r"^yolo_revoke_[A-Z0-9]+$")
FOOTGUN_CONFIRM_OPEN_ACTION_ID = "footgun_confirm_open"
ACTION_ID_TIER_PICK_PREFIX = "engram_tier_pick"
ACTION_ID_TIER_PICK = ACTION_ID_TIER_PICK_PREFIX
TIER_PICK_ACTION_PATTERN = re.compile(
    rf"^{re.escape(ACTION_ID_TIER_PICK_PREFIX)}(?::.+)?$"
)
ACTION_ID_YOLO_DURATION_PREFIX = "engram_yolo_duration"
ACTION_ID_YOLO_DURATION = ACTION_ID_YOLO_DURATION_PREFIX
YOLO_DURATION_ACTION_PATTERN = re.compile(
    rf"^{re.escape(ACTION_ID_YOLO_DURATION_PREFIX)}(?::.+)?$"
)
ACTION_ID_NIGHTLY_TOGGLE_PREFIX = "engram_nightly_toggle"
ACTION_ID_NIGHTLY_TOGGLE = ACTION_ID_NIGHTLY_TOGGLE_PREFIX
NIGHTLY_TOGGLE_ACTION_PATTERN = re.compile(
    rf"^{re.escape(ACTION_ID_NIGHTLY_TOGGLE_PREFIX)}(?::.+)?$"
)
ACTION_ID_CHANNELS_PAGE_PREFIX = "engram_channels_page"
ACTION_ID_CHANNELS_PAGE = ACTION_ID_CHANNELS_PAGE_PREFIX
CHANNELS_PAGE_ACTION_PATTERN = re.compile(
    rf"^{re.escape(ACTION_ID_CHANNELS_PAGE_PREFIX)}(?::.+)?$"
)
ACTION_ID_SESSION_NEW_PREFIX = "engram_session_new"
SESSION_NEW_ACTION_PATTERN = re.compile(
    r"^engram_session_new_(confirm|cancel):[A-Z][A-Z0-9]+$"
)
CHANNELS_DASHBOARD_PAGE_SIZE = 20
CHANNELS_DASHBOARD_BLOCK_ID_PATTERN = re.compile(
    r"^engram_channels:(?P<page>\d+):(?P<kind>nav|channel:[A-Z0-9]+)$"
)
UPGRADE_PICKER_BLOCK_ID_PREFIX = "engram_upgrade_picker:"
CHANNEL_REF_PATTERN = re.compile(r"<#(?P<id>[A-Z0-9]+)(?:\|[^>]+)?>")
CHANNEL_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+$")
_PENDING_UPGRADE_REQUESTS: dict[str, PendingUpgradeRequest] = {}
_PENDING_UPGRADE_REQUESTS_BY_CHANNEL: dict[str, str] = {}
_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
_YOLO_DURATION_PICKER_TEXT = (
    "YOLO mode will bypass HITL gates "
    "(footgun modal still active for destructive commands).\n"
    "Choose a duration:"
)
_YOLO_DURATION_ALIASES = {"6": "6h", "24": "24h", "72": "72h"}
SAFE_TIER_NIGHTLY_INCLUDE_ERROR = (
    "Cannot include a `safe` channel in the nightly summary. "
    "Safe channels are excluded by default to protect team privacy. "
    "Upgrade the channel to `trusted` first: `/engram upgrade`"
)
NON_OWNER_NIGHTLY_INCLUDE_ERROR = (
    "Only the owner can include a channel in the nightly summary."
)
SAFE_TIER_DOWNGRADE_NOTICE = (
    "Downgrading to `safe` also excluded this channel from nightly summary "
    "(required for safe tier)."
)


@dataclass(frozen=True)
class MetaEligibilityCommand:
    eligible: bool
    target: str | None


@dataclass(frozen=True)
class PendingUpgradeRequest:
    request_id: str
    source_channel_id: str
    source_channel_label: str
    source_message_ts: str
    owner_dm_channel_id: str
    owner_dm_message_ts: str
    requested_by_user_id: str | None
    from_tier: PermissionTier
    to_tier: PermissionTier
    reason: str | None


@dataclass(frozen=True)
class ChannelDashboardRow:
    channel_id: str
    label: str
    sort_label: str
    manifest: ChannelManifest
    is_owner_dm: bool
    is_private: bool
    is_archived: bool


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

    @app.action(FOOTGUN_CONFIRM_OPEN_ACTION_ID)
    async def on_footgun_confirm_open(ack, body, client):
        await ack()
        try:
            result = await handle_footgun_confirm_open(body, router, client)
            if not result.get("ok"):
                log.warning(
                    "ingress.footgun_confirm_open_failed error=%s",
                    result.get("error", "unknown"),
                )
        except Exception:
            log.exception("ingress.footgun_confirm_open_handler_failed")

    @app.view("footgun_confirm_submit")
    async def on_footgun_confirm_submit(ack, body, client):
        await ack()
        try:
            result = await handle_footgun_confirm_submit(body, router, client)
            if not result.get("ok"):
                log.warning(
                    "ingress.footgun_confirm_submit_failed error=%s",
                    result.get("error", "unknown"),
                )
        except Exception:
            log.exception("ingress.footgun_confirm_submit_handler_failed")

    @app.view_closed("footgun_confirm_submit")
    async def on_footgun_confirm_closed(ack, body, client):
        await ack()
        try:
            result = await handle_footgun_confirm_closed(body, router, client)
            if not result.get("ok"):
                log.warning(
                    "ingress.footgun_confirm_closed_failed error=%s",
                    result.get("error", "unknown"),
                )
        except Exception:
            log.exception("ingress.footgun_confirm_closed_handler_failed")

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

    @app.action(UPGRADE_ACTION_ID_PATTERN)
    async def on_upgrade_action(ack, body, client):
        await ack()

        async def _run() -> None:
            try:
                result = await handle_upgrade_action(
                    payload=body,
                    router=router,
                    slack_client=client,
                    owner_user_id=config.owner_user_id,
                )
                if not result.get("ok"):
                    log.warning(
                        "ingress.upgrade_action_failed error=%s",
                        result.get("error", "unknown"),
                    )
            except Exception:
                log.exception("ingress.upgrade_action_handler_failed")

        task = asyncio.create_task(_run())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    @app.action(YOLO_EXTEND_ACTION_ID_PATTERN)
    async def on_yolo_extend_action(ack, body, client):
        await ack()

        async def _run() -> None:
            try:
                result = await handle_yolo_action(
                    payload=body,
                    router=router,
                    config=config,
                    slack_client=client,
                    action_kind="extend",
                )
                if not result.get("ok"):
                    log.warning(
                        "ingress.yolo_extend_action_failed error=%s",
                        result.get("error", "unknown"),
                    )
            except Exception:
                log.exception("ingress.yolo_extend_action_handler_failed")

        task = asyncio.create_task(_run())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    @app.action(YOLO_REVOKE_ACTION_ID_PATTERN)
    async def on_yolo_revoke_action(ack, body, client):
        await ack()

        async def _run() -> None:
            try:
                result = await handle_yolo_action(
                    payload=body,
                    router=router,
                    config=config,
                    slack_client=client,
                    action_kind="revoke",
                )
                if not result.get("ok"):
                    log.warning(
                        "ingress.yolo_revoke_action_failed error=%s",
                        result.get("error", "unknown"),
                    )
            except Exception:
                log.exception("ingress.yolo_revoke_action_handler_failed")

        task = asyncio.create_task(_run())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    @app.action(TIER_PICK_ACTION_PATTERN)
    async def on_channels_tier_pick(ack, body, client, respond):
        await ack()
        try:
            result = await handle_tier_pick_action(
                payload=body,
                router=router,
                config=config,
                slack_client=client,
            )
            await _maybe_respond_with_dashboard(result=result, respond=respond)
            if not result.get("ok"):
                log.warning(
                    "ingress.channels_tier_pick_failed error=%s",
                    result.get("error", "unknown"),
                )
        except Exception:
            log.exception("ingress.channels_tier_pick_handler_failed")

    @app.action(YOLO_DURATION_ACTION_PATTERN)
    async def on_channels_yolo_duration(ack, body, client, respond):
        await ack()
        try:
            result = await handle_yolo_duration_action(
                payload=body,
                router=router,
                config=config,
                slack_client=client,
            )
            await _maybe_respond_with_dashboard(result=result, respond=respond)
            if not result.get("ok"):
                log.warning(
                    "ingress.channels_yolo_duration_failed error=%s",
                    result.get("error", "unknown"),
                )
        except Exception:
            log.exception("ingress.channels_yolo_duration_handler_failed")

    @app.action(NIGHTLY_TOGGLE_ACTION_PATTERN)
    async def on_channels_nightly_toggle(ack, body, client, respond):
        await ack()
        try:
            result = await handle_channels_dashboard_action(
                payload=body,
                router=router,
                config=config,
                slack_client=client,
            )
            await _maybe_respond_with_dashboard(result=result, respond=respond)
            if not result.get("ok"):
                log.warning(
                    "ingress.channels_nightly_toggle_failed error=%s",
                    result.get("error", "unknown"),
                )
        except Exception:
            log.exception("ingress.channels_nightly_toggle_handler_failed")

    @app.action(CHANNELS_PAGE_ACTION_PATTERN)
    async def on_channels_page(ack, body, client, respond):
        await ack()
        try:
            result = await handle_channels_dashboard_action(
                payload=body,
                router=router,
                config=config,
                slack_client=client,
            )
            await _maybe_respond_with_dashboard(result=result, respond=respond)
            if not result.get("ok"):
                log.warning(
                    "ingress.channels_page_failed error=%s",
                    result.get("error", "unknown"),
                )
        except Exception:
            log.exception("ingress.channels_page_handler_failed")

    @app.action(SESSION_NEW_ACTION_PATTERN)
    async def on_session_new_action(ack, body, client, respond):
        await ack()

        async def _run() -> None:
            try:
                result = await handle_new_confirm_action(
                    payload=body,
                    router=router,
                    config=config,
                    slack_client=client,
                )
                await _maybe_respond_with_dashboard(result=result, respond=respond)
                if not result.get("ok"):
                    log.warning(
                        "ingress.session_new_action_failed error=%s",
                        result.get("error", "unknown"),
                    )
            except Exception:
                log.exception("ingress.session_new_action_handler_failed")

        task = asyncio.create_task(_run())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    @app.command("/engram")
    async def on_engram(ack, body, client):
        await ack()
        try:
            log.info(
                "ingress.slash_command_received",
                extra={
                    "slash_command": "/engram",
                    "source_channel_id": str(body.get("channel_id") or ""),
                    "user_id": body.get("user_id"),
                },
            )
            await handle_engram_command(
                router=router,
                config=config,
                slack_client=client,
                source_channel_id=str(body.get("channel_id") or ""),
                source_channel_name=body.get("channel_name"),
                user_id=body.get("user_id"),
                command_text=body.get("text"),
            )
        except Exception:
            log.exception("ingress.engram_command_failed")

    @app.command("/exclude-from-nightly")
    async def on_exclude_from_nightly(ack, body, client):
        await ack()
        try:
            log.info(
                "ingress.slash_command_received",
                extra={
                    "slash_command": "/exclude-from-nightly",
                    "source_channel_id": str(body.get("channel_id") or ""),
                    "user_id": body.get("user_id"),
                },
            )
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
            log.info(
                "ingress.slash_command_received",
                extra={
                    "slash_command": "/include-in-nightly",
                    "source_channel_id": str(body.get("channel_id") or ""),
                    "user_id": body.get("user_id"),
                },
            )
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
                            tier=manifest.permission_tier.value,
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

        await _maybe_demote_expired_yolo(
            session=session,
            router=router,
            config=config,
            slack_client=client,
        )

        log.info(
            "ingress.received session=%s user=%s len=%d",
            session.label(),
            user_id,
            len(text),
        )

        greeting_prefix: str | None = None
        if session.session_just_started:
            session.session_just_started = False
            greeting_prefix = _build_session_greeting(session, router, config)

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
            greeting_prefix=greeting_prefix,
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


async def _maybe_demote_expired_yolo(
    *,
    session,
    router: Router,
    config: EngramConfig,
    slack_client,
) -> None:
    if session.manifest is None or router.home is None:
        return
    if session.manifest.permission_tier != PermissionTier.YOLO:
        return

    manifest_path = paths.channel_manifest_path(session.channel_id, router.home)
    async with session.agent_lock:
        try:
            latest = load_manifest(manifest_path)
        except ManifestError:
            log.warning(
                "ingress.yolo_manifest_refresh_failed channel=%s path=%s",
                session.channel_id,
                manifest_path,
                exc_info=True,
            )
            return
        router.replace_cached_manifest(latest)
        session.manifest = latest

        demotion = persist_yolo_demotion(
            manifest_path,
            trigger="lazy",
        )
        if demotion is None:
            return
        router.replace_cached_manifest(demotion.manifest)
        session.manifest = demotion.manifest

    owner_dm_channel_id = config.owner_dm_channel_id or router.owner_dm_channel_id
    if owner_dm_channel_id:
        try:
            await post_yolo_expired_notification(
                slack_client,
                owner_dm_channel_id=owner_dm_channel_id,
                channel_id=demotion.channel_id,
                channel_label=demotion.manifest.label,
                pre_yolo_tier=demotion.pre_yolo_tier,
                duration_used=demotion.duration_used,
            )
        except Exception:
            log.warning(
                "ingress.yolo_expired_notification_failed channel=%s",
                demotion.channel_id,
                exc_info=True,
            )
    else:
        log.warning(
            "ingress.yolo_expired_notification_dropped channel=%s reason=no_owner_dm",
            demotion.channel_id,
        )
    log.info(
        "channel.yolo_demoted channel_id=%s restored_tier=%s",
        demotion.channel_id,
        demotion.effective_tier,
    )


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


async def handle_footgun_confirm_open(payload: dict, router, slack_client) -> dict:
    """Open the type-to-confirm modal for a pending footgun question."""
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "error": "no actions"}

    permission_request_id = str(actions[0].get("value") or "").strip()
    if not permission_request_id:
        return {"ok": False, "error": "missing permission request id"}

    q = router.hitl.get_by_id(permission_request_id)
    if q is None:
        return {"ok": False, "error": "question not found (may be resolved)"}
    if q.future.done():
        return {"ok": True, "info": "already resolved"}
    if q.footgun_match is None:
        return {"ok": False, "error": "question is not a footgun confirmation"}

    clicker_user_id = payload.get("user", {}).get("id")
    if q.who_can_answer and clicker_user_id != q.who_can_answer:
        with contextlib.suppress(Exception):
            await slack_client.chat_postEphemeral(
                channel=q.channel_id,
                user=clicker_user_id,
                text="Owner approval required for destructive actions.",
            )
        return {"ok": False, "error": "not authorized"}

    trigger_id = payload.get("trigger_id")
    if not trigger_id:
        return {"ok": False, "error": "missing trigger id"}

    await slack_client.views_open(
        trigger_id=trigger_id,
        view=build_footgun_confirmation_modal(q),
    )
    return {"ok": True}


async def handle_footgun_confirm_submit(payload: dict, router, slack_client) -> dict:
    """Resolve a pending footgun confirmation from modal submission."""
    view = payload.get("view") or {}
    permission_request_id = str(view.get("private_metadata") or "").strip()
    if not permission_request_id:
        return {"ok": False, "error": "missing permission request id"}

    q = router.hitl.get_by_id(permission_request_id)
    if q is None:
        return {"ok": False, "error": "question not found (may be resolved)"}
    if q.future.done():
        return {"ok": True, "info": "already resolved"}
    if q.footgun_match is None:
        return {"ok": False, "error": "question is not a footgun confirmation"}

    submitter_user_id = payload.get("user", {}).get("id")
    if q.who_can_answer and submitter_user_id != q.who_can_answer:
        return {"ok": False, "error": "not authorized"}

    typed_value = _footgun_confirmation_value(view)
    confirmed = typed_value == "CONFIRM"
    await _resolve_footgun_question(
        q,
        router,
        slack_client,
        confirmed=confirmed,
        user_id=submitter_user_id,
        reason="submitted" if confirmed else "invalid_confirmation",
    )
    return {"ok": True}


async def handle_footgun_confirm_closed(payload: dict, router, slack_client) -> dict:
    """Resolve a pending footgun confirmation when the modal is cancelled."""
    view = payload.get("view") or {}
    permission_request_id = str(view.get("private_metadata") or "").strip()
    if not permission_request_id:
        return {"ok": False, "error": "missing permission request id"}

    q = router.hitl.get_by_id(permission_request_id)
    if q is None:
        return {"ok": False, "error": "question not found (may be resolved)"}
    if q.future.done():
        return {"ok": True, "info": "already resolved"}
    if q.footgun_match is None:
        return {"ok": False, "error": "question is not a footgun confirmation"}

    closer_user_id = payload.get("user", {}).get("id")
    if q.who_can_answer and closer_user_id != q.who_can_answer:
        return {"ok": False, "error": "not authorized"}

    await _resolve_footgun_question(
        q,
        router,
        slack_client,
        confirmed=False,
        user_id=closer_user_id,
        reason="cancelled",
    )
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
    """Update nightly cross-channel summary inclusion for a channel."""
    if not source_channel_id:
        return {"ok": False, "error": "missing source channel"}

    home = router.home or paths.engram_home()
    target_channel_id = _resolve_meta_target(
        target_text,
        source_channel_id=source_channel_id,
        home=home,
    )
    if target_channel_id is None:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text="Could not find that channel.",
        )
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
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=str(exc),
        )
        return {"ok": False, "error": "manifest not found"}

    if target_channel_id != source_channel_id and not _is_owner_user(
        config=config,
        user_id=user_id,
    ):
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text="Owner-only.",
        )
        return {"ok": False, "error": "not owner"}

    if eligible and manifest.tier_effective() == PermissionTier.TASK_ASSISTANT:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=SAFE_TIER_NIGHTLY_INCLUDE_ERROR,
        )
        return {"ok": False, "error": "safe tier"}

    if eligible and not _is_owner_user(config=config, user_id=user_id):
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=NON_OWNER_NIGHTLY_INCLUDE_ERROR,
        )
        return {"ok": False, "error": "not owner"}

    previous, updated, _manifest_path = set_channel_nightly_included(
        target_channel_id,
        eligible,
        home=home,
    )
    router.replace_cached_manifest(updated)
    changed = previous.nightly_included != updated.nightly_included
    text = (
        "Channel included in nightly cross-channel summary."
        if eligible and changed
        else (
            "Channel excluded from nightly cross-channel summary."
            if not eligible and changed
            else (
                "Channel is already included. No change."
                if eligible
                else "Channel is already excluded. No change."
            )
        )
    )
    await _post_ephemeral_reply(
        slack_client,
        channel_id=source_channel_id,
        user_id=user_id,
        text=text,
    )
    log.info(
        "ingress.nightly_inclusion_updated channel=%s nightly_included=%s changed=%s",
        target_channel_id,
        updated.nightly_included,
        changed,
    )
    return {
        "ok": True,
        "channel_id": target_channel_id,
        "nightly_included": updated.nightly_included,
        "changed": changed,
    }


async def handle_engram_command(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    source_channel_id: str,
    source_channel_name: str | None,
    user_id: str | None,
    command_text: str | None,
) -> dict[str, object]:
    parts = [part for part in str(command_text or "").split() if part]
    if not parts:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=(
                "Usage: /engram channels | /engram include | /engram exclude "
                "| /engram upgrade <tier> [reason...] "
                "| /engram yolo <list|off|extend> ... "
                "| /engram new"
            ),
        )
        return {"ok": False, "error": "missing subcommand"}

    subcommand = parts[0].lower()
    if subcommand == "channels":
        return await handle_channels_command(
            router=router,
            config=config,
            slack_client=slack_client,
            source_channel_id=source_channel_id,
            user_id=user_id,
        )

    if subcommand in {"include", "exclude"}:
        target_text = " ".join(parts[1:]).strip() or None
        return await handle_meta_eligibility_command(
            router=router,
            config=config,
            slack_client=slack_client,
            source_channel_id=source_channel_id,
            source_channel_name=source_channel_name,
            user_id=user_id,
            eligible=(subcommand == "include"),
            target_text=target_text,
        )

    if subcommand == "upgrade":
        if len(parts) < 2:
            return await handle_upgrade_picker_command(
                router=router,
                config=config,
                slack_client=slack_client,
                source_channel_id=source_channel_id,
                source_channel_name=source_channel_name,
                user_id=user_id,
            )

        try:
            tier, _deprecated_alias = parse_permission_tier(parts[1])
        except ValueError:
            await _post_ephemeral_reply(
                slack_client,
                channel_id=source_channel_id,
                user_id=user_id,
                text=(
                    f"Unknown tier: {parts[1]}. "
                    f"Use {permission_tier_choices_text()}."
                ),
            )
            return {"ok": False, "error": "unknown tier"}

        reason = " ".join(parts[2:]).strip() or None
        return await handle_upgrade_command(
            router=router,
            config=config,
            slack_client=slack_client,
            source_channel_id=source_channel_id,
            source_channel_name=source_channel_name,
            user_id=user_id,
            requested_tier=tier,
            reason=reason,
        )

    if subcommand == "yolo":
        return await handle_yolo_command(
            router=router,
            config=config,
            slack_client=slack_client,
            source_channel_id=source_channel_id,
            user_id=user_id,
            args=parts[1:],
        )

    if subcommand == "new":
        return await handle_new_command(
            router=router,
            config=config,
            slack_client=slack_client,
            source_channel_id=source_channel_id,
            source_channel_name=source_channel_name,
            user_id=user_id,
        )

    await _post_ephemeral_reply(
        slack_client,
        channel_id=source_channel_id,
        user_id=user_id,
        text=f"Unknown /engram subcommand: {subcommand}",
    )
    return {"ok": False, "error": "unknown subcommand"}


async def handle_channels_command(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    source_channel_id: str,
    user_id: str | None,
) -> dict[str, object]:
    owner_dm_channel_id = config.owner_dm_channel_id or router.owner_dm_channel_id
    if not owner_dm_channel_id or not config.owner_user_id:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=(
                "Channel dashboard is not configured yet. "
                "Ask the operator to set owner_dm_channel_id and owner_user_id."
            ),
        )
        return {"ok": False, "error": "owner approval config missing"}

    if source_channel_id != owner_dm_channel_id:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=(
                "Channel dashboard is DM-only. "
                "Run `/engram channels` from your DM with Engram."
            ),
        )
        return {"ok": False, "error": "dm_only"}

    if not _is_owner_user(config=config, user_id=user_id):
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text="Owner-only.",
        )
        return {"ok": False, "error": "not owner"}

    rows = await _collect_channels_dashboard_rows(
        router=router,
        config=config,
        slack_client=slack_client,
    )
    text, blocks, page = _render_channels_dashboard(rows, page=0)
    await _post_ephemeral_reply(
        slack_client,
        channel_id=source_channel_id,
        user_id=user_id,
        text=text,
        blocks=blocks,
    )
    return {"ok": True, "count": len(rows), "page": page}


async def handle_channels_dashboard_action(
    *,
    payload: dict[str, object],
    router: Router,
    config: EngramConfig,
    slack_client,
) -> dict[str, object]:
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "error": "no actions"}

    owner_dm_channel_id = config.owner_dm_channel_id or router.owner_dm_channel_id
    source_channel_id = str(payload.get("channel", {}).get("id") or "")
    clicker_user_id = str(payload.get("user", {}).get("id") or "") or None
    if not owner_dm_channel_id or source_channel_id != owner_dm_channel_id:
        text = (
            "Channel dashboard is DM-only. "
            "Run `/engram channels` from your DM with Engram."
        )
        return {
            "ok": False,
            "error": "wrong surface",
            "response": {"response_type": "ephemeral", "text": text},
        }
    if not _is_owner_user(config=config, user_id=clicker_user_id):
        return {
            "ok": False,
            "error": "not owner",
            "response": {"response_type": "ephemeral", "text": "Owner-only."},
        }

    action = actions[0]
    action_id = str(action.get("action_id") or "")
    current_page = _channels_dashboard_page_from_action(action)
    target_page = current_page
    notice: str | None = None

    if CHANNELS_PAGE_ACTION_PATTERN.match(action_id):
        parsed_page = _decode_channels_page_value(str(action.get("value") or ""))
        if parsed_page is None:
            notice = "Could not change dashboard page."
        else:
            target_page = parsed_page
    elif TIER_PICK_ACTION_PATTERN.match(action_id):
        parsed = _decode_channels_dashboard_pair(str(action.get("value") or ""))
        if parsed is None:
            notice = "Could not change channel tier."
        else:
            channel_id, target_tier_raw = parsed
            try:
                target_tier = PermissionTier(target_tier_raw)
            except (ManifestError, ValueError) as exc:
                notice = str(exc)
            else:
                if target_tier == PermissionTier.YOLO:
                    text, blocks = _render_yolo_duration_picker(channel_id=channel_id)
                    return {
                        "ok": True,
                        "page": current_page,
                        "response": _replace_original_ephemeral(
                            text=text,
                            blocks=blocks,
                        ),
                    }
                previous, updated, _manifest_path, _duration = set_channel_permission_tier(
                    channel_id,
                    target_tier,
                    duration="permanent",
                    home=router.home,
                )
                router.replace_cached_manifest(updated)
                await _maybe_post_safe_tier_downgrade_notice(
                    slack_client,
                    channel_id=channel_id,
                    previous=previous,
                    updated=updated,
                )
    elif YOLO_DURATION_ACTION_PATTERN.match(action_id):
        parsed = _decode_channels_dashboard_pair(str(action.get("value") or ""))
        if parsed is None:
            notice = "Could not extend YOLO."
        else:
            channel_id, duration = parsed
            result = await _extend_yolo_grant(
                router=router,
                config=config,
                slack_client=slack_client,
                channel_id=channel_id,
                duration=duration,
            )
            if not result["ok"]:
                notice = str(result["message"])
    elif NIGHTLY_TOGGLE_ACTION_PATTERN.match(action_id):
        parsed = _decode_channels_dashboard_pair(str(action.get("value") or ""))
        if parsed is None:
            notice = "Could not change nightly inclusion."
        else:
            channel_id, mode = parsed
            if mode not in {"include", "exclude"}:
                notice = "Could not change nightly inclusion."
            else:
                try:
                    updated = await _set_dashboard_nightly_included(
                        router=router,
                        channel_id=channel_id,
                        nightly_included=(mode == "include"),
                    )
                    if mode == "include" and updated.nightly_included:
                        notice = None
                except (ManifestError, ValueError) as exc:
                    notice = str(exc)
    else:
        return {"ok": False, "error": "unsupported action"}

    rows = await _collect_channels_dashboard_rows(
        router=router,
        config=config,
        slack_client=slack_client,
    )
    text, blocks, rendered_page = _render_channels_dashboard(
        rows,
        page=target_page,
        notice=notice,
    )
    return {
        "ok": notice is None,
        "page": rendered_page,
        "response": _channels_dashboard_replace_original(text=text, blocks=blocks),
    }


async def handle_yolo_command(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    source_channel_id: str,
    user_id: str | None,
    args: list[str],
) -> dict[str, object]:
    if not _is_owner_user(config=config, user_id=user_id):
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text="Owner-only.",
        )
        return {"ok": False, "error": "not owner"}

    if not args:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text="Usage: /engram yolo <list|off|extend> ...",
        )
        return {"ok": False, "error": "missing yolo subcommand"}

    action = args[0].lower()
    if action == "list":
        home = router.home or paths.engram_home()
        grants = _list_active_yolo_grants(home)
        text, blocks = render_active_yolo_grants(grants)
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=text,
            blocks=blocks,
        )
        return {"ok": True, "count": len(grants)}

    if action == "off":
        if len(args) < 2:
            await _post_ephemeral_reply(
                slack_client,
                channel_id=source_channel_id,
                user_id=user_id,
                text="Usage: /engram yolo off <channel-name-or-id>",
            )
            return {"ok": False, "error": "missing channel"}

        target_channel_id = _resolve_yolo_target(
            " ".join(args[1:]),
            source_channel_id=source_channel_id,
            home=router.home or paths.engram_home(),
        )
        if target_channel_id is None:
            await _post_ephemeral_reply(
                slack_client,
                channel_id=source_channel_id,
                user_id=user_id,
                text=f"Unknown channel: {' '.join(args[1:])}",
            )
            return {"ok": False, "error": "unknown channel"}

        result = await _revoke_yolo_grant(
            router=router,
            config=config,
            slack_client=slack_client,
            channel_id=target_channel_id,
        )
        if not result["ok"]:
            await _post_ephemeral_reply(
                slack_client,
                channel_id=source_channel_id,
                user_id=user_id,
                text=str(result["message"]),
            )
            return result

        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=f"Revoked yolo for {result['label']}.",
        )
        return result

    if action == "extend":
        if len(args) == 1:
            text, blocks = _render_yolo_duration_picker(channel_id=source_channel_id)
            await _post_ephemeral_reply(
                slack_client,
                channel_id=source_channel_id,
                user_id=user_id,
                text=text,
                blocks=blocks,
            )
            return {"ok": True, "picker": True}

        if len(args) == 2:
            try:
                normalized_duration = _normalize_yolo_duration(args[1])
            except ValueError:
                await _post_ephemeral_reply(
                    slack_client,
                    channel_id=source_channel_id,
                    user_id=user_id,
                    text=(
                        "Usage: /engram yolo extend [<6h|24h|72h>|"
                        "<channel> <6h|24h|72h>]"
                    ),
                )
                return {"ok": False, "error": "invalid duration"}

            result = await _activate_yolo_grant(
                router=router,
                slack_client=slack_client,
                channel_id=source_channel_id,
                duration=normalized_duration,
                clicker_user_id=user_id,
            )
            await _post_ephemeral_reply(
                slack_client,
                channel_id=source_channel_id,
                user_id=user_id,
                text=(
                    str(result["ack_text"])
                    if result["ok"]
                    else str(result["message"])
                ),
            )
            return result

        if len(args) < 3:
            await _post_ephemeral_reply(
                slack_client,
                channel_id=source_channel_id,
                user_id=user_id,
                text="Usage: /engram yolo extend [<6h|24h|72h>|<channel> <6h|24h|72h>]",
            )
            return {"ok": False, "error": "missing extend arguments"}

        target_channel_id = _resolve_yolo_target(
            " ".join(args[1:-1]),
            source_channel_id=source_channel_id,
            home=router.home or paths.engram_home(),
        )
        if target_channel_id is None:
            await _post_ephemeral_reply(
                slack_client,
                channel_id=source_channel_id,
                user_id=user_id,
                text=f"Unknown channel: {' '.join(args[1:-1])}",
            )
            return {"ok": False, "error": "unknown channel"}

        result = await _extend_yolo_grant(
            router=router,
            config=config,
            slack_client=slack_client,
            channel_id=target_channel_id,
            duration=args[-1],
        )
        if not result["ok"]:
            await _post_ephemeral_reply(
                slack_client,
                channel_id=source_channel_id,
                user_id=user_id,
                text=str(result["message"]),
            )
            return result

        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=(
                f"Extended {result['label']} by {result['duration']}. "
                f"Remaining: {result['remaining_text']}."
            ),
        )
        return result

    await _post_ephemeral_reply(
        slack_client,
        channel_id=source_channel_id,
        user_id=user_id,
        text=f"Unknown /engram yolo subcommand: {action}",
    )
    return {"ok": False, "error": "unknown yolo subcommand"}


async def handle_new_command(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    source_channel_id: str,
    source_channel_name: str | None,
    user_id: str | None,
) -> dict[str, object]:
    """Handle `/engram new` — show ephemeral confirm before resetting the session."""
    if not source_channel_id:
        return {"ok": False, "error": "missing source channel"}

    # Only the owner may reset a session.
    if not _is_owner_user(config=config, user_id=user_id):
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text="Owner-only. Only the owner can start a fresh conversation.",
        )
        return {"ok": False, "error": "not owner"}

    channel_label = _channel_label_from_name(source_channel_name) or source_channel_id
    text = (
        f"Start a new conversation in {channel_label}?\n"
        "• Memory is preserved (semantic search across past sessions still works)\n"
        "• The current Claude CLI session ends; a fresh one starts on your next message\n"
        "• MCP servers and project config are reloaded"
    )
    confirm_action_id = f"{ACTION_ID_SESSION_NEW_PREFIX}_confirm:{source_channel_id}"
    cancel_action_id = f"{ACTION_ID_SESSION_NEW_PREFIX}_cancel:{source_channel_id}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "block_id": f"{ACTION_ID_SESSION_NEW_PREFIX}:{source_channel_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Start fresh"},
                    "style": "primary",
                    "action_id": confirm_action_id,
                    "value": f"{source_channel_id}|{user_id or ''}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": cancel_action_id,
                    "value": f"{source_channel_id}|{user_id or ''}",
                },
            ],
        },
    ]
    await _post_ephemeral_reply(
        slack_client,
        channel_id=source_channel_id,
        user_id=user_id,
        text=text,
        blocks=blocks,
    )
    return {"ok": True, "pending_confirm": True}


async def handle_new_confirm_action(
    *,
    payload: dict[str, object],
    router: Router,
    config: EngramConfig,
    slack_client,
) -> dict[str, object]:
    """Handle the [Start fresh] or [Cancel] button from handle_new_command."""
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "error": "no actions"}

    action = actions[0]
    action_id = str(action.get("action_id") or "")
    m = SESSION_NEW_ACTION_PATTERN.match(action_id)
    if not m:
        return {"ok": False, "error": "unsupported action"}

    kind = m.group(1)  # "confirm" or "cancel"
    raw_value = str(action.get("value") or "")
    channel_id, sep, invoker_user_id = raw_value.partition("|")
    if not sep or not CHANNEL_ID_PATTERN.match(channel_id):
        return {
            "ok": False,
            "error": "malformed value",
            "response": _replace_original_ephemeral(
                text="Could not start fresh conversation (malformed request)."
            ),
        }

    clicker_user_id = str(payload.get("user", {}).get("id") or "") or None
    if clicker_user_id != invoker_user_id:
        return {
            "ok": False,
            "error": "identity mismatch",
            "response": _replace_original_ephemeral(
                text="This prompt was opened for a different user. Run `/engram new` yourself."
            ),
        }

    if kind == "cancel":
        return {
            "ok": True,
            "cancelled": True,
            "response": _replace_original_ephemeral(
                text="Cancelled. Current session unchanged."
            ),
        }

    # ── Perform the reset ────────────────────────────────────────────────
    session = await router.get(
        channel_id,
        channel_name=None,
        is_dm=channel_id.startswith("D"),
    )
    async with session.agent_lock:
        if session.agent_client is not None:
            try:
                await session.agent_client.disconnect()
            except Exception:
                log.debug(
                    "ingress.new_session_disconnect_failed session=%s",
                    session.label(),
                    exc_info=True,
                )
            session.agent_client = None
        archive_session_transcript(session.session_id, session.cwd)
        session.agent_session_initialized = False
        session.session_just_started = True

    log.info(
        "ingress.session_reset session=%s by_user=%s",
        session.label(),
        clicker_user_id,
    )

    # ── Build the public follow-up message ───────────────────────────────
    manifest = session.manifest
    if manifest is not None:
        effective_tier = manifest.tier_effective()
        if effective_tier == PermissionTier.YOLO and manifest.yolo_until is not None:
            expiry = manifest.yolo_until.strftime("%Y-%m-%d %H:%M UTC")
            tier_label = f"yolo (until {expiry})"
        else:
            tier_label = effective_tier.value

        mcp_names = list(manifest.mcp_servers.allowed or [])
        mcp_text = ", ".join(mcp_names) if mcp_names else "none"

        memory_count = _count_memory_entries(channel_id, router)
        follow_up = (
            f"🔄 Started a fresh conversation. "
            f"Tier: {tier_label} • "
            f"Memory: {memory_count:,} entries available • "
            f"MCP: {mcp_text}"
        )
    else:
        follow_up = "🔄 Started a fresh conversation."

    await slack_client.chat_postMessage(
        channel=channel_id,
        text=follow_up,
    )

    return {
        "ok": True,
        "reset": True,
        "response": _replace_original_ephemeral(
            text="✅ Fresh conversation started. Say anything to begin."
        ),
    }


def _count_memory_entries(channel_id: str, router: Router) -> int:
    """Return transcript row count for *channel_id* from the memory DB."""
    try:
        from engram.memory import open_memory_db  # local import avoids circular dep

        home = router.home or paths.engram_home()
        db_path = home / "memory.db"
        if not db_path.exists():
            return 0
        conn = open_memory_db(db_path)
        row = conn.execute(
            "SELECT count(*) FROM transcripts WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        log.debug("ingress.memory_count_failed channel=%s", channel_id, exc_info=True)
        return 0


def _build_session_greeting(session, router: Router, config: EngramConfig) -> str:
    """Build the one-time greeting block for the first reply of a new session."""
    manifest = session.manifest
    model = config.anthropic.model
    identity = manifest.identity.value if manifest is not None else "unknown"
    mcp_names: list[str] = []
    if manifest is not None:
        mcp_names = list(manifest.mcp_servers.allowed or [])
    memory_count = _count_memory_entries(session.channel_id, router)
    return build_session_greeting(
        model=model,
        identity=identity,
        mcp_server_names=mcp_names,
        memory_count=memory_count,
    )


async def handle_upgrade_command(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    source_channel_id: str,
    source_channel_name: str | None,
    user_id: str | None,
    requested_tier: PermissionTier,
    reason: str | None,
) -> dict[str, object]:
    if not source_channel_id:
        return {"ok": False, "error": "missing source channel"}

    result = await _set_channel_tier_from_request(
        router=router,
        config=config,
        slack_client=slack_client,
        channel_id=source_channel_id,
        channel_name=source_channel_name,
        clicker_user_id=user_id,
        target_tier=requested_tier,
        yolo_duration=_YOLO_DURATION_ALIASES["24"] if requested_tier == PermissionTier.YOLO else None,
    )
    if result["ok"]:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=str(result["ack_text"]),
        )
    else:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=user_id,
            text=str(result["message"]),
        )
    return result


async def handle_upgrade_picker_command(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    source_channel_id: str,
    source_channel_name: str | None,
    user_id: str | None,
) -> dict[str, object]:
    if not source_channel_id:
        return {"ok": False, "error": "missing source channel"}

    session = await router.get(
        source_channel_id,
        channel_name=_channel_label_from_name(source_channel_name),
        is_dm=source_channel_id.startswith("D"),
    )
    manifest = session.manifest
    current_tier = (
        manifest.tier_effective()
        if manifest is not None
        else PermissionTier.TASK_ASSISTANT
    )
    is_owner = _is_owner_user(config=config, user_id=user_id)
    text, blocks = build_tier_picker_blocks(
        channel_id=source_channel_id,
        current_tier=current_tier,
        is_owner=is_owner,
        invoker_user_id=user_id,
    )
    await _post_ephemeral_reply(
        slack_client,
        channel_id=source_channel_id,
        user_id=user_id,
        text=text,
        blocks=blocks,
    )
    return {
        "ok": True,
        "picker": True,
        "tier": current_tier.value,
        "is_owner": is_owner,
    }


async def handle_tier_pick_action(
    *,
    payload: dict[str, object],
    router: Router,
    config: EngramConfig,
    slack_client,
) -> dict[str, object]:
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "error": "no actions"}

    action = actions[0]
    block_id = str(action.get("block_id") or "")
    if CHANNELS_DASHBOARD_BLOCK_ID_PATTERN.match(block_id):
        return await handle_channels_dashboard_action(
            payload=payload,
            router=router,
            config=config,
            slack_client=slack_client,
        )
    if not block_id.startswith(UPGRADE_PICKER_BLOCK_ID_PREFIX):
        return {"ok": False, "error": "unsupported action"}

    clicker_user_id = str(payload.get("user", {}).get("id") or "") or None
    parsed = _decode_upgrade_picker_value(str(action.get("value") or ""))
    if parsed is None:
        return {
            "ok": False,
            "error": "malformed value",
            "response": _replace_original_ephemeral(
                text="Could not change channel tier."
            ),
        }

    channel_id, target_tier, invoker_user_id = parsed
    if clicker_user_id != invoker_user_id:
        return {
            "ok": False,
            "error": "identity mismatch",
            "response": _replace_original_ephemeral(
                text="This picker was opened for a different user. Run `/engram upgrade` yourself."
            ),
        }

    session = await router.get(
        channel_id,
        channel_name=None,
        is_dm=channel_id.startswith("D"),
    )
    manifest = session.manifest
    current_tier = (
        manifest.tier_effective()
        if manifest is not None
        else PermissionTier.TASK_ASSISTANT
    )
    transition_kind = _classify_tier_transition(
        current_tier=current_tier,
        target_tier=target_tier,
    )

    if target_tier == PermissionTier.YOLO:
        if transition_kind == "upgrade" and not config.owner_user_id:
            return {
                "ok": False,
                "error": "owner user is not configured",
                "response": _replace_original_ephemeral(
                    text=(
                        "Permission upgrades are not configured yet. "
                        "Ask the operator to set owner_user_id."
                    )
                ),
            }
        decision = _tier_change_decision(
            config=config,
            current_tier=current_tier,
            target_tier=target_tier,
            user_id=clicker_user_id,
        )
        if not decision.allowed:
            return {
                "ok": False,
                "error": "not owner",
                "response": _replace_original_ephemeral(
                    text=decision.reason
                ),
            }
        if transition_kind == "no-op":
            return {
                "ok": True,
                "changed": False,
                "response": _replace_original_ephemeral(text=decision.reason),
            }
        text, blocks = _render_yolo_duration_picker(channel_id=channel_id)
        return {
            "ok": True,
            "response": _replace_original_ephemeral(text=text, blocks=blocks),
        }

    result = await _set_channel_tier_from_request(
        router=router,
        config=config,
        slack_client=slack_client,
        channel_id=channel_id,
        channel_name=session.channel_name,
        clicker_user_id=clicker_user_id,
        target_tier=target_tier,
    )
    return {
        **result,
        "response": _replace_original_ephemeral(
            text=(
                str(result["ack_text"])
                if result["ok"]
                else str(result["message"])
            )
        ),
    }


async def handle_upgrade_action(
    *,
    payload: dict[str, object],
    router: Router,
    slack_client,
    owner_user_id: str | None,
) -> dict[str, object]:
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "error": "no actions"}

    action = actions[0]
    action_id = str(action.get("action_id") or "")
    if not UPGRADE_ACTION_ID_PATTERN.match(action_id):
        return {"ok": False, "error": "unsupported action"}

    clicker_user_id = payload.get("user", {}).get("id")
    if not owner_user_id:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=str(payload.get("channel", {}).get("id") or ""),
            user_id=clicker_user_id,
            text="Upgrade approvals are not configured.",
        )
        return {"ok": False, "error": "owner user is not configured"}
    if clicker_user_id != owner_user_id:
        await _post_ephemeral_reply(
            slack_client,
            channel_id=str(payload.get("channel", {}).get("id") or ""),
            user_id=clicker_user_id,
            text="Only the owner can approve upgrades.",
        )
        return {"ok": False, "error": "not owner"}

    request_ref = _decode_upgrade_action_value(str(action.get("value") or ""))
    if request_ref is None:
        return {"ok": False, "error": "malformed value"}

    request = _PENDING_UPGRADE_REQUESTS.get(request_ref["request_id"])
    if request is None:
        return {"ok": False, "error": "request not found"}
    if request.owner_dm_channel_id != payload.get("channel", {}).get("id"):
        return {"ok": False, "error": "wrong surface"}
    if (
        _PENDING_UPGRADE_REQUESTS_BY_CHANNEL.get(request.source_channel_id)
        != request.request_id
    ):
        await update_upgrade_request_dm(
            slack_client,
            channel_id=request.owner_dm_channel_id,
            message_ts=request.owner_dm_message_ts,
            text="⚪ Superseded by newer request.",
            detail="_Only the newest request in a channel stays actionable._",
        )
        _discard_pending_upgrade_request(request)
        return {"ok": False, "error": "request superseded"}

    if action_id == "upgrade_decision_deny":
        log.info(
            "permission.upgrade_denied",
            extra={
                "channel": request.source_channel_id,
                "approver": clicker_user_id,
            },
        )
        await post_upgrade_result_in_channel(
            slack_client,
            channel_id=request.source_channel_id,
            message_ts=request.source_message_ts,
            approved=False,
        )
        await update_upgrade_request_dm(
            slack_client,
            channel_id=request.owner_dm_channel_id,
            message_ts=request.owner_dm_message_ts,
            text=f"❌ Denied by <@{clicker_user_id}>.",
            detail=f"*Channel:* {request.source_channel_label} ({request.source_channel_id})",
        )
        _discard_pending_upgrade_request(request)
        return {"ok": True, "decision": "deny"}

    duration = _upgrade_duration_from_action(action_id)
    previous, updated, _manifest_path, normalized_duration = set_channel_permission_tier(
        request.source_channel_id,
        request.to_tier,
        duration=duration,
        home=router.home,
    )
    router.replace_cached_manifest(updated)
    log.info(
        "permission.upgrade_granted",
        extra={
            "channel": request.source_channel_id,
            "approver": clicker_user_id,
            "duration": normalized_duration,
        },
    )
    await post_upgrade_result_in_channel(
        slack_client,
        channel_id=request.source_channel_id,
        message_ts=request.source_message_ts,
        approved=True,
        tier=request.to_tier,
        approver_user_id=clicker_user_id,
    )
    await _maybe_post_safe_tier_downgrade_notice(
        slack_client,
        channel_id=request.source_channel_id,
        previous=previous,
        updated=updated,
    )
    await update_upgrade_request_dm(
        slack_client,
        channel_id=request.owner_dm_channel_id,
        message_ts=request.owner_dm_message_ts,
        text=f"✅ Approved by <@{clicker_user_id}>.",
        detail=(
            f"*Channel:* {request.source_channel_label} ({request.source_channel_id})"
            f" • *Tier:* `{request.to_tier.value}`"
            f" • *Duration:* `{normalized_duration}`"
        ),
    )
    _discard_pending_upgrade_request(request)
    return {"ok": True, "decision": "approve", "duration": normalized_duration}


async def handle_yolo_action(
    *,
    payload: dict[str, object],
    router: Router,
    config: EngramConfig,
    slack_client,
    action_kind: str,
) -> dict[str, object]:
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "error": "no actions"}

    action = actions[0]
    action_id = str(action.get("action_id") or "")
    expected_pattern = (
        YOLO_EXTEND_ACTION_ID_PATTERN
        if action_kind == "extend"
        else YOLO_REVOKE_ACTION_ID_PATTERN
    )
    if not expected_pattern.match(action_id):
        return {"ok": False, "error": "unsupported action"}

    clicker_user_id = str(payload.get("user", {}).get("id") or "") or None
    source_channel_id = str(payload.get("channel", {}).get("id") or "")
    if not _is_owner_user(config=config, user_id=clicker_user_id):
        await _post_ephemeral_reply(
            slack_client,
            channel_id=source_channel_id,
            user_id=clicker_user_id,
            text="Owner-only.",
        )
        return {"ok": False, "error": "not owner"}

    channel_id = _channel_id_from_yolo_action(action_id, str(action.get("value") or ""))
    if channel_id is None:
        return {"ok": False, "error": "malformed action"}

    if action_kind == "extend":
        result = await _extend_yolo_grant(
            router=router,
            config=config,
            slack_client=slack_client,
            channel_id=channel_id,
            duration="6h",
        )
        if result["ok"]:
            text = (
                f"Extended {result['label']} by 6h. "
                f"Remaining: {result['remaining_text']}."
            )
        else:
            text = str(result["message"])
    else:
        result = await _revoke_yolo_grant(
            router=router,
            config=config,
            slack_client=slack_client,
            channel_id=channel_id,
        )
        text = (
            f"Revoked yolo for {result['label']}."
            if result["ok"]
            else str(result["message"])
        )

    await _post_ephemeral_reply(
        slack_client,
        channel_id=source_channel_id,
        user_id=clicker_user_id,
        text=text,
    )
    return result


async def handle_yolo_duration_action(
    *,
    payload: dict[str, object],
    router: Router,
    config: EngramConfig,
    slack_client,
) -> dict[str, object]:
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "error": "no actions"}

    action = actions[0]
    action_id = str(action.get("action_id") or "")
    if not YOLO_DURATION_ACTION_PATTERN.match(action_id):
        return {"ok": False, "error": "unsupported action"}

    clicker_user_id = str(payload.get("user", {}).get("id") or "") or None
    if not _is_owner_user(config=config, user_id=clicker_user_id):
        return {
            "ok": False,
            "error": "not owner",
            "response": _replace_original_ephemeral(text="Owner-only."),
        }

    parsed = _decode_yolo_duration_value(str(action.get("value") or ""))
    if parsed is None:
        return {
            "ok": False,
            "error": "malformed value",
            "response": _replace_original_ephemeral(
                text="Could not update YOLO duration."
            ),
        }
    channel_id, duration = parsed
    if duration == "cancel":
        return {
            "ok": True,
            "cancelled": True,
            "response": _replace_original_ephemeral(
                text="YOLO cancelled. Current tier unchanged."
            ),
        }

    result = await _activate_yolo_grant(
        router=router,
        slack_client=slack_client,
        channel_id=channel_id,
        duration=duration,
        clicker_user_id=clicker_user_id,
    )
    return {
        **result,
        "response": _replace_original_ephemeral(
            text=(
                str(result["ack_text"])
                if result["ok"]
                else str(result["message"])
            )
        ),
    }


def parse_meta_eligibility_command(text: str) -> MetaEligibilityCommand | None:
    stripped = text.strip()
    if not stripped:
        return None

    parts = stripped.split(maxsplit=1)
    command = parts[0].lstrip("/").lower()
    if command == "engram":
        subparts = [part for part in (parts[1] if len(parts) > 1 else "").split(maxsplit=1) if part]
        if subparts and subparts[0].lower() in {"exclude", "include"}:
            return MetaEligibilityCommand(
                subparts[0].lower() == "include",
                _normalize_target_text(subparts[1] if len(subparts) > 1 else None),
            )
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


async def _post_ephemeral_reply(
    slack_client,
    *,
    channel_id: str,
    user_id: str | None,
    text: str,
    blocks: list[dict[str, object]] | None = None,
) -> None:
    if not channel_id:
        return
    if user_id:
        chat_post_ephemeral = getattr(slack_client, "chat_postEphemeral", None)
        if chat_post_ephemeral is not None:
            try:
                payload: dict[str, object] = {
                    "channel": channel_id,
                    "user": user_id,
                    "text": text,
                }
                if blocks is not None:
                    payload["blocks"] = blocks
                await chat_post_ephemeral(
                    **payload,
                )
                return
            except Exception:
                log.info(
                    "ingress.ephemeral_reply_failed channel=%s user=%s",
                    channel_id,
                    user_id,
                    exc_info=True,
                )
    payload = {"channel": channel_id, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    await slack_client.chat_postMessage(**payload)


async def _maybe_respond_with_dashboard(*, result: dict[str, object], respond) -> None:
    response = result.get("response")
    if not response or respond is None:
        return
    await respond(**response)


def _replace_original_ephemeral(
    *,
    text: str,
    blocks: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "response_type": "ephemeral",
        "replace_original": True,
        "text": text,
    }
    if blocks is not None:
        payload["blocks"] = blocks
    return payload


async def _maybe_post_safe_tier_downgrade_notice(
    slack_client,
    *,
    channel_id: str,
    previous: ChannelManifest,
    updated: ChannelManifest,
) -> None:
    if (
        previous.tier_effective() != PermissionTier.TASK_ASSISTANT
        and updated.tier_effective() == PermissionTier.TASK_ASSISTANT
        and previous.nightly_included
        and not updated.nightly_included
    ):
        await slack_client.chat_postMessage(
            channel=channel_id,
            text=SAFE_TIER_DOWNGRADE_NOTICE,
        )


def _channels_dashboard_replace_original(
    *,
    text: str,
    blocks: list[dict[str, object]],
) -> dict[str, object]:
    return _replace_original_ephemeral(text=text, blocks=blocks)


async def _set_channel_tier_from_request(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    channel_id: str,
    channel_name: str | None,
    clicker_user_id: str | None,
    target_tier: PermissionTier,
    yolo_duration: str | None = None,
) -> dict[str, object]:
    session = await router.get(
        channel_id,
        channel_name=_channel_label_from_name(channel_name),
        is_dm=channel_id.startswith("D"),
    )
    manifest = session.manifest
    current_tier = (
        manifest.tier_effective()
        if manifest is not None
        else PermissionTier.TASK_ASSISTANT
    )
    transition_kind = _classify_tier_transition(
        current_tier=current_tier,
        target_tier=target_tier,
    )
    if transition_kind == "upgrade" and not config.owner_user_id:
        return {
            "ok": False,
            "error": "owner user is not configured",
            "message": (
                "Permission upgrades are not configured yet. "
                "Ask the operator to set owner_user_id."
            ),
        }
    decision = _tier_change_decision(
        config=config,
        current_tier=current_tier,
        target_tier=target_tier,
        user_id=clicker_user_id,
    )
    if not decision.allowed:
        return {"ok": False, "error": "not owner", "message": decision.reason}
    if transition_kind == "no-op":
        return {"ok": True, "changed": False, "ack_text": decision.reason}

    if target_tier == PermissionTier.YOLO:
        return await _activate_yolo_grant(
            router=router,
            slack_client=slack_client,
            channel_id=channel_id,
            duration=yolo_duration or _YOLO_DURATION_ALIASES["24"],
            clicker_user_id=clicker_user_id,
        )

    try:
        previous, updated, _manifest_path, _normalized_duration = set_channel_permission_tier(
            channel_id,
            target_tier,
            duration="permanent",
            home=router.home,
        )
    except (ManifestError, ValueError) as exc:
        return {"ok": False, "error": "set tier failed", "message": str(exc)}

    router.replace_cached_manifest(updated)
    await slack_client.chat_postMessage(
        channel=channel_id,
        text=_tier_change_public_notice(
            previous_tier=current_tier,
            target_tier=target_tier,
            clicker_user_id=clicker_user_id,
        ),
    )
    return {
        "ok": True,
        "changed": previous != updated,
        "tier": updated.permission_tier.value,
        "ack_text": _tier_change_ack_text(target_tier),
    }


async def _set_dashboard_nightly_included(
    *,
    router: Router,
    channel_id: str,
    nightly_included: bool,
) -> ChannelManifest:
    _previous, updated, _manifest_path = set_channel_nightly_included(
        channel_id,
        nightly_included,
        home=router.home,
    )
    router.replace_cached_manifest(updated)
    return updated


async def _collect_channels_dashboard_rows(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
) -> list[ChannelDashboardRow]:
    home = router.home or paths.engram_home()
    owner_dm_channel_id = config.owner_dm_channel_id or router.owner_dm_channel_id
    manifests: list[ChannelManifest] = []
    for manifest_path in sorted(
        paths.contexts_dir(home).glob("*/.claude/channel-manifest.yaml")
    ):
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError:
            continue
        if manifest.status != ChannelStatus.ACTIVE:
            continue
        manifests.append(manifest)

    infos = await asyncio.gather(
        *[
            _fetch_channels_dashboard_info(
                slack_client,
                channel_id=manifest.channel_id,
            )
            for manifest in manifests
        ]
    )
    rows = [
        _build_channels_dashboard_row(
            manifest=manifest,
            channel_info=channel_info,
            owner_dm_channel_id=owner_dm_channel_id,
        )
        for manifest, channel_info in zip(manifests, infos, strict=False)
    ]
    rows.sort(key=_channels_dashboard_sort_key)
    return rows


async def _fetch_channels_dashboard_info(
    slack_client,
    *,
    channel_id: str,
) -> dict[str, Any]:
    conversations_info = getattr(slack_client, "conversations_info", None)
    if conversations_info is None:
        return {}
    try:
        response = await conversations_info(channel=channel_id)
    except Exception:
        return {}
    channel = response.get("channel") if isinstance(response, dict) else None
    return channel if isinstance(channel, dict) else {}


def _build_channels_dashboard_row(
    *,
    manifest: ChannelManifest,
    channel_info: dict[str, Any],
    owner_dm_channel_id: str | None,
) -> ChannelDashboardRow:
    is_owner_dm = bool(
        owner_dm_channel_id and manifest.channel_id == owner_dm_channel_id
    )
    is_private = _channels_dashboard_is_private(
        channel_id=manifest.channel_id,
        channel_info=channel_info,
        is_owner_dm=is_owner_dm,
    )
    is_archived = bool(
        channel_info.get("is_archived") or channel_info.get("is_read_only")
    )
    label = _channels_dashboard_label(
        manifest=manifest,
        channel_info=channel_info,
        is_owner_dm=is_owner_dm,
        is_archived=is_archived,
    )
    return ChannelDashboardRow(
        channel_id=manifest.channel_id,
        label=label,
        sort_label=label.replace(" [archived]", "").casefold(),
        manifest=manifest,
        is_owner_dm=is_owner_dm,
        is_private=is_private,
        is_archived=is_archived,
    )


def _channels_dashboard_is_private(
    *,
    channel_id: str,
    channel_info: dict[str, Any],
    is_owner_dm: bool,
) -> bool:
    if is_owner_dm:
        return False
    if channel_info.get("is_private") or channel_info.get("is_group"):
        return True
    return channel_id.startswith(("D", "G"))


def _channels_dashboard_label(
    *,
    manifest: ChannelManifest,
    channel_info: dict[str, Any],
    is_owner_dm: bool,
    is_archived: bool,
) -> str:
    if is_owner_dm:
        return "owner-dm (you)"

    raw_label = (
        str(channel_info.get("name") or "").strip()
        or str(manifest.label or "").strip()
        or manifest.channel_id
    )
    normalized = raw_label.lstrip("#") or manifest.channel_id
    if is_archived:
        return f"{normalized} [archived]"
    return normalized


def _channels_dashboard_sort_key(row: ChannelDashboardRow) -> tuple[int, str]:
    if row.is_owner_dm:
        return (0, row.sort_label)
    if row.is_archived:
        return (3, row.sort_label)
    return (1 if row.is_private else 2, row.sort_label)


def _render_channels_dashboard(
    rows: list[ChannelDashboardRow],
    *,
    page: int,
    notice: str | None = None,
) -> tuple[str, list[dict[str, object]], int]:
    text = "Engram channel dashboard"
    if not rows:
        blocks: list[dict[str, object]] = []
        if notice:
            blocks.append(_channels_dashboard_notice_block(notice))
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "No Engram-enabled channels yet. "
                        "Add Engram to a channel and re-run `/engram channels`."
                    ),
                },
            }
        )
        return text, blocks, 0

    total_pages = (len(rows) + CHANNELS_DASHBOARD_PAGE_SIZE - 1) // CHANNELS_DASHBOARD_PAGE_SIZE
    page_index = max(0, min(page, total_pages - 1))
    start = page_index * CHANNELS_DASHBOARD_PAGE_SIZE
    page_rows = rows[start : start + CHANNELS_DASHBOARD_PAGE_SIZE]

    blocks = []
    if notice:
        blocks.append(_channels_dashboard_notice_block(notice))
    header = (
        f"Engram channels (page {page_index + 1}/{total_pages})"
        if total_pages > 1
        else "Engram channels"
    )
    blocks.append(
        {
            "type": "section",
            "text": {"type": "plain_text", "text": header, "emoji": True},
        }
    )
    for row in page_rows:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": _channels_dashboard_row_text(row),
                    "emoji": True,
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": _channels_dashboard_block_id(
                    page=page_index,
                    channel_id=row.channel_id,
                ),
                "elements": _channels_dashboard_row_actions(row),
            }
        )

    if total_pages > 1:
        nav_elements: list[dict[str, object]] = []
        if page_index > 0:
            nav_elements.append(
                _dashboard_button(
                    text="Previous",
                    action_id=_channels_page_action_id(target_page=page_index - 1),
                    value=str(page_index - 1),
                )
            )
        if page_index + 1 < total_pages:
            nav_elements.append(
                _dashboard_button(
                    text="Next",
                    action_id=_channels_page_action_id(target_page=page_index + 1),
                    value=str(page_index + 1),
                )
            )
        blocks.append(
            {
                "type": "actions",
                "block_id": _channels_dashboard_block_id(
                    page=page_index,
                    channel_id=None,
                ),
                "elements": nav_elements,
            }
        )

    return text, blocks, page_index


def _channels_dashboard_notice_block(text: str) -> dict[str, object]:
    return {
        "type": "section",
        "text": {"type": "plain_text", "text": text[:3000], "emoji": True},
    }


def _channels_dashboard_row_text(row: ChannelDashboardRow) -> str:
    tier = row.manifest.tier_effective()
    prefix = "📬" if row.is_owner_dm else "💬"
    if tier == PermissionTier.YOLO and row.manifest.yolo_until is not None:
        nightly = (
            "Included in nightly ✓"
            if row.manifest.nightly_included
            else "Nightly: excluded"
        )
        return (
            f"{prefix} {row.label} [{_channels_dashboard_tier_label(tier)}"
            f" • {_channels_dashboard_expiry_text(row.manifest.yolo_until)}]\n"
            f"{nightly}"
        )
    if tier == PermissionTier.TASK_ASSISTANT:
        return (
            f"{prefix} {row.label} [{_channels_dashboard_tier_label(tier)}]\n"
            "Nightly: excluded (safe tier)"
        )
    nightly = (
        "Included in nightly ✓"
        if row.manifest.nightly_included
        else "Nightly: excluded"
    )
    return (
        f"{prefix} {row.label} [{_channels_dashboard_tier_label(tier)}]\n"
        f"{nightly}"
    )


def _channels_dashboard_tier_label(tier: PermissionTier) -> str:
    if tier == PermissionTier.TASK_ASSISTANT:
        return "🔒 safe"
    if tier == PermissionTier.YOLO:
        return "🚀 yolo"
    return "✨ trusted"


def _classify_tier_transition(
    *,
    current_tier: PermissionTier,
    target_tier: PermissionTier,
) -> str:
    return classify_transition(current_tier.value, target_tier.value)


def _tier_change_decision(
    *,
    config: EngramConfig,
    current_tier: PermissionTier,
    target_tier: PermissionTier,
    user_id: str | None,
):
    return can_change_tier(
        current_tier=current_tier.value,
        target_tier=target_tier.value,
        invoker_user_id=user_id or "",
        channel_owner_user_id=config.owner_user_id or "",
    )


def _tier_picker_button_text(
    tier: PermissionTier,
    *,
    current_tier: PermissionTier,
    is_owner: bool,
) -> str:
    base = {
        PermissionTier.TASK_ASSISTANT: "🔒 Safe",
        PermissionTier.OWNER_SCOPED: "✨ Trusted",
        PermissionTier.YOLO: "🚀 YOLO",
    }[tier]
    transition_kind = _classify_tier_transition(
        current_tier=current_tier,
        target_tier=tier,
    )
    if transition_kind == "no-op":
        return f"{base} (current)"
    if not is_owner and transition_kind == "upgrade":
        return f"{base} (owner only)"
    return base


def _tier_picker_button_style(
    tier: PermissionTier,
    *,
    current_tier: PermissionTier,
    is_owner: bool,
) -> str | None:
    transition_kind = _classify_tier_transition(
        current_tier=current_tier,
        target_tier=tier,
    )
    if transition_kind == "no-op":
        return None
    if not is_owner and transition_kind == "upgrade":
        return None
    return "primary"


def build_tier_picker_blocks(
    *,
    channel_id: str,
    current_tier: PermissionTier,
    is_owner: bool,
    invoker_user_id: str | None,
) -> tuple[str, list[dict[str, object]]]:
    current_text = f"Current tier: {current_tier.value}"
    if is_owner:
        intro = "Pick a new tier:"
    else:
        has_downgrade = any(
            _classify_tier_transition(current_tier=current_tier, target_tier=tier)
            == "downgrade"
            for tier in PermissionTier
        )
        if has_downgrade:
            intro = "Only the channel owner can upgrade. You can downgrade:"
        else:
            intro = (
                "Only the channel owner can upgrade. "
                "This channel is already on the lowest tier."
            )

    buttons = [
        _dashboard_button(
            text=_tier_picker_button_text(
                tier,
                current_tier=current_tier,
                is_owner=is_owner,
            ),
            action_id=_tier_pick_action_id(tier=tier),
            value=f"{channel_id}|{tier.value}|{invoker_user_id or ''}",
            style=_tier_picker_button_style(
                tier,
                current_tier=current_tier,
                is_owner=is_owner,
            ),
        )
        for tier in (
            PermissionTier.TASK_ASSISTANT,
            PermissionTier.OWNER_SCOPED,
            PermissionTier.YOLO,
        )
    ]
    text = f"{current_text}\n{intro}"
    return text, [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{current_text}*\n{intro}"},
        },
        {
            "type": "actions",
            "block_id": f"{UPGRADE_PICKER_BLOCK_ID_PREFIX}{channel_id}",
            "elements": buttons,
        },
    ]


def _decode_upgrade_picker_value(
    raw: str,
) -> tuple[str, PermissionTier, str] | None:
    channel_id, first_sep, remainder = raw.partition("|")
    if not first_sep or not CHANNEL_ID_PATTERN.match(channel_id):
        return None
    target_tier_raw, second_sep, invoker_user_id = remainder.partition("|")
    if not second_sep or not invoker_user_id:
        return None
    try:
        target_tier, _deprecated_alias = parse_permission_tier(target_tier_raw)
    except ValueError:
        return None
    return channel_id, target_tier, invoker_user_id


def _tier_change_summary(target_tier: PermissionTier) -> str:
    if target_tier == PermissionTier.TASK_ASSISTANT:
        return "Read-only tools no longer auto-allow."
    if target_tier == PermissionTier.OWNER_SCOPED:
        return "Read-only tools now auto-allow."
    return "HITL gates are bypassed temporarily."


def _tier_change_icon(target_tier: PermissionTier) -> str:
    if target_tier == PermissionTier.TASK_ASSISTANT:
        return "🔒"
    if target_tier == PermissionTier.YOLO:
        return "🚀"
    return "✨"


def _tier_change_ack_text(target_tier: PermissionTier) -> str:
    return f"Tier set to `{target_tier.value}`. {_tier_change_summary(target_tier)}"


def _tier_change_public_notice(
    *,
    previous_tier: PermissionTier,
    target_tier: PermissionTier,
    clicker_user_id: str | None,
) -> str:
    actor = f"<@{clicker_user_id}>" if clicker_user_id else "Someone"
    verb = (
        "upgraded"
        if _classify_tier_transition(
            current_tier=previous_tier,
            target_tier=target_tier,
        )
        == "upgrade"
        else "downgraded"
    )
    return (
        f"{_tier_change_icon(target_tier)} {actor} {verb} this channel to "
        f"`{target_tier.value}`. {_tier_change_summary(target_tier)} "
        "Type `/engram` to see current settings."
    )


def _channels_dashboard_expiry_text(expires_at: datetime.datetime) -> str:
    local = expires_at.astimezone(_PACIFIC_TZ)
    remaining = max(
        datetime.timedelta(),
        expires_at - datetime.datetime.now(datetime.UTC),
    )
    return (
        f"expires {local.strftime('%Y-%m-%d %H:%M %Z')} "
        f"({_format_duration(remaining)} left)"
    )


def _channels_dashboard_row_actions(
    row: ChannelDashboardRow,
) -> list[dict[str, object]]:
    tier = row.manifest.tier_effective()
    channel_id = row.channel_id
    buttons: list[dict[str, object]] = []
    if tier == PermissionTier.TASK_ASSISTANT:
        buttons.append(
            _dashboard_button(
                text="Upgrade",
                action_id=_tier_pick_action_id(
                    tier=PermissionTier.OWNER_SCOPED,
                    channel_id=channel_id,
                ),
                value=f"{channel_id}|{PermissionTier.OWNER_SCOPED.value}",
                style="primary",
            )
        )
        buttons.append(
            _dashboard_button(
                text="Exclude from nightly ✓",
                action_id=_nightly_toggle_action_id(
                    mode="exclude",
                    channel_id=channel_id,
                ),
                value=f"{channel_id}|exclude",
            )
        )
        return buttons

    if tier == PermissionTier.YOLO:
        target_tier = row.manifest.pre_yolo_tier or PermissionTier.TASK_ASSISTANT
        buttons.append(
            _dashboard_button(
                text="Downgrade",
                action_id=_tier_pick_action_id(
                    tier=target_tier,
                    channel_id=channel_id,
                ),
                value=f"{channel_id}|{target_tier.value}",
            )
        )
        buttons.append(
            _dashboard_button(
                text="Extend YOLO",
                action_id=_tier_pick_action_id(
                    tier=PermissionTier.YOLO,
                    channel_id=channel_id,
                ),
                value=f"{channel_id}|{PermissionTier.YOLO.value}",
            )
        )
    else:
        buttons.append(
            _dashboard_button(
                text="Upgrade to YOLO",
                action_id=_tier_pick_action_id(
                    tier=PermissionTier.YOLO,
                    channel_id=channel_id,
                ),
                value=f"{channel_id}|{PermissionTier.YOLO.value}",
                style="primary",
            )
        )
        buttons.append(
            _dashboard_button(
                text="Downgrade to Safe",
                action_id=_tier_pick_action_id(
                    tier=PermissionTier.TASK_ASSISTANT,
                    channel_id=channel_id,
                ),
                value=f"{channel_id}|{PermissionTier.TASK_ASSISTANT.value}",
            )
        )

    include = not row.manifest.nightly_included
    buttons.append(
        _dashboard_button(
            text="Include in nightly" if include else "Exclude from nightly",
            action_id=_nightly_toggle_action_id(
                mode="include" if include else "exclude",
                channel_id=channel_id,
            ),
            value=f"{channel_id}|{'include' if include else 'exclude'}",
        )
    )
    return buttons


def _dashboard_button(
    *,
    text: str,
    action_id: str,
    value: str,
    style: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": value,
    }
    if style is not None:
        payload["style"] = style
    return payload


def _tier_pick_action_id(
    *,
    tier: PermissionTier,
    channel_id: str | None = None,
) -> str:
    if channel_id:
        return f"{ACTION_ID_TIER_PICK_PREFIX}:{tier.value}:{channel_id}"
    return f"{ACTION_ID_TIER_PICK_PREFIX}:{tier.value}"


def _yolo_duration_action_id(*, choice: str, channel_id: str) -> str:
    return f"{ACTION_ID_YOLO_DURATION_PREFIX}:{choice}:{channel_id}"


def _nightly_toggle_action_id(*, mode: str, channel_id: str) -> str:
    return f"{ACTION_ID_NIGHTLY_TOGGLE_PREFIX}:{mode}:{channel_id}"


def _channels_page_action_id(*, target_page: int) -> str:
    return f"{ACTION_ID_CHANNELS_PAGE_PREFIX}:{target_page}"


def _channels_dashboard_block_id(*, page: int, channel_id: str | None) -> str:
    kind = "nav" if channel_id is None else f"channel:{channel_id}"
    return f"engram_channels:{page}:{kind}"


def _channels_dashboard_page_from_action(action: dict[str, object]) -> int:
    block_id = str(action.get("block_id") or "")
    match = CHANNELS_DASHBOARD_BLOCK_ID_PATTERN.match(block_id)
    if match is None:
        return 0
    return int(match.group("page"))


def _decode_channels_dashboard_pair(raw: str) -> tuple[str, str] | None:
    channel_id, sep, value = raw.partition("|")
    if not sep or not CHANNEL_ID_PATTERN.match(channel_id) or not value:
        return None
    return channel_id, value


def _decode_yolo_duration_value(raw: str) -> tuple[str, str] | None:
    parsed = _decode_channels_dashboard_pair(raw)
    if parsed is None:
        return None
    channel_id, duration = parsed
    normalized = str(duration or "").strip().lower()
    if normalized == "cancel":
        return channel_id, normalized
    try:
        return channel_id, _normalize_yolo_duration(normalized)
    except ValueError:
        return None


def _decode_channels_page_value(raw: str) -> int | None:
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _encode_upgrade_action_value(*, request_id: str, channel_id: str) -> str:
    return (
        f'{{"channel_id":"{channel_id}","request_id":"{request_id}"}}'
    )


def _decode_upgrade_action_value(raw: str) -> dict[str, str] | None:
    match = re.fullmatch(
        r'\{"channel_id":"(?P<channel_id>[A-Z0-9]+)","request_id":"(?P<request_id>[^"]+)"\}',
        raw,
    )
    if match is None:
        return None
    return {
        "channel_id": match.group("channel_id"),
        "request_id": match.group("request_id"),
    }


def _upgrade_duration_from_action(action_id: str) -> str:
    if action_id == "upgrade_decision_approve_permanent":
        return "permanent"
    if action_id == "upgrade_decision_approve_30d":
        return "30d"
    if action_id == "upgrade_decision_approve_24h":
        return "24h"
    if action_id == "upgrade_decision_approve_6h":
        return "6h"
    raise ValueError(f"unsupported upgrade action {action_id}")


async def _supersede_pending_upgrade_for_channel(
    *,
    source_channel_id: str,
    slack_client,
) -> None:
    previous_request_id = _PENDING_UPGRADE_REQUESTS_BY_CHANNEL.get(source_channel_id)
    if previous_request_id is None:
        return
    previous = _PENDING_UPGRADE_REQUESTS.get(previous_request_id)
    if previous is None:
        _PENDING_UPGRADE_REQUESTS_BY_CHANNEL.pop(source_channel_id, None)
        return
    await update_upgrade_request_dm(
        slack_client,
        channel_id=previous.owner_dm_channel_id,
        message_ts=previous.owner_dm_message_ts,
        text="⚪ Superseded by newer request.",
        detail="_Only the newest request in a channel stays actionable._",
    )
    _discard_pending_upgrade_request(previous)


def _discard_pending_upgrade_request(request: PendingUpgradeRequest) -> None:
    _PENDING_UPGRADE_REQUESTS.pop(request.request_id, None)
    current = _PENDING_UPGRADE_REQUESTS_BY_CHANNEL.get(request.source_channel_id)
    if current == request.request_id:
        _PENDING_UPGRADE_REQUESTS_BY_CHANNEL.pop(request.source_channel_id, None)


def _normalize_target_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    target = raw.strip()
    if not target or target.lower() in {"this", "this channel"}:
        return None
    return target


def _is_owner_user(*, config: EngramConfig, user_id: str | None) -> bool:
    return bool(config.owner_user_id and user_id == config.owner_user_id)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _list_active_yolo_grants(home, *, now: datetime.datetime | None = None) -> list[ActiveYoloGrantRow]:
    current_time = now or datetime.datetime.now(datetime.UTC)
    grants: list[ActiveYoloGrantRow] = []
    for manifest_path in sorted(paths.contexts_dir(home).glob("*/.claude/channel-manifest.yaml")):
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError:
            continue
        if manifest.permission_tier != PermissionTier.YOLO or manifest.yolo_until is None:
            continue
        if manifest.yolo_until <= current_time:
            continue
        grants.append(
            ActiveYoloGrantRow(
                channel_id=manifest.channel_id,
                channel_label=manifest.label,
                remaining=manifest.yolo_until - current_time,
                pre_yolo_tier=manifest.pre_yolo_tier or PermissionTier.TASK_ASSISTANT,
            )
        )
    return grants


def _resolve_yolo_target(
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

    wanted = first_token.lstrip("#").lower()
    for manifest_path in sorted(paths.contexts_dir(home).glob("*/.claude/channel-manifest.yaml")):
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError:
            continue
        labels = {manifest.channel_id.lower()}
        if manifest.label:
            labels.add(manifest.label.lower())
            labels.add(manifest.label.lstrip("#").lower())
        if first_token.lower() in labels or wanted in labels:
            return manifest.channel_id
    return None


def _channel_id_from_yolo_action(action_id: str, raw_value: str) -> str | None:
    if CHANNEL_ID_PATTERN.match(raw_value):
        return raw_value
    parts = action_id.split("_")
    if len(parts) < 3:
        return None
    candidate = parts[-1]
    return candidate if CHANNEL_ID_PATTERN.match(candidate) else None


def _normalize_yolo_duration(duration: str) -> str:
    raw = str(duration or "").strip().lower()
    normalized = validate_upgrade_duration(_YOLO_DURATION_ALIASES.get(raw, raw))
    if normalized not in YOLO_DURATION_CHOICES:
        raise ValueError("Duration must be one of 6h, 24h, or 72h.")
    return normalized


def _yolo_extension_delta(duration: str) -> datetime.timedelta:
    return {
        "6h": datetime.timedelta(hours=6),
        "24h": datetime.timedelta(hours=24),
        "72h": datetime.timedelta(hours=72),
    }[duration]


def _load_active_yolo_manifest(
    *,
    channel_id: str,
    home,
    now: datetime.datetime | None = None,
) -> ChannelManifest | None:
    manifest_path = paths.channel_manifest_path(channel_id, home)
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError:
        return None
    current_time = now or datetime.datetime.now(datetime.UTC)
    if (
        manifest.permission_tier != PermissionTier.YOLO
        or manifest.yolo_until is None
        or manifest.yolo_until <= current_time
    ):
        return None
    return manifest


async def _revoke_yolo_grant(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    channel_id: str,
) -> dict[str, object]:
    home = router.home or paths.engram_home()
    manifest = _load_active_yolo_manifest(channel_id=channel_id, home=home)
    if manifest is None:
        return {
            "ok": False,
            "error": "no active yolo grant",
            "message": f"No active yolo grant for {channel_id}.",
        }

    previous, updated, _manifest_path, _normalized_duration = set_channel_permission_tier(
        channel_id,
        manifest.pre_yolo_tier or PermissionTier.TASK_ASSISTANT,
        duration="permanent",
        home=home,
    )
    router.replace_cached_manifest(updated)
    label = _manifest_display_label(previous)
    await slack_client.chat_postMessage(channel=channel_id, text="YOLO ended by owner")
    await _maybe_post_safe_tier_downgrade_notice(
        slack_client,
        channel_id=channel_id,
        previous=previous,
        updated=updated,
    )

    owner_dm_channel_id = config.owner_dm_channel_id or router.owner_dm_channel_id
    if owner_dm_channel_id:
        await slack_client.chat_postMessage(
            channel=owner_dm_channel_id,
            text=f"YOLO ended on {label} — restored to {updated.permission_tier.value}.",
        )

    return {
        "ok": True,
        "channel_id": channel_id,
        "label": label,
        "tier": updated.permission_tier.value,
    }


async def _extend_yolo_grant(
    *,
    router: Router,
    config: EngramConfig,
    slack_client,
    channel_id: str,
    duration: str,
) -> dict[str, object]:
    home = router.home or paths.engram_home()
    try:
        normalized_duration = _normalize_yolo_duration(duration)
    except ValueError as exc:
        return {"ok": False, "error": "invalid duration", "message": str(exc)}

    current_time = _utc_now()
    manifest = _load_active_yolo_manifest(
        channel_id=channel_id,
        home=home,
        now=current_time,
    )
    if manifest is None:
        return {
            "ok": False,
            "error": "no active yolo grant",
            "message": f"No active yolo grant for {channel_id}.",
        }

    remaining = manifest.yolo_until - current_time
    requested_delta = _yolo_extension_delta(normalized_duration)
    if remaining + requested_delta > YOLO_MAX_DURATION:
        return {
            "ok": False,
            "error": "yolo cap exceeded",
            "message": "Cannot extend beyond 72h total remaining.",
        }

    _previous, updated, _manifest_path, _normalized = set_channel_permission_tier(
        channel_id,
        PermissionTier.YOLO,
        duration=normalized_duration,
        home=home,
        now=current_time,
    )
    router.replace_cached_manifest(updated)
    new_remaining = (
        updated.yolo_until - current_time
        if updated.yolo_until is not None
        else datetime.timedelta()
    )
    label = _manifest_display_label(updated)
    owner_dm_channel_id = config.owner_dm_channel_id or router.owner_dm_channel_id
    if owner_dm_channel_id:
        await slack_client.chat_postMessage(
            channel=owner_dm_channel_id,
            text=(
                f"YOLO extended on {label} by {normalized_duration}. "
                f"Remaining: {_format_duration(new_remaining)}."
            ),
        )

    return {
        "ok": True,
        "channel_id": channel_id,
        "label": label,
        "duration": normalized_duration,
        "remaining_text": _format_duration(new_remaining),
    }


def _render_yolo_duration_picker(
    *,
    channel_id: str,
) -> tuple[str, list[dict[str, object]]]:
    buttons = [
        _dashboard_button(
            text="⏱️ 6h",
            action_id=_yolo_duration_action_id(choice="6", channel_id=channel_id),
            value=f"{channel_id}|6",
        ),
        _dashboard_button(
            text="⏱️ 24h",
            action_id=_yolo_duration_action_id(choice="24", channel_id=channel_id),
            value=f"{channel_id}|24",
        ),
        _dashboard_button(
            text="⏱️ 72h",
            action_id=_yolo_duration_action_id(choice="72", channel_id=channel_id),
            value=f"{channel_id}|72",
        ),
        _dashboard_button(
            text="✕ Cancel",
            action_id=_yolo_duration_action_id(
                choice="cancel",
                channel_id=channel_id,
            ),
            value=f"{channel_id}|cancel",
        ),
    ]
    return _YOLO_DURATION_PICKER_TEXT, [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _YOLO_DURATION_PICKER_TEXT},
        },
        {
            "type": "actions",
            "block_id": f"engram_yolo_duration:{channel_id}",
            "elements": buttons,
        },
    ]


async def _slack_user_timezone(
    slack_client,
    *,
    user_id: str | None,
) -> datetime.tzinfo | None:
    if not user_id:
        return None
    users_info = getattr(slack_client, "users_info", None)
    if users_info is None:
        return None
    try:
        response = await users_info(user=user_id)
    except Exception:
        log.info(
            "ingress.yolo_timezone_lookup_failed user=%s",
            user_id,
            exc_info=True,
        )
        return None
    user = response.get("user") if isinstance(response, dict) else None
    tz_name = user.get("tz") if isinstance(user, dict) else None
    if not tz_name:
        return None
    with contextlib.suppress(Exception):
        return ZoneInfo(str(tz_name))
    return None


async def _format_yolo_expiry_for_user(
    slack_client,
    *,
    user_id: str | None,
    expires_at: datetime.datetime,
) -> str:
    timezone = await _slack_user_timezone(slack_client, user_id=user_id)
    localized = expires_at.astimezone(timezone or datetime.UTC)
    return localized.strftime("%Y-%m-%d %H:%M %Z")


async def _activate_yolo_grant(
    *,
    router: Router,
    slack_client,
    channel_id: str,
    duration: str,
    clicker_user_id: str | None,
) -> dict[str, object]:
    home = router.home or paths.engram_home()
    try:
        normalized_duration = _normalize_yolo_duration(duration)
        current_time = _utc_now()
        existing = load_manifest(paths.channel_manifest_path(channel_id, home))
        was_active_yolo = (
            existing.permission_tier == PermissionTier.YOLO
            and existing.yolo_until is not None
            and existing.yolo_until > current_time
        )
        _previous, updated, _manifest_path, _normalized = set_channel_permission_tier(
            channel_id,
            PermissionTier.YOLO,
            duration=normalized_duration,
            home=home,
            now=current_time,
        )
    except (ManifestError, ValueError) as exc:
        return {"ok": False, "error": "activate failed", "message": str(exc)}

    router.replace_cached_manifest(updated)
    expires_at = updated.yolo_until
    if expires_at is None:
        return {
            "ok": False,
            "error": "activate failed",
            "message": "Could not determine YOLO expiry.",
        }

    expiry_text = await _format_yolo_expiry_for_user(
        slack_client,
        user_id=clicker_user_id,
        expires_at=expires_at,
    )
    verb = "extended" if was_active_yolo else "enabled"
    user_ref = f"<@{clicker_user_id}>" if clicker_user_id else "Owner"
    await slack_client.chat_postMessage(
        channel=channel_id,
        text=(
            f"🚀 {user_ref} {verb} YOLO mode for {normalized_duration}. "
            f"HITL gates bypassed until {expiry_text}. "
            "Destructive-command modals still active."
        ),
    )
    return {
        "ok": True,
        "channel_id": channel_id,
        "duration": normalized_duration,
        "expires_at": expires_at,
        "ack_text": (
            f"YOLO {verb} for {normalized_duration} "
            f"(expires {expiry_text}). Type `/engram` to manage."
        ),
    }


def _format_duration(duration: datetime.timedelta) -> str:
    total_minutes = max(0, int(duration.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"


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


def _footgun_confirmation_value(view: dict) -> str:
    state_values = view.get("state", {}).get("values", {})
    input_block = state_values.get("footgun_confirm_input", {})
    input_value = input_block.get("confirmation_text", {}).get("value")
    return str(input_value or "")


async def _resolve_footgun_question(
    q: PendingQuestion,
    router,
    slack_client,
    *,
    confirmed: bool,
    user_id: str | None,
    reason: str,
) -> None:
    tier = (
        q.channel_manifest.tier_effective().value
        if q.channel_manifest is not None
        else None
    )
    if confirmed:
        # Preserve the exact reviewed input so typed-confirm confirmations do
        # not accidentally drop a pre-sanitized updated_input.
        result = PermissionResultAllow(updated_input=q.tool_input)
        answer_text = "Confirmed destructive action"
    else:
        result = _resolve_question(q, choice="deny")
        answer_text = "Destructive action denied"

    resolved = router.hitl.resolve(q.permission_request_id, result)
    if not resolved:
        return

    match = q.footgun_match
    if match is not None:
        if confirmed:
            hitl_log.info(
                "footgun.confirmed",
                extra={
                    "pattern": match.pattern.pattern,
                    "command": match.command,
                    "user": user_id,
                    "tier": tier,
                    "duration_to_confirm_ms": max(
                        0,
                        int(
                            (
                                datetime.datetime.now(datetime.UTC) - q.posted_at
                            ).total_seconds()
                            * 1000
                        ),
                    ),
                },
            )
        else:
            hitl_log.info(
                "footgun.cancelled_or_timed_out",
                extra={
                    "pattern": match.pattern.pattern,
                    "command": match.command,
                    "user": user_id,
                    "tier": tier,
                    "reason": reason,
                },
            )

    if q.on_resolve is not None:
        await q.on_resolve(result)

    await update_question_resolved(
        q,
        answer_text,
        slack_client,
        allowed=confirmed,
    )


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
    if q.footgun_match is not None:
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
