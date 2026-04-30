from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp import types

from engram.mcp_tools import make_memory_search_server
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
    assert Path(result["report_path"]).exists()


async def test_pipeline_report_suppress_skips_success_dm(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("nightly:\n  report:\n    suppress: true\n", encoding="utf-8")
    run_date = date(2026, 4, 22)
    posts: list[str] = []

    def fake_harvest(**kwargs: Any) -> HarvestResult:
        output_root = kwargs["output_root"]
        path = output_root / run_date.isoformat() / "harvest.json"
        payload = {
            "date": run_date.isoformat(),
            "channels": [{"channel_id": "C07TEST123", "rows": []}],
            "skipped_channels": [],
        }
        write_json(path, payload)
        return HarvestResult(output_path=path, payload=payload)

    async def fake_synthesize(
        harvest_json: Path,
        *,
        output_root: Path,
        **_: Any,
    ) -> SynthesisResult:
        path = output_root / "archive" / run_date.isoformat() / "synthesis.json"
        payload = {
            "date": run_date.isoformat(),
            "trigger": "nightly",
            "channels": [
                {
                    "channel_id": "C07TEST123",
                    "status": "synthesized",
                    "cost_usd": "0.040000",
                    "row_count": 1,
                    "token_count": 5,
                    "synthesis": {
                        "summary": "nightly summary",
                        "highlights": [],
                        "decisions": [],
                        "action_items": [{"text": "follow up"}],
                        "open_questions": [],
                    },
                }
            ],
            "skipped_channels": [],
            "totals": {"cost_usd": "0.040000"},
            "harvest_path": str(harvest_json),
        }
        write_json(path, payload)
        return SynthesisResult(output_path=path, payload=payload)

    async def fake_apply(synthesis_json: Path, **_: Any) -> ApplyResult:
        payload = json.loads(synthesis_json.read_text(encoding="utf-8"))
        return ApplyResult(
            output_path=None,
            rows_written=1,
            rows_queued=0,
            dry_run=False,
            payload=payload,
        )

    async def success_dm(text: str) -> None:
        posts.append(text)

    result = await run_nightly_pipeline(
        target_date=run_date,
        db_path=db_path,
        output_root=tmp_path / "nightly",
        config_path=config_path,
        clock=lambda: datetime(2026, 4, 22, 23, tzinfo=UTC),
        harvest_func=fake_harvest,
        synthesize_func=fake_synthesize,
        apply_func=fake_apply,
        success_dm=success_dm,
    )

    report_path = Path(result["report_path"])
    assert posts == []
    assert report_path == tmp_path / "nightly" / "archive" / "2026-04-22" / "report.md"
    report = report_path.read_text(encoding="utf-8")
    assert "C07TEST123" in report
    assert "$0.0400" in report


async def test_pipeline_isolation_canary_excluded_channel_absent_from_artifacts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "memory.db"
    config_path = tmp_path / "config.yaml"
    excluded_channel = "C07EXCLUDED"
    canary = f"[ISOLATION-CANARY-{uuid.uuid4().hex}]"
    run_date = date(2026, 4, 22)
    config_path.write_text(
        "\n".join(
            [
                "nightly:",
                "  min_evidence: 1",
                "  excluded_channels:",
                f"    - {excluded_channel}",
                "  report:",
                "    suppress: true",
            ]
        ),
        encoding="utf-8",
    )
    with closing(open_memory_db(db_path)) as conn:
        insert_transcript(
            conn,
            session_id="session-keep",
            channel_id="C07KEEP",
            ts=datetime(2026, 4, 22, 9, tzinfo=UTC),
            role="user",
            message_uuid="keep-canary-test",
            parent_uuid=None,
            text="allowed nightly source",
        )
        insert_transcript(
            conn,
            session_id="session-excluded",
            channel_id=excluded_channel,
            ts=datetime(2026, 4, 22, 9, 5, tzinfo=UTC),
            role="user",
            message_uuid="excluded-canary-test",
            parent_uuid=None,
            text=f"excluded private source {canary}",
        )

    async def fake_synthesize(
        harvest_json: Path,
        *,
        output_root: Path,
        **_: Any,
    ) -> SynthesisResult:
        harvest = json.loads(harvest_json.read_text(encoding="utf-8"))
        assert canary not in json.dumps(harvest)
        channels = [
            {
                "channel_id": channel["channel_id"],
                "status": "synthesized",
                "cost_usd": "0.000000",
                "row_count": channel["row_count"],
                "token_count": channel["token_count"],
                "synthesis": {
                    "summary": f"summary for {channel['channel_id']}",
                    "highlights": [],
                    "decisions": [],
                    "action_items": [],
                    "open_questions": [],
                    "source_row_ids": [row["id"] for row in channel["rows"]],
                },
            }
            for channel in harvest["channels"]
        ]
        payload = {
            "schema_version": 1,
            "date": harvest["date"],
            "trigger": "nightly",
            "channels": channels,
            "skipped_channels": harvest["skipped_channels"],
            "totals": {"cost_usd": "0.000000"},
        }
        path = output_root / "archive" / harvest["date"] / "synthesis.json"
        write_json(path, payload)
        return SynthesisResult(output_path=path, payload=payload)

    async def fake_apply(synthesis_json: Path, **_: Any) -> ApplyResult:
        payload = json.loads(synthesis_json.read_text(encoding="utf-8"))
        return ApplyResult(
            output_path=None,
            rows_written=len(payload["channels"]),
            rows_queued=0,
            dry_run=False,
            payload=payload,
        )

    result = await run_nightly_pipeline(
        target_date=run_date,
        db_path=db_path,
        output_root=tmp_path / "nightly",
        config_path=config_path,
        clock=lambda: datetime(2026, 4, 22, 23, tzinfo=UTC),
        synthesize_func=fake_synthesize,
        apply_func=fake_apply,
    )

    artifact_paths = [path for path in (tmp_path / "nightly").rglob("*") if path.is_file()]
    assert Path(result["report_path"]) in artifact_paths
    assert not [
        path
        for path in artifact_paths
        if canary in path.read_text(encoding="utf-8")
    ]


async def test_concurrent_memory_search_during_nightly_write_has_no_busy_or_dirty_reads(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "memory.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "nightly:\n  min_evidence: 1\n  report:\n    suppress: true\n",
        encoding="utf-8",
    )
    run_date = date(2026, 4, 22)
    with closing(open_memory_db(db_path)) as conn:
        insert_transcript(
            conn,
            session_id="session-live",
            channel_id="C07LIVE",
            ts=datetime(2026, 4, 22, 8, tzinfo=UTC),
            role="user",
            message_uuid="committed-live",
            parent_uuid=None,
            text="committedtoken durable memory",
        )

    async def fake_synthesize(
        harvest_json: Path,
        *,
        output_root: Path,
        **_: Any,
    ) -> SynthesisResult:
        harvest = json.loads(harvest_json.read_text(encoding="utf-8"))
        channels = [
            {
                "channel_id": channel["channel_id"],
                "status": "synthesized",
                "cost_usd": "0.000000",
                "row_count": channel["row_count"],
                "token_count": channel["token_count"],
                "synthesis": {
                    "summary": "committed nightly summary",
                    "highlights": [],
                    "decisions": [],
                    "action_items": [],
                    "open_questions": [],
                    "source_row_ids": [row["id"] for row in channel["rows"]],
                },
            }
            for channel in harvest["channels"]
        ]
        payload = {
            "schema_version": 1,
            "date": harvest["date"],
            "trigger": "nightly",
            "channels": channels,
            "skipped_channels": [],
            "totals": {"cost_usd": "0.000000"},
        }
        path = output_root / "archive" / harvest["date"] / "synthesis.json"
        write_json(path, payload)
        return SynthesisResult(output_path=path, payload=payload)

    async def fake_apply(
        synthesis_json: Path,
        *,
        db_path: Path,
        summary_trigger: str,
        clock,
        **_: Any,
    ) -> ApplyResult:
        payload = json.loads(synthesis_json.read_text(encoding="utf-8"))
        server = make_memory_search_server("C07LIVE", db_path)

        async def reader() -> tuple[dict[str, Any], dict[str, Any]]:
            committed = await _call_memory_search(
                server,
                {"query": "committedtoken", "scope": "this_channel"},
            )
            dirty = await _call_memory_search(
                server,
                {"query": "dirtyreadtoken", "scope": "this_channel"},
            )
            return committed, dirty

        with closing(open_memory_db(db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            upsert_nightly_summary(
                conn,
                channel_id="C07LIVE",
                day=run_date,
                ts=clock(),
                trigger=summary_trigger,
                summary_text="dirtyreadtoken uncommitted nightly summary",
            )
            try:
                reads = await asyncio.gather(*(reader() for _ in range(10)))
            finally:
                conn.rollback()

        for committed, dirty in reads:
            assert "isError" not in committed
            assert "isError" not in dirty
            assert [row["message_uuid"] for row in _rows_from_memory_response(committed)] == [
                "committed-live"
            ]
            assert _rows_from_memory_response(dirty) == []

        with closing(open_memory_db(db_path)) as conn:
            upsert_nightly_summary(
                conn,
                channel_id="C07LIVE",
                day=run_date,
                ts=clock(),
                trigger=summary_trigger,
                summary_text="committed nightly summary",
            )
        return ApplyResult(
            output_path=None,
            rows_written=len(payload["channels"]),
            rows_queued=0,
            dry_run=False,
            payload=payload,
        )

    result = await run_nightly_pipeline(
        target_date=run_date,
        db_path=db_path,
        output_root=tmp_path / "nightly",
        config_path=config_path,
        clock=lambda: datetime(2026, 4, 22, 23, tzinfo=UTC),
        synthesize_func=fake_synthesize,
        apply_func=fake_apply,
    )

    assert result["channels_covered"] == 1


async def test_pipeline_dry_run_passes_apply_flag_and_verbose_phase_events(
    tmp_path: Path,
    caplog,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("nightly:\n  min_evidence: 1\n", encoding="utf-8")
    apply_dry_runs: list[bool] = []

    def fake_harvest(**kwargs: Any) -> HarvestResult:
        payload = {
            "date": "2026-04-22",
            "channels": [],
            "skipped_channels": [],
        }
        path = kwargs["output_root"] / "2026-04-22" / "harvest.json"
        write_json(path, payload)
        return HarvestResult(output_path=path, payload=payload)

    async def fake_synthesize(
        harvest_json: Path,
        *,
        output_root: Path,
        **_: Any,
    ) -> SynthesisResult:
        payload = {
            "schema_version": 1,
            "date": "2026-04-22",
            "channels": [],
            "skipped_channels": [],
            "totals": {"cost_usd": "0.000000"},
        }
        path = output_root / "2026-04-22" / "synthesis.json"
        write_json(path, payload)
        return SynthesisResult(output_path=path, payload=payload)

    async def fake_apply(
        synthesis_json: Path,
        *,
        dry_run: bool,
        **_: Any,
    ) -> ApplyResult:
        apply_dry_runs.append(dry_run)
        payload = json.loads(synthesis_json.read_text(encoding="utf-8"))
        return ApplyResult(
            output_path=tmp_path / "dry-run" / "synthesis.json",
            rows_written=0,
            rows_queued=0,
            dry_run=dry_run,
            payload=payload,
        )

    caplog.set_level(logging.INFO, logger="engram.nightly")

    result = await run_nightly_pipeline(
        dry_run=True,
        verbose=True,
        target_date=date(2026, 4, 22),
        db_path=tmp_path / "memory.db",
        output_root=tmp_path / "nightly",
        config_path=config_path,
        harvest_func=fake_harvest,
        synthesize_func=fake_synthesize,
        apply_func=fake_apply,
    )

    assert result["channels_covered"] == 0
    assert apply_dry_runs == [True]
    events = [record.getMessage() for record in caplog.records]
    assert events == [
        "nightly.harvest_started",
        "nightly.harvest_completed",
        "nightly.synthesis_started",
        "nightly.synthesis_completed",
        "nightly.apply_started",
        "nightly.apply_completed",
    ]
    assert all(getattr(record, "dry_run", None) is True for record in caplog.records)


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


async def _call_memory_search(
    server_config: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    server = server_config["instance"]
    handler = server.request_handlers[types.CallToolRequest]
    result = await handler(
        types.CallToolRequest(
            params=types.CallToolRequestParams(
                name="memory_search",
                arguments=args,
            )
        )
    )
    root = result.root
    response: dict[str, Any] = {
        "content": [{"type": item.type, "text": item.text} for item in root.content]
    }
    if root.isError:
        response["isError"] = True
    return response


def _rows_from_memory_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    return json.loads(response["content"][0]["text"])
