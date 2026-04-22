"""HITL foundation tests."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import PermissionRuleValue, PermissionUpdate

from engram.hitl import (
    HITLRateLimiter,
    HITLRegistry,
    PendingQuestion,
    _permission_result_to_hook_output,
    build_permission_request_hook,
)
from engram.router import Router


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


def permission_request_input() -> dict:
    return {
        "hook_event_name": "PermissionRequest",
        "session_id": "session-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "tool_name": "Bash",
        "tool_input": {"cmd": "pytest"},
        "permission_suggestions": [
            PermissionUpdate(
                type="addRules",
                rules=[PermissionRuleValue(tool_name="Bash", rule_content="pytest")],
                behavior="allow",
                destination="session",
            )
        ],
    }


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


async def _next_question(questions: list[PendingQuestion]) -> PendingQuestion:
    while not questions:
        await asyncio.sleep(0)
    return questions[0]


@pytest.mark.asyncio
async def test_hook_posts_question_and_awaits():
    router = Router()
    questions: list[PendingQuestion] = []

    async def on_new_question(q: PendingQuestion) -> None:
        questions.append(q)

    hook = build_permission_request_hook(
        router=router,
        channel_id="C07TEST123",
        client_provider=lambda: None,
        on_new_question=on_new_question,
    )

    task = asyncio.create_task(hook(permission_request_input(), "tool-1", {}))
    q = await asyncio.wait_for(_next_question(questions), timeout=1)

    assert router.hitl.get_by_id(q.permission_request_id) is q
    assert q.session_id == "session-1"
    assert q.tool_name == "Bash"
    assert q.tool_input == {"cmd": "pytest"}
    assert q.suggestions == permission_request_input()["permission_suggestions"]

    router.hitl.resolve(q.permission_request_id, PermissionResultAllow())

    assert await task == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    assert router.hitl.get_by_id(q.permission_request_id) is None


@pytest.mark.asyncio
async def test_hook_timeout_calls_interrupt_and_denies():
    class FakeClient:
        def __init__(self) -> None:
            self.interrupted = False

        async def interrupt(self) -> None:
            self.interrupted = True

    client = FakeClient()

    async def on_new_question(_q: PendingQuestion) -> None:
        return None

    hook = build_permission_request_hook(
        router=Router(),
        channel_id="C07TEST123",
        client_provider=lambda: client,
        on_new_question=on_new_question,
        default_timeout_s=0,
    )

    assert await hook(permission_request_input(), "tool-1", {}) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "question timed out after 0s",
                "interrupt": True,
            },
        }
    }
    assert client.interrupted is True


@pytest.mark.asyncio
async def test_hook_rate_limit_auto_denies():
    router = Router()
    router.hitl.register(make_question())
    called = False

    async def on_new_question(_q: PendingQuestion) -> None:
        nonlocal called
        called = True

    hook = build_permission_request_hook(
        router=router,
        channel_id="C07TEST123",
        client_provider=lambda: None,
        on_new_question=on_new_question,
    )

    assert await hook(permission_request_input(), "tool-1", {}) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": (
                    "HITL rate-limited: another question already pending "
                    "in this channel"
                ),
            },
        }
    }
    assert called is False
    assert len(router.hitl.pending_for_channel("C07TEST123")) == 1


@pytest.mark.asyncio
async def test_hook_daily_cap_auto_denies():
    router = Router()
    for _ in range(5):
        router.hitl_limiter.reserve("C07TEST123")
    called = False

    async def on_new_question(_q: PendingQuestion) -> None:
        nonlocal called
        called = True

    hook = build_permission_request_hook(
        router=router,
        channel_id="C07TEST123",
        client_provider=lambda: None,
        on_new_question=on_new_question,
    )

    assert await hook(permission_request_input(), "tool-1", {}) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "HITL rate-limited: daily question budget exhausted (5/day)",
            },
        }
    }
    assert called is False
    assert router.hitl.pending_for_channel("C07TEST123") == []


@pytest.mark.asyncio
async def test_hook_on_new_question_failure_denies_fast():
    async def on_new_question(_q: PendingQuestion) -> None:
        raise RuntimeError("slack failed")

    router = Router()
    hook = build_permission_request_hook(
        router=router,
        channel_id="C07TEST123",
        client_provider=lambda: None,
        on_new_question=on_new_question,
    )

    assert await hook(permission_request_input(), "tool-1", {}) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "failed to post question",
            },
        }
    }
    assert router.hitl.pending_for_channel("C07TEST123") == []


def test_hook_output_shape_allow():
    assert _permission_result_to_hook_output(PermissionResultAllow()) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


def test_hook_output_shape_deny():
    assert _permission_result_to_hook_output(
        PermissionResultDeny(message="no", interrupt=True)
    ) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "no",
                "interrupt": True,
            },
        }
    }


def test_hook_output_shape_allow_with_updated_permissions():
    update = PermissionUpdate(
        type="addRules",
        rules=[PermissionRuleValue(tool_name="Bash", rule_content="pytest")],
        behavior="allow",
        destination="session",
    )

    assert _permission_result_to_hook_output(
        PermissionResultAllow(updated_permissions=[update])
    ) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "allow",
                "updatedPermissions": [
                    {
                        "type": "addRules",
                        "destination": "session",
                        "rules": [
                            {
                                "toolName": "Bash",
                                "ruleContent": "pytest",
                            }
                        ],
                        "behavior": "allow",
                    }
                ],
            },
        }
    }
