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
from decimal import Decimal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    tag_session,
)

from engram.budget import BUDGET_PAUSE_MESSAGE, Budget, CheckResult
from engram.config import EngramConfig
from engram.router import SessionState
from engram.scope import build_scope_decision, build_tool_guard
from engram.tools import build_memory_mcp_server

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
    budget_warnings: tuple[Decimal, ...] = ()
    budget_month_to_date_usd: Decimal | None = None
    budget_monthly_cap_usd: Decimal | None = None


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
        budget: Budget | None = None,
    ):
        self._config = config
        self._client_factory = client_factory
        self._budget = budget

    async def run_turn(
        self,
        session: SessionState,
        user_text: str,
        *,
        user_id: str | None = None,
    ) -> AgentTurn:
        """Run one turn for the given channel. Returns aggregated response."""
        text_chunks: list[str] = []
        result: ResultMessage | None = None
        error_message: str | None = None
        budget_checks: list[CheckResult] = []

        await session.agent_lock.acquire()
        try:
            session.turn_count += 1
            budget_check = self._check_budget(session.channel_id)
            if budget_check is not None:
                budget_checks.append(budget_check)
                if budget_check.pause:
                    return AgentTurn(
                        text=BUDGET_PAUSE_MESSAGE,
                        cost_usd=None,
                        duration_ms=None,
                        num_turns=None,
                        is_error=False,
                        budget_warnings=budget_check.thresholds_fired,
                        budget_month_to_date_usd=budget_check.month_to_date_usd,
                        budget_monthly_cap_usd=budget_check.monthly_cap_usd,
                    )

            client = await self._ensure_client(session)
            self._refresh_client_budget_limit(client)

            await client.query(user_text, session_id=session.session_id)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in getattr(message, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            text_chunks.append(text)
                elif isinstance(message, ResultMessage):
                    result = message
            if result is not None and self._budget is not None:
                try:
                    self._budget.record(session.channel_id, user_id, result)
                    budget_checks.append(self._budget.check(session.channel_id))
                except Exception:
                    log.warning(
                        "agent.budget_record_failed session=%s",
                        session.label(),
                        exc_info=True,
                    )
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

        budget_warnings, budget_mtd, budget_cap = _summarize_budget_checks(
            budget_checks
        )
        return AgentTurn(
            text=text,
            cost_usd=getattr(result, "total_cost_usd", None) if result else None,
            duration_ms=getattr(result, "duration_ms", None) if result else None,
            num_turns=getattr(result, "num_turns", None) if result else None,
            is_error=bool(error_message) or (result.is_error if result else False),
            error_message=error_message,
            budget_warnings=budget_warnings,
            budget_month_to_date_usd=budget_mtd,
            budget_monthly_cap_usd=budget_cap,
        )

    # ──────────────────────────────────────────────────────────────
    # Option construction
    # ──────────────────────────────────────────────────────────────

    async def _ensure_client(self, session: SessionState) -> ClaudeSDKClient:
        """Create and connect the per-channel client if needed.

        Caller must hold session.agent_lock.
        """
        if session.agent_client is not None:
            self._refresh_client_budget_limit(session.agent_client)
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
        can_use_tool = None
        mcp_servers = {
            "engram_memory": build_memory_mcp_server(
                default_channel_id=session.channel_id,
            )
        }

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
            skills=skills,
            mcp_servers=mcp_servers,
            # Scope (runtime — final enforcement)
            can_use_tool=can_use_tool,
            max_budget_usd=self._max_budget_usd_for_options(),
        )

    def _check_budget(self, channel_id: str) -> CheckResult | None:
        if self._budget is None:
            return None
        try:
            return self._budget.check(channel_id)
        except Exception:
            log.warning("agent.budget_check_failed channel=%s", channel_id, exc_info=True)
            return None

    def _max_budget_usd_for_options(self) -> float | None:
        if self._budget is None:
            return None
        try:
            remaining = self._budget.remaining_usd()
        except Exception:
            log.warning("agent.budget_remaining_failed", exc_info=True)
            return None
        if remaining <= 0 and not self._budget.config.hard_cap_enabled:
            return None
        return float(remaining)

    def _refresh_client_budget_limit(self, client: ClaudeSDKClient) -> None:
        options = getattr(client, "options", None)
        if options is None:
            return
        options.max_budget_usd = self._max_budget_usd_for_options()


def _summarize_budget_checks(
    checks: list[CheckResult],
) -> tuple[tuple[Decimal, ...], Decimal | None, Decimal | None]:
    warnings: list[Decimal] = []
    month_to_date: Decimal | None = None
    monthly_cap: Decimal | None = None
    for check in checks:
        month_to_date = check.month_to_date_usd
        monthly_cap = check.monthly_cap_usd
        for threshold in check.thresholds_fired:
            if threshold not in warnings:
                warnings.append(threshold)
    return tuple(warnings), month_to_date, monthly_cap
