"""Egress — posts agent responses back to Slack with simple safety rails.

M1 scope:
  - Post the response text to the originating channel/thread.
  - Chunk if message exceeds Slack's markdown block limit.
  - Log cost/duration for dev visibility.

Later: redaction, attachment upload, block-kit formatting, per-channel
rate limiting, auto-retry on Slack rate-limit errors.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from engram.agent import AgentTurn
from engram.footguns import FootgunMatch, match_footgun
from engram.hitl import PendingQuestion
from engram.manifest import PermissionTier
from engram.mcp import resolve_team_mcp_servers
from engram.mcp_health import _extract_servers
from engram.router import SessionState

log = logging.getLogger(__name__)

SLACK_MAX_TEXT_LEN = 12_000  # Slack markdown blocks cap text at 12,000 chars
_STICKY_INELIGIBLE_TOOLS = {
    "Bash",
    "BashOutput",
    "KillShell",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Task",
    "SlashCommand",
}


@dataclass
class EgressResult:
    posted_message_ts: str | None
    chunks_posted: int


@dataclass(frozen=True)
class ActiveYoloGrantRow:
    channel_id: str
    channel_label: str | None
    remaining: timedelta
    pre_yolo_tier: PermissionTier


async def post_reply(
    slack_client,
    channel_id: str,
    turn: AgentTurn,
    *,
    thread_ts: str | None = None,
    session_label: str = "",
    session: SessionState | None = None,
    model: str | None = None,
    memory_db_path: Path | None = None,
) -> EgressResult:
    """Post an agent turn back to Slack.

    Agent replies are sent through Slack's native ``markdown`` block so
    CommonMark emitted by the model renders cleanly in Slack clients.
    """
    text = turn.text or "(empty reply)"
    if session is not None and session.session_just_started:
        text = (
            await build_fresh_session_greeting(
                session,
                model=model,
                memory_db_path=memory_db_path,
            )
            + "\n\n"
            + text
        )
    footer = ""
    if turn.cost_usd is not None:
        footer = f"\n\ncost: ${turn.cost_usd:.4f} · {turn.duration_ms or 0}ms"
    chunk_limit = max(1, SLACK_MAX_TEXT_LEN - len(footer)) if footer else SLACK_MAX_TEXT_LEN
    chunks = _chunk_text(text, chunk_limit)
    posted_ts: str | None = None
    n = 0
    for i, chunk in enumerate(chunks):
        # Keep the cost footer plain text; only the model body relies on markdown conversion.
        body = chunk
        if i == len(chunks) - 1 and footer:
            body = f"{chunk}{footer}"
        try:
            resp = await slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                blocks=[{"type": "markdown", "text": body}],
                text=_notification_fallback(body),
            )
        except Exception as e:
            log.exception(
                "egress.chunk_failed session=%s chunk=%d/%d error_type=%s",
                session_label,
                i + 1,
                len(chunks),
                type(e).__name__,
            )
            raise
        if i == 0:
            posted_ts = resp.get("ts") if isinstance(resp, dict) else None
        n += 1

    log.info(
        "egress.posted session=%s chunks=%d cost=%s duration_ms=%s",
        session_label,
        n,
        turn.cost_usd,
        turn.duration_ms,
    )
    if session is not None and session.session_just_started:
        session.session_just_started = False
    return EgressResult(posted_message_ts=posted_ts, chunks_posted=n)


async def build_fresh_session_greeting(
    session: SessionState,
    *,
    model: str | None = None,
    memory_db_path: Path | None = None,
) -> str:
    manifest = session.manifest
    identity = manifest.identity.value if manifest is not None else "default"
    tier = (
        manifest.tier_effective().value
        if manifest is not None
        else PermissionTier.TASK_ASSISTANT.value
    )
    memory_count = memory_entry_count(
        session.channel_id,
        memory_db_path=memory_db_path,
    )
    return (
        "👋 Fresh session loaded. "
        f"Model: {model or 'default'} • "
        f"Identity: {identity} • "
        f"MCPs: {await _mcp_summary(session)} • "
        f"Memory: {memory_count:,} entries searchable • "
        f"Tier: {tier}"
    )


async def build_new_session_public_notice(
    session: SessionState,
    *,
    memory_db_path: Path | None = None,
) -> str:
    manifest = session.manifest
    tier = (
        manifest.tier_effective().value
        if manifest is not None
        else PermissionTier.TASK_ASSISTANT.value
    )
    memory_count = memory_entry_count(
        session.channel_id,
        memory_db_path=memory_db_path,
    )
    return (
        "🔄 Started a fresh conversation. "
        f"Tier: {tier} • "
        f"Memory: {memory_count:,} entries available • "
        f"MCP: {await _mcp_summary(session)}"
    )


def memory_entry_count(
    channel_id: str,
    *,
    memory_db_path: Path | None = None,
) -> int:
    db_path = memory_db_path or (Path.home() / ".engram" / "memory.db")
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(db_path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "memory_segments" in tables:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memory_segments WHERE channel_id=?",
                    (channel_id,),
                ).fetchone()
                return int(row[0] or 0) if row else 0
            total = 0
            for table in ("transcripts", "summaries"):
                if table not in tables:
                    continue
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE channel_id=?",
                    (channel_id,),
                ).fetchone()
                total += int(row[0] or 0) if row else 0
            return total
    except sqlite3.Error:
        log.warning("egress.memory_count_failed channel=%s", channel_id, exc_info=True)
        return 0


async def _mcp_summary(session: SessionState) -> str:
    status_summary = await _mcp_status_summary(session.agent_client)
    if status_summary:
        return status_summary
    if session.manifest is None:
        return "none"
    servers, _allowed, _missing = resolve_team_mcp_servers(session.manifest)
    names = list(servers)
    return ", ".join(names) if names else "none"


async def _mcp_status_summary(client: Any) -> str | None:
    get_mcp_status = getattr(client, "get_mcp_status", None)
    if not callable(get_mcp_status):
        return None
    try:
        status = await get_mcp_status()
    except Exception:
        log.debug("egress.mcp_status_for_greeting_failed", exc_info=True)
        return None

    servers = _extract_servers(status)
    if not servers:
        return None

    parts: list[str] = []
    for server in servers:
        name = server.get("name")
        if not name:
            continue
        count = _mcp_tool_count(server)
        parts.append(f"{name} ({count} tool{'s' if count != 1 else ''})")
    return ", ".join(parts) if parts else None


def _mcp_tool_count(server: dict[str, Any]) -> int:
    for key in ("toolCount", "tool_count"):
        value = server.get(key)
        if isinstance(value, int):
            return value
    tools = server.get("tools")
    if isinstance(tools, list):
        return len(tools)
    return 0


def _notification_fallback(body: str, max_len: int = 120) -> str:
    """Produce a plain-text fallback for Slack notifications and screen readers."""
    if not body:
        return ""

    text = body.replace("\r\n", "\n")
    text = re.sub(r"```[^\n`]*\n?", "", text)
    text = text.replace("```", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if len(first) > max_len:
        return first[: max_len - 1].rstrip() + "…"
    return first


async def post_question(q: PendingQuestion, slack_client) -> tuple[str, str]:
    """Post a HITL question to Slack as Block Kit with buttons.

    Returns (channel_ts, thread_ts), which are stored on the question for later
    message updates.
    """
    if _ensure_footgun_match(q) is not None:
        return await post_footgun_confirmation_card(q, slack_client)

    header = q.prompt_title or f"🤔 Can I proceed with `{q.tool_name}`?"
    input_summary = (
        q.prompt_body_markdown
        if q.prompt_body_markdown is not None
        else f"```{json.dumps(q.tool_input, indent=2)[:800]}```"
    )
    action_elements = []
    sticky_eligible = _is_sticky_eligible(
        q.tool_name,
        getattr(q, "channel_manifest", None),
        tool_input=q.tool_input,
    )

    # Always include a primary "Allow" button (index 0), even when the SDK
    # returned no suggestions — otherwise the user would only see "Deny",
    # which is a UX dead end. If the SDK did return suggestions, we use
    # _suggestion_label to label each one (but never fall back to the meaningless
    # word "choice" — see _suggestion_label docstring).
    suggestions = list(q.suggestions[:5]) if q.suggestions else [None]
    for i, suggestion in enumerate(suggestions):
        label = _suggestion_label(suggestion, tool_name=q.tool_name)
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label},
                "value": f"{q.permission_request_id}|{i}",
                "action_id": f"hitl_choice_{i}",
                # Highlight the primary allow action so it reads clearly against Deny.
                **({"style": "primary"} if i == 0 else {}),
            }
        )
        if i == 0 and sticky_eligible:
            action_elements.append(
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": _always_allow_label(q.tool_name),
                    },
                    "value": f"{q.permission_request_id}|always|{q.tool_name}",
                    "action_id": f"hitl_choice_always_{i}",
                }
            )

    action_elements.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": q.deny_button_label},
            "value": f"{q.permission_request_id}|deny",
            "action_id": "hitl_choice_deny",
            "style": "danger",
        }
    )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": input_summary},
        },
        {"type": "actions", "block_id": "hitl_actions", "elements": action_elements},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "_Or reply in this thread to answer in your own words. "
                        "Times out in 5 minutes._"
                    ),
                }
            ],
        },
    ]

    response = await slack_client.chat_postMessage(
        channel=_question_channel(q),
        blocks=blocks,
        text=header,
    )
    return (response["ts"], response["ts"])


async def post_footgun_confirmation_card(
    q: PendingQuestion,
    slack_client,
) -> tuple[str, str]:
    """Post the destructive-action confirmation card for a footgun match."""
    match = q.footgun_match
    if match is None:
        raise ValueError("footgun confirmation card requires q.footgun_match")

    header = "⚠️ Destructive action confirmation required"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Matched rule:* {match.description}",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```{match.command}```"},
        },
        {
            "type": "actions",
            "block_id": "footgun_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Confirm..."},
                    "value": q.permission_request_id,
                    "action_id": "footgun_confirm_open",
                    "style": "danger",
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "_Requires a fresh typed confirmation. Times out in 5 minutes._",
                }
            ],
        },
    ]

    response = await slack_client.chat_postMessage(
        channel=q.channel_id,
        blocks=blocks,
        text=header,
    )
    return (response["ts"], response["ts"])


def build_footgun_confirmation_modal(q: PendingQuestion) -> dict:
    """Build the Slack modal for typed destructive-action confirmation."""
    match = q.footgun_match
    if match is None:
        raise ValueError("footgun confirmation modal requires q.footgun_match")

    return {
        "type": "modal",
        "callback_id": "footgun_confirm_submit",
        "notify_on_close": True,
        "private_metadata": q.permission_request_id,
        "title": {"type": "plain_text", "text": "Confirm Action"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Matched rule:* {match.description}",
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{match.command}```"},
            },
            {
                "type": "input",
                "block_id": "footgun_confirm_input",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "confirmation_text",
                },
                "label": {
                    "type": "plain_text",
                    "text": "Type CONFIRM to proceed",
                },
            },
        ],
    }


async def post_meta_eligibility_question(
    q: PendingQuestion,
    slack_client,
    *,
    channel_label: str,
    eligible: bool,
) -> tuple[str, str]:
    """Post the OQ31 nightly meta-summary eligibility confirmation card."""
    action = "Include" if eligible else "Exclude"
    preposition = "in" if eligible else "from"
    text = f"{action} {channel_label} {preposition} nightly meta-summary?"
    action_elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Confirm"},
            "value": f"{q.permission_request_id}|0",
            "action_id": "hitl_choice_0",
            "style": "primary",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Deny"},
            "value": f"{q.permission_request_id}|deny",
            "action_id": "hitl_choice_deny",
            "style": "danger",
        },
    ]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "block_id": "hitl_actions", "elements": action_elements},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "_Deny or timeout leaves the current manifest setting unchanged._",
                }
            ],
        },
    ]
    response = await slack_client.chat_postMessage(
        channel=q.channel_id,
        blocks=blocks,
        text=text,
    )
    return (response["ts"], response["ts"])


async def post_upgrade_waiting_message(
    slack_client,
    *,
    channel_id: str,
) -> str:
    text = "⏳ Permission upgrade requested — waiting for owner approval."
    response = await slack_client.chat_postMessage(
        channel=channel_id,
        text=text,
    )
    return str(response["ts"])


async def post_upgrade_request_dm(
    slack_client,
    *,
    owner_dm_channel_id: str,
    source_channel_id: str,
    source_channel_label: str,
    requested_by_user_id: str | None,
    from_tier: PermissionTier,
    to_tier: PermissionTier,
    reason: str | None,
    action_value: str,
) -> str:
    requested_by = (
        f"<@{requested_by_user_id}>"
        if requested_by_user_id
        else "_unknown_"
    )
    reason_text = _escape_mrkdwn(reason) if reason else "_No reason provided._"
    details = "\n".join(
        [
            "*Permission upgrade request*",
            f"• *Channel:* {source_channel_label} ({source_channel_id})",
            f"• *Requested by:* {requested_by}",
            f"• *Current tier:* `{from_tier.value}`",
            f"• *Requested tier:* `{to_tier.value}`",
            f"• *Reason:* {reason_text}",
        ]
    )
    if to_tier == PermissionTier.YOLO:
        actions = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve 24h"},
                "value": action_value,
                "action_id": "upgrade_decision_approve_24h",
                "style": "primary",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve 6h"},
                "value": action_value,
                "action_id": "upgrade_decision_approve_6h",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Deny"},
                "value": action_value,
                "action_id": "upgrade_decision_deny",
                "style": "danger",
            },
        ]
    else:
        actions = [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Approve until revoked",
                },
                "value": action_value,
                "action_id": "upgrade_decision_approve_permanent",
                "style": "primary",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve 30d"},
                "value": action_value,
                "action_id": "upgrade_decision_approve_30d",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Deny"},
                "value": action_value,
                "action_id": "upgrade_decision_deny",
                "style": "danger",
            },
        ]

    response = await slack_client.chat_postMessage(
        channel=owner_dm_channel_id,
        text="Permission upgrade request",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": details},
            },
            {
                "type": "actions",
                "block_id": "upgrade_actions",
                "elements": actions,
            },
        ],
    )
    return str(response["ts"])


async def update_upgrade_request_dm(
    slack_client,
    *,
    channel_id: str,
    message_ts: str,
    text: str,
    detail: str | None = None,
) -> None:
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        }
    ]
    if detail:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": detail}],
            }
        )
    await slack_client.chat_update(
        channel=channel_id,
        ts=message_ts,
        text=_notification_fallback(text),
        blocks=blocks,
    )


async def post_upgrade_result_in_channel(
    slack_client,
    *,
    channel_id: str,
    message_ts: str,
    approved: bool,
    tier: PermissionTier | None = None,
    approver_user_id: str | None = None,
) -> None:
    if approved:
        approver = f"<@{approver_user_id}>" if approver_user_id else "_unknown_"
        if tier is None:
            raise ValueError("tier is required for approved upgrade results")
        text = f"✅ Upgraded to {tier.value} by {approver}."
    else:
        text = "❌ Request denied."

    await slack_client.chat_update(
        channel=channel_id,
        ts=message_ts,
        text=text,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


async def post_yolo_expired_notification(
    slack_client,
    *,
    owner_dm_channel_id: str,
    channel_id: str,
    channel_label: str | None,
    pre_yolo_tier: PermissionTier,
    duration_used: timedelta | None,
) -> str | None:
    label = _channel_label(channel_id, channel_label)
    duration_text = _format_duration_used(duration_used)
    text = (
        f"YOLO expired on {label} — reverted to {pre_yolo_tier.value}. "
        f"Duration used: {duration_text}."
    )
    extend_command = "`/engram upgrade yolo`"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"To extend, run {extend_command} in the channel.",
                }
            ],
        },
    ]
    response = await slack_client.chat_postMessage(
        channel=owner_dm_channel_id,
        blocks=blocks,
        text=_notification_fallback(text),
    )
    return response.get("ts") if isinstance(response, dict) else None


def render_active_yolo_grants(
    grants: Sequence[ActiveYoloGrantRow],
) -> tuple[str, list[dict[str, object]]]:
    if not grants:
        text = "No active yolo grants."
        return text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    blocks: list[dict[str, object]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Active yolo grants*"},
        }
    ]
    for grant in grants:
        label = _channel_label(grant.channel_id, grant.channel_label)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{label}* (`{grant.channel_id}`)\n"
                        f"Remaining: {_format_duration_used(grant.remaining)}"
                        f" • Restores to: `{grant.pre_yolo_tier.value}`"
                    ),
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": f"yolo_actions_{grant.channel_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Extend 6h"},
                        "action_id": f"yolo_extend_{grant.channel_id}",
                        "value": grant.channel_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Revoke"},
                        "action_id": f"yolo_revoke_{grant.channel_id}",
                        "value": grant.channel_id,
                        "style": "danger",
                    },
                ],
            }
        )

    return "Active yolo grants", blocks


async def update_question_resolved(
    q: PendingQuestion,
    answer_text: str,
    slack_client,
    *,
    allowed: bool = True,
) -> None:
    """Edit the original question message to show the resolved answer."""
    prefix = "✅" if allowed else "❌"
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{prefix} Answered: {answer_text}"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"_Question was about `{q.tool_name}`._",
                }
            ],
        },
    ]
    await slack_client.chat_update(
        channel=_question_channel(q),
        ts=q.slack_channel_ts,
        blocks=blocks,
        text=f"Answered: {answer_text}",
    )


async def update_question_timeout(q: PendingQuestion, slack_client) -> None:
    """Edit the original question message to show timeout."""
    if _ensure_footgun_match(q) is not None:
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "⏱️ Destructive action confirmation timed out - command denied.",
                },
            },
        ]
        await slack_client.chat_update(
            channel=_question_channel(q),
            ts=q.slack_channel_ts,
            blocks=blocks,
            text="Timed out",
        )
        return

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "⏱️ Question timed out — I'll proceed with best guess.",
            },
        },
    ]
    await slack_client.chat_update(
        channel=_question_channel(q),
        ts=q.slack_channel_ts,
        blocks=blocks,
        text="Timed out",
    )


def _question_channel(q: PendingQuestion) -> str:
    return q.approval_channel_id or q.channel_id


# Map from Claude tool names to human-friendly verb fragments. We surface the
# verb in the HITL approval label so users see "Allow fetch" instead of just
# "Allow" or the useless "choice" placeholder. Unknown tools fall back to
# "Allow" — never the tool name itself (avoids leaking implementation jargon
# like "BashOutput" into the UI).
_TOOL_VERB = {
    "WebFetch": "fetch",
    "WebSearch": "search",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Bash": "shell command",
    "BashOutput": "shell output",
    "KillShell": "kill shell",
    "Task": "subtask",
    "TodoWrite": "todos update",
    "Grep": "grep",
    "Glob": "glob",
    "SlashCommand": "slash command",
}


def _suggestion_label(suggestion, *, tool_name: str | None = None) -> str:
    """Build a human-readable button label for a HITL permission suggestion.

    The Claude Agent SDK emits ``suggestions`` as a list of ``PermissionUpdate``
    dataclasses (see ``claude_agent_sdk.types.PermissionUpdate``); these have
    no ``name`` / ``label`` attributes, so earlier versions of this helper
    always fell back to the placeholder string ``"choice"``. That was leaking
    to Slack as a mystery button label.

    Label precedence (first match wins):
      1. ``suggestion["name"]`` or ``suggestion["label"]`` (explicit override,
         used by our own internal flows e.g. the OQ31 nightly-eligibility card)
      2. A friendly label derived from a ``PermissionUpdate``'s ``type`` field
         (e.g. ``addRules`` → "Always allow")
      3. ``"Allow <verb>"`` where ``<verb>`` is mapped from ``tool_name``
      4. Plain ``"Allow"`` as the universal fallback — never ``"choice"``.
    """
    # 1. Explicit override in a dict (preserves internal callers e.g. {"name": "Confirm"})
    if isinstance(suggestion, dict):
        explicit = suggestion.get("name") or suggestion.get("label")
        if explicit:
            return str(explicit)[:40]

    # 2. SDK PermissionUpdate dataclass — derive from .type
    update_type = getattr(suggestion, "type", None)
    if update_type == "addRules":
        return "Always allow"
    if update_type == "replaceRules":
        return "Replace rules"
    if update_type == "setMode":
        mode = getattr(suggestion, "mode", None)
        return f"Set mode: {mode}"[:40] if mode else "Set mode"
    if update_type == "addDirectories":
        return "Add to allowed dirs"

    # 3 + 4. Default "Allow" label, optionally specialized by tool
    if tool_name:
        verb = _TOOL_VERB.get(tool_name)
        if verb:
            return f"Allow {verb}"[:40]
    return "Allow"


def _always_allow_label(tool_name: str | None) -> str:
    allow_label = _suggestion_label(None, tool_name=tool_name)
    if allow_label == "Allow":
        return "Always allow"
    if allow_label.startswith("Allow "):
        return f"Always allow {allow_label[6:]}"[:40]
    return "Always allow"


def _is_sticky_eligible(
    tool_name: str,
    channel_manifest,
    *,
    tool_input: dict | None = None,
) -> bool:
    """Return True iff a HITL prompt may offer channel-scoped sticky allow."""
    if (
        channel_manifest is None
        or channel_manifest.tier_effective() != PermissionTier.OWNER_SCOPED
    ):
        return False
    if tool_name.startswith("mcp__"):
        return False
    if tool_input is not None and match_footgun(tool_name, tool_input) is not None:
        return False
    return tool_name not in _STICKY_INELIGIBLE_TOOLS


def _ensure_footgun_match(q: PendingQuestion) -> FootgunMatch | None:
    """Populate missing footgun metadata from the pending tool invocation."""
    if q.footgun_match is None:
        q.footgun_match = match_footgun(q.tool_name, q.tool_input)
    return q.footgun_match


def _escape_mrkdwn(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _channel_label(channel_id: str, channel_label: str | None) -> str:
    label = (channel_label or channel_id).strip()
    if label.startswith(("#", "@")):
        return label
    if channel_id.startswith("D"):
        return label
    return f"#{label}"


def _format_duration_used(duration: timedelta | None) -> str:
    if duration is None:
        return "unknown"
    total_minutes = max(0, int(duration.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"


def _chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        # Prefer splitting on a blank line boundary near the limit
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks
