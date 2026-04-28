"""Agent — thin wrapper around the Claude Agent SDK.

M1: one Slack message = one `query()` call = one turn.
M2: the per-channel ChannelManifest drives scope (tools/MCPs/skills),
    setting_sources, max_turns, cwd, and system-prompt identity.
M3: one ClaudeSDKClient per active Slack channel, serialized by the
    per-channel SessionState.agent_lock.
M4 will add AskUserQuestion stream-watching.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    RateLimitEvent,
    RateLimitStatus,
    ResultMessage,
    UserMessage,
    tag_session,
)

from engram import paths
from engram.budget import BUDGET_PAUSE_MESSAGE, Budget, CheckResult
from engram.config import EngramConfig
from engram.costs import CostDatabase, RateLimitRecord
from engram.embeddings import EmbeddingQueue
from engram.footguns import match_footgun
from engram.hitl import PendingQuestion, build_hitl_tool_guard, watch_pending_question
from engram.hooks import build_hooks
from engram.manifest import (
    ManifestError,
    PermissionTier,
    detect_mcp_allow_list_additions,
    persist_approved_mcp_manifest_change,
)
from engram.mcp import resolve_team_mcp_servers, warn_missing_mcp_servers
from engram.mcp_health import (
    DEFAULT_RECONNECT_FAIL_THRESHOLD,
    DEFAULT_WATCHDOG_POLL_INTERVAL_S,
    McpHealthWatchdog,
    disable_failed_mcps_pre_turn,
    warning_chunk_for_pre_turn,
)
from engram.mcp_manifest_gate import (
    MCPApprovalDisposition,
    request_approved_mcp_manifest_change,
)
from engram.mcp_tools import (
    MEMORY_SEARCH_FULL_TOOL_NAMES,
    MEMORY_SEARCH_SERVER_NAME,
    make_memory_search_server,
)
from engram.mcp_trust import (
    add_trusted_publishers,
    render_owner_approval_markdown,
    render_trust_add_recovery_message,
    resolve_mcp_server_trust,
)
from engram.memory_hooks import make_memory_hooks_with_embeddings
from engram.router import Router, SessionState
from engram.scope import build_scope_decision, build_tool_guard
from engram.telemetry import cli_stderr_logger

log = logging.getLogger(__name__)

_CLAUDE_MAX_SANITIZED_LENGTH = 200
_CLAUDE_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")


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
OwnerAlert = Callable[[str], Awaitable[None] | None]


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
        owner_alert: OwnerAlert | None = None,
        cost_db: CostDatabase | None = None,
        router: Router | None = None,
        embedder: object | None = None,
        embedding_queue: EmbeddingQueue | None = None,
        retry_base_delay_seconds: float = 0.5,
    ):
        self._config = config
        self._client_factory = client_factory
        self._budget = budget
        self._owner_alert = owner_alert
        self._cost_db = cost_db
        self._router = router
        self._embedder = embedder
        self._embedding_queue = embedding_queue
        self._retry_base_delay_seconds = retry_base_delay_seconds
        # GRO-555 Layer 1+2 tuning. Exposed as instance attributes (not
        # constructor args) to keep the public Agent signature stable;
        # tests reach in to override them.
        self._mcp_watchdog_threshold = DEFAULT_RECONNECT_FAIL_THRESHOLD
        self._mcp_watchdog_poll_interval_s = DEFAULT_WATCHDOG_POLL_INTERVAL_S

    async def _maybe_gate_manifest_mcp_additions(
        self,
        *,
        session: SessionState,
        manifest,
        tool_name: str,
        tool_input: dict[str, object],
        on_new_question: Callable[[PendingQuestion], Awaitable[None]],
        timeout_s: int,
        max_per_day: int,
    ) -> PermissionResultAllow | PermissionResultDeny | None:
        if self._router is None or self._router.home is None:
            return None
        if tool_name not in {"Write", "Edit", "MultiEdit"}:
            return None

        manifest_path = paths.channel_manifest_path(
            session.channel_id,
            self._router.home,
        )
        inventory = self._load_manifest_inventory()
        try:
            plan = detect_mcp_allow_list_additions(
                tool_name,
                tool_input,
                manifest_path=manifest_path,
                cwd=session.cwd,
                inventory=inventory,
            )
        except ManifestError as exc:
            return PermissionResultDeny(
                message=f"Blocked manifest edit: unable to inspect MCP allow-list change ({exc}).",
            )
        if plan is None:
            return None
        block_reason: str | None = None

        async def _confirm_unknown(_plan, decisions) -> MCPApprovalDisposition:
            nonlocal block_reason
            owner_dm_channel_id = (
                self._config.owner_dm_channel_id or self._router.owner_dm_channel_id
            )
            owner_user_id = self._config.owner_user_id
            if not owner_dm_channel_id or not owner_user_id:
                block_reason = (
                    "Blocked MCP manifest update: owner approval is required for "
                    "untrusted MCPs, but owner_dm_channel_id or owner_user_id is unset."
                )
                return MCPApprovalDisposition.DENIED

            allowed, reason = self._router.hitl_limiter.check(
                session.channel_id,
                max_per_day=max_per_day,
            )
            if not allowed:
                if reason.startswith("another question already pending"):
                    block_reason = (
                        "MCP trust approval is already pending in the owner DM. "
                        "Wait for approval, then retry the manifest update."
                    )
                else:
                    block_reason = f"MCP trust approval unavailable: {reason}"
                return MCPApprovalDisposition.DENIED

            q = PendingQuestion(
                permission_request_id=str(uuid.uuid4()),
                channel_id=session.channel_id,
                session_id=session.session_id,
                turn_id=str(uuid.uuid4()),
                tool_name=tool_name,
                tool_input=tool_input,
                suggestions=[
                    {"name": "Approve once"},
                    {"name": "Approve + add publisher to trust list"},
                ],
                who_can_answer=owner_user_id,
                posted_at=datetime.datetime.now(datetime.UTC),
                timeout_s=timeout_s,
                channel_manifest=manifest,
                approval_channel_id=owner_dm_channel_id,
                prompt_title="🔐 Owner approval required for MCP addition",
                prompt_body_markdown=render_owner_approval_markdown(
                    channel_id=session.channel_id,
                    channel_label=manifest.label,
                    decisions=decisions,
                ),
                deny_button_label="Reject",
            )

            async def _apply_approved_manifest(_result) -> None:
                if q.resolution_choice == "deny":
                    return
                _previous, updated, _path = persist_approved_mcp_manifest_change(plan)
                self._router.replace_cached_manifest(updated)
                if q.resolution_choice == "1":
                    trusted_publishers = [
                        (decision.registry, decision.publisher or "")
                        for decision in decisions
                        if decision.publisher
                    ]
                    try:
                        add_trusted_publishers(
                            trusted_publishers,
                            home=self._router.home,
                        )
                    except Exception as exc:
                        q.resolution_status_message = (
                            render_trust_add_recovery_message(trusted_publishers)
                        )
                        log.warning(
                            "mcp.publisher_trust_add_failed_after_manifest_persist "
                            "permission_request_id=%s publishers=%r: %s",
                            q.permission_request_id,
                            trusted_publishers,
                            exc,
                        )

            q.on_resolve = _apply_approved_manifest
            self._router.hitl.register(q)
            self._router.hitl_limiter.reserve(session.channel_id)
            try:
                await on_new_question(q)
            except Exception:
                self._router.hitl.resolve(
                    q.permission_request_id,
                    PermissionResultDeny(message="failed to post question"),
                )
                self._router.hitl.cleanup_resolved()
                block_reason = (
                    "Blocked MCP manifest update: failed to post owner approval request."
                )
                return MCPApprovalDisposition.DENIED
            watch_pending_question(self._router, q)
            block_reason = (
                "MCP trust approval requested in the owner DM. "
                "The manifest was not updated; retry after approval."
            )
            return MCPApprovalDisposition.PENDING

        approval = await request_approved_mcp_manifest_change(
            plan,
            channel_label=manifest.label,
            owner_alert=self._owner_alert,
            confirm_unknown=_confirm_unknown,
            home=self._router.home,
            inventory=inventory,
            trust_resolver=resolve_mcp_server_trust,
        )
        if approval.plan is not None:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=block_reason or "Blocked MCP manifest update: owner approval declined."
        )

    def _load_manifest_inventory(self) -> dict[str, dict[str, object]]:
        from engram.mcp import load_claude_mcp_servers

        raw_inventory = load_claude_mcp_servers()
        return {
            name: dict(config)
            for name, config in raw_inventory.items()
            if isinstance(config, dict)
        }

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
            session.current_user_id = user_id
            if self._is_rate_limited(session):
                reset = _format_reset_at(session.rate_limit_reset_at)
                log.info(
                    "agent.rate_limit_skip session=%s status=%s reset_at=%s",
                    session.label(),
                    session.rate_limit_status,
                    session.rate_limit_reset_at,
                )
                return AgentTurn(
                    text=f"Claude rate limit is active for this channel until {reset}.",
                    cost_usd=None,
                    duration_ms=None,
                    num_turns=None,
                    is_error=False,
                )

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

            session.turn_count += 1
            attempt = 0
            while True:
                try:
                    text_chunks, result = await self._run_sdk_turn_once(
                        session,
                        user_text,
                    )
                    break
                except (CLINotFoundError, CLIConnectionError) as e:
                    error_message = f"{type(e).__name__}: {e}"
                    log.error(
                        "agent.cli_error session=%s error_class=%s",
                        session.label(),
                        type(e).__name__,
                        exc_info=True,
                    )
                    await self._drop_client(session)
                    await self._alert_owner(
                        f"Engram bridge SDK error in {session.label()}: "
                        f"{type(e).__name__}: {e}"
                    )
                    break
                except (ProcessError, CLIJSONDecodeError) as e:
                    log.error(
                        "agent.retryable_cli_error session=%s error_class=%s attempt=%d",
                        session.label(),
                        type(e).__name__,
                        attempt + 1,
                        exc_info=True,
                    )
                    if attempt >= 1:
                        error_message = f"{type(e).__name__}: {e}"
                        break
                    attempt += 1
                    await self._drop_client(session)
                    await asyncio.sleep(
                        self._retry_base_delay_seconds * (2 ** (attempt - 1))
                    )
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
                    session,
                    session.agent_client,
                )
        except Exception as e:
            error_message = f"{type(e).__name__}: {e}"
            log.exception(
                "agent.run_turn failed for session=%s", session.label()
            )
        finally:
            session.current_user_id = None
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

    async def _run_sdk_turn_once(
        self,
        session: SessionState,
        user_text: str,
    ) -> tuple[list[str], ResultMessage | None]:
        """Run one SDK attempt. Caller must hold session.agent_lock."""
        text_chunks: list[str] = []
        result: ResultMessage | None = None
        pending_tool_use_ids: set[str] = set()
        client = await self._ensure_client(session)
        uses_raw_stream = callable(getattr(client, "receive_messages", None))

        # GRO-555 Layer 1: snapshot MCP health before the turn starts and
        # disable any server already in failed/needs-auth state, so the
        # model never tries to invoke its tools during this turn. Skipped
        # silently for fakes that don't expose MCP control.
        pre_turn_warning: str | None = None
        if self._supports_mcp_control(client):
            outcome = await disable_failed_mcps_pre_turn(
                client,
                session_label=session.label(),
                already_disabled=session.disabled_mcp_servers,
            )
            pre_turn_warning = warning_chunk_for_pre_turn(outcome.disabled)

        # GRO-555 Layer 2: spawn the in-turn watchdog. It polls MCP health
        # at a fixed cadence and circuit-breaks any server that stays in
        # a failed state across consecutive polls. Always cancelled in the
        # finally block.
        watchdog: McpHealthWatchdog | None = None
        watchdog_task: asyncio.Task[None] | None = None
        if self._supports_mcp_control(client):
            watchdog = McpHealthWatchdog(
                client,
                session_label=session.label(),
                already_disabled=session.disabled_mcp_servers,
                threshold=self._mcp_watchdog_threshold,
                poll_interval_s=self._mcp_watchdog_poll_interval_s,
            )
            watchdog_task = asyncio.create_task(watchdog.run())

        try:
            await client.query(user_text, session_id=session.session_id)
            # receive_response() stops at the first ResultMessage, but tool-driven
            # turns can continue with tool_result + assistant text after an
            # intermediate result. Consume the raw message stream instead.
            async for message in self._iter_turn_messages(client):
                if isinstance(message, AssistantMessage):
                    for block in getattr(message, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            text_chunks.append(text)
                        tool_use_id = getattr(block, "id", None)
                        tool_use_input = getattr(block, "input", None)
                        if tool_use_id and isinstance(tool_use_input, dict):
                            pending_tool_use_ids.add(tool_use_id)
                elif isinstance(message, UserMessage):
                    for block in getattr(message, "content", []) or []:
                        tool_use_id = getattr(block, "tool_use_id", None)
                        if tool_use_id:
                            pending_tool_use_ids.discard(tool_use_id)
                elif isinstance(message, ResultMessage):
                    result = message
                    if uses_raw_stream and self._is_terminal_turn_result(
                        message,
                        pending_tool_use_ids=pending_tool_use_ids,
                    ):
                        break
                elif isinstance(message, RateLimitEvent):
                    await self._handle_rate_limit_event(session, message)
        finally:
            if watchdog_task is not None:
                watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watchdog_task

        # Splice user-visible warnings AFTER the model output so the
        # response itself isn't disrupted. Pre-turn warning first (it
        # tells the user why a tool was missing); then any in-turn
        # circuit-breaker trips.
        if pre_turn_warning:
            text_chunks.append(pre_turn_warning)
        if watchdog is not None:
            text_chunks.extend(watchdog.warnings)
        return text_chunks, result

    @staticmethod
    def _supports_mcp_control(client: object) -> bool:
        """True iff ``client`` exposes the MCP-control primitives we need.

        Tests pass minimal fakes that only implement query/receive; those
        skip the GRO-555 health layers entirely.
        """
        return callable(getattr(client, "get_mcp_status", None)) and callable(
            getattr(client, "toggle_mcp_server", None)
        )

    def _iter_turn_messages(self, client: ClaudeSDKClient):
        receive_messages = getattr(client, "receive_messages", None)
        if callable(receive_messages):
            return receive_messages()
        return client.receive_response()

    @staticmethod
    def _is_terminal_turn_result(
        result: ResultMessage,
        *,
        pending_tool_use_ids: set[str],
    ) -> bool:
        if result.stop_reason == "tool_use":
            return False
        return not pending_tool_use_ids

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

        jsonl_path = _claude_cli_jsonl_for(session.session_id, session.cwd)
        jsonl_exists = jsonl_path.exists()
        if jsonl_exists and not session.agent_session_initialized:
            session.agent_session_initialized = True
            log.info(
                "agent.session_resume_from_disk session=%s jsonl_exists=%s",
                session.label(),
                jsonl_exists,
            )

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

    async def _drop_client(self, session: SessionState) -> None:
        if session.agent_client is None:
            return
        try:
            await session.agent_client.disconnect()
        except Exception:
            log.debug(
                "agent.client_disconnect_before_retry_failed session=%s",
                session.label(),
                exc_info=True,
            )
        finally:
            session.agent_client = None
            # GRO-555: reset the per-channel circuit-breaker ban list. A
            # new client gets a fresh CLI subprocess and reloads all MCPs.
            session.disabled_mcp_servers.clear()

    async def _tag_session(
        self,
        session: SessionState,
        client: ClaudeSDKClient | None,
    ) -> bool:
        """Tag the Claude session with the Slack channel id when possible."""
        if client is None:
            return False
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

    async def _handle_rate_limit_event(
        self,
        session: SessionState,
        event: RateLimitEvent,
    ) -> None:
        info = event.rate_limit_info
        status: RateLimitStatus = info.status
        session.rate_limit_status = status
        session.rate_limit_reset_at = info.resets_at
        session.rate_limit_updated_at = datetime.datetime.now(
            datetime.UTC
        ).isoformat()

        if self._cost_db is not None:
            self._cost_db.record_rate_limit(
                RateLimitRecord(
                    timestamp=session.rate_limit_updated_at,
                    channel_id=session.channel_id,
                    session_id=session.session_id,
                    status=status,
                    reset_at=info.resets_at,
                    rate_limit_type=info.rate_limit_type,
                    utilization=info.utilization,
                    raw=info.raw,
                )
            )

        reset = _format_reset_at(info.resets_at)
        if status == "allowed_warning":
            log.warning(
                "agent.rate_limit_warning session=%s reset_at=%s",
                session.label(),
                info.resets_at,
            )
            await self._alert_owner(f"Rate limit warning, resets at {reset}")
        elif status == "rejected":
            log.error(
                "agent.rate_limit_rejected session=%s reset_at=%s",
                session.label(),
                info.resets_at,
            )
            await self._alert_owner(
                f"Rate limit rejected for {session.label()}, resets at {reset}"
            )

    def _is_rate_limited(self, session: SessionState) -> bool:
        if session.rate_limit_status != "rejected" and self._cost_db is not None:
            latest = self._cost_db.latest_rate_limit(session.channel_id)
            reset_at = latest.get("reset_at")
            if latest.get("status") == "rejected" and (
                reset_at is None or time.time() < int(reset_at)
            ):
                session.rate_limit_status = "rejected"
                session.rate_limit_reset_at = int(reset_at) if reset_at else None
                session.rate_limit_updated_at = latest.get("ts")
        if session.rate_limit_status != "rejected":
            return False
        if session.rate_limit_reset_at is None:
            return True
        if time.time() < session.rate_limit_reset_at:
            return True
        session.rate_limit_status = "allowed"
        session.rate_limit_reset_at = None
        session.rate_limit_updated_at = datetime.datetime.now(
            datetime.UTC
        ).isoformat()
        return False

    async def _alert_owner(self, message: str) -> None:
        if self._owner_alert is None:
            return
        maybe_awaitable = self._owner_alert(message)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

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
        mcp_servers = {}
        extra_args: dict[str, str | None] = {}
        strict_mcp_config = False

        if manifest is not None:
            # setting_sources: cost-significant. Team channels should use
            # ["project"] to avoid pulling in the operator's personal
            # user-level settings (and to keep cost profile predictable).
            setting_sources = list(manifest.setting_sources)

            # Behavior overrides
            if manifest.behavior.max_turns is not None:
                max_turns = manifest.behavior.max_turns
            permission_mode = (
                "bypassPermissions"
                if manifest.tier_effective() == PermissionTier.YOLO
                else manifest.behavior.permission_mode
            )

            # Static scope
            decision = build_scope_decision(manifest)
            allowed_tools = decision.allowed_tools
            disallowed_tools = decision.disallowed_tools
            skills = decision.skills

            # Runtime scope guard (covers MCPs, which the static fields
            # can't always enumerate).
            can_use_tool = build_tool_guard(manifest)

            if not manifest.is_owner_dm():
                strict_mcp_config = True
                mcp_servers, _mcp_allowed, missing_mcp = resolve_team_mcp_servers(
                    manifest,
                    embedder=self._embedder,
                    log_exclusions=True,
                )
                warn_missing_mcp_servers(
                    manifest.channel_id,
                    missing_mcp,
                    logger=log,
                )
                extra_args["strict-mcp-config"] = None
            else:
                mcp_servers[MEMORY_SEARCH_SERVER_NAME] = make_memory_search_server(
                    session.channel_id,
                    embedder=self._embedder,
                    excluded_channels=manifest.memory.excluded_channels,
                )

            if (
                MEMORY_SEARCH_SERVER_NAME in mcp_servers
            ):
                for full_tool_name in MEMORY_SEARCH_FULL_TOOL_NAMES:
                    if full_tool_name not in allowed_tools:
                        allowed_tools.append(full_tool_name)

            if strict_mcp_config and not mcp_servers:
                extra_args["mcp-config"] = json.dumps({"mcpServers": {}})

        hitl_config = (
            self._router.hitl_config_for_channel(
                session.channel_id,
                manifest=manifest,
            )
            if self._router is not None
            else None
        )
        async def _noop_on_new_question(q) -> None:
            log.warning(
                "HITL question fired but no egress wired: pid=%s tool=%s",
                q.permission_request_id,
                q.tool_name,
            )

        on_new_question = getattr(
            self,
            "_on_new_question",
            _noop_on_new_question,
        )

        scope_can_use_tool = can_use_tool

        async def _scope_and_manifest_precheck(tool_name, tool_input, context):
            effective_input = tool_input
            if scope_can_use_tool is not None:
                scope_result = await scope_can_use_tool(
                    tool_name,
                    tool_input,
                    context,
                )
                if isinstance(scope_result, PermissionResultDeny):
                    return scope_result
                if scope_result.updated_input is not None:
                    effective_input = scope_result.updated_input

            if (
                manifest is not None
                and hitl_config is not None
                and self._router is not None
            ):
                trust_result = await self._maybe_gate_manifest_mcp_additions(
                    session=session,
                    manifest=manifest,
                    tool_name=tool_name,
                    tool_input=effective_input,
                    on_new_question=on_new_question,
                    timeout_s=hitl_config.timeout_s,
                    max_per_day=hitl_config.max_per_day,
                )
                if trust_result is not None:
                    if (
                        isinstance(trust_result, PermissionResultAllow)
                        and effective_input is not tool_input
                    ):
                        return PermissionResultAllow(updated_input=effective_input)
                    return trust_result

            if effective_input is tool_input:
                return PermissionResultAllow()
            return PermissionResultAllow(updated_input=effective_input)

        if self._router is not None and hitl_config is not None:
            can_use_tool = _scope_and_manifest_precheck

        if (
            self._router is not None
            and hitl_config is not None
            and hitl_config.enabled
        ):
            async def _scope_and_footgun_precheck(tool_name, tool_input, context):
                effective_tier = (
                    manifest.tier_effective() if manifest is not None else None
                )

                def _log_footgun_match(footgun_match) -> None:
                    if effective_tier is None:
                        return
                    log.info(
                        "footgun.matched",
                        extra={
                            "pattern": footgun_match.pattern.pattern,
                            "command": footgun_match.command,
                            "tier": effective_tier.value,
                        },
                    )

                initial_footgun_match = (
                    match_footgun(tool_name, tool_input)
                    if manifest is not None
                    else None
                )
                if initial_footgun_match is not None:
                    _log_footgun_match(initial_footgun_match)
                    if effective_tier == PermissionTier.TASK_ASSISTANT:
                        log.info(
                            "footgun.denied_task_assistant",
                            extra={
                                "pattern": initial_footgun_match.pattern.pattern,
                                "command": initial_footgun_match.command,
                                "tier": effective_tier.value,
                            },
                        )
                        return PermissionResultDeny(
                            message=(
                                "Destructive command blocked in safe tier. "
                                "Request upgrade to trusted."
                            ),
                        )

                scope_result = await _scope_and_manifest_precheck(
                    tool_name,
                    tool_input,
                    context,
                )
                if isinstance(scope_result, PermissionResultDeny):
                    return scope_result
                effective_input = scope_result.updated_input or tool_input

                if manifest is None:
                    return scope_result

                footgun_match = initial_footgun_match or match_footgun(
                    tool_name,
                    effective_input,
                )
                if footgun_match is None:
                    return scope_result

                if initial_footgun_match is None:
                    _log_footgun_match(footgun_match)
                if effective_tier == PermissionTier.TASK_ASSISTANT:
                    log.info(
                        "footgun.denied_task_assistant",
                        extra={
                            "pattern": footgun_match.pattern.pattern,
                            "command": footgun_match.command,
                            "tier": effective_tier.value,
                        },
                    )
                    return PermissionResultDeny(
                        message=(
                            "Destructive command blocked in safe tier. "
                            "Request upgrade to trusted."
                        ),
                    )

                if effective_input is tool_input:
                    return PermissionResultAllow()
                return PermissionResultAllow(updated_input=effective_input)

            def _question_metadata(tool_name, tool_input, _context):
                if manifest is None:
                    return {}
                effective_tier = manifest.tier_effective()
                if effective_tier not in {
                    PermissionTier.OWNER_SCOPED,
                    PermissionTier.YOLO,
                }:
                    return {}
                footgun_match = match_footgun(tool_name, tool_input)
                if footgun_match is None:
                    return {}
                metadata = {"footgun_match": footgun_match}
                if self._config.owner_user_id:
                    metadata["who_can_answer"] = self._config.owner_user_id
                elif session.current_user_id:
                    metadata["who_can_answer"] = session.current_user_id
                return metadata

            can_use_tool = build_hitl_tool_guard(
                router=self._router,
                channel_id=session.channel_id,
                session_id=session.session_id,
                client_provider=lambda: session.agent_client,
                on_new_question=on_new_question,
                default_timeout_s=hitl_config.timeout_s,
                max_per_day=hitl_config.max_per_day,
                precheck=_scope_and_footgun_precheck,
                question_metadata_provider=_question_metadata,
            )

        session_kwargs = (
            {"resume": session.session_id}
            if resume
            else {"session_id": session.session_id}
        )

        options = ClaudeAgentOptions(
            # Identity & discovery
            setting_sources=setting_sources,
            cwd=str(session.cwd) if session.cwd else None,
            model=self._config.anthropic.model,
            mcp_servers=mcp_servers,
            extra_args=extra_args,
            **session_kwargs,
            # Runtime limits
            max_turns=max_turns,
            permission_mode=permission_mode,
            # Scope (static — helps SDK skip priming denied entries)
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            skills=skills,
            # Scope (runtime — final enforcement)
            can_use_tool=can_use_tool,
            max_budget_usd=self._max_budget_usd_for_options(),
            hooks=self._build_hooks(session),
            stderr=cli_stderr_logger(session.channel_id),
        )
        # SDK 0.1.x exposes --strict-mcp-config through extra_args. Keep a
        # plain attribute so diagnostics and tests can inspect the policy.
        options.strict_mcp_config = strict_mcp_config
        return options

    def _build_hooks(self, session: SessionState) -> dict[str, list[HookMatcher]]:
        hooks = build_hooks(channel_id=session.channel_id, cost_db=self._cost_db)
        if self._router is None:
            return hooks

        stop_hook, precompact_hook = make_memory_hooks_with_embeddings(
            self._router,
            embedding_queue=self._embedding_queue,
        )
        hooks.setdefault("Stop", []).append(stop_hook)
        hooks.setdefault("PreCompact", []).append(precompact_hook)
        return hooks

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


def _format_reset_at(reset_at: int | None) -> str:
    if reset_at is None:
        return "an unknown reset time"
    return datetime.datetime.fromtimestamp(
        reset_at,
        tz=datetime.UTC,
    ).isoformat()


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


def _claude_cli_jsonl_for(session_id: str, cwd: Path | str | None = None) -> Path:
    """Return the Claude CLI transcript path for a session/cwd pair."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        claude_home = Path(unicodedata.normalize("NFC", config_dir))
    else:
        claude_home = Path(unicodedata.normalize("NFC", str(Path.home() / ".claude")))

    project_cwd = str(cwd if cwd is not None else Path.cwd())
    project_path = unicodedata.normalize("NFC", os.path.realpath(project_cwd))
    return (
        claude_home
        / "projects"
        / _claude_sanitize_path(project_path)
        / f"{session_id}.jsonl"
    )


def _claude_sanitize_path(name: str) -> str:
    sanitized = _CLAUDE_SANITIZE_RE.sub("-", name)
    if len(sanitized) <= _CLAUDE_MAX_SANITIZED_LENGTH:
        return sanitized
    return f"{sanitized[:_CLAUDE_MAX_SANITIZED_LENGTH]}-{_claude_simple_hash(name)}"


def _claude_simple_hash(value: str) -> str:
    hash_value = 0
    for char in value:
        hash_value = ((hash_value << 5) - hash_value + ord(char)) & 0xFFFFFFFF
        if hash_value >= 0x80000000:
            hash_value -= 0x100000000
    hash_value = abs(hash_value)
    if hash_value == 0:
        return "0"

    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    encoded = []
    while hash_value:
        encoded.append(digits[hash_value % 36])
        hash_value //= 36
    return "".join(reversed(encoded))
