#!/usr/bin/env python3
"""Dry-run transcript dedup thresholds against Engram memory.db.

This script is intentionally read-only: it opens the SQLite database in read-only
mode and only prints a calibration matrix to stdout.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

DEFAULT_THRESHOLDS = (0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
DEFAULT_DAYS = 7
DEFAULT_MIN_ROWS = 10
WORD_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class TranscriptRow:
    id: int
    channel_id: str
    ts: str
    text: str


@dataclass(frozen=True)
class ThresholdResult:
    threshold: float
    after_count: int
    reduction_pct: float
    below_floor: bool


@dataclass(frozen=True)
class ChannelResult:
    channel_id: str
    before_count: int
    thresholds: tuple[ThresholdResult, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run Jaccard word-overlap dedup thresholds against the last N days "
            "of Engram transcript rows."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".engram" / "memory.db",
        help="Path to memory.db. Defaults to ~/.engram/memory.db.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Lookback window in days. Defaults to {DEFAULT_DAYS}.",
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=DEFAULT_MIN_ROWS,
        help=f"Minimum evidence floor to flag. Defaults to {DEFAULT_MIN_ROWS}.",
    )
    parser.add_argument(
        "--thresholds",
        type=_parse_thresholds,
        default=DEFAULT_THRESHOLDS,
        help=(
            "Comma-separated thresholds to test. "
            "Defaults to 0.70,0.75,0.80,0.85,0.90,0.95."
        ),
    )
    parser.add_argument(
        "--now",
        type=_parse_datetime,
        default=None,
        help="Override the current time as an ISO timestamp. Intended for reproducible runs.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.days <= 0:
        print("--days must be positive", file=sys.stderr)
        return 2
    if args.min_rows < 0:
        print("--min-rows must be non-negative", file=sys.stderr)
        return 2

    db_path = args.db.expanduser()
    if not db_path.exists():
        print(f"memory.db not found: {db_path}", file=sys.stderr)
        return 1

    now = args.now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    since = now.astimezone(UTC) - timedelta(days=args.days)

    try:
        rows = load_transcript_rows(db_path, since)
    except sqlite3.Error as exc:
        print(f"failed to read {db_path}: {exc}", file=sys.stderr)
        return 1

    results = calibrate(rows, thresholds=args.thresholds, min_rows=args.min_rows)
    print(render_markdown(results, db_path=db_path, since=since, now=now, min_rows=args.min_rows))
    return 0


def _parse_thresholds(raw: str) -> tuple[float, ...]:
    try:
        thresholds = tuple(float(value.strip()) for value in raw.split(",") if value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("thresholds must be decimal values") from exc
    if not thresholds:
        raise argparse.ArgumentTypeError("at least one threshold is required")
    invalid = [threshold for threshold in thresholds if threshold < 0 or threshold > 1]
    if invalid:
        raise argparse.ArgumentTypeError("thresholds must be between 0 and 1")
    return thresholds


def _parse_datetime(raw: str) -> datetime:
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--now must be an ISO timestamp") from exc
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def load_transcript_rows(db_path: Path, since: datetime) -> list[TranscriptRow]:
    db_uri = f"file:{quote(str(db_path.resolve()))}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            """
            SELECT id, channel_id, ts, text
            FROM transcripts
            WHERE datetime(ts) >= datetime(?)
            ORDER BY channel_id ASC, datetime(ts) ASC, id ASC
            """,
            (since.astimezone(UTC).isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return [
        TranscriptRow(
            id=int(row["id"]),
            channel_id=str(row["channel_id"]),
            ts=str(row["ts"]),
            text=str(row["text"]),
        )
        for row in rows
    ]


def calibrate(
    rows: Iterable[TranscriptRow],
    *,
    thresholds: Iterable[float] = DEFAULT_THRESHOLDS,
    min_rows: int = DEFAULT_MIN_ROWS,
) -> list[ChannelResult]:
    thresholds_tuple = tuple(thresholds)
    rows_by_channel: dict[str, list[TranscriptRow]] = defaultdict(list)
    for row in rows:
        rows_by_channel[row.channel_id].append(row)

    results: list[ChannelResult] = []
    for channel_id in sorted(rows_by_channel):
        channel_rows = sorted(rows_by_channel[channel_id], key=lambda row: (row.ts, row.id))
        token_sets = [_tokenize(row.text) for row in channel_rows]
        threshold_results = tuple(
            _calibrate_threshold(token_sets, threshold, min_rows)
            for threshold in thresholds_tuple
        )
        results.append(
            ChannelResult(
                channel_id=channel_id,
                before_count=len(channel_rows),
                thresholds=threshold_results,
            )
        )
    return results


def _calibrate_threshold(
    token_sets: list[frozenset[str]],
    threshold: float,
    min_rows: int,
) -> ThresholdResult:
    kept: list[frozenset[str]] = []
    for token_set in token_sets:
        if any(jaccard_overlap(token_set, kept_tokens) >= threshold for kept_tokens in kept):
            continue
        kept.append(token_set)

    before_count = len(token_sets)
    after_count = len(kept)
    reduction_pct = 0.0
    if before_count:
        reduction_pct = ((before_count - after_count) / before_count) * 100
    return ThresholdResult(
        threshold=threshold,
        after_count=after_count,
        reduction_pct=reduction_pct,
        below_floor=after_count < min_rows,
    )


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(match.group(0).lower() for match in WORD_RE.finditer(text))


def jaccard_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def render_markdown(
    results: list[ChannelResult],
    *,
    db_path: Path,
    since: datetime,
    now: datetime,
    min_rows: int = DEFAULT_MIN_ROWS,
) -> str:
    threshold_values = _threshold_values(results)
    lines = [
        "# Dedup Threshold Calibration",
        "",
        f"- DB: `{db_path}`",
        f"- Window: `{since.astimezone(UTC).isoformat()}` to `{now.astimezone(UTC).isoformat()}`",
        f"- Min-evidence floor: `{min_rows}` rows",
        f"- Channels: `{len(results)}`",
        "",
    ]

    if not results:
        lines.append("No transcript rows found in the selected window.")
        return "\n".join(lines)

    header = ["Channel", "Before", *(_format_threshold(threshold) for threshold in threshold_values)]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for result in results:
        row = [result.channel_id, str(result.before_count)]
        row.extend(
            _format_result_cell(threshold_result, min_rows)
            for threshold_result in result.thresholds
        )
        lines.append("| " + " | ".join(row) + " |")

    flagged = [
        f"{result.channel_id} at {_format_threshold(threshold_result.threshold)} "
        f"({threshold_result.after_count} rows)"
        for result in results
        for threshold_result in result.thresholds
        if threshold_result.below_floor
    ]
    if flagged:
        lines.extend(["", "Low-evidence flags:", *[f"- {entry}" for entry in flagged]])

    return "\n".join(lines)


def _threshold_values(results: list[ChannelResult]) -> tuple[float, ...]:
    if not results:
        return DEFAULT_THRESHOLDS
    return tuple(threshold.threshold for threshold in results[0].thresholds)


def _format_threshold(threshold: float) -> str:
    return f"{threshold:.2f}"


def _format_result_cell(result: ThresholdResult, min_rows: int) -> str:
    flag = f", LOW<{min_rows}" if result.below_floor else ""
    return f"{result.after_count} ({result.reduction_pct:.1f}% red{flag})"


if __name__ == "__main__":
    raise SystemExit(main())
