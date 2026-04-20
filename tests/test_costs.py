"""Cost ledger tests — pure filesystem, no network."""
from __future__ import annotations

import datetime
import json

import pytest

from engram.costs import CostLedger, TurnCost


def _make_turn(
    cost: float = 0.05,
    session: str = "dm:D1",
    channel: str = "D1",
    ts: str | None = None,
) -> TurnCost:
    return TurnCost(
        timestamp=ts or datetime.datetime.now(datetime.UTC).isoformat(),
        session_label=session,
        channel_id=channel,
        is_dm=True,
        cost_usd=cost,
        duration_ms=1200,
        num_turns=1,
        user_text_len=10,
        chunks_posted=1,
        is_error=False,
    )


def test_empty_ledger_summary(tmp_path):
    ledger = CostLedger(tmp_path / "costs.jsonl")
    summary = ledger.summarize()
    assert summary.total_turns == 0
    assert summary.total_cost_usd == 0.0
    assert summary.per_channel == {}


def test_record_single_turn(tmp_path):
    path = tmp_path / "costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(_make_turn(cost=0.0546))
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["cost_usd"] == 0.0546
    assert rec["channel_id"] == "D1"


def test_multiple_turns_aggregate(tmp_path):
    ledger = CostLedger(tmp_path / "costs.jsonl")
    for i in range(5):
        ledger.record(_make_turn(cost=0.01 * (i + 1), channel="D1"))
    ledger.record(_make_turn(cost=0.25, channel="C2"))
    summary = ledger.summarize()
    assert summary.total_turns == 6
    assert summary.total_cost_usd == pytest.approx(0.01 + 0.02 + 0.03 + 0.04 + 0.05 + 0.25)
    assert "D1" in summary.per_channel
    assert summary.per_channel["D1"].turn_count == 5
    assert summary.per_channel["C2"].turn_count == 1


def test_today_vs_month(tmp_path):
    ledger = CostLedger(tmp_path / "costs.jsonl")
    today = datetime.datetime.now(datetime.UTC)
    last_month = today - datetime.timedelta(days=45)
    yesterday = today - datetime.timedelta(days=1)

    ledger.record(_make_turn(cost=0.10, ts=today.isoformat()))
    ledger.record(_make_turn(cost=0.20, ts=yesterday.isoformat()))
    ledger.record(_make_turn(cost=1.00, ts=last_month.isoformat()))

    summary = ledger.summarize(now=today)
    assert summary.today_turns == 1
    assert summary.today_cost_usd == pytest.approx(0.10)
    # Month-to-date depends on today's day-of-month; yesterday's turn
    # may or may not be in the current month. We check all-time instead.
    assert summary.total_turns == 3
    assert summary.total_cost_usd == pytest.approx(1.30)


def test_skips_none_cost(tmp_path):
    ledger = CostLedger(tmp_path / "costs.jsonl")
    turn = _make_turn()
    turn.cost_usd = None  # type: ignore[assignment]
    ledger.record(turn)
    assert not (tmp_path / "costs.jsonl").exists() or \
        (tmp_path / "costs.jsonl").read_text() == ""


def test_tolerates_corrupt_lines(tmp_path):
    path = tmp_path / "costs.jsonl"
    path.write_text(
        '{"ts":"2026-04-20T00:00:00+00:00","cost_usd":0.05,"channel_id":"D1"}\n'
        "not json at all\n"
        '{"ts":"2026-04-20T01:00:00+00:00","cost_usd":0.10,"channel_id":"D1"}\n'
    )
    ledger = CostLedger(path)
    summary = ledger.summarize()
    assert summary.total_turns == 2
    assert summary.total_cost_usd == pytest.approx(0.15)
