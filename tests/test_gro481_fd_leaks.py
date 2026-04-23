from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage

import engram.runtime as runtime
from engram.agent import Agent
from engram.budget import Budget, BudgetConfig
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.costs import CostDatabase, CostLedger, RateLimitRecord, TurnCost
from engram.ingress import register_listeners
from engram.router import Router


class _DecoratorApp:
    def __init__(self) -> None:
        self.actions = []
        self.commands = []
        self.events = []

    def action(self, pattern):
        def decorator(func):
            self.actions.append((pattern, func))
            return func

        return decorator

    def command(self, command_name):
        def decorator(func):
            self.commands.append((command_name, func))
            return func

        return decorator

    def event(self, event_name):
        def decorator(func):
            self.events.append((event_name, func))
            return func

        return decorator


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeSDKClient:
    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.last_session_id: str | None = None

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str, *, session_id: str | None = None) -> None:
        del prompt
        self.last_session_id = session_id

    async def receive_response(self):
        yield AssistantMessage(
            content=[_TextBlock("ok")],
            model="claude-test-model",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self.last_session_id or "session-test",
            total_cost_usd=0.01,
            usage={
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            model_usage={"claude-test-model": {"input_tokens": 10}},
        )

    async def tag_session(self, *, session_id: str, tags: dict[str, str]) -> None:
        del session_id, tags


class _FakeSlackClient:
    async def chat_postMessage(self, **kwargs):
        del kwargs
        return {"ok": True, "ts": "1713800000.000200"}


def _config() -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-test"),
        max_turns_per_message=3,
    )


def _budget(tmp_path: Path) -> Budget:
    return Budget(BudgetConfig(), db_path=tmp_path / "cost.db")


def _result(cost: float = 0.01) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-test",
        total_cost_usd=cost,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 11,
        },
        model_usage={"claude-test-model": {"input_tokens": 100}},
    )


def _fd_in_use() -> int:
    snapshot = runtime.fd_usage_snapshot()
    if snapshot is None or snapshot.get("in_use") is None:
        pytest.skip("fd counting is unavailable on this platform")
    return int(snapshot["in_use"])


def _assert_fd_growth_within(baseline: int, *, allowance: int = 2) -> None:
    growth = _fd_in_use() - baseline
    assert growth <= allowance


def test_budget_record_does_not_leak_file_descriptors(tmp_path: Path) -> None:
    budget = _budget(tmp_path)
    now = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.UTC)
    budget.record("C07TEST123", "U07TEST123", _result(), now=now)

    baseline = _fd_in_use()
    for i in range(500):
        budget.record(
            "C07TEST123",
            "U07TEST123",
            _result(),
            now=now + dt.timedelta(seconds=i),
        )

    _assert_fd_growth_within(baseline)


def test_cost_database_record_turn_does_not_leak_file_descriptors(
    tmp_path: Path,
) -> None:
    db = CostDatabase(tmp_path / "cost.db")
    now = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.UTC)
    db.record_turn(
        TurnCost(
            timestamp=now.isoformat(),
            session_label="ch:C07TEST123",
            session_id="session-test",
            channel_id="C07TEST123",
            is_dm=False,
            cost_usd=0.01,
            duration_ms=1,
            num_turns=1,
            user_text_len=1,
            chunks_posted=1,
            is_error=False,
        )
    )

    baseline = _fd_in_use()
    for i in range(500):
        db.record_turn(
            TurnCost(
                timestamp=(now + dt.timedelta(seconds=i)).isoformat(),
                session_label="ch:C07TEST123",
                session_id="session-test",
                channel_id="C07TEST123",
                is_dm=False,
                cost_usd=0.01,
                duration_ms=1,
                num_turns=1,
                user_text_len=1,
                chunks_posted=1,
                is_error=False,
            )
        )

    _assert_fd_growth_within(baseline)


def test_cost_database_latest_rate_limit_does_not_leak_file_descriptors(
    tmp_path: Path,
) -> None:
    db = CostDatabase(tmp_path / "cost.db")
    future_reset = int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp())
    db.record_rate_limit(
        RateLimitRecord(
            timestamp=dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.UTC).isoformat(),
            channel_id="C07TEST123",
            session_id="session-test",
            status="allowed_warning",
            reset_at=future_reset,
        )
    )

    baseline = _fd_in_use()
    for _ in range(500):
        latest = db.latest_rate_limit("C07TEST123")
        assert latest["status"] == "allowed_warning"

    _assert_fd_growth_within(baseline)


@pytest.mark.asyncio
async def test_ingress_message_loop_does_not_leak_cost_db_descriptors(
    tmp_path: Path,
) -> None:
    app = _DecoratorApp()
    router = Router()
    budget = _budget(tmp_path)
    cost_ledger = CostLedger(tmp_path / "costs.jsonl", db_path=budget.db_path)
    agent = Agent(
        _config(),
        client_factory=_FakeSDKClient,
        budget=budget,
        cost_db=cost_ledger.db,
    )
    register_listeners(app, _config(), router, agent, cost_ledger=cost_ledger)
    message_handler = next(
        handler for event_name, handler in app.events if event_name == "message"
    )
    slack = _FakeSlackClient()

    async def say(*, text: str, thread_ts: str | None = None):
        del text, thread_ts
        return {"ts": "1713800000.000300"}

    await message_handler(
        event={
            "channel": "D07TEST123",
            "channel_type": "im",
            "user": "U07TEST123",
            "text": "warmup",
            "ts": "1713800000.000100",
        },
        say=say,
        client=slack,
    )

    baseline = _fd_in_use()
    for i in range(100):
        await message_handler(
            event={
                "channel": "D07TEST123",
                "channel_type": "im",
                "user": "U07TEST123",
                "text": f"hello {i}",
                "ts": f"1713800000.{i:06d}",
            },
            say=say,
            client=slack,
        )

    _assert_fd_growth_within(baseline)


@pytest.mark.asyncio
async def test_runtime_snapshot_warns_when_fd_usage_exceeds_half_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(runtime, "_fd_warning_active", False)
    monkeypatch.setattr(
        runtime,
        "fd_usage_snapshot",
        lambda: {"in_use": 205, "soft_limit": 400, "hard_limit": 800},
    )
    monkeypatch.setattr(
        runtime,
        "memory_tool_metrics",
        lambda: {"embedding_queue": {"enabled": True}},
    )

    with caplog.at_level("WARNING", logger="engram.runtime"):
        snapshot = await runtime.write_runtime_snapshot(
            state_dir=tmp_path,
            router=Router(),
            cost_db=None,
        )
        await runtime.write_runtime_snapshot(
            state_dir=tmp_path,
            router=Router(),
            cost_db=None,
        )

    assert snapshot["bridge"]["fds"] == {
        "in_use": 205,
        "soft_limit": 400,
        "hard_limit": 800,
    }
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert status["bridge"]["fds"]["in_use"] == 205
    assert health["fds"]["soft_limit"] == 400

    warnings = [
        record
        for record in caplog.records
        if "runtime.fd_usage_high" in record.getMessage()
    ]
    assert len(warnings) == 1
