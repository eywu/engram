from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from engram.config import NightlyConfig
from engram.memory import insert_summary, insert_transcript, open_memory_db
from engram.nightly.harvest import run_harvest

BASE_DAY = date(2026, 4, 22)
BASE_TS = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)


def test_harvest_scopes_rows_per_channel_and_includes_summaries(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    with closing(open_memory_db(db_path)) as conn:
        _seed_transcript(conn, "C07TESTA", "a-1", BASE_TS + timedelta(hours=1), "alpha row")
        _seed_summary(conn, "C07TESTA", BASE_TS + timedelta(hours=2), "alpha summary")
        _seed_transcript(conn, "C07TESTB", "b-1", BASE_TS + timedelta(hours=3), "beta row")
        _seed_transcript(conn, "C07TESTC", "c-1", BASE_TS + timedelta(hours=4), "gamma row")
        _seed_transcript(
            conn,
            "C07TESTC",
            "c-old",
            BASE_TS - timedelta(seconds=1),
            "old outside window",
        )
        _seed_transcript(
            conn,
            "C07TESTC",
            "c-future",
            BASE_TS + timedelta(days=1),
            "future outside window",
        )

    result = run_harvest(
        db_path=db_path,
        output_root=tmp_path / "nightly",
        target_date=BASE_DAY,
        config=NightlyConfig(dedup_overlap=1.0, min_evidence=1),
    )

    payload = json.loads(result.output_path.read_text())
    by_channel = {channel["channel_id"]: channel for channel in payload["channels"]}
    assert result.output_path == tmp_path / "nightly" / "2026-04-22" / "harvest.json"
    assert set(by_channel) == {"C07TESTA", "C07TESTB", "C07TESTC"}
    assert [row["kind"] for row in by_channel["C07TESTA"]["rows"]] == [
        "transcript",
        "summary",
    ]
    assert [row["text"] for row in by_channel["C07TESTC"]["rows"]] == ["gamma row"]


def test_harvest_dedups_with_configured_jaccard_threshold(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    with closing(open_memory_db(db_path)) as conn:
        _seed_transcript(
            conn,
            "C07DEDUP",
            "dedup-1",
            BASE_TS + timedelta(minutes=1),
            "alpha beta gamma",
        )
        _seed_transcript(
            conn,
            "C07DEDUP",
            "dedup-2",
            BASE_TS + timedelta(minutes=2),
            "alpha beta gamma delta",
        )

    strict = run_harvest(
        db_path=db_path,
        output_root=tmp_path / "strict",
        target_date=BASE_DAY,
        config=NightlyConfig(dedup_overlap=0.80, min_evidence=1),
    )
    loose = run_harvest(
        db_path=db_path,
        output_root=tmp_path / "loose",
        target_date=BASE_DAY,
        config=NightlyConfig(dedup_overlap=0.75, min_evidence=1),
    )

    assert strict.payload["channels"][0]["row_count"] == 2
    assert loose.payload["channels"][0]["row_count"] == 1


def test_harvest_skips_channels_below_min_evidence(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="engram.nightly.harvest")
    db_path = tmp_path / "memory.db"
    with closing(open_memory_db(db_path)) as conn:
        _seed_transcript(conn, "C07LOW", "low-1", BASE_TS + timedelta(minutes=1), "only row")

    result = run_harvest(
        db_path=db_path,
        output_root=tmp_path / "nightly",
        target_date=BASE_DAY,
        config=NightlyConfig(min_evidence=2),
    )

    assert result.payload["channels"] == []
    assert result.payload["skipped_channels"][0]["reason"] == "min_evidence"
    record = _single_log(caplog.records, "harvest.channel_skipped")
    assert record.phase == "harvest"
    assert record.skipped is True
    assert record.channel_id == "C07LOW"


def test_harvest_token_cap_truncates_to_most_recent_rows_and_logs(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="engram.nightly.harvest")
    db_path = tmp_path / "memory.db"
    with closing(open_memory_db(db_path)) as conn:
        _seed_transcript(conn, "C07CAP", "cap-1", BASE_TS + timedelta(minutes=1), "older one")
        _seed_transcript(conn, "C07CAP", "cap-2", BASE_TS + timedelta(minutes=2), "middle two")
        _seed_transcript(conn, "C07CAP", "cap-3", BASE_TS + timedelta(minutes=3), "recent three")

    result = run_harvest(
        db_path=db_path,
        output_root=tmp_path / "nightly",
        target_date=BASE_DAY,
        config=NightlyConfig(min_evidence=1, max_tokens_per_channel=4),
    )

    channel = result.payload["channels"][0]
    assert channel["truncated"] is True
    assert channel["token_count"] == 4
    assert [row["message_uuid"] for row in channel["rows"]] == ["cap-2", "cap-3"]
    record = _single_log(caplog.records, "harvest.truncated")
    assert record.phase == "harvest"
    assert record.channel_id == "C07CAP"
    assert record.final_token_count == 4


def test_harvest_excludes_configured_channels_and_logs(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="engram.nightly.harvest")
    db_path = tmp_path / "memory.db"
    with closing(open_memory_db(db_path)) as conn:
        _seed_transcript(conn, "C07KEEP", "keep-1", BASE_TS + timedelta(minutes=1), "keep row")
        _seed_transcript(conn, "C07SKIP", "skip-1", BASE_TS + timedelta(minutes=1), "skip row")

    result = run_harvest(
        db_path=db_path,
        output_root=tmp_path / "nightly",
        target_date=BASE_DAY,
        config=NightlyConfig(min_evidence=1, excluded_channels=("C07SKIP",)),
    )

    assert [channel["channel_id"] for channel in result.payload["channels"]] == ["C07KEEP"]
    assert result.payload["skipped_channels"][0]["reason"] == "excluded"
    record = _single_log(caplog.records, "harvest.channel_excluded")
    assert record.phase == "harvest"
    assert record.excluded is True
    assert record.channel_id == "C07SKIP"


def _seed_transcript(
    conn: sqlite3.Connection,
    channel_id: str,
    message_uuid: str,
    ts: datetime,
    text: str,
) -> None:
    insert_transcript(
        conn,
        session_id=f"session-{channel_id}",
        channel_id=channel_id,
        ts=ts,
        role="assistant",
        message_uuid=message_uuid,
        parent_uuid=None,
        text=text,
    )


def _seed_summary(
    conn: sqlite3.Connection,
    channel_id: str,
    ts: datetime,
    text: str,
) -> int:
    return insert_summary(
        conn,
        session_id=f"session-{channel_id}",
        channel_id=channel_id,
        ts=ts,
        trigger="manual",
        day=ts.date(),
        custom_instructions=None,
        summary_text=text,
        embedding=None,
    )


def _single_log(records, message: str):
    matches = [record for record in records if record.getMessage() == message]
    assert len(matches) == 1
    return matches[0]
