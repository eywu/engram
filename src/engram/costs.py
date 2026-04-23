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
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import RateLimitStatus

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
    session_id: str | None = None
    source: str = "turn"
    subagent_id: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
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
            "source": self.source,
        }
        if self.session_id:
            data["session_id"] = self.session_id
        if self.subagent_id:
            data["subagent_id"] = self.subagent_id
        if self.metadata:
            data["metadata"] = self.metadata
        return data


@dataclass
class RateLimitRecord:
    timestamp: str
    channel_id: str
    session_id: str
    status: RateLimitStatus
    reset_at: int | None = None
    rate_limit_type: str | None = None
    utilization: float | None = None
    raw: dict[str, Any] | None = None


@dataclass
class CostQueryResult:
    turns: int
    total_cost_usd: float
    per_channel: dict[str, ChannelTotal] = field(default_factory=dict)


@dataclass
class ChannelTotal:
    turn_count: int = 0
    total_cost_usd: float = 0.0


class CostLedger:
    """Thread-safe append-only turn-cost ledger.

    Exposes aggregates for `engram status`. For M3 we'll layer budget
    enforcement on top of the same storage.
    """

    def __init__(self, path: Path, *, db_path: Path | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = CostDatabase(db_path or _default_cost_db_path(path))

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


class CostDatabase:
    """SQLite-backed cost and rate-limit event store."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    ts TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    user_id TEXT,
                    model TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rate_limit_events (
                    ts TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reset_at TEXT
                );
                """
            )
            _ensure_columns(
                conn,
                "turns",
                {
                    "session_label": "TEXT",
                    "session_id": "TEXT",
                    "is_dm": "INTEGER NOT NULL DEFAULT 0",
                    "duration_ms": "INTEGER",
                    "num_turns": "INTEGER",
                    "user_text_len": "INTEGER",
                    "chunks_posted": "INTEGER",
                    "is_error": "INTEGER NOT NULL DEFAULT 0",
                    "source": "TEXT NOT NULL DEFAULT 'turn'",
                    "subagent_id": "TEXT",
                    "metadata_json": "TEXT",
                },
            )
            _ensure_columns(
                conn,
                "rate_limit_events",
                {
                    "session_id": "TEXT NOT NULL DEFAULT ''",
                    "rate_limit_type": "TEXT",
                    "utilization": "REAL",
                    "raw_json": "TEXT",
                },
            )
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
                CREATE INDEX IF NOT EXISTS idx_turns_ts_channel ON turns(ts, channel_id);
                CREATE INDEX IF NOT EXISTS idx_rate_limit_channel_ts
                ON rate_limit_events(channel_id, ts);
                """
            )

    def record_turn(self, cost: TurnCost) -> None:
        if cost.cost_usd is None:
            return
        metadata = cost.metadata or {}
        with self._lock, self._connect() as conn:
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
                    cost_usd,
                    session_label,
                    session_id,
                    is_dm,
                    duration_ms,
                    num_turns,
                    user_text_len,
                    chunks_posted,
                    is_error,
                    source,
                    subagent_id,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cost.timestamp,
                    cost.channel_id,
                    str(metadata.get("user_id") or ""),
                    metadata.get("model"),
                    _int(metadata.get("input_tokens")),
                    _int(metadata.get("output_tokens")),
                    _int(metadata.get("cache_creation_input_tokens")),
                    _int(metadata.get("cache_read_input_tokens")),
                    f"{float(cost.cost_usd):.6f}",
                    cost.session_label,
                    cost.session_id,
                    1 if cost.is_dm else 0,
                    cost.duration_ms,
                    cost.num_turns,
                    cost.user_text_len,
                    cost.chunks_posted,
                    1 if cost.is_error else 0,
                    cost.source,
                    cost.subagent_id,
                    json.dumps(cost.metadata or {}, separators=(",", ":")),
                ),
            )

    def record_rate_limit(self, event: RateLimitRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rate_limit_events (
                    ts, channel_id, session_id, status, reset_at,
                    rate_limit_type, utilization, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.timestamp,
                    event.channel_id,
                    event.session_id,
                    event.status,
                    str(event.reset_at) if event.reset_at is not None else None,
                    event.rate_limit_type,
                    event.utilization,
                    json.dumps(event.raw or {}, separators=(",", ":")),
                ),
            )

    def latest_rate_limit(self, channel_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status, reset_at, rate_limit_type, utilization, ts
                FROM rate_limit_events
                WHERE channel_id = ?
                ORDER BY ts DESC, rowid DESC
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
        if row is None:
            return {"status": "allowed", "reset_at": None}
        reset_at = row["reset_at"]
        reset_ts = _reset_ts(reset_at)
        if row["status"] in {"allowed_warning", "rejected"} and (
            reset_ts is not None and time.time() >= reset_ts
        ):
            return {"status": "allowed", "reset_at": None}
        return {
            "status": row["status"],
            "reset_at": reset_at,
            "rate_limit_type": row["rate_limit_type"],
            "utilization": row["utilization"],
            "ts": row["ts"],
        }

    def query(
        self,
        *,
        since: datetime.datetime | None = None,
        until: datetime.datetime | None = None,
        by_channel: bool = False,
    ) -> CostQueryResult:
        clauses: list[str] = []
        params: list[str] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("ts < ?")
            params.append(until.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) AS turns,
                       COALESCE(SUM(CAST(cost_usd AS REAL)), 0) AS cost
                FROM turns
                {where}
                """,
                params,
            ).fetchone()
            per_channel: dict[str, ChannelTotal] = {}
            if by_channel:
                rows = conn.execute(
                    f"""
                    SELECT channel_id,
                           COUNT(*) AS turns,
                           COALESCE(SUM(CAST(cost_usd AS REAL)), 0) AS cost
                    FROM turns
                    {where}
                    GROUP BY channel_id
                    ORDER BY cost DESC, channel_id ASC
                    """,
                    params,
                ).fetchall()
                per_channel = {
                    row["channel_id"]: ChannelTotal(
                        turn_count=int(row["turns"]),
                        total_cost_usd=float(row["cost"]),
                    )
                    for row in rows
                }

        return CostQueryResult(
            turns=int(total_row["turns"] if total_row else 0),
            total_cost_usd=float(total_row["cost"] if total_row else 0.0),
            per_channel=per_channel,
        )

    def record_subagent_completion(
        self,
        *,
        channel_id: str,
        session_id: str,
        subagent_id: str,
        agent_type: str | None,
        transcript_path: str | None,
        cost_usd: float | None,
    ) -> None:
        self.record_turn(
            TurnCost(
                timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
                session_label=f"subagent:{channel_id}",
                session_id=session_id,
                channel_id=channel_id,
                is_dm=False,
                cost_usd=cost_usd or 0.0,
                duration_ms=None,
                num_turns=None,
                user_text_len=0,
                chunks_posted=0,
                is_error=False,
                source="subagent",
                subagent_id=subagent_id,
                metadata={
                    "agent_type": agent_type,
                    "transcript_path": transcript_path,
                },
            )
        )


def _default_cost_db_path(path: Path) -> Path:
    if path.parent.name == "logs":
        return path.parent.parent / "cost.db"
    return path.parent / "cost.db"


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _reset_ts(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
