"""GRO-394 budget tracking tests."""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage

from engram.agent import Agent
from engram.budget import BUDGET_PAUSE_MESSAGE, Budget, BudgetConfig
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.router import SessionState

LA = ZoneInfo("America/Los_Angeles")


def _budget(
    tmp_path: Path,
    *,
    cap: str = "500.00",
    hard_cap_enabled: bool = False,
) -> Budget:
    return Budget(
        BudgetConfig(
            monthly_cap_usd=Decimal(cap),
            hard_cap_enabled=hard_cap_enabled,
            warn_thresholds=(Decimal("0.60"), Decimal("0.80"), Decimal("1.00")),
            timezone="America/Los_Angeles",
        ),
        db_path=tmp_path / "cost.db",
    )


def _cfg(budget_config: BudgetConfig | None = None) -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-test"),
        max_turns_per_message=3,
        budget=budget_config or BudgetConfig(),
    )


def _result(total_cost_usd: float = 0.01) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-test",
        total_cost_usd=total_cost_usd,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 11,
        },
        model_usage={"claude-test-model": {"input_tokens": 100}},
    )


def _insert_turn(
    budget: Budget,
    cost: str,
    *,
    ts: dt.datetime,
    channel_id: str = "C07TEST123",
    user_id: str = "U07TEST123",
) -> None:
    with sqlite3.connect(budget.db_path) as conn:
        conn.execute(
            """
            INSERT INTO turns (
                ts,
                channel_id,
                user_id,
                model,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                cost_usd
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts.isoformat(),
                channel_id,
                user_id,
                "claude-test-model",
                1,
                1,
                0,
                0,
                str(Decimal(cost).quantize(Decimal("0.000001"))),
            ),
        )
        conn.commit()


def test_record_inserts_row_with_decimal_precision(tmp_path):
    budget = _budget(tmp_path)
    now = dt.datetime(2026, 4, 21, 9, 30, tzinfo=LA)

    budget.record("C07TEST123", "U07TEST123", _result(0.054321), now=now)

    with sqlite3.connect(budget.db_path) as conn:
        row = conn.execute(
            """
            SELECT channel_id,
                   user_id,
                   model,
                   input_tokens,
                   output_tokens,
                   cache_creation_input_tokens,
                   cache_read_input_tokens,
                   cost_usd
            FROM turns
            """
        ).fetchone()

    assert row == (
        "C07TEST123",
        "U07TEST123",
        "claude-test-model",
        100,
        20,
        7,
        11,
        "0.054321",
    )
    assert Decimal(row[-1]) == Decimal("0.054321")


def test_month_to_date_matches_sum(tmp_path):
    budget = _budget(tmp_path)
    now = dt.datetime(2026, 4, 21, 12, 0, tzinfo=LA)

    for cost in ("1.00", "2.00", "3.00", "4.00", "5.00"):
        _insert_turn(
            budget,
            cost,
            ts=dt.datetime(2026, 4, 10, 10, 0, tzinfo=LA),
        )
    for cost in ("10.00", "20.00", "30.00"):
        _insert_turn(
            budget,
            cost,
            ts=dt.datetime(2026, 3, 10, 10, 0, tzinfo=LA),
        )

    assert budget.month_to_date_usd(now=now) == Decimal("15.000000")


def test_month_boundary_timezone_correct(tmp_path):
    budget = _budget(tmp_path)
    now = dt.datetime(2026, 3, 1, 12, 0, tzinfo=LA)

    _insert_turn(
        budget,
        "9.99",
        ts=dt.datetime(2026, 2, 28, 23, 30, tzinfo=LA),
    )

    assert budget.month_to_date_usd(now=now) == Decimal("0.000000")


def test_warning_fires_at_60_80_100_once_each(tmp_path):
    budget = _budget(tmp_path)
    now = dt.datetime(2026, 4, 21, 12, 0, tzinfo=LA)

    _insert_turn(budget, "299.00", ts=now)
    assert budget.check("C07TEST123", now=now).thresholds_fired == ()

    _insert_turn(budget, "2.00", ts=now)
    assert budget.check("C07TEST123", now=now).thresholds_fired == (
        Decimal("0.60"),
    )

    _insert_turn(budget, "100.00", ts=now)
    assert budget.check("C07TEST123", now=now).thresholds_fired == (
        Decimal("0.80"),
    )

    _insert_turn(budget, "100.00", ts=now)
    assert budget.check("C07TEST123", now=now).thresholds_fired == (
        Decimal("1.00"),
    )
    assert budget.check("C07TEST123", now=now).thresholds_fired == ()


def test_warnings_dedup_within_month(tmp_path):
    budget = _budget(tmp_path)
    now = dt.datetime(2026, 4, 21, 12, 0, tzinfo=LA)

    _insert_turn(budget, "401.00", ts=now)
    assert budget.check("C07TEST123", now=now).thresholds_fired == (
        Decimal("0.60"),
        Decimal("0.80"),
    )

    _insert_turn(budget, "25.00", ts=now)
    assert budget.check("C07TEST123", now=now).thresholds_fired == ()


def test_hard_cap_disabled_never_pauses(tmp_path):
    budget = _budget(tmp_path, hard_cap_enabled=False)
    now = dt.datetime(2026, 4, 21, 12, 0, tzinfo=LA)
    _insert_turn(budget, "600.00", ts=now)

    result = budget.check("C07TEST123", now=now)

    assert result.allow
    assert result.thresholds_fired == (
        Decimal("0.60"),
        Decimal("0.80"),
        Decimal("1.00"),
    )


@pytest.mark.asyncio
async def test_hard_cap_enabled_pauses_at_100(tmp_path):
    budget = _budget(tmp_path, hard_cap_enabled=True)
    now = dt.datetime.now(LA)
    _insert_turn(budget, "501.00", ts=now)
    factory_called = False

    def factory(options: ClaudeAgentOptions):
        nonlocal factory_called
        factory_called = True
        raise AssertionError("SDK should not be invoked over hard cap")

    agent = Agent(_cfg(budget.config), client_factory=factory, budget=budget)
    session = SessionState(channel_id="C07TEST123")

    turn = await agent.run_turn(session, "hello", user_id="U07TEST123")

    assert turn.text == BUDGET_PAUSE_MESSAGE
    assert turn.cost_usd is None
    assert turn.budget_warnings == (
        Decimal("0.60"),
        Decimal("0.80"),
        Decimal("1.00"),
    )
    assert not factory_called
    assert session.agent_client is None


@dataclass
class _TextBlock:
    text: str


class _BudgetCaptureClient:
    def __init__(self, options: ClaudeAgentOptions):
        self.options = options
        self.connected = False
        self._session_id = ""

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        pass

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self._session_id = session_id

    async def receive_response(self):
        yield AssistantMessage(content=[_TextBlock("ok")], model="fake")
        yield _result(0.01)


@pytest.mark.asyncio
async def test_max_budget_usd_set_on_options(tmp_path):
    budget = _budget(tmp_path)
    now = dt.datetime.now(LA)
    _insert_turn(budget, "123.45", ts=now)
    options_seen: list[ClaudeAgentOptions] = []

    def factory(options: ClaudeAgentOptions) -> _BudgetCaptureClient:
        options_seen.append(options)
        return _BudgetCaptureClient(options)

    agent = Agent(_cfg(budget.config), client_factory=factory, budget=budget)

    turn = await agent.run_turn(SessionState(channel_id="C07TEST123"), "hello")

    assert turn.text == "ok"
    assert len(options_seen) == 1
    assert options_seen[0].max_budget_usd == 376.55
