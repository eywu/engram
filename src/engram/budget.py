"""Monthly budget tracking backed by ``~/.engram/cost.db``.

The database is separate from the legacy JSONL cost ledger because M3 needs
queryable month-to-date totals, warning deduplication, and future rate-limit
event storage.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from engram import paths

log = logging.getLogger(__name__)

USD_QUANT = Decimal("0.000001")
DEFAULT_MONTHLY_CAP_USD = Decimal("500.00")
DEFAULT_WARN_THRESHOLDS = (Decimal("0.60"), Decimal("0.80"), Decimal("1.00"))
DEFAULT_TIMEZONE = "America/Los_Angeles"
BUDGET_PAUSE_MESSAGE = (
    "Engram is paused because the configured monthly budget has been reached. "
    "Please check the budget settings before sending more requests."
)


@dataclass(frozen=True)
class BudgetConfig:
    monthly_cap_usd: Decimal = DEFAULT_MONTHLY_CAP_USD
    hard_cap_enabled: bool = False
    warn_thresholds: tuple[Decimal, ...] = field(
        default_factory=lambda: DEFAULT_WARN_THRESHOLDS
    )
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        object.__setattr__(self, "monthly_cap_usd", _decimal(self.monthly_cap_usd))
        object.__setattr__(self, "hard_cap_enabled", _bool(self.hard_cap_enabled))
        object.__setattr__(
            self,
            "warn_thresholds",
            tuple(_decimal(v) for v in (self.warn_thresholds or DEFAULT_WARN_THRESHOLDS)),
        )
        object.__setattr__(self, "timezone", self.timezone or DEFAULT_TIMEZONE)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> BudgetConfig:
        raw = raw or {}
        thresholds = raw.get("warn_thresholds") or DEFAULT_WARN_THRESHOLDS
        return cls(
            monthly_cap_usd=_decimal(raw.get("monthly_cap_usd", DEFAULT_MONTHLY_CAP_USD)),
            hard_cap_enabled=_bool(raw.get("hard_cap_enabled", False)),
            warn_thresholds=tuple(_decimal(v) for v in thresholds),
            timezone=str(raw.get("timezone") or DEFAULT_TIMEZONE),
        )


@dataclass(frozen=True)
class CheckResult:
    action: Literal["allow", "pause"]
    month_to_date_usd: Decimal
    monthly_cap_usd: Decimal
    thresholds_fired: tuple[Decimal, ...] = ()

    @property
    def allow(self) -> bool:
        return self.action == "allow"

    @property
    def pause(self) -> bool:
        return self.action == "pause"


class Budget:
    """Records per-turn spend and computes monthly budget state."""

    def __init__(
        self,
        config: BudgetConfig | None = None,
        *,
        db_path: Path | None = None,
    ):
        self.config = config or BudgetConfig()
        self.db_path = (db_path or (paths.engram_home() / "cost.db")).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migrate()

    def record(
        self,
        channel_id: str,
        user_id: str | None,
        result_message: Any,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        """Insert one ``turns`` row for a Claude SDK ``ResultMessage``."""
        usage, model = _extract_usage_and_model(result_message)
        cost = _decimal(getattr(result_message, "total_cost_usd", None) or 0)
        row = (
            _utc_iso(now),
            channel_id,
            user_id or "",
            model,
            usage["input_tokens"],
            usage["output_tokens"],
            usage["cache_creation_input_tokens"],
            usage["cache_read_input_tokens"],
            _format_usd(cost),
        )
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
                    cost_usd
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            conn.commit()
        log.info(
            "budget.recorded channel=%s user=%s model=%s cost=%s",
            channel_id,
            user_id or "",
            model or "",
            _format_usd(cost),
        )

    def month_to_date_usd(self, *, now: dt.datetime | None = None) -> Decimal:
        """Return current calendar-month spend in the configured timezone."""
        with self._lock, self._connect() as conn:
            return self._month_to_date_usd(conn, now=now)

    def remaining_usd(self, *, now: dt.datetime | None = None) -> Decimal:
        return self.config.monthly_cap_usd - self.month_to_date_usd(now=now)

    def check(
        self,
        channel_id: str,
        *,
        now: dt.datetime | None = None,
    ) -> CheckResult:
        """Evaluate warning thresholds and optional hard-cap enforcement.

        Warning thresholds are persisted before returning so callers can send
        exactly-once owner DMs. Hard caps default off; when disabled this method
        always returns ``allow`` even if spend is already over the cap.
        """
        with self._lock, self._connect() as conn:
            mtd = self._month_to_date_usd(conn, now=now)
            year_month = _year_month(now, self.config.timezone)
            fired: list[Decimal] = []

            for threshold in sorted(self.config.warn_thresholds):
                if mtd < self.config.monthly_cap_usd * threshold:
                    continue
                threshold_key = _format_threshold(threshold)
                before = conn.total_changes
                conn.execute(
                    """
                    INSERT OR IGNORE INTO warnings_fired (year_month, threshold_pct)
                    VALUES (?, ?)
                    """,
                    (year_month, threshold_key),
                )
                if conn.total_changes > before:
                    fired.append(threshold)

            conn.commit()

        action: Literal["allow", "pause"] = "allow"
        if (
            self.config.hard_cap_enabled
            and mtd >= self.config.monthly_cap_usd
        ):
            action = "pause"

        log.info(
            "budget.check channel=%s action=%s mtd=%s cap=%s fired=%s",
            channel_id,
            action,
            _format_usd(mtd),
            self.config.monthly_cap_usd,
            ",".join(_format_threshold(t) for t in fired),
        )
        return CheckResult(
            action=action,
            month_to_date_usd=mtd,
            monthly_cap_usd=self.config.monthly_cap_usd,
            thresholds_fired=tuple(fired),
        )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _migrate(self) -> None:
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

                CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);

                CREATE TABLE IF NOT EXISTS rate_limit_events (
                    ts TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reset_at TEXT
                );

                CREATE TABLE IF NOT EXISTS warnings_fired (
                    year_month TEXT NOT NULL,
                    threshold_pct TEXT NOT NULL,
                    PRIMARY KEY (year_month, threshold_pct)
                );
                """
            )
            conn.commit()

    def _month_to_date_usd(
        self,
        conn: sqlite3.Connection,
        *,
        now: dt.datetime | None,
    ) -> Decimal:
        start_utc, end_utc = _month_bounds_utc(now, self.config.timezone)
        total = Decimal("0")
        rows = conn.execute("SELECT ts, cost_usd FROM turns").fetchall()
        for ts_raw, cost_raw in rows:
            try:
                ts = _parse_ts(ts_raw)
                cost = _decimal(cost_raw)
            except (TypeError, ValueError, ArithmeticError):
                continue
            if start_utc <= ts < end_utc:
                total += cost
        return total.quantize(USD_QUANT)


def _extract_usage_and_model(result_message: Any) -> tuple[dict[str, int], str | None]:
    usage = getattr(result_message, "usage", None) or {}
    model_usage = getattr(result_message, "model_usage", None) or {}

    totals = {
        "input_tokens": _int_token(usage.get("input_tokens")),
        "output_tokens": _int_token(usage.get("output_tokens")),
        "cache_creation_input_tokens": _int_token(
            usage.get("cache_creation_input_tokens")
        ),
        "cache_read_input_tokens": _int_token(usage.get("cache_read_input_tokens")),
    }

    model: str | None = None
    if isinstance(model_usage, dict) and model_usage:
        model_keys = [str(k) for k in model_usage if k]
        if model_keys:
            model = ",".join(sorted(model_keys))

        if not any(totals.values()):
            for value in model_usage.values():
                if not isinstance(value, dict):
                    continue
                totals["input_tokens"] += _int_token(value.get("input_tokens"))
                totals["output_tokens"] += _int_token(value.get("output_tokens"))
                totals["cache_creation_input_tokens"] += _int_token(
                    value.get("cache_creation_input_tokens")
                )
                totals["cache_read_input_tokens"] += _int_token(
                    value.get("cache_read_input_tokens")
                )

    return totals, model


def _int_token(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _utc_iso(now: dt.datetime | None = None) -> str:
    value = now or dt.datetime.now(dt.UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _format_usd(value: Decimal) -> str:
    return str(value.quantize(USD_QUANT, rounding=ROUND_HALF_UP))


def _format_threshold(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _zoneinfo(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        log.warning("budget.invalid_timezone tz=%s fallback=%s", tz_name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def _local_now(now: dt.datetime | None, tz_name: str) -> dt.datetime:
    tz = _zoneinfo(tz_name)
    value = now or dt.datetime.now(tz)
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def _month_bounds_utc(
    now: dt.datetime | None,
    tz_name: str,
) -> tuple[dt.datetime, dt.datetime]:
    local = _local_now(now, tz_name)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    return start_local.astimezone(dt.UTC), end_local.astimezone(dt.UTC)


def _year_month(now: dt.datetime | None, tz_name: str) -> str:
    local = _local_now(now, tz_name)
    return f"{local.year:04d}-{local.month:02d}"


def _parse_ts(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def load_budget_config(config_path: Path | None = None) -> BudgetConfig:
    """Load just the ``budget`` section from ``~/.engram/config.yaml``."""
    config_path = config_path or (paths.engram_home() / "config.yaml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text()) or {}
    return BudgetConfig.from_mapping(raw.get("budget"))


_DEFAULT_BUDGET: Budget | None = None


def default_budget() -> Budget:
    global _DEFAULT_BUDGET
    if _DEFAULT_BUDGET is None:
        _DEFAULT_BUDGET = Budget(load_budget_config())
    return _DEFAULT_BUDGET


def record(channel_id: str, user_id: str | None, result_message: Any) -> None:
    default_budget().record(channel_id, user_id, result_message)


def month_to_date_usd() -> Decimal:
    return default_budget().month_to_date_usd()


def check(channel_id: str) -> CheckResult:
    return default_budget().check(channel_id)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
