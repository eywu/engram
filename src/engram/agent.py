"""Agent — thin wrapper around the Claude Agent SDK.

M1: one Slack message = one `query()` call = one turn.
M2: the per-channel ChannelManifest drives scope (tools/MCPs/skills),
    setting_sources, max_turns, cwd, and system-prompt identity.
M3 will add cost-budget enforcement on top of this.
M4 will add AskUserQuestion stream-watching.
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
from engram.scope import build_scope_decision, build_tool_guard

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

    Stateless; SessionState is passed per call. When the session carries a
    ChannelManifest (M2), agent reads scope from it; otherwise falls back
    to M1 behavior (setting_sources=["user"], no tool guards).
    """

    def __init__(self, config: EngramConfig):
        self._config = config

    async def run_turn(self, session: SessionState, user_text: str) -> AgentTurn:
        """Run one turn for the given channel. Returns aggregated response."""
        session.turn_count += 1

        options = self._build_options(session)

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

    # ──────────────────────────────────────────────────────────────
    # Option construction
    # ──────────────────────────────────────────────────────────────

    def _build_options(self, session: SessionState) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions from config + (optional) channel manifest."""
        manifest = session.manifest

        # Defaults — applied when no manifest is available (legacy tests).
        setting_sources: list[str] = ["user"]
        max_turns = self._config.max_turns_per_message
        permission_mode = "default"
        allowed_tools: list[str] = []
        disallowed_tools: list[str] = []
        skills: list[str] | str | None = "all"
        can_use_tool = None

        if manifest is not None:
            # setting_sources: cost-significant. Team channels should use
            # ["project"] to avoid pulling in the operator's personal
            # user-level settings (and to keep cost profile predictable).
            setting_sources = list(manifest.setting_sources)

            # Behavior overrides
            if manifest.behavior.max_turns is not None:
                max_turns = manifest.behavior.max_turns
            permission_mode = manifest.behavior.permission_mode

            # Static scope
            decision = build_scope_decision(manifest)
            allowed_tools = decision.allowed_tools
            disallowed_tools = decision.disallowed_tools
            skills = decision.skills

            # Runtime scope guard (covers MCPs, which the static fields
            # can't always enumerate).
            can_use_tool = build_tool_guard(manifest)

        return ClaudeAgentOptions(
            # Identity & discovery
            setting_sources=setting_sources,
            cwd=str(session.cwd) if session.cwd else None,
            model=self._config.anthropic.model,
            # Runtime limits
            max_turns=max_turns,
            permission_mode=permission_mode,
            # Scope (static — helps SDK skip priming denied entries)
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            skills=skills,
            # Scope (runtime — final enforcement)
            can_use_tool=can_use_tool,
        )
