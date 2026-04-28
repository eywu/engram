"""GRO-390 ClaudeSDKClient lifecycle and concurrency tests."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ProcessError, ResultMessage
from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock, UserMessage

from engram.agent import Agent, _claude_cli_jsonl_for
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.egress import post_reply
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    ScopeList,
)
from engram.router import (
    Router,
    SessionState,
    archive_session_transcript,
    derive_session_id,
)


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


@dataclass
class _TranscriptFakeClient:
    options: ClaudeAgentOptions
    _prompt: str = ""
    _session_id: str = ""

    async def connect(self) -> None:
        session_id = self.options.session_id or self.options.resume
        transcript_path = _claude_cli_jsonl_for(session_id or "", self.options.cwd)
        if self.options.session_id and transcript_path.exists():
            raise ProcessError(
                f"Error: Session ID {self.options.session_id} is already in use.",
                exit_code=1,
            )

    async def disconnect(self) -> None:
        pass

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self._prompt = prompt
        self._session_id = session_id

    async def receive_response(self):
        transcript_path = _claude_cli_jsonl_for(self._session_id, self.options.cwd)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with transcript_path.open("a", encoding="utf-8") as transcript:
            transcript.write(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": self._session_id,
                        "message": {"content": self._prompt},
                    }
                )
                + "\n"
            )
            transcript.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": self._session_id,
                        "message": {"content": f"ok:{self._prompt}"},
                    }
                )
                + "\n"
            )

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


@dataclass
class _MultiTurnToolClient:
    options: ClaudeAgentOptions
    _prompt: str = ""
    _session_id: str = ""
    receive_messages_calls: int = 0
    receive_response_calls: int = 0

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self._prompt = prompt
        self._session_id = session_id

    async def receive_messages(self):
        self.receive_messages_calls += 1
        yield AssistantMessage(
            content=[_TextBlock("Both files written successfully. ")],
            model="fake",
        )
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="tool-todo-write",
                    name="TodoWrite",
                    input={"todos": [{"content": "mark done"}]},
                )
            ],
            model="fake",
            stop_reason="tool_use",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self._session_id,
            stop_reason="tool_use",
            total_cost_usd=0.01,
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tool-todo-write",
                    content="TodoWrite OK",
                    is_error=False,
                )
            ],
            parent_tool_use_id="tool-todo-write",
            tool_use_result={"ok": True},
        )
        yield AssistantMessage(
            content=[_TextBlock("Done. Here's what was set up.")],
            model="fake",
            stop_reason="end_turn",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=2,
            duration_api_ms=2,
            is_error=False,
            num_turns=2,
            session_id=self._session_id,
            stop_reason="end_turn",
            total_cost_usd=0.02,
        )

    async def receive_response(self):
        self.receive_response_calls += 1
        async for message in self.receive_messages():
            yield message
            if isinstance(message, ResultMessage):
                return

    async def tag_session(self, *, session_id: str, tags: dict[str, str]) -> None:
        return None


class _SlackRecorder:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, object]] = []
        self.chat_postMessage = self._chat_post_message

    async def _chat_post_message(self, **kwargs):
        self.post_calls.append(kwargs)
        return {"ts": "1713800000.000100"}


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
async def test_manifest_mcp_exclusion_logs_once_per_client_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "linear": {
                        "type": "http",
                        "url": "https://linear.example/mcp",
                    },
                    "camoufox": {"command": "camoufox-mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    manifest = ChannelManifest(
        channel_id="C07TEST123",
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
        mcp_servers=ScopeList(allowed=["linear"]),
    )
    session = SessionState(channel_id="C07TEST123", manifest=manifest)
    clients: list[_FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> _FakeClient:
        client = _FakeClient(options)
        clients.append(client)
        return client

    agent = Agent(_cfg(), client_factory=factory)
    caplog.set_level(logging.INFO, logger="engram.mcp")

    await agent.run_turn(session, "first")
    await agent.run_turn(session, "second")

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "mcp.excluded_by_manifest"
    ]
    assert len(clients) == 1
    assert len(records) == 1
    assert records[0].channel_id == "C07TEST123"
    assert records[0].mcp_name == "camoufox"
    assert records[0].reason == "not_in_allowed"


@pytest.mark.asyncio
async def test_existing_transcript_after_state_reset_resumes_from_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    channel_id = "C07TEST123"
    session_id = derive_session_id(channel_id)
    transcript_path = _claude_cli_jsonl_for(session_id, cwd)
    options_seen: list[ClaudeAgentOptions] = []

    def factory(options: ClaudeAgentOptions) -> _TranscriptFakeClient:
        options_seen.append(options)
        return _TranscriptFakeClient(options)

    agent = Agent(_cfg(), client_factory=factory, retry_base_delay_seconds=0)
    first_session = SessionState(channel_id=channel_id, cwd=cwd)
    first = await agent.run_turn(first_session, "first")

    assert first.error_message is None
    assert transcript_path.exists()

    second_session = SessionState(channel_id=channel_id, cwd=cwd)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="engram.agent")
    second = await agent.run_turn(second_session, "second")

    assert second.error_message is None
    assert len(options_seen) == 2
    assert options_seen[0].session_id == session_id
    assert options_seen[0].resume is None
    assert options_seen[1].session_id is None
    assert options_seen[1].resume == session_id
    assert "agent.session_resume_from_disk" in caplog.text

    records = [
        json.loads(line)
        for line in transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    user_prompts = [
        record["message"]["content"]
        for record in records
        if record["type"] == "user"
    ]
    assert user_prompts == ["first", "second"]


def test_archive_session_transcript_renames_existing_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = derive_session_id("C07TEST123")
    transcript_path = _claude_cli_jsonl_for(session_id, cwd)
    transcript_path.parent.mkdir(parents=True)
    transcript_path.write_text('{"type":"user"}\n', encoding="utf-8")

    archived = archive_session_transcript(session_id, cwd)

    assert archived is not None
    assert archived.name.startswith(f"{session_id}.jsonl.archived-")
    assert archived.read_text(encoding="utf-8") == '{"type":"user"}\n'
    assert not transcript_path.exists()


def test_archive_session_transcript_noops_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))

    assert archive_session_transcript(derive_session_id("C07TEST123"), tmp_path) is None


@pytest.mark.asyncio
async def test_start_new_conversation_disconnects_archives_and_disables_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    router = Router(shared_cwd=cwd)
    session = await router.get("C07TEST123")
    client = _FakeClient(ClaudeAgentOptions())
    session.agent_client = client
    session.agent_session_initialized = True
    transcript_path = _claude_cli_jsonl_for(session.session_id, cwd)
    transcript_path.parent.mkdir(parents=True)
    transcript_path.write_text('{"type":"user"}\n', encoding="utf-8")

    archived = await router.start_new_conversation("C07TEST123")

    assert client.disconnected is True
    assert session.agent_client is None
    assert session.agent_session_initialized is False
    assert session.agent_session_tagged is False
    assert session.session_just_started is True
    assert archived is not None
    assert archived.exists()
    assert not transcript_path.exists()


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


@pytest.mark.asyncio
async def test_run_turn_collects_text_after_tool_result_before_terminal_result():
    client: _MultiTurnToolClient | None = None

    def factory(options: ClaudeAgentOptions) -> _MultiTurnToolClient:
        nonlocal client
        client = _MultiTurnToolClient(options)
        return client

    agent = Agent(_cfg(), client_factory=factory)
    session = SessionState(channel_id="C07TEST123")

    turn = await agent.run_turn(session, "hook up camoufox")

    assert turn.text == (
        "Both files written successfully. Done. Here's what was set up."
    )
    assert turn.cost_usd == pytest.approx(0.02)
    assert turn.num_turns == 2
    assert client is not None
    assert client.receive_messages_calls == 1
    assert client.receive_response_calls == 0


@pytest.mark.asyncio
async def test_post_reply_egresses_full_summary_after_tool_result():
    def factory(options: ClaudeAgentOptions) -> _MultiTurnToolClient:
        return _MultiTurnToolClient(options)

    agent = Agent(_cfg(), client_factory=factory)
    session = SessionState(channel_id="C07TEST123")
    slack = _SlackRecorder()

    turn = await agent.run_turn(session, "hook up camoufox")
    result = await post_reply(
        slack,
        session.channel_id,
        turn,
        session_label=session.label(),
    )

    assert result.chunks_posted == 1
    assert len(slack.post_calls) == 1
    body = slack.post_calls[0]["blocks"][0]["text"]
    assert "Both files written successfully." in body
    assert "Done. Here's what was set up." in body
