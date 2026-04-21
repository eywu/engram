"""GRO-390 ClaudeSDKClient lifecycle and concurrency tests."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage

from engram.agent import Agent
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.router import Router, SessionState, derive_session_id


def _cfg() -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-test"),
        max_turns_per_message=3,
    )


@dataclass
class _TextBlock:
    text: str


@dataclass
class _FakeClient:
    options: ClaudeAgentOptions
    response_delay: float = 0.0
    events: list[str] = field(default_factory=list)
    active_counter: dict[str, int] | None = None
    connected: bool = False
    disconnected: bool = False
    tag_calls: list[dict[str, object]] = field(default_factory=list)
    _prompt: str = ""
    _session_id: str = ""

    async def connect(self) -> None:
        self.connected = True
        self.events.append("connect")

    async def disconnect(self) -> None:
        self.disconnected = True
        self.events.append("disconnect")

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self._prompt = prompt
        self._session_id = session_id
        self.events.append(f"query:{prompt}")
        if self.active_counter is not None:
            self.active_counter["current"] += 1
            self.active_counter["max"] = max(
                self.active_counter["max"],
                self.active_counter["current"],
            )

    async def receive_response(self):
        self.events.append(f"receive:{self._prompt}")
        if self.response_delay:
            await asyncio.sleep(self.response_delay)
        yield AssistantMessage(
            content=[_TextBlock(f"{self._prompt}:{self._session_id}")],
            model="fake",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self._session_id,
            total_cost_usd=0.01,
        )
        self.events.append(f"done:{self._prompt}")
        if self.active_counter is not None:
            self.active_counter["current"] -= 1

    async def tag_session(
        self,
        *,
        session_id: str,
        tags: dict[str, str],
    ) -> None:
        self.tag_calls.append({"session_id": session_id, "tags": tags})


def test_session_id_is_deterministic():
    channel_id = "C07TEST123"
    expected = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"engram-v3/{channel_id}"))

    assert derive_session_id(channel_id) == expected
    assert derive_session_id(channel_id) == derive_session_id(channel_id)
    assert derive_session_id(channel_id) != derive_session_id("C07TEST456")

    session = SessionState(channel_id=channel_id)
    assert session.session_id == expected


@pytest.mark.asyncio
async def test_per_channel_lock_serializes_turns():
    events: list[str] = []
    clients: list[_FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> _FakeClient:
        client = _FakeClient(options, response_delay=0.03, events=events)
        clients.append(client)
        return client

    agent = Agent(_cfg(), client_factory=factory)
    session = SessionState(channel_id="C07TEST123")

    first, second = await asyncio.gather(
        agent.run_turn(session, "first"),
        agent.run_turn(session, "second"),
    )

    assert first.text == f"first:{session.session_id}"
    assert second.text == f"second:{session.session_id}"
    assert len(clients) == 1
    assert events == [
        "connect",
        "query:first",
        "receive:first",
        "done:first",
        "query:second",
        "receive:second",
        "done:second",
    ]


@pytest.mark.asyncio
async def test_different_channels_run_concurrently():
    counter = {"current": 0, "max": 0}

    def factory(options: ClaudeAgentOptions) -> _FakeClient:
        return _FakeClient(
            options,
            response_delay=0.05,
            active_counter=counter,
        )

    agent = Agent(_cfg(), client_factory=factory)
    a = SessionState(channel_id="C07TESTA")
    b = SessionState(channel_id="C07TESTB")

    await asyncio.gather(
        agent.run_turn(a, "alpha"),
        agent.run_turn(b, "beta"),
    )

    assert counter["max"] == 2


@pytest.mark.asyncio
async def test_idle_client_is_closed_after_timeout():
    router = Router()
    session = await router.get("C07TEST123")
    client = _FakeClient(ClaudeAgentOptions())
    session.agent_client = client
    session.agent_last_active_at = 10.0

    closed = await router.close_idle_agent_clients(
        idle_timeout_seconds=5.0,
        now=16.0,
    )

    assert closed == 1
    assert client.disconnected
    assert session.agent_client is None


@pytest.mark.asyncio
async def test_first_turn_uses_session_id_subsequent_turns_use_resume():
    router = Router()
    session = await router.get("C07TEST123")
    options_seen: list[ClaudeAgentOptions] = []

    def factory(options: ClaudeAgentOptions) -> _FakeClient:
        options_seen.append(options)
        return _FakeClient(options)

    agent = Agent(_cfg(), client_factory=factory)

    await agent.run_turn(session, "first")
    await router.close_all_agent_clients()
    await agent.run_turn(session, "second")

    assert len(options_seen) == 2
    assert options_seen[0].session_id == session.session_id
    assert options_seen[0].resume is None
    assert options_seen[1].session_id is None
    assert options_seen[1].resume == session.session_id


@pytest.mark.asyncio
async def test_shutdown_closes_all_active_clients():
    router = Router()
    clients: list[_FakeClient] = []

    for channel_id in ("C07TEST1", "C07TEST2", "C07TEST3"):
        session = await router.get(channel_id)
        client = _FakeClient(ClaudeAgentOptions())
        session.agent_client = client
        clients.append(client)

    closed = await router.close_all_agent_clients()

    assert closed == 3
    assert all(client.disconnected for client in clients)
    assert all(session.agent_client is None for session in router.list_sessions())
