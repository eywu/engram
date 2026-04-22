"""GRO-407 HITL end-to-end integration tests."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow

from engram.egress import post_question, update_question_timeout
from engram.hitl import PendingQuestion, build_permission_request_hook
from engram.ingress import handle_block_action, handle_thread_reply
from engram.router import Router

CHANNEL_ID = "C07TEST123"
THREAD_TS = "1713800000.000100"


class FakeSlackClient:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.chat_postMessage = self._chat_post_message

    async def _chat_post_message(self, **kwargs: Any) -> dict[str, str]:
        self.post_calls.append(kwargs)
        return {"ts": THREAD_TS}

    async def chat_update(self, **kwargs: Any) -> dict[str, bool]:
        self.update_calls.append(kwargs)
        return {"ok": True}


class MockClaudeSDKClient:
    def __init__(self) -> None:
        self.interrupt_calls = 0

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


class HITLHarness:
    def __init__(
        self,
        *,
        router: Router | None = None,
        default_timeout_s: int = 300,
        update_on_timeout: bool = False,
    ) -> None:
        self.router = router or Router()
        self.slack = FakeSlackClient()
        self.client = MockClaudeSDKClient()
        self.questions: list[PendingQuestion] = []
        self.timeout_update_tasks: list[asyncio.Task[None]] = []
        self.hook = build_permission_request_hook(
            router=self.router,
            channel_id=CHANNEL_ID,
            client_provider=lambda: self.client,
            on_new_question=self._on_new_question,
            default_timeout_s=default_timeout_s,
        )
        self._update_on_timeout = update_on_timeout

    async def _on_new_question(self, q: PendingQuestion) -> None:
        self.questions.append(q)
        channel_ts, thread_ts = await post_question(q, self.slack)
        q.slack_channel_ts = channel_ts
        q.slack_thread_ts = thread_ts

        if self._update_on_timeout:

            def update_if_timed_out(future: asyncio.Future[Any]) -> None:
                if future.cancelled():
                    self.timeout_update_tasks.append(
                        asyncio.create_task(update_question_timeout(q, self.slack))
                    )

            q.future.add_done_callback(update_if_timed_out)


def permission_request_input(
    *,
    session_id: str = "session-1",
    tool_input: dict[str, Any] | None = None,
    suggestions: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "hook_event_name": "PermissionRequest",
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "tool_name": "Bash",
        "tool_input": tool_input or {"cmd": "pytest"},
        "permission_suggestions": suggestions or [],
    }


def block_action_payload(value: str, *, user_id: str = "U123") -> dict[str, Any]:
    return {
        "type": "block_actions",
        "actions": [{"value": value}],
        "user": {"id": user_id},
    }


async def wait_until(predicate, *, timeout_s: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            pytest.fail("condition was not met before timeout")
        await asyncio.sleep(0)


async def wait_for_question(harness: HITLHarness) -> PendingQuestion:
    await wait_until(lambda: len(harness.questions) == 1)
    return harness.questions[0]


def hook_decision(output: dict[str, Any]) -> dict[str, Any]:
    return output["hookSpecificOutput"]["decision"]


@pytest.mark.asyncio
async def test_two_rapid_questions_second_denied():
    harness = HITLHarness()

    first_task = asyncio.create_task(
        harness.hook(permission_request_input(session_id="session-a"), "tool-a", {})
    )
    first_q = await wait_for_question(harness)

    assert harness.router.hitl.get_by_id(first_q.permission_request_id) is first_q

    started_at = time.perf_counter()
    second_output = await harness.hook(
        permission_request_input(session_id="session-b"), "tool-b", {}
    )
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.1
    assert hook_decision(second_output)["behavior"] == "deny"
    assert "another question already pending" in hook_decision(second_output)["message"]
    assert not first_q.future.done()
    assert harness.router.hitl.pending_for_channel(CHANNEL_ID) == [first_q]

    harness.router.hitl.resolve(first_q.permission_request_id, PermissionResultAllow())
    first_output = await asyncio.wait_for(first_task, timeout=1)
    assert hook_decision(first_output) == {"behavior": "allow"}


@pytest.mark.asyncio
async def test_timeout_triggers_interrupt_and_deny():
    harness = HITLHarness(default_timeout_s=1, update_on_timeout=True)

    output = await harness.hook(permission_request_input(), "tool-1", {})

    assert hook_decision(output) == {
        "behavior": "deny",
        "message": "question timed out after 1s",
        "interrupt": True,
    }
    assert harness.client.interrupt_calls == 1
    await wait_until(lambda: len(harness.slack.update_calls) == 1)
    assert harness.slack.update_calls[0]["text"] == "Timed out"
    assert "⏱️ Question timed out" in harness.slack.update_calls[0]["blocks"][0]["text"]["text"]


@pytest.mark.asyncio
async def test_client_disconnect_during_wait_cancels_future():
    harness = HITLHarness()

    task = asyncio.create_task(harness.hook(permission_request_input(), "tool-1", {}))
    q = await wait_for_question(harness)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert q.future.cancelled()
    harness.router.hitl.cleanup_resolved()
    assert harness.router.hitl.get_by_id(q.permission_request_id) is None
    assert harness.router.hitl.pending_for_channel(CHANNEL_ID) == []


@pytest.mark.asyncio
async def test_bridge_restart_loses_pending_but_recovers():
    first_bridge = HITLHarness()
    task = asyncio.create_task(
        first_bridge.hook(permission_request_input(), "tool-1", {})
    )
    q = await wait_for_question(first_bridge)

    restarted_router = Router()
    ack = await handle_block_action(
        block_action_payload(f"{q.permission_request_id}|0"),
        restarted_router,
        first_bridge.slack,
    )

    assert ack == {"ok": False, "error": "question not found (may be resolved)"}
    assert restarted_router.hitl.pending_for_channel(CHANNEL_ID) == []
    assert first_bridge.slack.update_calls == []

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_full_happy_path_allow():
    suggestion = {"name": "Run pytest"}
    harness = HITLHarness()

    task = asyncio.create_task(
        harness.hook(
            permission_request_input(suggestions=[suggestion]),
            "tool-1",
            {},
        )
    )
    q = await wait_for_question(harness)

    assert len(harness.slack.post_calls) == 1
    ack = await handle_block_action(
        block_action_payload(f"{q.permission_request_id}|0"),
        harness.router,
        harness.slack,
    )

    assert ack == {"ok": True}
    output = await asyncio.wait_for(task, timeout=1)
    assert hook_decision(output) == {
        "behavior": "allow",
        "updatedInput": {"cmd": "pytest"},
    }
    await wait_until(lambda: len(harness.slack.update_calls) == 1)
    assert harness.slack.update_calls[0]["text"] == "Answered: Run pytest"


@pytest.mark.asyncio
async def test_full_happy_path_thread_reply():
    harness = HITLHarness()

    task = asyncio.create_task(harness.hook(permission_request_input(), "tool-1", {}))
    q = await wait_for_question(harness)

    await handle_thread_reply(
        {
            "channel": CHANNEL_ID,
            "thread_ts": q.slack_thread_ts,
            "text": "Please run only the focused pytest target.",
            "user": "U123",
        },
        harness.router,
        harness.slack,
    )

    output = await asyncio.wait_for(task, timeout=1)
    assert hook_decision(output) == {
        "behavior": "allow",
        "updatedInput": {
            "cmd": "pytest",
            "_user_answer": "Please run only the focused pytest target.",
        },
    }
    assert harness.slack.update_calls[0]["text"] == (
        "Answered: Please run only the focused pytest target."
    )


@pytest.mark.asyncio
async def test_daily_cap_across_sessions():
    router = Router()
    harness = HITLHarness(router=router)
    for _ in range(5):
        router.hitl_limiter.reserve(CHANNEL_ID)

    old_session_output = await harness.hook(
        permission_request_input(session_id="session-old"), "tool-old", {}
    )
    new_session_output = await harness.hook(
        permission_request_input(session_id="session-new"), "tool-new", {}
    )

    expected = {
        "behavior": "deny",
        "message": "HITL rate-limited: daily question budget exhausted (5/day)",
    }
    assert hook_decision(old_session_output) == expected
    assert hook_decision(new_session_output) == expected
    assert harness.questions == []
    assert harness.slack.post_calls == []
