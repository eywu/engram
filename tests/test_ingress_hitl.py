"""Ingress HITL tests for Slack button actions and thread replies."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import PermissionRuleValue, PermissionUpdate

from engram.hitl import PendingQuestion
from engram.ingress import handle_block_action, handle_thread_reply
from engram.router import Router


class FakeSlackClient:
    def __init__(self) -> None:
        self.update_calls = []

    async def chat_update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"ok": True}


def make_question(
    permission_request_id: str = "prq-1",
    *,
    channel_id: str = "C07TEST123",
    suggestions=None,
    who_can_answer: str | None = None,
) -> PendingQuestion:
    return PendingQuestion(
        permission_request_id=permission_request_id,
        channel_id=channel_id,
        session_id="session-1",
        turn_id="turn-1",
        tool_name="Bash",
        tool_input={"cmd": "pytest", "timeout": 30},
        suggestions=list(suggestions or []),
        who_can_answer=who_can_answer,
        posted_at=datetime(2026, 4, 22, tzinfo=UTC),
        timeout_s=300,
        slack_channel_ts="1713800000.000100",
        slack_thread_ts="1713800000.000100",
    )


def block_action_payload(value: str, *, user_id: str = "U123") -> dict:
    return {
        "type": "block_actions",
        "actions": [{"value": value}],
        "user": {"id": user_id},
    }


def permission_update() -> PermissionUpdate:
    return PermissionUpdate(
        type="addRules",
        rules=[PermissionRuleValue(tool_name="Bash", rule_content="pytest")],
        behavior="allow",
        destination="session",
    )


async def wait_until(predicate) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1
    while not predicate():
        if loop.time() > deadline:
            pytest.fail("condition was not met before timeout")
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_block_action_happy_path():
    router = Router()
    slack = FakeSlackClient()
    suggestion = permission_update()
    q = make_question(suggestions=[suggestion])
    router.hitl.register(q)

    ack = await handle_block_action(
        block_action_payload("prq-1|0"), router, slack
    )

    assert ack == {"ok": True}
    await wait_until(lambda: q.future.done() and len(slack.update_calls) == 1)
    result = q.future.result()
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == q.tool_input
    assert result.updated_permissions == [suggestion]
    assert slack.update_calls[0]["channel"] == "C07TEST123"
    assert slack.update_calls[0]["ts"] == "1713800000.000100"


@pytest.mark.asyncio
async def test_block_action_deny_button():
    router = Router()
    slack = FakeSlackClient()
    q = make_question()
    router.hitl.register(q)

    ack = await handle_block_action(
        block_action_payload("prq-1|deny"), router, slack
    )

    assert ack == {"ok": True}
    await wait_until(lambda: q.future.done() and len(slack.update_calls) == 1)
    result = q.future.result()
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "user denied"
    assert slack.update_calls[0]["text"] == "Answered: Deny"


@pytest.mark.asyncio
async def test_block_action_wrong_user_rejected():
    router = Router()
    slack = FakeSlackClient()
    q = make_question(who_can_answer="U_ALLOWED")
    router.hitl.register(q)

    ack = await handle_block_action(
        block_action_payload("prq-1|0", user_id="U_OTHER"), router, slack
    )

    assert ack == {"ok": False, "error": "not authorized"}
    assert not q.future.done()
    assert slack.update_calls == []


@pytest.mark.asyncio
async def test_block_action_missing_question_ok():
    router = Router()
    slack = FakeSlackClient()

    ack = await handle_block_action(
        block_action_payload("missing|0"), router, slack
    )

    assert ack == {"ok": False, "error": "question not found (may be resolved)"}
    assert slack.update_calls == []


@pytest.mark.asyncio
async def test_block_action_already_resolved_idempotent():
    router = Router()
    slack = FakeSlackClient()
    q = make_question()
    router.hitl.register(q)
    original_result = PermissionResultAllow()
    router.hitl.resolve("prq-1", original_result)

    ack = await handle_block_action(
        block_action_payload("prq-1|deny"), router, slack
    )

    assert ack == {"ok": True, "info": "already resolved"}
    assert q.future.result() is original_result
    assert slack.update_calls == []


@pytest.mark.asyncio
async def test_thread_reply_happy_path():
    router = Router()
    slack = FakeSlackClient()
    q = make_question()
    router.hitl.register(q)

    await handle_thread_reply(
        {
            "channel": "C07TEST123",
            "thread_ts": "1713800000.000100",
            "text": "Please run the focused pytest target.",
            "user": "U123",
        },
        router,
        slack,
    )

    assert q.future.done()
    result = q.future.result()
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {
        "cmd": "pytest",
        "timeout": 30,
        "_user_answer": "Please run the focused pytest target.",
    }
    assert slack.update_calls[0]["text"] == (
        "Answered: Please run the focused pytest target."
    )


@pytest.mark.asyncio
async def test_thread_reply_wrong_channel_ignored():
    router = Router()
    slack = FakeSlackClient()
    q = make_question(channel_id="C07TEST123")
    router.hitl.register(q)

    await handle_thread_reply(
        {
            "channel": "C07OTHER",
            "thread_ts": "1713800000.000100",
            "text": "This should not resolve the question.",
            "user": "U123",
        },
        router,
        slack,
    )

    assert not q.future.done()
    assert slack.update_calls == []
