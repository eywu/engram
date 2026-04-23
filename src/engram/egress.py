"""Egress — posts agent responses back to Slack with simple safety rails.

M1 scope:
  - Post the response text to the originating channel/thread.
  - Chunk if message exceeds Slack's 40k-char limit.
  - Log cost/duration for dev visibility.

Later: redaction, attachment upload, block-kit formatting, per-channel
rate limiting, auto-retry on Slack rate-limit errors.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from engram.agent import AgentTurn
from engram.hitl import PendingQuestion

log = logging.getLogger(__name__)

SLACK_MAX_TEXT_LEN = 39_000  # stay well under the 40k limit


@dataclass
class EgressResult:
    posted_message_ts: str | None
    chunks_posted: int


async def post_reply(
    say,
    turn: AgentTurn,
    *,
    thread_ts: str | None = None,
    session_label: str = "",
) -> EgressResult:
    """Post an agent turn back to Slack.

    `say` is the Bolt-provided callable that posts to the originating
    channel/thread.
    """
    text = turn.text or "(empty reply)"
    chunks = _chunk_text(text, SLACK_MAX_TEXT_LEN)
    posted_ts: str | None = None
    n = 0
    for i, chunk in enumerate(chunks):
        # On the first chunk we include a subtle footer with cost if we have it.
        body = chunk
        if i == len(chunks) - 1 and turn.cost_usd is not None:
            body = f"{chunk}\n\n_cost: ${turn.cost_usd:.4f} · {turn.duration_ms or 0}ms_"
        resp = await say(text=body, thread_ts=thread_ts)
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
    return EgressResult(posted_message_ts=posted_ts, chunks_posted=n)


async def post_question(q: PendingQuestion, slack_client) -> tuple[str, str]:
    """Post a HITL question to Slack as Block Kit with buttons.

    Returns (channel_ts, thread_ts), which are stored on the question for later
    message updates.
    """
    header = f"🤔 Can I proceed with `{q.tool_name}`?"
    input_summary = json.dumps(q.tool_input, indent=2)[:800]
    action_elements = []

    for i, suggestion in enumerate(q.suggestions[:5]):
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": _suggestion_label(suggestion)},
                "value": f"{q.permission_request_id}|{i}",
                "action_id": f"hitl_choice_{i}",
            }
        )

    action_elements.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Deny"},
            "value": f"{q.permission_request_id}|deny",
            "action_id": "hitl_choice_deny",
            "style": "danger",
        }
    )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```{input_summary}```"},
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
        channel=q.channel_id,
        blocks=blocks,
        text=header,
    )
    return (response["ts"], response["ts"])


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


async def update_question_resolved(
    q: PendingQuestion,
    answer_text: str,
    slack_client,
) -> None:
    """Edit the original question message to show the resolved answer."""
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"✅ Answered: *{answer_text}*"},
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
        channel=q.channel_id,
        ts=q.slack_channel_ts,
        blocks=blocks,
        text=f"Answered: {answer_text}",
    )


async def update_question_timeout(q: PendingQuestion, slack_client) -> None:
    """Edit the original question message to show timeout."""
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
        channel=q.channel_id,
        ts=q.slack_channel_ts,
        blocks=blocks,
        text="Timed out",
    )


def _suggestion_label(suggestion) -> str:
    """Extract a display label from an SDK-opaque permission suggestion."""
    if isinstance(suggestion, dict):
        return str(suggestion.get("name") or suggestion.get("label") or "choice")[:40]
    return str(suggestion)[:40]


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
