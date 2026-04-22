"""In-memory human-in-the-loop primitives."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

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
        self, channel_id: str, now: datetime | None = None
    ) -> tuple[bool, str]:
        """Check if a new question is allowed. Does not reserve capacity."""
        now = now or datetime.now(UTC)
        if self._registry.pending_for_channel(channel_id):
            return (False, "another question already pending in this channel")

        today = now.date()
        day, count = self._daily_counts.get(channel_id, (today, 0))
        if day != today:
            count = 0
        if count >= self._max_per_day:
            return (
                False,
                f"daily question budget exhausted ({self._max_per_day}/day)",
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
