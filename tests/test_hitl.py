"""HITL foundation tests."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from engram.hitl import (
    HITLRateLimiter,
    HITLRegistry,
    PendingQuestion,
)

# NOTE: build_permission_request_hook was removed in GRO-432 (M4.5).
# Its tests (output-shape probes + hook-based integration tests) were
# deleted here. The surviving tests below exercise HITLRegistry and
# HITLRateLimiter directly. Integration coverage of the full tool-guard
# → Slack → resolve flow lives in tests/test_hitl_integration.py and
# tests/test_hitl_precheck.py.


def make_question(
    permission_request_id: str = "prq-1",
    *,
    channel_id: str = "C07TEST123",
) -> PendingQuestion:
    return PendingQuestion(
        permission_request_id=permission_request_id,
        channel_id=channel_id,
        session_id="session-1",
        turn_id="turn-1",
        tool_name="Bash",
        tool_input={"cmd": "pytest"},
        suggestions=[],
        who_can_answer=None,
        posted_at=datetime(2026, 4, 21, tzinfo=UTC),
        timeout_s=300,
    )


def test_registry_register_and_get():
    registry = HITLRegistry()
    q = make_question()

    registry.register(q)

    assert registry.get_by_id("prq-1") is q


def test_registry_duplicate_id_raises():
    registry = HITLRegistry()
    registry.register(make_question())

    with pytest.raises(ValueError, match="Duplicate permission_request_id"):
        registry.register(make_question())


def test_registry_resolve_sets_future():
    registry = HITLRegistry()
    q = make_question()
    result = PermissionResultAllow()
    registry.register(q)

    assert registry.resolve("prq-1", result) is True

    assert q.future.done()
    assert q.future.result() is result


def test_registry_resolve_missing_returns_false():
    registry = HITLRegistry()

    assert registry.resolve("missing", PermissionResultAllow()) is False


def test_registry_resolve_already_done_returns_false():
    registry = HITLRegistry()
    registry.register(make_question())

    assert registry.resolve("prq-1", PermissionResultAllow()) is True

    assert registry.resolve("prq-1", PermissionResultDeny(message="no")) is False


def test_registry_pending_for_channel_filters_by_channel():
    registry = HITLRegistry()
    channel_q = make_question("prq-channel", channel_id="C07TEST123")
    other_q = make_question("prq-other", channel_id="C07OTHER")
    registry.register(channel_q)
    registry.register(other_q)

    assert registry.pending_for_channel("C07TEST123") == [channel_q]


def test_registry_pending_for_channel_excludes_resolved():
    registry = HITLRegistry()
    q = make_question()
    registry.register(q)
    registry.resolve("prq-1", PermissionResultAllow())

    assert registry.pending_for_channel("C07TEST123") == []


def test_registry_cleanup_resolved_removes_done():
    registry = HITLRegistry()
    resolved_q = make_question("prq-resolved")
    pending_q = make_question("prq-pending")
    registry.register(resolved_q)
    registry.register(pending_q)
    registry.resolve("prq-resolved", PermissionResultAllow())

    assert registry.cleanup_resolved() == 1
    assert registry.get_by_id("prq-resolved") is None
    assert registry.get_by_id("prq-pending") is pending_q


def test_limiter_first_question_allowed():
    limiter = HITLRateLimiter(HITLRegistry())

    assert limiter.check("C07TEST123") == (True, "")


def test_limiter_second_open_question_denied():
    registry = HITLRegistry()
    registry.register(make_question())
    limiter = HITLRateLimiter(registry)

    allowed, reason = limiter.check("C07TEST123")

    assert allowed is False
    assert reason == "another question already pending in this channel"


def test_limiter_daily_cap_enforced():
    limiter = HITLRateLimiter(HITLRegistry())
    now = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    for _ in range(5):
        limiter.reserve("C07TEST123", now=now)

    allowed, reason = limiter.check("C07TEST123", now=now)

    assert allowed is False
    assert reason == "daily question budget exhausted (5/day)"


def test_limiter_midnight_reset():
    limiter = HITLRateLimiter(HITLRegistry())
    day_one = datetime(2026, 4, 21, 23, 59, tzinfo=UTC)
    day_two = datetime(2026, 4, 22, 0, 1, tzinfo=UTC)
    for _ in range(5):
        limiter.reserve("C07TEST123", now=day_one)

    assert limiter.check("C07TEST123", now=day_one)[0] is False
    assert limiter.check("C07TEST123", now=day_two) == (True, "")


