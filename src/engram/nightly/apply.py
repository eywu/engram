"""Apply nightly synthesis output back into memory.db.

The apply phase upserts one ``summaries`` row per synthesized channel and then
flushes the embedding queue inline. A normal process exit therefore only happens
after nightly summary embeddings have been persisted; abnormal termination during
the flush can be recovered by rerunning this idempotent phase for the same date.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from engram import paths
from engram.config import DEFAULT_CONFIG_PATH, EmbeddingsConfig
from engram.embeddings import EmbeddingQueue, GeminiEmbedder
from engram.memory import open_memory_db
from engram.telemetry import configure_logging

DEFAULT_MEMORY_DB = Path.home() / ".engram" / "memory.db"
DRY_RUN_ROOT = Path("/tmp")
APPLY_SUMMARY_TRIGGERS = frozenset({"nightly", "nightly-weekly"})

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApplyRow:
    channel_id: str
    summary_text: str
    source_row_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ApplyResult:
    output_path: Path | None
    rows_written: int
    rows_queued: int
    dry_run: bool
    payload: dict[str, Any]


async def apply_synthesis(
    synthesis_json: Path,
    *,
    db_path: Path = DEFAULT_MEMORY_DB,
    config_path: Path = DEFAULT_CONFIG_PATH,
    dry_run: bool = False,
    summary_trigger: str = "nightly",
    embedding_queue: EmbeddingQueue | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ApplyResult:
    """Read ``synthesis.json`` and apply synthesized channel summaries."""
    if summary_trigger not in APPLY_SUMMARY_TRIGGERS:
        allowed = ", ".join(sorted(APPLY_SUMMARY_TRIGGERS))
        raise ValueError(f"invalid summary trigger {summary_trigger!r}; expected one of: {allowed}")

    clock = clock or (lambda: datetime.now(UTC))
    source_path = synthesis_json.expanduser()
    source_text = source_path.read_text(encoding="utf-8")
    payload = json.loads(source_text)
    run_date = _payload_date(payload, clock=clock)
    rows = _extract_rows(payload, summary_trigger=summary_trigger)

    if dry_run:
        output_path = DRY_RUN_ROOT / f"engram-nightly-dryrun-{run_date.isoformat()}" / "synthesis.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(source_text, encoding="utf-8")
        log.info(
            "apply.dry_run",
            extra={
                "phase": "apply",
                "date": run_date.isoformat(),
                "output_path": str(output_path),
                "channels": len(rows),
            },
        )
        return ApplyResult(
            output_path=output_path,
            rows_written=0,
            rows_queued=0,
            dry_run=True,
            payload=payload,
        )

    queue = embedding_queue or _configured_embedding_queue(
        config_path=config_path.expanduser(),
        db_path=db_path.expanduser(),
    )
    applied_at = clock()
    rows_queued = 0

    with closing(open_memory_db(db_path.expanduser())) as conn:
        for row in rows:
            summary_id, overwritten = upsert_nightly_summary(
                conn,
                channel_id=row.channel_id,
                day=run_date,
                ts=applied_at,
                trigger=summary_trigger,
                summary_text=row.summary_text,
            )
            if overwritten:
                log.info(
                    "apply.upsert_overwrite",
                    extra={
                        "phase": "apply",
                        "date": run_date.isoformat(),
                        "channel_id": row.channel_id,
                        "trigger": summary_trigger,
                        "summary_id": summary_id,
                    },
                )
            await queue.enqueue_summary(summary_id, row.summary_text)
            rows_queued += 1

    await queue.flush()
    log.info(
        "apply.complete",
        extra={
            "phase": "apply",
            "date": run_date.isoformat(),
            "trigger": summary_trigger,
            "channels": len(rows),
            "rows_queued": rows_queued,
            "embedding_queue_depth": queue.depth,
            "embedding_drop_count": queue.drop_count,
        },
    )
    return ApplyResult(
        output_path=None,
        rows_written=len(rows),
        rows_queued=rows_queued,
        dry_run=False,
        payload=payload,
    )


def upsert_nightly_summary(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    day: date,
    ts: datetime,
    trigger: str = "nightly",
    summary_text: str,
) -> tuple[int, bool]:
    """Upsert one nightly summary and return ``(summary_id, overwritten)``."""
    if trigger not in APPLY_SUMMARY_TRIGGERS:
        raise ValueError("nightly apply only supports nightly summary triggers")

    existing = conn.execute(
        """
        SELECT id
        FROM summaries
        WHERE channel_id = ? AND day = ? AND trigger = ?
        """,
        (channel_id, day.isoformat(), trigger),
    ).fetchone()

    conn.execute(
        """
        INSERT INTO summaries (
            session_id,
            channel_id,
            ts,
            trigger,
            day,
            custom_instructions,
            summary_text
        )
        VALUES (NULL, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(channel_id, day, trigger) DO UPDATE SET
            summary_text=excluded.summary_text,
            ts=excluded.ts
        """,
        (channel_id, ts.isoformat(), trigger, day.isoformat(), summary_text),
    )
    row = conn.execute(
        """
        SELECT id
        FROM summaries
        WHERE channel_id = ? AND day = ? AND trigger = ?
        """,
        (channel_id, day.isoformat(), trigger),
    ).fetchone()
    if row is None:
        raise RuntimeError("nightly summary upsert did not produce a row")
    return int(row["id"]), existing is not None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply a nightly synthesis.json into memory.db."
    )
    parser.add_argument("synthesis_json", type=Path, help="Path to synthesis.json.")
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
        "--dry-run",
        action="store_true",
        help="Copy synthesis.json to /tmp/engram-nightly-dryrun-<date>/ without touching memory.db.",
    )
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="Write synthesized rows with trigger='nightly-weekly'.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()
    try:
        result = asyncio.run(
            apply_synthesis(
                args.synthesis_json,
                db_path=args.db,
                config_path=args.config,
                dry_run=args.dry_run,
                summary_trigger="nightly-weekly" if args.weekly else "nightly",
            )
        )
    except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        print(f"apply failed: {exc}", file=sys.stderr)
        return 1

    payload: dict[str, object] = {
        "dry_run": result.dry_run,
        "rows_written": result.rows_written,
        "rows_queued": result.rows_queued,
    }
    if result.output_path is not None:
        payload["output_path"] = str(result.output_path)
    print(json.dumps(payload, sort_keys=True))
    return 0


def _configured_embedding_queue(*, config_path: Path, db_path: Path) -> EmbeddingQueue:
    _load_env_files()
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = EmbeddingsConfig.from_mapping(raw.get("embeddings"))
    embedder = GeminiEmbedder(config)
    return EmbeddingQueue(embedder, db_path=db_path)


def _load_env_files() -> None:
    for candidate in (
        Path.cwd() / ".env",
        paths.engram_home() / ".env",
        Path.home() / "code" / "_secret" / ".env",
    ):
        if candidate.exists():
            load_dotenv(candidate, override=False)


def _payload_date(payload: dict[str, Any], *, clock: Callable[[], datetime]) -> date:
    raw_date = payload.get("date")
    if raw_date:
        return date.fromisoformat(str(raw_date))
    return clock().date()


def _extract_rows(payload: dict[str, Any], *, summary_trigger: str) -> list[ApplyRow]:
    rows: list[ApplyRow] = []
    for channel in payload.get("channels") or []:
        if not isinstance(channel, dict):
            continue
        status = str(channel.get("status") or "")
        if status != "synthesized":
            log.info(
                "apply.channel_skipped",
                extra={
                    "phase": "apply",
                    "channel_id": channel.get("channel_id"),
                    "status": status,
                },
            )
            continue
        synthesis = channel.get("synthesis")
        if not isinstance(synthesis, dict):
            raise ValueError("synthesized channel is missing synthesis object")
        channel_id = str(channel.get("channel_id") or synthesis.get("channel_id") or "").strip()
        synthesis_channel_id = str(synthesis.get("channel_id") or channel_id).strip()
        if not channel_id:
            raise ValueError("synthesized channel is missing channel_id")
        if synthesis_channel_id and synthesis_channel_id != channel_id:
            raise ValueError(
                f"synthesis channel_id {synthesis_channel_id!r} does not match {channel_id!r}"
            )
        summary_text = synthesis.get("summary")
        if not isinstance(summary_text, str) or not summary_text.strip():
            raise ValueError(f"synthesized channel {channel_id!r} is missing summary text")
        source_row_ids = _source_row_ids(synthesis.get("source_row_ids"))
        if summary_trigger == "nightly-weekly":
            if len(source_row_ids) != 7:
                raise ValueError(
                    f"synthesized weekly channel {channel_id!r} must reference 7 daily rows"
                )
            source_text = ", ".join(str(row_id) for row_id in source_row_ids)
            summary_text = f"{summary_text.strip()}\n\nSource daily summary row ids: {source_text}"
        rows.append(
            ApplyRow(
                channel_id=channel_id,
                summary_text=summary_text,
                source_row_ids=source_row_ids,
            )
        )
    return rows


def _source_row_ids(raw: Any) -> tuple[int, ...]:
    if not isinstance(raw, list):
        return ()
    ids: list[int] = []
    for value in raw:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return tuple(ids)


if __name__ == "__main__":
    raise SystemExit(main())
