"""Ingress HITL tests for Slack button actions and thread replies."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import PermissionRuleValue, PermissionUpdate

from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.hitl import PendingQuestion
from engram.ingress import (
    HITL_ACTION_ID_PATTERN,
    handle_block_action,
    handle_thread_reply,
    register_listeners,
)
from engram.router import Router


class DecoratorApp:
    def __init__(self) -> None:
        self.actions = []
        self.events = []

    def action(self, pattern):
        def decorator(func):
            self.actions.append((pattern, func))
            return func

        return decorator

    def event(self, event_name):
        def decorator(func):
            self.events.append((event_name, func))
            return func

        return decorator


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
    choice_key = value.split("|", 1)[1] if "|" in value else "0"
    return {
        "type": "block_actions",
        "actions": [
            {
                "action_id": f"hitl_choice_{choice_key}",
                "block_id": "hitl_actions",
                "value": value,
            }
        ],
        "user": {"id": user_id},
    }


def permission_update() -> PermissionUpdate:
    return PermissionUpdate(
        type="addRules",
        rules=[PermissionRuleValue(tool_name="Bash", rule_content="pytest")],
        behavior="allow",
        destination="session",
    )


def make_config() -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )


async def wait_until(predicate) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1
    while not predicate():
        if loop.time() > deadline:
            pytest.fail("condition was not met before timeout")
        await asyncio.sleep(0)


def test_register_listeners_attaches_hitl_action_handler():
    app = DecoratorApp()

    register_listeners(app, make_config(), Router(), agent=object())

    assert len(app.actions) == 1
    pattern, _handler = app.actions[0]
    assert pattern is HITL_ACTION_ID_PATTERN
    assert pattern.match("hitl_choice_0")
    assert pattern.match("hitl_choice_4")
    assert pattern.match("hitl_choice_deny")
    assert not pattern.match("hitl_other_0")
    assert not pattern.match("hitl_choice_cancel")


@pytest.mark.asyncio
async def test_registered_hitl_action_handler_acks_and_resolves_question():
    app = DecoratorApp()
    router = Router()
    slack = FakeSlackClient()
    q = make_question(suggestions=[{"name": "Run pytest"}])
    router.hitl.register(q)
    ack_calls = 0

    async def ack():
        nonlocal ack_calls
        ack_calls += 1

    register_listeners(app, make_config(), router, agent=object())
    _pattern, handler = app.actions[0]

    await handler(
        ack=ack,
        body=block_action_payload("prq-1|0"),
        client=slack,
    )

    assert ack_calls == 1
    await wait_until(lambda: q.future.done() and len(slack.update_calls) == 1)
    result = q.future.result()
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == q.tool_input
    assert slack.update_calls[0]["text"] == "Answered: Run pytest"


@pytest.mark.asyncio
async def test_block_action_happy_path(caplog: pytest.LogCaptureFixture):
    router = Router()
    slack = FakeSlackClient()
    suggestion = permission_update()
    q = make_question(suggestions=[suggestion])
    router.hitl.register(q)

    with caplog.at_level(logging.INFO, logger="engram.hitl"):
        ack = await handle_block_action(
            block_action_payload("prq-1|0"), router, slack
        )
        await wait_until(lambda: q.future.done() and len(slack.update_calls) == 1)

    assert ack == {"ok": True}
    result = q.future.result()
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == q.tool_input
    assert result.updated_permissions == [suggestion]
    assert slack.update_calls[0]["channel"] == "C07TEST123"
    assert slack.update_calls[0]["ts"] == "1713800000.000100"
    answer_records = [
        record
        for record in caplog.records
        if record.name == "engram.hitl"
        and record.getMessage() == "hitl.answer_received"
    ]
    assert len(answer_records) == 1
    answer = answer_records[0]
    assert answer.permission_request_id == "prq-1"
    assert answer.choice == "0"
    assert answer.decision == "allow"


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
