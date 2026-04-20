"""Agent — thin wrapper around the Claude Agent SDK.

M1: one Slack message = one `query()` call = one turn.
M2 adds per-channel system prompts and tool scoping via `canUseTool`.
M3 adds cost tracking.
M4 adds AskUserQuestion stream-watching (fallback path per M0-F3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from engram.config import EngramConfig
from engram.router import SessionState

log = logging.getLogger(__name__)


@dataclass
class AgentTurn:
    """Outcome of one message-turn."""

    text: str
    cost_usd: float | None
    duration_ms: int | None
    num_turns: int | None
    is_error: bool
    error_message: str | None = None


class Agent:
    """Runs a single Claude turn for a channel.

    Stateless; SessionState is passed per call. This keeps the agent
    trivially testable and lets M2 introduce per-channel overrides without
    re-plumbing.
    """

    def __init__(self, config: EngramConfig):
        self._config = config

    async def run_turn(self, session: SessionState, user_text: str) -> AgentTurn:
        """Run one turn for the given channel. Returns aggregated response."""
        session.turn_count += 1

        options = ClaudeAgentOptions(
            max_turns=self._config.max_turns_per_message,
            setting_sources=["user", "project"],
            cwd=str(session.cwd) if session.cwd else None,
            model=self._config.anthropic.model,
        )

        text_chunks: list[str] = []
        result: ResultMessage | None = None
        error_message: str | None = None

        try:
            async for message in query(prompt=user_text, options=options):
                if isinstance(message, AssistantMessage):
                    for block in getattr(message, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            text_chunks.append(text)
                elif isinstance(message, ResultMessage):
                    result = message
        except Exception as e:
            error_message = f"{type(e).__name__}: {e}"
            log.exception(
                "agent.run_turn failed for session=%s", session.label()
            )

        text = "".join(text_chunks).strip()
        if not text and error_message:
            text = (
                "I ran into an error processing that. "
                f"({error_message.split(':', 1)[0]})"
            )
        elif not text:
            text = "(no response)"

        return AgentTurn(
            text=text,
            cost_usd=getattr(result, "total_cost_usd", None) if result else None,
            duration_ms=getattr(result, "duration_ms", None) if result else None,
            num_turns=getattr(result, "num_turns", None) if result else None,
            is_error=bool(error_message) or (result.is_error if result else False),
            error_message=error_message,
        )
