"""In-memory human-in-the-loop primitives."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import (
    CanUseTool,
    HookCallback,
    PermissionRequestHookInput,
    PermissionRequestHookSpecificOutput,
    SyncHookJSONOutput,
    ToolPermissionContext,
)

PermissionResult = PermissionResultAllow | PermissionResultDeny
ToolPrecheck = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResult],
]
log = logging.getLogger(__name__)


def _create_future() -> asyncio.Future[PermissionResult]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.create_future()


@dataclass
class PendingQuestion:
    permission_request_id: str
    channel_id: str
    session_id: str
    turn_id: str
    tool_name: str
    tool_input: dict[str, Any]
    suggestions: list[Any]
    who_can_answer: str | None
    posted_at: datetime
    timeout_s: int
    future: asyncio.Future[PermissionResult] = field(default_factory=_create_future)
    slack_channel_ts: str | None = None
    slack_thread_ts: str | None = None


class HITLRegistry:
    """In-memory registry of pending questions, keyed by permission_request_id."""

    def __init__(self) -> None:
        self._by_id: dict[str, PendingQuestion] = {}

    def register(self, q: PendingQuestion) -> None:
        if q.permission_request_id in self._by_id:
            raise ValueError(
                f"Duplicate permission_request_id: {q.permission_request_id}"
            )
        self._by_id[q.permission_request_id] = q

    def resolve(self, permission_request_id: str, result: PermissionResult) -> bool:
        """Set the future for this question. Returns True if resolved."""
        q = self._by_id.get(permission_request_id)
        if q is None:
            return False
        if q.future.done():
            return False
        q.future.set_result(result)
        return True

    def get_by_id(self, permission_request_id: str) -> PendingQuestion | None:
        return self._by_id.get(permission_request_id)

    def pending_for_channel(self, channel_id: str) -> list[PendingQuestion]:
        """Return unresolved questions for this channel."""
        return [
            q
            for q in self._by_id.values()
            if q.channel_id == channel_id and not q.future.done()
        ]

    def cleanup_resolved(self) -> int:
        """Remove resolved questions from the registry and return count removed."""
        resolved_ids = [pid for pid, q in self._by_id.items() if q.future.done()]
        for pid in resolved_ids:
            del self._by_id[pid]
        return len(resolved_ids)


class HITLRateLimiter:
    """Per-channel rate limiter for HITL questions."""

    def __init__(self, registry: HITLRegistry, max_per_day: int = 5) -> None:
        self._registry = registry
        self._max_per_day = max_per_day
        self._daily_counts: dict[str, tuple[date, int]] = {}

    def check(
        self,
        channel_id: str,
        now: datetime | None = None,
        max_per_day: int | None = None,
    ) -> tuple[bool, str]:
        """Check if a new question is allowed. Does not reserve capacity."""
        now = now or datetime.now(UTC)
        daily_limit = self._max_per_day if max_per_day is None else max_per_day
        if self._registry.pending_for_channel(channel_id):
            return (False, "another question already pending in this channel")

        today = now.date()
        day, count = self._daily_counts.get(channel_id, (today, 0))
        if day != today:
            count = 0
        if count >= daily_limit:
            return (
                False,
                f"daily question budget exhausted ({daily_limit}/day)",
            )
        return (True, "")

    def reserve(self, channel_id: str, now: datetime | None = None) -> None:
        """Increment the daily counter after a successful register."""
        now = now or datetime.now(UTC)
        today = now.date()
        day, count = self._daily_counts.get(channel_id, (today, 0))
        if day != today:
            count = 0
        self._daily_counts[channel_id] = (today, count + 1)


def build_permission_request_hook(
    router: Any,
    channel_id: str,
    client_provider: Callable[[], Any | None],
    on_new_question: Callable[[PendingQuestion], Awaitable[None]],
    default_timeout_s: int = 300,
    max_per_day: int | None = None,
) -> HookCallback:
    """Build a PermissionRequest hook that round-trips through HITL."""

    async def hook(
        input_data: PermissionRequestHookInput,
        _tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> SyncHookJSONOutput:
        tool_use_id = _tool_use_id or input_data.get("tool_use_id")
        result = await _request_hitl_decision(
            router=router,
            channel_id=channel_id,
            client_provider=client_provider,
            on_new_question=on_new_question,
            session_id=input_data["session_id"],
            tool_name=input_data["tool_name"],
            tool_input=input_data["tool_input"],
            suggestions=list(input_data.get("permission_suggestions") or []),
            tool_use_id=tool_use_id,
            default_timeout_s=default_timeout_s,
            max_per_day=max_per_day,
            fired_event="hitl.hook_fired",
            returned_event="hitl.hook_returned",
        )
        return _permission_result_to_hook_output(result)

    return hook


def build_hitl_tool_guard(
    router: Any,
    channel_id: str,
    session_id: str,
    client_provider: Callable[[], Any | None],
    on_new_question: Callable[[PendingQuestion], Awaitable[None]],
    default_timeout_s: int = 300,
    max_per_day: int | None = None,
    precheck: ToolPrecheck | None = None,
) -> CanUseTool:
    """Build a blocking can_use_tool callback backed by HITL.

    The Claude Agent SDK waits for can_use_tool before dispatching the tool.
    Scope policy can be composed in front with ``precheck`` so manifest-denied
    tools are rejected without asking Slack.
    """

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        effective_input = tool_input
        if precheck is not None:
            precheck_result = await precheck(tool_name, tool_input, context)
            if isinstance(precheck_result, PermissionResultDeny):
                return precheck_result
            if precheck_result.updated_input is not None:
                effective_input = precheck_result.updated_input

        return await _request_hitl_decision(
            router=router,
            channel_id=channel_id,
            client_provider=client_provider,
            on_new_question=on_new_question,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=effective_input,
            suggestions=list(getattr(context, "suggestions", []) or []),
            tool_use_id=getattr(context, "tool_use_id", None),
            default_timeout_s=default_timeout_s,
            max_per_day=max_per_day,
            fired_event="hitl.tool_guard_fired",
            returned_event="hitl.tool_guard_returned",
        )

    return can_use_tool


async def _request_hitl_decision(
    *,
    router: Any,
    channel_id: str,
    client_provider: Callable[[], Any | None],
    on_new_question: Callable[[PendingQuestion], Awaitable[None]],
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    suggestions: list[Any],
    tool_use_id: str | None,
    default_timeout_s: int,
    max_per_day: int | None,
    fired_event: str,
    returned_event: str,
) -> PermissionResult:
    started_at = time.monotonic()
    result: PermissionResult | None = None
    log.info(
        fired_event,
        extra={
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "channel_id": channel_id,
            "session_id": session_id,
        },
    )
    try:
        daily_limit = max_per_day
        if daily_limit is None:
            daily_limit = router.hitl_config_for_channel(channel_id).max_per_day
        allowed, reason = router.hitl_limiter.check(
            channel_id,
            max_per_day=daily_limit,
        )
        if not allowed:
            log.info(
                "hitl.rate_limited",
                extra={"reason": reason, "channel_id": channel_id},
            )
            result = PermissionResultDeny(message=f"HITL rate-limited: {reason}")
            return result

        permission_request_id = str(uuid.uuid4())
        q = PendingQuestion(
            permission_request_id=permission_request_id,
            channel_id=channel_id,
            session_id=session_id,
            turn_id=str(uuid.uuid4()),
            tool_name=tool_name,
            tool_input=tool_input,
            suggestions=suggestions,
            who_can_answer=None,
            posted_at=datetime.now(UTC),
            timeout_s=default_timeout_s,
        )

        router.hitl.register(q)
        log.info(
            "hitl.question_registered",
            extra={
                "permission_request_id": permission_request_id,
                "tool_name": q.tool_name,
            },
        )
        router.hitl_limiter.reserve(channel_id)

        try:
            await on_new_question(q)
            log.info(
                "hitl.question_posted",
                extra={
                    "slack_channel_ts": q.slack_channel_ts,
                    "permission_request_id": permission_request_id,
                },
            )
        except Exception as exc:
            log.error(
                "hitl.question_post_failed",
                exc_info=True,
                extra={
                    "exception": f"{type(exc).__name__}: {exc}",
                    "permission_request_id": permission_request_id,
                },
            )
            router.hitl.resolve(
                permission_request_id,
                PermissionResultDeny(message="failed to post question"),
            )

        try:
            result = await asyncio.wait_for(q.future, timeout=q.timeout_s)
        except TimeoutError:
            log.info(
                "hitl.question_timed_out",
                extra={
                    "permission_request_id": permission_request_id,
                    "timeout_s": q.timeout_s,
                },
            )
            client = client_provider()
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.interrupt()
            result = PermissionResultDeny(
                interrupt=True,
                message=f"question timed out after {q.timeout_s}s",
            )
        finally:
            router.hitl.cleanup_resolved()

        return result
    finally:
        if result is not None:
            log.info(
                returned_event,
                extra={
                    "decision": _decision_label(result),
                    "duration_ms": _duration_ms(started_at),
                },
            )


def _permission_result_to_hook_output(
    result: PermissionResult,
) -> SyncHookJSONOutput:
    if isinstance(result, PermissionResultAllow):
        decision: dict[str, Any] = {"behavior": "allow"}
        if result.updated_input is not None:
            decision["updatedInput"] = result.updated_input
        if result.updated_permissions is not None:
            decision["updatedPermissions"] = [
                permission.to_dict() for permission in result.updated_permissions
            ]
        return _permission_request_output(decision)

    if isinstance(result, PermissionResultDeny):
        decision = {"behavior": "deny", "message": result.message}
        if result.interrupt:
            decision["interrupt"] = True
        return _permission_request_output(decision)

    raise TypeError(f"Unknown PermissionResult type: {type(result)}")


def _decision_label(result: PermissionResult) -> str:
    if isinstance(result, PermissionResultAllow):
        return "allow"
    if isinstance(result, PermissionResultDeny):
        return "deny"
    raise TypeError(f"Unknown PermissionResult type: {type(result)}")


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.monotonic() - started_at) * 1000))


def _permission_request_output(decision: dict[str, Any]) -> SyncHookJSONOutput:
    hook_specific: PermissionRequestHookSpecificOutput = {
        "hookEventName": "PermissionRequest",
        "decision": decision,
    }
    return {"hookSpecificOutput": hook_specific}
