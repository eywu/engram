"""Egress — posts agent responses back to Slack with simple safety rails.

M1 scope:
  - Post the response text to the originating channel/thread.
  - Chunk if message exceeds Slack's 40k-char limit.
  - Log cost/duration for dev visibility.

Later: redaction, attachment upload, block-kit formatting, per-channel
rate limiting, auto-retry on Slack rate-limit errors.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from engram.agent import AgentTurn

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
