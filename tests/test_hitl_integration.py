"""GRO-407 HITL end-to-end integration tests."""
from __future__ import annotations

import asyncio
import fnmatch
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
)
from claude_agent_sdk.types import ToolPermissionContext

from engram.agent import Agent
from engram.bootstrap import ensure_project_root
from engram.config import AnthropicConfig, EngramConfig, HITLConfig, SlackConfig
from engram.egress import post_question, update_question_timeout
from engram.hitl import PendingQuestion, build_hitl_tool_guard
from engram.ingress import handle_block_action, handle_thread_reply
from engram.main import _schedule_timeout_update
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


@dataclass
class _TextBlock:
    text: str


def _matches_permission_rule(
    tool_name: str,
    tool_input: dict[str, Any],
    rule: str,
) -> bool:
    if "(" in rule and rule.endswith(")"):
        rule_tool, specifier = rule[:-1].split("(", 1)
    else:
        rule_tool, specifier = rule, None

    if rule_tool != tool_name:
        return False
    if specifier is None:
        return True
    if tool_name not in {"Read", "Grep", "Glob"}:
        return False

    candidate = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(candidate, str):
        return False

    expanded_candidate = str(Path(candidate).expanduser())
    expanded_specifier = (
        str(Path(specifier).expanduser())
        if specifier.startswith("~")
        else specifier
    )
    return fnmatch.fnmatch(expanded_candidate, expanded_specifier)


class PermissionAwareToolClient:
    """Test-only client that mirrors the SDK's permission ordering.

    Native Claude Code permission rules are applied before the runtime
    `can_use_tool` callback: `disallowed_tools` first, then `allowed_tools`,
    and only then `can_use_tool` for unresolved requests.
    """

    def __init__(
        self,
        options,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        self.options = options
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.events: list[str] = []
        self.interrupt_calls = 0
        self.permission_result = None
        self._session_id = ""

    async def connect(self) -> None:
        self.events.append("connect")

    async def disconnect(self) -> None:
        self.events.append("disconnect")

    async def query(self, _prompt: str, session_id: str = "default") -> None:
        self._session_id = session_id
        self.events.append("query")

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        self.events.append("interrupt")

    async def tag_session(self, *, session_id: str, tags: dict[str, str]) -> None:
        self.events.append(f"tag:{session_id}:{tags['channel_id']}")

    async def receive_response(self):
        result = await self._dispatch_tool()
        self.permission_result = result

        if isinstance(result, PermissionResultAllow):
            self.events.append("tool_ran")
            text = "tool ran"
        else:
            if result.interrupt:
                await self.interrupt()
            text = result.message or "denied"

        yield AssistantMessage(content=[_TextBlock(text)], model="fake")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self._session_id,
            total_cost_usd=0.01,
        )

    async def _dispatch_tool(self):
        for rule in self.options.disallowed_tools:
            if _matches_permission_rule(self.tool_name, self.tool_input, rule):
                self.events.append("sdk_disallowed")
                return PermissionResultDeny(
                    message=f"denied by SDK rule: {rule}",
                    interrupt=False,
                )

        for rule in self.options.allowed_tools:
            if _matches_permission_rule(self.tool_name, self.tool_input, rule):
                self.events.append("sdk_allowed")
                return PermissionResultAllow(updated_input=self.tool_input)

        if self.options.can_use_tool is None:
            self.events.append("implicit_allow")
            return PermissionResultAllow(updated_input=self.tool_input)

        self.events.append("permission_requested")
        return await self.options.can_use_tool(
            self.tool_name,
            self.tool_input,
            ToolPermissionContext(tool_use_id=f"tool-{self.tool_name.lower()}"),
        )


class ToolGateFakeClient:
    def __init__(self, options, target_path: Path) -> None:
        self.options = options
        self.target_path = target_path
        self.content = "engram haiku\n"
        self.events: list[str] = []
        self.interrupt_calls = 0
        self.permission_result = None
        self._session_id = ""

    async def connect(self) -> None:
        self.events.append("connect")

    async def disconnect(self) -> None:
        self.events.append("disconnect")

    async def query(self, _prompt: str, session_id: str = "default") -> None:
        self._session_id = session_id
        self.events.append("query")

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        self.events.append("interrupt")

    async def tag_session(self, *, session_id: str, tags: dict[str, str]) -> None:
        self.events.append(f"tag:{session_id}:{tags['channel_id']}")

    async def receive_response(self):
        assert self.options.can_use_tool is not None
        tool_input = {"file_path": str(self.target_path), "content": self.content}
        self.events.append("permission_requested")
        result = await self.options.can_use_tool(
            "Write",
            tool_input,
            ToolPermissionContext(tool_use_id="tool-write"),
        )
        self.permission_result = result
        if isinstance(result, PermissionResultAllow):
            write_input = result.updated_input or tool_input
            Path(write_input["file_path"]).write_text(
                write_input["content"],
                encoding="utf-8",
            )
            self.events.append("write")
            text = "wrote file"
        else:
            if result.interrupt:
                await self.interrupt()
            text = result.message or "denied"

        yield AssistantMessage(content=[_TextBlock(text)], model="fake")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self._session_id,
            total_cost_usd=0.01,
        )


def _agent_cfg() -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )


async def _build_owner_dm_permission_harness(
    tmp_path: Path,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
):
    ensure_project_root(home=tmp_path)
    owner_dm_id = "D07OWNER477"
    router = Router(
        home=tmp_path,
        owner_dm_channel_id=owner_dm_id,
        hitl=HITLConfig(timeout_s=30),
    )
    questions: list[PendingQuestion] = []
    clients: list[PermissionAwareToolClient] = []

    async def on_new_question(q: PendingQuestion) -> None:
        q.slack_channel_ts = THREAD_TS
        q.slack_thread_ts = THREAD_TS
        questions.append(q)

    def client_factory(options) -> PermissionAwareToolClient:
        client = PermissionAwareToolClient(
            options,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        clients.append(client)
        return client

    agent = Agent(_agent_cfg(), client_factory=client_factory, router=router)
    agent._on_new_question = on_new_question
    session = await router.get(owner_dm_id, is_dm=True)
    return agent, session, router, questions, clients


class HITLHarness:
    """Exercises the HITL core (``_request_hitl_decision``) via the
    production entry point :func:`build_hitl_tool_guard`.

    GRO-432 migrated these tests off the deprecated
    ``build_permission_request_hook``. The harness still drives the same
    rate-limit, timeout, Slack round-trip, and registry behaviors — only
    the SDK adapter layer changed.
    """

    # Fixed session id used by all harness-driven guard invocations.
    # Real bridge flows derive this from the channel; tests don't care.
    SESSION_ID = "session-harness"

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
        self.guard = build_hitl_tool_guard(
            router=self.router,
            channel_id=CHANNEL_ID,
            session_id=self.SESSION_ID,
            client_provider=lambda: self.client,
            on_new_question=self._on_new_question,
            default_timeout_s=default_timeout_s,
        )
        self._update_on_timeout = update_on_timeout

    async def ask(
        self,
        *,
        tool_name: str = "Bash",
        tool_input: dict[str, Any] | None = None,
        tool_use_id: str = "tool-1",
        suggestions: list[Any] | None = None,
    ) -> Any:
        """Drive the tool guard with a synthetic tool invocation.

        Mirrors what the SDK's ``can_use_tool`` path does at runtime:
        the guard posts a Slack question, awaits the operator's
        resolution (or a timeout), and returns a ``PermissionResult``.
        """
        ctx = ToolPermissionContext(tool_use_id=tool_use_id)
        ctx.suggestions = list(suggestions or [])  # type: ignore[attr-defined]
        return await self.guard(
            tool_name,
            dict(tool_input or {"cmd": "pytest"}),
            ctx,
        )

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


@pytest.mark.asyncio
async def test_agent_hitl_tool_guard_blocks_write_until_resolved(tmp_path: Path):
    router = Router(hitl=HITLConfig(timeout_s=30))
    questions: list[PendingQuestion] = []
    clients: list[ToolGateFakeClient] = []
    target_path = tmp_path / "m4-debug.txt"

    async def on_new_question(q: PendingQuestion) -> None:
        q.slack_channel_ts = THREAD_TS
        q.slack_thread_ts = THREAD_TS
        questions.append(q)

    def client_factory(options) -> ToolGateFakeClient:
        client = ToolGateFakeClient(options, target_path)
        clients.append(client)
        return client

    agent = Agent(_agent_cfg(), client_factory=client_factory, router=router)
    agent._on_new_question = on_new_question
    session = await router.get(CHANNEL_ID, is_dm=True)

    turn_task = asyncio.create_task(
        agent.run_turn(
            session,
            "write me a haiku about engrams and save it to m4-debug.txt",
        )
    )
    await wait_until(lambda: len(questions) == 1 and len(clients) == 1)
    await asyncio.sleep(0.05)

    assert not target_path.exists()
    assert not turn_task.done()
    assert clients[0].events == ["connect", "query", "permission_requested"]

    router.hitl.resolve(questions[0].permission_request_id, PermissionResultAllow())
    turn = await asyncio.wait_for(turn_task, timeout=1)

    assert target_path.read_text(encoding="utf-8") == "engram haiku\n"
    assert turn.text == "wrote file"
    assert "write" in clients[0].events


@pytest.mark.asyncio
async def test_agent_hitl_deny_button_denies_write_and_interrupts(tmp_path: Path):
    router = Router(hitl=HITLConfig(timeout_s=30))
    slack = FakeSlackClient()
    questions: list[PendingQuestion] = []
    clients: list[ToolGateFakeClient] = []
    target_path = tmp_path / "m4-debug-denied.txt"

    async def on_new_question(q: PendingQuestion) -> None:
        q.slack_channel_ts = THREAD_TS
        q.slack_thread_ts = THREAD_TS
        questions.append(q)

    def client_factory(options) -> ToolGateFakeClient:
        client = ToolGateFakeClient(options, target_path)
        clients.append(client)
        return client

    agent = Agent(_agent_cfg(), client_factory=client_factory, router=router)
    agent._on_new_question = on_new_question
    session = await router.get(CHANNEL_ID, is_dm=True)

    turn_task = asyncio.create_task(
        agent.run_turn(
            session,
            "write me a haiku about engrams and save it to m4-debug-denied.txt",
        )
    )
    await wait_until(lambda: len(questions) == 1 and len(clients) == 1)
    q = questions[0]

    ack = await handle_block_action(
        block_action_payload(f"{q.permission_request_id}|deny"),
        router,
        slack,
    )
    turn = await asyncio.wait_for(turn_task, timeout=1)

    assert ack == {"ok": True}
    assert not target_path.exists()
    assert isinstance(clients[0].permission_result, PermissionResultDeny)
    assert clients[0].permission_result.message == "user denied"
    assert clients[0].permission_result.interrupt is True
    assert clients[0].interrupt_calls == 1
    assert turn.text == "user denied"


@pytest.mark.asyncio
async def test_owner_dm_webfetch_allow_list_skips_hitl_prompt(tmp_path: Path):
    agent, session, _router, questions, clients = (
        await _build_owner_dm_permission_harness(
            tmp_path,
            tool_name="WebFetch",
            tool_input={"url": "https://example.com"},
        )
    )

    turn = await agent.run_turn(session, "fetch example.com")

    assert turn.text == "tool ran"
    assert questions == []
    assert "permission_requested" not in clients[0].events
    assert "sdk_allowed" in clients[0].events
    assert "tool_ran" in clients[0].events
    assert clients[0].interrupt_calls == 0


@pytest.mark.asyncio
async def test_owner_dm_bash_still_triggers_hitl_prompt(tmp_path: Path):
    agent, session, router, questions, clients = (
        await _build_owner_dm_permission_harness(
            tmp_path,
            tool_name="Bash",
            tool_input={"cmd": "pwd"},
        )
    )

    turn_task = asyncio.create_task(agent.run_turn(session, "run pwd"))
    await wait_until(lambda: len(questions) == 1 and len(clients) == 1)

    assert "permission_requested" in clients[0].events
    assert not turn_task.done()

    router.hitl.resolve(questions[0].permission_request_id, PermissionResultAllow())
    turn = await asyncio.wait_for(turn_task, timeout=1)

    assert turn.text == "tool ran"
    assert "tool_ran" in clients[0].events


@pytest.mark.asyncio
async def test_owner_dm_read_secret_path_denied_before_hitl(tmp_path: Path):
    agent, session, _router, questions, clients = (
        await _build_owner_dm_permission_harness(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": str(Path("~/.ssh/id_rsa").expanduser())},
        )
    )

    turn = await agent.run_turn(session, "read my ssh key")

    assert turn.text == "denied by SDK rule: Read(~/.ssh/**)"
    assert questions == []
    assert "permission_requested" not in clients[0].events
    assert "sdk_disallowed" in clients[0].events


@pytest.mark.asyncio
async def test_two_rapid_questions_second_denied():
    harness = HITLHarness()

    first_task = asyncio.create_task(harness.ask(tool_use_id="tool-a"))
    first_q = await wait_for_question(harness)

    assert harness.router.hitl.get_by_id(first_q.permission_request_id) is first_q

    started_at = time.perf_counter()
    second_output = await harness.ask(tool_use_id="tool-b")
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.1
    assert isinstance(second_output, PermissionResultDeny)
    assert "another question already pending" in second_output.message
    assert not first_q.future.done()
    assert harness.router.hitl.pending_for_channel(CHANNEL_ID) == [first_q]

    harness.router.hitl.resolve(first_q.permission_request_id, PermissionResultAllow())
    first_output = await asyncio.wait_for(first_task, timeout=1)
    assert isinstance(first_output, PermissionResultAllow)


@pytest.mark.asyncio
async def test_timeout_triggers_interrupt_and_deny():
    harness = HITLHarness(default_timeout_s=1, update_on_timeout=True)

    output = await harness.ask()

    assert isinstance(output, PermissionResultDeny)
    assert output.message == "question timed out after 1s"
    assert output.interrupt is True
    assert harness.client.interrupt_calls == 1
    await wait_until(lambda: len(harness.slack.update_calls) == 1)
    assert harness.slack.update_calls[0]["text"] == "Timed out"
    assert "⏱️ Question timed out" in harness.slack.update_calls[0]["blocks"][0]["text"]["text"]


@pytest.mark.asyncio
async def test_production_timeout_callback_updates_slack():
    harness = HITLHarness(default_timeout_s=1)

    output_task = asyncio.create_task(harness.ask())
    q = await wait_for_question(harness)
    _schedule_timeout_update(q, harness.slack)

    output = await asyncio.wait_for(output_task, timeout=2)

    assert isinstance(output, PermissionResultDeny)
    assert output.message == "question timed out after 1s"
    assert output.interrupt is True
    await wait_until(lambda: len(harness.slack.update_calls) == 1)
    assert harness.slack.update_calls[0]["text"] == "Timed out"


@pytest.mark.asyncio
async def test_client_disconnect_during_wait_cancels_future():
    harness = HITLHarness()

    task = asyncio.create_task(harness.ask())
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
    task = asyncio.create_task(first_bridge.ask())
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

    task = asyncio.create_task(harness.ask(suggestions=[suggestion]))
    q = await wait_for_question(harness)

    assert len(harness.slack.post_calls) == 1
    ack = await handle_block_action(
        block_action_payload(f"{q.permission_request_id}|0"),
        harness.router,
        harness.slack,
    )

    assert ack == {"ok": True}
    output = await asyncio.wait_for(task, timeout=1)
    assert isinstance(output, PermissionResultAllow)
    assert output.updated_input == {"cmd": "pytest"}
    await wait_until(lambda: len(harness.slack.update_calls) == 1)
    assert harness.slack.update_calls[0]["text"] == "Answered: Run pytest"


@pytest.mark.asyncio
async def test_full_happy_path_thread_reply():
    harness = HITLHarness()

    task = asyncio.create_task(harness.ask())
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
    assert isinstance(output, PermissionResultAllow)
    assert output.updated_input == {
        "cmd": "pytest",
        "_user_answer": "Please run only the focused pytest target.",
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

    old_session_output = await harness.ask(tool_use_id="tool-old")
    new_session_output = await harness.ask(tool_use_id="tool-new")

    expected_message = "HITL rate-limited: daily question budget exhausted (5/day)"
    assert isinstance(old_session_output, PermissionResultDeny)
    assert old_session_output.message == expected_message
    assert isinstance(new_session_output, PermissionResultDeny)
    assert new_session_output.message == expected_message
    assert harness.questions == []
    assert harness.slack.post_calls == []
