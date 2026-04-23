from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from engram.memory import insert_summary, insert_transcript, open_memory_db
from engram.nightly.apply import ApplyResult, upsert_nightly_summary
from engram.nightly.harvest import HarvestResult, run_harvest, run_weekly_harvest
from engram.nightly.pipeline import run_nightly_pipeline
from engram.nightly.synthesize import SynthesisResult
from engram.telemetry import write_json


async def test_weekly_pipeline_runs_daily_first_and_includes_todays_row(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "memory.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("nightly:\n  min_evidence: 1\n", encoding="utf-8")
    monday = date(2026, 4, 20)
    events: list[str] = []
    weekly_days_seen: list[str] = []
    weekly_texts_seen: list[str] = []

    with closing(open_memory_db(db_path)) as conn:
        for offset in range(6):
            day = monday - timedelta(days=6 - offset)
            _seed_summary(conn, "C07TEST123", day, f"prior daily {day.isoformat()}")
        insert_transcript(
            conn,
            session_id="session-C07TEST123",
            channel_id="C07TEST123",
            ts=datetime(2026, 4, 20, 9, tzinfo=UTC),
            role="assistant",
            message_uuid="msg-monday",
            parent_uuid=None,
            text="monday daily source",
        )

    def recording_harvest(**kwargs: Any) -> HarvestResult:
        events.append("harvest:daily")
        return run_harvest(**kwargs)

    def recording_weekly_harvest(**kwargs: Any) -> HarvestResult:
        events.append("harvest:weekly")
        result = run_weekly_harvest(**kwargs)
        channel = result.payload["channels"][0]
        weekly_days_seen.extend(row["day"] for row in channel["rows"])
        weekly_texts_seen.extend(row["text"] for row in channel["rows"])
        return result

    async def fake_synthesize(
        harvest_json: Path,
        *,
        weekly: bool,
        output_root: Path,
        **_: Any,
    ) -> SynthesisResult:
        trigger = "nightly-weekly" if weekly else "nightly"
        events.append(f"synthesize:{trigger}")
        harvest = json.loads(harvest_json.read_text(encoding="utf-8"))
        channels = []
        for channel in harvest["channels"]:
            source_row_ids = [row["id"] for row in channel["rows"]]
            channels.append(
                {
                    "channel_id": channel["channel_id"],
                    "status": "synthesized",
                    "synthesis": {
                        "schema_version": 1,
                        "date": harvest["date"],
                        "channel_id": channel["channel_id"],
                        "summary": f"{trigger} summary",
                        "highlights": [],
                        "decisions": [],
                        "action_items": [],
                        "open_questions": [],
                        "source_row_ids": source_row_ids,
                    },
                    "cost_usd": "0.010000",
                }
            )
        path = output_root / f"{trigger}.json"
        payload = {
            "schema_version": 1,
            "date": harvest["date"],
            "trigger": trigger,
            "channels": channels,
            "skipped_channels": [],
            "totals": {"cost_usd": "0.010000"},
        }
        write_json(path, payload)
        return SynthesisResult(output_path=path, payload=payload)

    async def fake_apply(
        synthesis_json: Path,
        *,
        summary_trigger: str,
        db_path: Path,
        clock,
        **_: Any,
    ) -> ApplyResult:
        events.append(f"apply:{summary_trigger}")
        payload = json.loads(synthesis_json.read_text(encoding="utf-8"))
        run_date = date.fromisoformat(payload["date"])
        rows_written = 0
        with closing(open_memory_db(db_path)) as conn:
            for channel in payload["channels"]:
                synthesis = channel["synthesis"]
                upsert_nightly_summary(
                    conn,
                    channel_id=channel["channel_id"],
                    day=run_date,
                    ts=clock(),
                    trigger=summary_trigger,
                    summary_text=synthesis["summary"],
                )
                rows_written += 1
        return ApplyResult(
            output_path=None,
            rows_written=rows_written,
            rows_queued=0,
            dry_run=False,
            payload=payload,
        )

    result = await run_nightly_pipeline(
        weekly=True,
        target_date=monday,
        db_path=db_path,
        output_root=tmp_path / "nightly",
        config_path=config_path,
        clock=lambda: datetime(2026, 4, 20, 23, tzinfo=UTC),
        harvest_func=recording_harvest,
        weekly_harvest_func=recording_weekly_harvest,
        synthesize_func=fake_synthesize,
        apply_func=fake_apply,
    )

    assert events == [
        "harvest:daily",
        "synthesize:nightly",
        "apply:nightly",
        "harvest:weekly",
        "synthesize:nightly-weekly",
        "apply:nightly-weekly",
    ]
    assert weekly_days_seen == [
        "2026-04-14",
        "2026-04-15",
        "2026-04-16",
        "2026-04-17",
        "2026-04-18",
        "2026-04-19",
        "2026-04-20",
    ]
    assert weekly_texts_seen[-1] == "nightly summary"
    assert result["channels_covered"] == 2


def _seed_summary(
    conn: sqlite3.Connection,
    channel_id: str,
    day: date,
    text: str,
) -> None:
    insert_summary(
        conn,
        session_id=f"session-{channel_id}",
        channel_id=channel_id,
        ts=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        trigger="nightly",
        day=day,
        custom_instructions=None,
        summary_text=text,
        embedding=None,
    )
