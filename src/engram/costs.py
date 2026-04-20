"""Per-turn cost tracking and persistence.

Promoted from M3 into M1 based on the first live turn data point
(cost/turn ~$0.055, 3.6x the M0 baseline estimate). Having cost data
from day one is cheap; retrofitting it later would lose history.

Design choices (kept minimal):
  * JSONL append-log at ~/.engram/logs/costs.jsonl. Each line = one turn.
  * In-memory CostLedger aggregates per-channel + rolling daily/monthly.
  * File is append-only; readers tolerate missing/corrupt lines.
  * No rotation in M1. JSONL at typical volumes stays well under 10MB/mo.

Not yet in scope (deferred to M3):
  * Hard budget caps / pause-on-over.
  * Slack-native budget warnings.
  * Per-model pricing tables (we just record what the SDK reports).
"""
from __future__ import annotations

import datetime
import json
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class TurnCost:
    timestamp: str  # ISO 8601 UTC
    session_label: str
    channel_id: str
    is_dm: bool
    cost_usd: float
    duration_ms: int | None
    num_turns: int | None
    user_text_len: int
    chunks_posted: int
    is_error: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.timestamp,
            "session": self.session_label,
            "channel_id": self.channel_id,
            "is_dm": self.is_dm,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "num_turns": self.num_turns,
            "user_text_len": self.user_text_len,
            "chunks_posted": self.chunks_posted,
            "is_error": self.is_error,
        }


@dataclass
class ChannelTotal:
    turn_count: int = 0
    total_cost_usd: float = 0.0


class CostLedger:
    """Thread-safe append-only turn-cost ledger.

    Exposes aggregates for `engram status`. For M3 we'll layer budget
    enforcement on top of the same storage.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, cost: TurnCost) -> None:
        """Append a single turn's cost to the JSONL log."""
        if cost.cost_usd is None:
            return  # nothing useful to record
        line = json.dumps(cost.to_dict(), separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        log.info(
            "costs.recorded session=%s cost=$%.4f channel=%s",
            cost.session_label,
            cost.cost_usd,
            cost.channel_id,
        )

    def summarize(self, now: datetime.datetime | None = None) -> CostSummary:
        """Read the full JSONL log and return aggregates."""
        now = now or datetime.datetime.now(datetime.UTC)
        today = now.date()
        month_start = today.replace(day=1)

        per_channel: dict[str, ChannelTotal] = defaultdict(ChannelTotal)
        today_cost = 0.0
        today_turns = 0
        month_cost = 0.0
        month_turns = 0
        total_cost = 0.0
        total_turns = 0

        if not self.path.exists():
            return CostSummary(
                total_turns=0,
                total_cost_usd=0.0,
                today_turns=0,
                today_cost_usd=0.0,
                month_turns=0,
                month_cost_usd=0.0,
                per_channel={},
            )

        with self.path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    rec = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue  # skip corrupt line
                try:
                    ts = datetime.datetime.fromisoformat(rec["ts"])
                except (KeyError, ValueError):
                    continue
                cost = float(rec.get("cost_usd") or 0.0)
                total_cost += cost
                total_turns += 1
                channel = rec.get("channel_id", "?")
                c = per_channel[channel]
                c.turn_count += 1
                c.total_cost_usd += cost
                ts_date = ts.date()
                if ts_date == today:
                    today_cost += cost
                    today_turns += 1
                if ts_date >= month_start:
                    month_cost += cost
                    month_turns += 1

        return CostSummary(
            total_turns=total_turns,
            total_cost_usd=total_cost,
            today_turns=today_turns,
            today_cost_usd=today_cost,
            month_turns=month_turns,
            month_cost_usd=month_cost,
            per_channel=dict(per_channel),
        )


@dataclass
class CostSummary:
    total_turns: int
    total_cost_usd: float
    today_turns: int
    today_cost_usd: float
    month_turns: int
    month_cost_usd: float
    per_channel: dict[str, ChannelTotal] = field(default_factory=dict)
