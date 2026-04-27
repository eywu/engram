"""GRO-555: pre-turn MCP health check + in-turn watchdog.

Covers ``engram.mcp_health`` (pure unit tests on the primitives) and the
agent's wiring through ``_run_sdk_turn_once`` (one integration-style test
per layer).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage

from engram.agent import Agent
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.mcp_health import (
    DEFAULT_RECONNECT_FAIL_THRESHOLD,
    McpHealthWatchdog,
    PreTurnDisableOutcome,
    _extract_servers,
    disable_failed_mcps_pre_turn,
    warning_chunk_for_pre_turn,
)
from engram.router import SessionState


def _cfg() -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-test"),
        max_turns_per_message=3,
    )


@dataclass
class _TextBlock:
    text: str


# ──────────────────────────────────────────────────────────────────────────
# Pure unit tests on mcp_health primitives
# ──────────────────────────────────────────────────────────────────────────


def test_extract_servers_handles_dict_and_camelcase_and_snakecase() -> None:
    assert _extract_servers({"mcpServers": [{"name": "a"}]}) == [{"name": "a"}]
    assert _extract_servers({"mcp_servers": [{"name": "b"}]}) == [{"name": "b"}]
    assert _extract_servers(None) == []
    assert _extract_servers({"mcpServers": []}) == []
    # Non-dict entries dropped silently.
    assert _extract_servers({"mcpServers": [{"name": "ok"}, "garbage"]}) == [
        {"name": "ok"}
    ]


def test_extract_servers_handles_model_dump() -> None:
    class _Resp:
        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            return {"mcpServers": [{"name": "x", "status": "connected"}]}

    assert _extract_servers(_Resp()) == [{"name": "x", "status": "connected"}]


def test_warning_chunk_for_pre_turn_renders_one_or_many() -> None:
    assert warning_chunk_for_pre_turn([]) is None
    assert warning_chunk_for_pre_turn(["camoufox"]) is not None
    assert "`camoufox`" in (warning_chunk_for_pre_turn(["camoufox"]) or "")
    multi = warning_chunk_for_pre_turn(["camoufox", "playwright"]) or ""
    assert "`camoufox`" in multi and "`playwright`" in multi
    # Empty / whitespace names are filtered.
    assert warning_chunk_for_pre_turn(["", None]) is None  # type: ignore[list-item]


# ──────────────────────────────────────────────────────────────────────────
# Pre-turn disable behavior
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _ControlClient:
    """Minimal fake exposing get_mcp_status + toggle_mcp_server."""

    statuses: list[Any] = field(default_factory=list)
    toggled: list[tuple[str, bool]] = field(default_factory=list)
    raise_on_status: Exception | None = None
    raise_on_toggle: Exception | None = None
    _poll_idx: int = 0

    async def get_mcp_status(self) -> Any:
        if self.raise_on_status is not None:
            raise self.raise_on_status
        if not self.statuses:
            return {"mcpServers": []}
        idx = min(self._poll_idx, len(self.statuses) - 1)
        self._poll_idx += 1
        return self.statuses[idx]

    async def toggle_mcp_server(self, server_name: str, enabled: bool) -> None:
        if self.raise_on_toggle is not None:
            raise self.raise_on_toggle
        self.toggled.append((server_name, enabled))


@pytest.mark.asyncio
async def test_disable_failed_mcps_pre_turn_disables_failed_and_needs_auth() -> None:
    client = _ControlClient(
        statuses=[
            {
                "mcpServers": [
                    {"name": "camoufox", "status": "failed"},
                    {"name": "playwright", "status": "needs-auth"},
                    {"name": "memory", "status": "connected"},
                ]
            }
        ]
    )
    already: set[str] = set()

    out = await disable_failed_mcps_pre_turn(
        client, session_label="ch:CTEST", already_disabled=already
    )

    assert isinstance(out, PreTurnDisableOutcome)
    assert sorted(out.disabled) == ["camoufox", "playwright"]
    assert out.healthy == ["memory"]
    assert out.error is None
    assert sorted(already) == ["camoufox", "playwright"]
    assert sorted(client.toggled) == [("camoufox", False), ("playwright", False)]


@pytest.mark.asyncio
async def test_pre_turn_skips_already_disabled() -> None:
    client = _ControlClient(
        statuses=[
            {
                "mcpServers": [
                    {"name": "camoufox", "status": "failed"},
                    {"name": "playwright", "status": "failed"},
                ]
            }
        ]
    )
    already: set[str] = {"camoufox"}

    out = await disable_failed_mcps_pre_turn(
        client, session_label="ch:CTEST", already_disabled=already
    )

    assert out.disabled == ["playwright"]
    assert out.skipped_already_disabled == ["camoufox"]
    assert client.toggled == [("playwright", False)]
    assert already == {"camoufox", "playwright"}


@pytest.mark.asyncio
async def test_pre_turn_status_error_returns_outcome_with_error_set() -> None:
    client = _ControlClient(raise_on_status=RuntimeError("boom"))
    already: set[str] = set()

    out = await disable_failed_mcps_pre_turn(
        client, session_label="ch:CTEST", already_disabled=already
    )

    assert out.disabled == []
    assert "RuntimeError" in (out.error or "")
    assert client.toggled == []
    assert already == set()


@pytest.mark.asyncio
async def test_pre_turn_toggle_error_does_not_mark_disabled() -> None:
    client = _ControlClient(
        statuses=[{"mcpServers": [{"name": "camoufox", "status": "failed"}]}],
        raise_on_toggle=RuntimeError("nope"),
    )
    already: set[str] = set()

    out = await disable_failed_mcps_pre_turn(
        client, session_label="ch:CTEST", already_disabled=already
    )

    # Server stays unmarked so the caller can re-attempt next turn.
    assert out.disabled == []
    assert already == set()


# ──────────────────────────────────────────────────────────────────────────
# Watchdog behavior
# ──────────────────────────────────────────────────────────────────────────


def _failed(name: str) -> dict[str, Any]:
    return {"name": name, "status": "failed"}


def _connected(name: str) -> dict[str, Any]:
    return {"name": name, "status": "connected"}


@pytest.mark.asyncio
async def test_watchdog_trips_after_threshold_consecutive_failures() -> None:
    statuses = [
        {"mcpServers": [_failed("camoufox")]},  # poll 1
        {"mcpServers": [_failed("camoufox")]},  # poll 2
        {"mcpServers": [_failed("camoufox")]},  # poll 3 → trip
        {"mcpServers": [_failed("camoufox")]},  # would trip again, idempotent
    ]
    client = _ControlClient(statuses=statuses)
    already: set[str] = set()
    wd = McpHealthWatchdog(
        client,
        session_label="ch:CTEST",
        already_disabled=already,
        threshold=3,
        poll_interval_s=0.001,
    )

    # Drive the loop manually so the test is deterministic.
    for _ in range(4):
        await wd._poll_once()

    assert client.toggled == [("camoufox", False)]
    assert already == {"camoufox"}
    assert len(wd.trips) == 1
    assert wd.trips[0].server == "camoufox"
    assert wd.trips[0].consecutive_failures == 3
    assert len(wd.warnings) == 1
    assert "`camoufox`" in wd.warnings[0]


@pytest.mark.asyncio
async def test_watchdog_resets_counter_on_recovery() -> None:
    statuses = [
        {"mcpServers": [_failed("camoufox")]},
        {"mcpServers": [_failed("camoufox")]},
        {"mcpServers": [_connected("camoufox")]},  # recovers — counter resets
        {"mcpServers": [_failed("camoufox")]},
        {"mcpServers": [_failed("camoufox")]},
        # 5 polls, only 2 consecutive at the end → no trip
    ]
    client = _ControlClient(statuses=statuses)
    wd = McpHealthWatchdog(
        client,
        session_label="ch:CTEST",
        already_disabled=set(),
        threshold=3,
        poll_interval_s=0.001,
    )

    for _ in range(5):
        await wd._poll_once()

    assert client.toggled == []
    assert wd.trips == []


@pytest.mark.asyncio
async def test_watchdog_skips_already_disabled_servers() -> None:
    statuses = [{"mcpServers": [_failed("camoufox"), _failed("playwright")]}] * 5
    client = _ControlClient(statuses=statuses)
    already: set[str] = {"camoufox"}  # already pre-turn disabled
    wd = McpHealthWatchdog(
        client,
        session_label="ch:CTEST",
        already_disabled=already,
        threshold=3,
        poll_interval_s=0.001,
    )

    for _ in range(5):
        await wd._poll_once()

    # Only playwright trips — camoufox was already disabled coming in.
    assert client.toggled == [("playwright", False)]
    assert already == {"camoufox", "playwright"}


@pytest.mark.asyncio
async def test_watchdog_swallows_status_errors() -> None:
    """A transient SDK error during a poll must not kill the watchdog."""
    client = _ControlClient(raise_on_status=RuntimeError("transient"))
    wd = McpHealthWatchdog(
        client,
        session_label="ch:CTEST",
        already_disabled=set(),
        threshold=3,
        poll_interval_s=0.001,
    )

    # Should not raise.
    await wd._poll_once()
    await wd._poll_once()
    assert wd.trips == []


@pytest.mark.asyncio
async def test_watchdog_run_loop_cancellable() -> None:
    """Spawn the loop, wait one poll interval, then cancel cleanly."""
    statuses = [{"mcpServers": [_connected("memory")]}] * 100
    client = _ControlClient(statuses=statuses)
    wd = McpHealthWatchdog(
        client,
        session_label="ch:CTEST",
        already_disabled=set(),
        threshold=3,
        poll_interval_s=0.001,
    )

    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.005)  # let it tick a few times
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_watchdog_rejects_invalid_threshold() -> None:
    client = _ControlClient()
    with pytest.raises(ValueError):
        McpHealthWatchdog(
            client,
            session_label="x",
            already_disabled=set(),
            threshold=0,
        )
    with pytest.raises(ValueError):
        McpHealthWatchdog(
            client,
            session_label="x",
            already_disabled=set(),
            threshold=1,
            poll_interval_s=0,
        )


# ──────────────────────────────────────────────────────────────────────────
# Agent integration: pre-turn warning gets spliced into text_chunks
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _AgentClientWithMcp:
    """Fake ClaudeSDKClient with MCP control + a basic single-turn response."""

    options: ClaudeAgentOptions
    mcp_statuses: list[Any] = field(default_factory=list)
    toggled: list[tuple[str, bool]] = field(default_factory=list)
    response_text: str = "ok"
    _poll_idx: int = 0
    _prompt: str = ""
    _session_id: str = ""

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self._prompt = prompt
        self._session_id = session_id

    async def receive_messages(self):
        yield AssistantMessage(
            content=[_TextBlock(self.response_text)],
            model="fake",
            stop_reason="end_turn",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self._session_id,
            stop_reason="end_turn",
            total_cost_usd=0.001,
        )

    async def get_mcp_status(self) -> Any:
        if not self.mcp_statuses:
            return {"mcpServers": []}
        idx = min(self._poll_idx, len(self.mcp_statuses) - 1)
        self._poll_idx += 1
        return self.mcp_statuses[idx]

    async def toggle_mcp_server(self, server_name: str, enabled: bool) -> None:
        self.toggled.append((server_name, enabled))


@pytest.mark.asyncio
async def test_agent_pre_turn_disables_failed_mcp_and_warns_user() -> None:
    """Layer 1: a pre-existing failed MCP gets disabled + a warning shown."""
    statuses = [
        {
            "mcpServers": [
                {"name": "camoufox", "status": "failed"},
                {"name": "memory", "status": "connected"},
            ]
        }
    ]

    def factory(options: ClaudeAgentOptions) -> _AgentClientWithMcp:
        return _AgentClientWithMcp(
            options=options, mcp_statuses=statuses, response_text="hello"
        )

    agent = Agent(_cfg(), client_factory=factory)
    # Make the watchdog poll so slowly it never fires during this short turn,
    # so we exercise Layer 1 in isolation.
    agent._mcp_watchdog_poll_interval_s = 60.0
    session = SessionState(channel_id="C07TEST123")

    turn = await agent.run_turn(session, "hi")

    assert turn.text.startswith("hello")
    assert "`camoufox`" in turn.text
    assert "unhealthy" in turn.text
    assert "camoufox" in session.disabled_mcp_servers
    assert "memory" not in session.disabled_mcp_servers


@pytest.mark.asyncio
async def test_agent_no_warning_when_all_mcps_healthy() -> None:
    statuses = [
        {"mcpServers": [{"name": "memory", "status": "connected"}]}
    ]

    def factory(options: ClaudeAgentOptions) -> _AgentClientWithMcp:
        return _AgentClientWithMcp(
            options=options, mcp_statuses=statuses, response_text="hello"
        )

    agent = Agent(_cfg(), client_factory=factory)
    agent._mcp_watchdog_poll_interval_s = 60.0
    session = SessionState(channel_id="C07TEST123")

    turn = await agent.run_turn(session, "hi")

    assert turn.text == "hello"
    assert "⚠️" not in turn.text
    assert session.disabled_mcp_servers == set()


@pytest.mark.asyncio
async def test_agent_skips_mcp_layers_for_clients_without_control_methods() -> None:
    """Fakes that don't expose get_mcp_status must not break anything."""

    @dataclass
    class _MinimalClient:
        options: ClaudeAgentOptions
        _prompt: str = ""
        _session_id: str = ""

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            self._prompt = prompt
            self._session_id = session_id

        async def receive_response(self):
            yield AssistantMessage(
                content=[_TextBlock("hello")],
                model="fake",
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=self._session_id,
                total_cost_usd=0.001,
            )

    def factory(options: ClaudeAgentOptions) -> _MinimalClient:
        return _MinimalClient(options=options)

    agent = Agent(_cfg(), client_factory=factory)
    session = SessionState(channel_id="C07TEST123")

    turn = await agent.run_turn(session, "hi")

    assert turn.text == "hello"
    assert "⚠️" not in turn.text
    assert session.disabled_mcp_servers == set()


@pytest.mark.asyncio
async def test_runtime_reconnect_helper_skips_disabled_servers() -> None:
    """GRO-555: status-snapshot reconnect loop must respect ban list."""
    from engram.runtime import _reconnect_failed_mcp_servers

    @dataclass
    class _SnapshotClient:
        reconnect_calls: list[str] = field(default_factory=list)

        async def reconnect_mcp_server(self, name: str) -> None:
            self.reconnect_calls.append(name)

    client = _SnapshotClient()
    session = SessionState(channel_id="C07TEST123")
    session.disabled_mcp_servers.add("camoufox")

    await _reconnect_failed_mcp_servers(
        client,
        {
            "mcpServers": [
                {"name": "camoufox", "status": "failed"},
                {"name": "playwright", "status": "failed"},
                {"name": "memory", "status": "connected"},
            ]
        },
        session,
    )

    # camoufox is in the ban list → skipped.
    # playwright is failed and NOT banned → reconnect attempted.
    # memory is connected → not a candidate.
    assert client.reconnect_calls == ["playwright"]


@pytest.mark.asyncio
async def test_drop_client_clears_disabled_mcp_set() -> None:
    """GRO-555: a fresh CLI subprocess gets a fresh ban list."""

    def factory(options: ClaudeAgentOptions) -> _AgentClientWithMcp:
        return _AgentClientWithMcp(
            options=options,
            mcp_statuses=[{"mcpServers": []}],
            response_text="x",
        )

    agent = Agent(_cfg(), client_factory=factory)
    session = SessionState(channel_id="C07TEST123")
    session.disabled_mcp_servers.update({"camoufox", "playwright"})

    # Force a client to exist so _drop_client has work to do.
    await agent._ensure_client(session)
    assert session.agent_client is not None

    await agent._drop_client(session)
    assert session.disabled_mcp_servers == set()
    assert session.agent_client is None


@pytest.mark.asyncio
async def test_default_threshold_is_three() -> None:
    """Lock the default in a test so it doesn't drift silently."""
    assert DEFAULT_RECONNECT_FAIL_THRESHOLD == 3
