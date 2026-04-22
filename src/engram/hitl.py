"""In-memory human-in-the-loop primitives."""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import (
    HookCallback,
    PermissionRequestHookInput,
    PermissionRequestHookSpecificOutput,
    SyncHookJSONOutput,
)

PermissionResult = PermissionResultAllow | PermissionResultDeny


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
        daily_limit = max_per_day
        if daily_limit is None:
            daily_limit = router.hitl_config_for_channel(channel_id).max_per_day
        allowed, reason = router.hitl_limiter.check(
            channel_id,
            max_per_day=daily_limit,
        )
        if not allowed:
            return _permission_result_to_hook_output(
                PermissionResultDeny(message=f"HITL rate-limited: {reason}")
            )

        permission_request_id = str(uuid.uuid4())
        q = PendingQuestion(
            permission_request_id=permission_request_id,
            channel_id=channel_id,
            session_id=input_data["session_id"],
            turn_id=str(uuid.uuid4()),
            tool_name=input_data["tool_name"],
            tool_input=input_data["tool_input"],
            suggestions=list(input_data.get("permission_suggestions") or []),
            who_can_answer=None,
            posted_at=datetime.now(UTC),
            timeout_s=default_timeout_s,
        )

        router.hitl.register(q)
        router.hitl_limiter.reserve(channel_id)

        try:
            await on_new_question(q)
        except Exception:
            router.hitl.resolve(
                permission_request_id,
                PermissionResultDeny(message="failed to post question"),
            )

        try:
            result = await asyncio.wait_for(q.future, timeout=q.timeout_s)
        except TimeoutError:
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

        return _permission_result_to_hook_output(result)

    return hook


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


def _permission_request_output(decision: dict[str, Any]) -> SyncHookJSONOutput:
    hook_specific: PermissionRequestHookSpecificOutput = {
        "hookEventName": "PermissionRequest",
        "decision": decision,
    }
    return {"hookSpecificOutput": hook_specific}
