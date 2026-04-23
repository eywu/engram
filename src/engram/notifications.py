"""Static Slack notifications for pending-channel approval flows."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    ManifestError,
    load_manifest,
    set_channel_status,
)

log = logging.getLogger(__name__)

PENDING_CHANNEL_ACTION_ID_PATTERN = re.compile(
    r"^pending_channel_(approve|deny|view_manifest)$"
)
_PENDING_NOTIFICATION_WINDOW_S = 60.0
_PENDING_NOTIFICATION_LIMIT = 10
_PENDING_NOTIFICATION_TIMES: deque[float] = deque()
_REDACTED_MANIFEST_KEYS = {"api_key", "secret", "token", "path", "paths"}


def pending_channel_ack_text(
    channel_id: str,
    *,
    owner_dm_channel_id: str | None,
) -> str:
    text = "👋 I've been added to this channel but I'm waiting for my operator to approve me."
    if owner_dm_channel_id:
        return (
            f"{text}\n"
            "An approval request has been sent to the owner. I'll respond once they approve."
        )
    return f"{text}\nAsk your operator to run `engram channels approve {channel_id}`."


async def post_pending_channel_ack(
    slack_client,
    *,
    channel_id: str,
    user_id: str | None,
    thread_ts: str | None,
    owner_dm_channel_id: str | None,
) -> str:
    """Post the one-time pending-channel acknowledgement."""
    text = pending_channel_ack_text(
        channel_id,
        owner_dm_channel_id=owner_dm_channel_id,
    )

    if user_id:
        chat_post_ephemeral = getattr(slack_client, "chat_postEphemeral", None)
        if chat_post_ephemeral is not None:
            try:
                await chat_post_ephemeral(
                    channel=channel_id,
                    user=user_id,
                    text=text,
                )
                return text
            except Exception:
                log.info(
                    "notifications.pending_ack_ephemeral_failed channel_id=%s user_id=%s",
                    channel_id,
                    user_id,
                    exc_info=True,
                )

    await slack_client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=text,
    )
    return text


async def notify_pending_channel(
    *,
    slack_client,
    owner_dm_channel_id: str | None,
    channel_id: str,
    channel_label: str,
    invited_by_user_id: str | None,
    template: str,
    first_message: str,
    source_thread_ts: str | None,
) -> str | None:
    """Post the owner-DM approval card for a newly pending channel."""
    if not owner_dm_channel_id:
        log.warning(
            "notifications.pending_owner_dm_unset channel_id=%s",
            channel_id,
        )
        return None
    if not _allow_pending_channel_notification():
        log.warning(
            "notifications.pending_owner_dm_rate_limited channel_id=%s owner_dm=%s",
            channel_id,
            owner_dm_channel_id,
        )
        return None

    action_value = _encode_action_value(
        channel_id=channel_id,
        source_thread_ts=source_thread_ts,
    )
    preview = _escape_mrkdwn(_truncate(first_message.strip() or "(empty)", 160))
    invited_by = f"<@{invited_by_user_id}>" if invited_by_user_id else "_unknown_"
    title = "📥 *New channel awaiting approval*"
    details = "\n".join(
        [
            f"• *Channel:* {channel_label} ({channel_id})",
            f"• *Invited by:* {invited_by}",
            f"• *Template:* {template}",
            f'• *First message:* "{preview}"',
        ]
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": details}},
        {
            "type": "actions",
            "block_id": "pending_channel_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": action_value,
                    "action_id": "pending_channel_approve",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "value": action_value,
                    "action_id": "pending_channel_deny",
                    "style": "danger",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View manifest"},
                    "value": action_value,
                    "action_id": "pending_channel_view_manifest",
                },
            ],
        },
    ]
    response = await slack_client.chat_postMessage(
        channel=owner_dm_channel_id,
        text="New channel awaiting approval",
        blocks=blocks,
    )
    log.info(
        "notifications.pending_owner_dm_posted channel_id=%s owner_dm=%s",
        channel_id,
        owner_dm_channel_id,
    )
    return response.get("ts")


async def handle_pending_channel_action(
    payload: dict[str, Any],
    router,
    slack_client,
) -> dict[str, object]:
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "error": "no actions"}

    action = actions[0]
    action_id = str(action.get("action_id") or "")
    if not PENDING_CHANNEL_ACTION_ID_PATTERN.match(action_id):
        return {"ok": False, "error": "unsupported action"}

    if router.home is None:
        return {"ok": False, "error": "router home is unavailable"}

    owner_dm_channel_id = payload.get("channel", {}).get("id")
    if (
        router.owner_dm_channel_id is not None
        and owner_dm_channel_id != router.owner_dm_channel_id
    ):
        return {"ok": False, "error": "wrong surface"}

    request = _decode_action_value(action.get("value", ""))
    if request is None:
        return {"ok": False, "error": "malformed value"}

    if action_id == "pending_channel_approve":
        return await handle_approval_button(
            payload=payload,
            router=router,
            slack_client=slack_client,
            channel_id=request["channel_id"],
            source_thread_ts=request.get("source_thread_ts"),
        )
    if action_id == "pending_channel_deny":
        return await handle_denial_button(
            payload=payload,
            router=router,
            slack_client=slack_client,
            channel_id=request["channel_id"],
        )
    return await handle_manifest_view_button(
        payload=payload,
        router=router,
        slack_client=slack_client,
        channel_id=request["channel_id"],
    )


async def handle_approval_button(
    *,
    payload: dict[str, Any],
    router,
    slack_client,
    channel_id: str,
    source_thread_ts: str | None,
) -> dict[str, object]:
    clicker_user_id = payload.get("user", {}).get("id")
    previous, updated, _manifest_path = set_channel_status(
        channel_id,
        ChannelStatus.ACTIVE,
        home=router.home,
    )
    await router.invalidate(channel_id)
    await _update_pending_owner_dm_request(
        payload,
        slack_client,
        channel_id=channel_id,
        channel_label=updated.label or channel_id,
        status_label="Approved",
        clicker_user_id=clicker_user_id,
    )
    if previous.status != ChannelStatus.ACTIVE:
        await slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=source_thread_ts,
            text=f"✅ Approved by <@{clicker_user_id}>. Standing by.",
        )
    return {"ok": True, "channel_id": channel_id, "status": updated.status}


async def handle_denial_button(
    *,
    payload: dict[str, Any],
    router,
    slack_client,
    channel_id: str,
) -> dict[str, object]:
    _previous, updated, _manifest_path = set_channel_status(
        channel_id,
        ChannelStatus.DENIED,
        home=router.home,
    )
    await router.invalidate(channel_id)
    await _update_pending_owner_dm_request(
        payload,
        slack_client,
        channel_id=channel_id,
        channel_label=updated.label or channel_id,
        status_label="Denied",
        clicker_user_id=payload.get("user", {}).get("id"),
    )
    return {"ok": True, "channel_id": channel_id, "status": updated.status}


async def handle_manifest_view_button(
    *,
    payload: dict[str, Any],
    router,
    slack_client,
    channel_id: str,
) -> dict[str, object]:
    manifest_path = _manifest_path(router.home, channel_id)
    manifest = load_manifest(manifest_path)
    body = _render_manifest_yaml(manifest)
    await slack_client.chat_postMessage(
        channel=payload.get("channel", {}).get("id"),
        thread_ts=_owner_dm_message_ts(payload),
        text=f"Manifest for {channel_id}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Manifest for {channel_id}*\n```{body}```",
                },
            }
        ],
    )
    return {"ok": True, "channel_id": channel_id}


async def _update_pending_owner_dm_request(
    payload: dict[str, Any],
    slack_client,
    *,
    channel_id: str,
    channel_label: str,
    status_label: str,
    clicker_user_id: str | None,
) -> None:
    user_ref = f"<@{clicker_user_id}>" if clicker_user_id else "_unknown_"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ *{status_label}:* {channel_label} ({channel_id})\n"
                    f"*By:* {user_ref}"
                ),
            },
        }
    ]
    await slack_client.chat_update(
        channel=payload.get("channel", {}).get("id"),
        ts=_owner_dm_message_ts(payload),
        text=f"{status_label}: {channel_id}",
        blocks=blocks,
    )


def _manifest_path(home: Path, channel_id: str) -> Path:
    from engram import paths

    return paths.channel_manifest_path(channel_id, home)


def _owner_dm_message_ts(payload: dict[str, Any]) -> str:
    container_ts = payload.get("container", {}).get("message_ts")
    if container_ts:
        return str(container_ts)
    message_ts = payload.get("message", {}).get("ts")
    if message_ts:
        return str(message_ts)
    raise ManifestError("Owner-DM message timestamp missing from action payload")


def _allow_pending_channel_notification(now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    while (
        _PENDING_NOTIFICATION_TIMES
        and current - _PENDING_NOTIFICATION_TIMES[0]
        > _PENDING_NOTIFICATION_WINDOW_S
    ):
        _PENDING_NOTIFICATION_TIMES.popleft()
    if len(_PENDING_NOTIFICATION_TIMES) >= _PENDING_NOTIFICATION_LIMIT:
        return False
    _PENDING_NOTIFICATION_TIMES.append(current)
    return True


def _encode_action_value(
    *,
    channel_id: str,
    source_thread_ts: str | None,
) -> str:
    return json.dumps(
        {
            "channel_id": channel_id,
            "source_thread_ts": source_thread_ts,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_action_value(raw: str) -> dict[str, str | None] | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    channel_id = parsed.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        return None
    source_thread_ts = parsed.get("source_thread_ts")
    if source_thread_ts is not None and not isinstance(source_thread_ts, str):
        return None
    return {
        "channel_id": channel_id,
        "source_thread_ts": source_thread_ts,
    }


def _render_manifest_yaml(manifest: ChannelManifest) -> str:
    scrubbed = _scrub_manifest(manifest.model_dump(mode="json", exclude_none=False))
    return yaml.safe_dump(
        scrubbed,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
    ).strip()


def _scrub_manifest(data: Any) -> Any:
    if isinstance(data, dict):
        scrubbed: dict[str, Any] = {}
        for key, value in data.items():
            if key.lower() in _REDACTED_MANIFEST_KEYS:
                scrubbed[key] = "[redacted]"
            else:
                scrubbed[key] = _scrub_manifest(value)
        return scrubbed
    if isinstance(data, list):
        return [_scrub_manifest(value) for value in data]
    return data


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _escape_mrkdwn(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
