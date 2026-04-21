"""Agent — thin wrapper around the Claude Agent SDK.

M1: one Slack message = one `query()` call = one turn.
M2: the per-channel ChannelManifest drives scope (tools/MCPs/skills),
    setting_sources, max_turns, cwd, and system-prompt identity.
M3: one ClaudeSDKClient per active Slack channel, serialized by the
    per-channel SessionState.agent_lock.
M4 will add AskUserQuestion stream-watching.
"""
from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    tag_session,
)

from engram import tools as engram_tools
from engram.config import EngramConfig
from engram.manifest import ChannelManifest
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


ClientFactory = Callable[[ClaudeAgentOptions], ClaudeSDKClient]


class Agent:
    """Runs a single Claude turn for a channel.

    SessionState owns the per-channel ClaudeSDKClient and asyncio.Lock. When
    the session carries a ChannelManifest (M2), agent reads scope from it;
    otherwise falls back to M1 behavior (setting_sources=["user"], no guards).
    """

    def __init__(
        self,
        config: EngramConfig,
        *,
        client_factory: ClientFactory = ClaudeSDKClient,
    ):
        self._config = config
        self._client_factory = client_factory

    async def run_turn(self, session: SessionState, user_text: str) -> AgentTurn:
        """Run one turn for the given channel. Returns aggregated response."""
        text_chunks: list[str] = []
        result: ResultMessage | None = None
        error_message: str | None = None

        await session.agent_lock.acquire()
        try:
            session.turn_count += 1
            client = await self._ensure_client(session)

            await client.query(user_text, session_id=session.session_id)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in getattr(message, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            text_chunks.append(text)
                elif isinstance(message, ResultMessage):
                    result = message
            if not session.agent_session_tagged:
                session.agent_session_tagged = await self._tag_session(
                    session, client
                )
        except Exception as e:
            error_message = f"{type(e).__name__}: {e}"
            log.exception(
                "agent.run_turn failed for session=%s", session.label()
            )
        finally:
            session.agent_last_active_at = time.monotonic()
            session.agent_lock.release()

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

    async def _ensure_client(self, session: SessionState) -> ClaudeSDKClient:
        """Create and connect the per-channel client if needed.

        Caller must hold session.agent_lock.
        """
        if session.agent_client is not None:
            return session.agent_client

        options = self._build_options(
            session,
            resume=session.agent_session_initialized,
        )
        client = self._client_factory(options)
        try:
            await client.connect()
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                log.debug(
                    "agent.client_disconnect_after_connect_failure_failed "
                    "session=%s",
                    session.label(),
                    exc_info=True,
                )
            raise

        session.agent_client = client
        session.agent_session_initialized = True
        log.info("agent.client_connected session=%s", session.label())
        return client

    async def _tag_session(
        self,
        session: SessionState,
        client: ClaudeSDKClient,
    ) -> bool:
        """Tag the Claude session with the Slack channel id when possible."""
        method = getattr(client, "tag_session", None)
        if method is not None:
            try:
                maybe_awaitable = method(
                    session_id=session.session_id,
                    tags={"channel_id": session.channel_id},
                )
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
            except Exception:
                log.warning(
                    "agent.session_tag_failed session=%s session_id=%s",
                    session.label(),
                    session.session_id,
                    exc_info=True,
                )
                return False
            else:
                return True

        try:
            tag_session(
                session.session_id,
                f"channel_id:{session.channel_id}",
                directory=str(session.cwd) if session.cwd else None,
            )
        except FileNotFoundError:
            log.debug(
                "agent.session_tag_deferred session=%s session_id=%s",
                session.label(),
                session.session_id,
            )
            return False
        except Exception:
            log.warning(
                "agent.session_tag_failed session=%s session_id=%s",
                session.label(),
                session.session_id,
                exc_info=True,
            )
            return False
        return True

    def _build_options(
        self,
        session: SessionState,
        *,
        resume: bool = False,
    ) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions from config + (optional) channel manifest."""
        manifest = session.manifest

        # Defaults — applied when no manifest is available (legacy tests).
        setting_sources: list[str] = ["user"]
        max_turns = self._config.max_turns_per_message
        permission_mode = "default"
        allowed_tools: list[str] = []
        disallowed_tools: list[str] = []
        skills: list[str] | str | None = "all"
        mcp_servers = {}
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

        if _memory_search_allowed(manifest):
            mcp_servers[engram_tools.ENGRAM_MCP_SERVER_NAME] = (
                engram_tools.create_sdk_mcp_server(
                    channel_id=session.channel_id,
                )
            )
            if engram_tools.MEMORY_SEARCH_CANONICAL_NAME not in allowed_tools:
                allowed_tools.append(engram_tools.MEMORY_SEARCH_CANONICAL_NAME)

        session_kwargs = (
            {"resume": session.session_id}
            if resume
            else {"session_id": session.session_id}
        )

        return ClaudeAgentOptions(
            # Identity & discovery
            setting_sources=setting_sources,
            cwd=str(session.cwd) if session.cwd else None,
            model=self._config.anthropic.model,
            **session_kwargs,
            # Runtime limits
            max_turns=max_turns,
            permission_mode=permission_mode,
            # Scope (static — helps SDK skip priming denied entries)
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            mcp_servers=mcp_servers,
            skills=skills,
            # Scope (runtime — final enforcement)
            can_use_tool=can_use_tool,
        )


def _memory_search_allowed(manifest: ChannelManifest | None) -> bool:
    """Memory search is available unless a non-owner channel denies Engram."""
    if manifest is None or manifest.is_owner_dm():
        return True
    return engram_tools.ENGRAM_MCP_SERVER_NAME not in manifest.mcp_servers.disallowed
