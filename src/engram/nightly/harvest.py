"""Nightly transcript and summary harvest.

This job is intentionally local-only: it reads memory.db, applies deterministic
deduplication and evidence gates, and writes JSON for later nightly stages.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from engram.config import DEFAULT_CONFIG_PATH, NightlyConfig, load_nightly_config
from engram.memory import open_memory_db
from engram.telemetry import configure_logging, write_json

WORD_RE = re.compile(r"[A-Za-z0-9_]+")
DEFAULT_MEMORY_DB = Path.home() / ".engram" / "memory.db"
DEFAULT_OUTPUT_ROOT = Path.home() / ".engram" / "nightly"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HarvestRow:
    kind: str
    id: int
    channel_id: str
    ts: str
    text: str
    token_count: int
    session_id: str | None = None
    role: str | None = None
    message_uuid: str | None = None
    parent_uuid: str | None = None
    trigger: str | None = None
    day: str | None = None
    custom_instructions: str | None = None

    def sort_key(self) -> tuple[datetime, int, str]:
        return (_parse_db_datetime(self.ts), self.id, self.kind)

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "id": self.id,
            "channel_id": self.channel_id,
            "ts": _iso_datetime(self.ts),
            "token_count": self.token_count,
            "text": self.text,
        }
        if self.kind == "transcript":
            payload.update(
                {
                    "session_id": self.session_id,
                    "role": self.role,
                    "message_uuid": self.message_uuid,
                    "parent_uuid": self.parent_uuid,
                }
            )
        else:
            payload.update(
                {
                    "session_id": self.session_id,
                    "trigger": self.trigger,
                    "day": self.day,
                    "custom_instructions": self.custom_instructions,
                }
            )
        return payload


@dataclass(frozen=True)
class HarvestResult:
    output_path: Path
    payload: dict[str, object]


def run_harvest(
    *,
    db_path: Path = DEFAULT_MEMORY_DB,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    target_date: date | None = None,
    config: NightlyConfig | None = None,
) -> HarvestResult:
    """Run one harvest and write ``harvest.json`` under the date output dir."""
    cfg = config or NightlyConfig()
    harvest_date = target_date or datetime.now(UTC).date()
    window_start = datetime.combine(harvest_date, time.min, tzinfo=UTC)
    window_end = window_start + timedelta(days=1)

    with closing(open_memory_db(db_path.expanduser())) as conn:
        rows = load_harvest_rows(conn, window_start=window_start, window_end=window_end)

    rows_by_channel = _group_by_channel(rows)
    excluded_channels = set(cfg.excluded_channels)
    channels_payload: list[dict[str, object]] = []
    skipped_payload: list[dict[str, object]] = []

    for channel_id in sorted(rows_by_channel):
        channel_rows = sorted(rows_by_channel[channel_id], key=lambda row: row.sort_key())
        if channel_id in excluded_channels:
            skipped_payload.append(
                {
                    "channel_id": channel_id,
                    "reason": "excluded",
                    "rows_before": len(channel_rows),
                }
            )
            log.info(
                "harvest.channel_excluded",
                extra={
                    "phase": "harvest",
                    "channel_id": channel_id,
                    "excluded": True,
                    "rows_before": len(channel_rows),
                },
            )
            continue

        deduped_rows = deduplicate_rows(channel_rows, overlap_threshold=cfg.dedup_overlap)
        if len(deduped_rows) < cfg.min_evidence:
            skipped_payload.append(
                {
                    "channel_id": channel_id,
                    "reason": "min_evidence",
                    "rows_before": len(channel_rows),
                    "rows_after_dedup": len(deduped_rows),
                    "min_evidence": cfg.min_evidence,
                }
            )
            log.info(
                "harvest.channel_skipped",
                extra={
                    "phase": "harvest",
                    "channel_id": channel_id,
                    "skipped": True,
                    "reason": "min_evidence",
                    "rows_after_dedup": len(deduped_rows),
                    "min_evidence": cfg.min_evidence,
                },
            )
            continue

        capped_rows, token_count, truncated, before_token_count = apply_token_cap(
            deduped_rows,
            max_tokens=cfg.max_tokens_per_channel,
        )
        if truncated:
            log.info(
                "harvest.truncated",
                extra={
                    "phase": "harvest",
                    "channel_id": channel_id,
                    "truncated": True,
                    "max_tokens_per_channel": cfg.max_tokens_per_channel,
                    "before_token_count": before_token_count,
                    "final_token_count": token_count,
                    "rows_before_truncate": len(deduped_rows),
                    "rows_after_truncate": len(capped_rows),
                },
            )

        channels_payload.append(
            {
                "channel_id": channel_id,
                "rows_before": len(channel_rows),
                "rows_after_dedup": len(deduped_rows),
                "row_count": len(capped_rows),
                "token_count": token_count,
                "truncated": truncated,
                "rows": [row.to_json() for row in capped_rows],
            }
        )
        log.info(
            "harvest.channel_harvested",
            extra={
                "phase": "harvest",
                "channel_id": channel_id,
                "row_count": len(capped_rows),
                "token_count": token_count,
                "truncated": truncated,
            },
        )

    payload: dict[str, object] = {
        "date": harvest_date.isoformat(),
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "config": {
            "dedup_overlap": cfg.dedup_overlap,
            "min_evidence": cfg.min_evidence,
            "max_tokens_per_channel": cfg.max_tokens_per_channel,
            "excluded_channels": list(cfg.excluded_channels),
        },
        "channels": channels_payload,
        "skipped_channels": skipped_payload,
    }

    output_path = output_root.expanduser() / harvest_date.isoformat() / "harvest.json"
    write_json(output_path, payload)
    log.info(
        "harvest.complete",
        extra={
            "phase": "harvest",
            "date": harvest_date.isoformat(),
            "output_path": str(output_path),
            "channels": len(channels_payload),
            "skipped_channels": len(skipped_payload),
        },
    )
    return HarvestResult(output_path=output_path, payload=payload)


def run_weekly_harvest(
    *,
    db_path: Path = DEFAULT_MEMORY_DB,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    target_date: date | None = None,
    config: NightlyConfig | None = None,
) -> HarvestResult:
    """Harvest the seven daily nightly summaries ending on ``target_date``."""
    cfg = config or NightlyConfig()
    week_end = target_date or datetime.now(UTC).date()
    week_start = week_end - timedelta(days=6)
    expected_days = {
        (week_start + timedelta(days=offset)).isoformat()
        for offset in range(7)
    }

    with closing(open_memory_db(db_path.expanduser())) as conn:
        rows = load_weekly_harvest_rows(
            conn,
            week_start=week_start,
            week_end=week_end,
        )

    rows_by_channel = _group_by_channel(rows)
    excluded_channels = set(cfg.excluded_channels)
    channels_payload: list[dict[str, object]] = []
    skipped_payload: list[dict[str, object]] = []

    for channel_id in sorted(rows_by_channel):
        channel_rows = sorted(rows_by_channel[channel_id], key=lambda row: row.day or "")
        if channel_id in excluded_channels:
            skipped_payload.append(
                {
                    "channel_id": channel_id,
                    "reason": "excluded",
                    "rows_before": len(channel_rows),
                }
            )
            continue

        row_days = {str(row.day) for row in channel_rows}
        if row_days != expected_days:
            skipped_payload.append(
                {
                    "channel_id": channel_id,
                    "reason": "weekly_window_incomplete",
                    "rows_before": len(channel_rows),
                    "required_days": sorted(expected_days),
                    "present_days": sorted(row_days),
                }
            )
            log.info(
                "harvest.weekly_channel_skipped",
                extra={
                    "phase": "weekly-harvest",
                    "channel_id": channel_id,
                    "reason": "weekly_window_incomplete",
                    "rows_before": len(channel_rows),
                    "present_days": sorted(row_days),
                },
            )
            continue

        token_count = sum(row.token_count for row in channel_rows)
        channels_payload.append(
            {
                "channel_id": channel_id,
                "rows_before": len(channel_rows),
                "rows_after_dedup": len(channel_rows),
                "row_count": len(channel_rows),
                "token_count": token_count,
                "truncated": False,
                "rows": [row.to_json() for row in channel_rows],
            }
        )
        log.info(
            "harvest.weekly_channel_harvested",
            extra={
                "phase": "weekly-harvest",
                "channel_id": channel_id,
                "row_count": len(channel_rows),
                "token_count": token_count,
            },
        )

    payload: dict[str, object] = {
        "date": week_end.isoformat(),
        "trigger": "nightly-weekly",
        "window": {
            "start_day": week_start.isoformat(),
            "end_day": week_end.isoformat(),
        },
        "config": {
            "required_daily_rows": 7,
            "excluded_channels": list(cfg.excluded_channels),
        },
        "channels": channels_payload,
        "skipped_channels": skipped_payload,
    }

    output_path = output_root.expanduser() / week_end.isoformat() / "weekly-harvest.json"
    write_json(output_path, payload)
    log.info(
        "harvest.weekly_complete",
        extra={
            "phase": "weekly-harvest",
            "date": week_end.isoformat(),
            "output_path": str(output_path),
            "channels": len(channels_payload),
            "skipped_channels": len(skipped_payload),
        },
    )
    return HarvestResult(output_path=output_path, payload=payload)


def load_harvest_rows(
    conn: sqlite3.Connection,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[HarvestRow]:
    rows = conn.execute(
        """
        SELECT
            'transcript' AS kind,
            id,
            session_id,
            channel_id,
            ts,
            role,
            message_uuid,
            parent_uuid,
            text,
            NULL AS trigger,
            NULL AS day,
            NULL AS custom_instructions
        FROM transcripts
        WHERE datetime(ts) >= datetime(?) AND datetime(ts) < datetime(?)
        UNION ALL
        SELECT
            'summary' AS kind,
            id,
            session_id,
            channel_id,
            ts,
            NULL AS role,
            NULL AS message_uuid,
            NULL AS parent_uuid,
            summary_text AS text,
            trigger,
            day,
            custom_instructions
        FROM summaries
        WHERE datetime(ts) >= datetime(?) AND datetime(ts) < datetime(?)
        ORDER BY channel_id ASC, ts ASC, kind ASC, id ASC
        """,
        (
            window_start.astimezone(UTC).isoformat(),
            window_end.astimezone(UTC).isoformat(),
            window_start.astimezone(UTC).isoformat(),
            window_end.astimezone(UTC).isoformat(),
        ),
    ).fetchall()
    return [_row_from_sql(row) for row in rows]


def load_weekly_harvest_rows(
    conn: sqlite3.Connection,
    *,
    week_start: date,
    week_end: date,
) -> list[HarvestRow]:
    rows = conn.execute(
        """
        SELECT
            'summary' AS kind,
            id,
            session_id,
            channel_id,
            ts,
            NULL AS role,
            NULL AS message_uuid,
            NULL AS parent_uuid,
            summary_text AS text,
            trigger,
            day,
            custom_instructions
        FROM summaries
        WHERE trigger = 'nightly'
          AND day >= ?
          AND day <= ?
        ORDER BY channel_id ASC, day ASC, id ASC
        """,
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    return [_row_from_sql(row) for row in rows]


def deduplicate_rows(
    rows: Iterable[HarvestRow],
    *,
    overlap_threshold: float,
) -> list[HarvestRow]:
    threshold = min(1.0, max(0.0, float(overlap_threshold)))
    kept: list[HarvestRow] = []
    kept_tokens: list[frozenset[str]] = []
    for row in sorted(rows, key=lambda item: item.sort_key()):
        token_set = tokenize(row.text)
        if any(jaccard_overlap(token_set, existing) >= threshold for existing in kept_tokens):
            continue
        kept.append(row)
        kept_tokens.append(token_set)
    return kept


def apply_token_cap(
    rows: Sequence[HarvestRow],
    *,
    max_tokens: int,
) -> tuple[list[HarvestRow], int, bool, int]:
    before_token_count = sum(row.token_count for row in rows)
    if before_token_count <= max_tokens:
        return list(rows), before_token_count, False, before_token_count

    kept_desc: list[HarvestRow] = []
    running_tokens = 0
    for row in sorted(rows, key=lambda item: item.sort_key(), reverse=True):
        if running_tokens + row.token_count > max_tokens:
            if not kept_desc:
                kept_desc.append(row)
                running_tokens += row.token_count
            break
        kept_desc.append(row)
        running_tokens += row.token_count

    kept = sorted(kept_desc, key=lambda item: item.sort_key())
    return kept, running_tokens, True, before_token_count


def tokenize(text: str) -> frozenset[str]:
    return frozenset(match.group(0).lower() for match in WORD_RE.finditer(text))


def jaccard_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harvest the configured Engram memory.db 24h window into nightly JSON."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_MEMORY_DB,
        help="Path to memory.db. Defaults to ~/.engram/memory.db.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.yaml. Defaults to ~/.engram/config.yaml.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Directory for dated nightly outputs. Defaults to ~/.engram/nightly.",
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        help="Harvest date as YYYY-MM-DD. Defaults to the current UTC date.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()
    try:
        config = load_nightly_config(args.config.expanduser())
        result = run_harvest(
            db_path=args.db,
            output_root=args.output_root,
            target_date=args.date,
            config=config,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"harvest failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"harvest": str(result.output_path)}, sort_keys=True))
    return 0


def _group_by_channel(rows: Iterable[HarvestRow]) -> dict[str, list[HarvestRow]]:
    grouped: dict[str, list[HarvestRow]] = defaultdict(list)
    for row in rows:
        grouped[row.channel_id].append(row)
    return grouped


def _row_from_sql(row: sqlite3.Row) -> HarvestRow:
    text = str(row["text"])
    return HarvestRow(
        kind=str(row["kind"]),
        id=int(row["id"]),
        session_id=row["session_id"],
        channel_id=str(row["channel_id"]),
        ts=str(row["ts"]),
        role=row["role"],
        message_uuid=row["message_uuid"],
        parent_uuid=row["parent_uuid"],
        text=text,
        token_count=count_tokens(text),
        trigger=row["trigger"],
        day=row["day"],
        custom_instructions=row["custom_instructions"],
    )


def count_tokens(text: str) -> int:
    return len(tuple(WORD_RE.finditer(text)))


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--date must be YYYY-MM-DD") from exc


def _parse_db_datetime(raw: str) -> datetime:
    value = datetime.fromisoformat(str(raw))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_datetime(raw: str) -> str:
    return _parse_db_datetime(raw).isoformat()
