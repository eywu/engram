from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from engram.memory import insert_transcript, open_memory_db

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "nightly" / "calibrate_dedup.py"
SPEC = importlib.util.spec_from_file_location("calibrate_dedup", SCRIPT_PATH)
assert SPEC is not None
calibrate_dedup = importlib.util.module_from_spec(SPEC)
sys.modules["calibrate_dedup"] = calibrate_dedup
assert SPEC.loader is not None
SPEC.loader.exec_module(calibrate_dedup)


BASE_TS = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)


def test_calibration_outputs_markdown_matrix_for_last_seven_days(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    conn = open_memory_db(db_path)
    try:
        _seed_channel(conn, "C07TESTA", 10, "alpha")
        insert_transcript(
            conn,
            session_id="session-C07TESTA",
            channel_id="C07TESTA",
            ts=BASE_TS + timedelta(minutes=30),
            role="assistant",
            message_uuid="C07TESTA-dupe",
            parent_uuid=None,
            text="alpha row 0 token-0",
        )

        _seed_channel(conn, "C07TESTB", 9, "beta")
        insert_transcript(
            conn,
            session_id="session-C07TESTB",
            channel_id="C07TESTB",
            ts=BASE_TS + timedelta(minutes=30),
            role="assistant",
            message_uuid="C07TESTB-dupe",
            parent_uuid=None,
            text="beta row 0 token-0",
        )

        _seed_channel(conn, "C07TESTC", 3, "gamma")
        insert_transcript(
            conn,
            session_id="session-C07OLD",
            channel_id="C07OLD",
            ts=BASE_TS - timedelta(days=8),
            role="assistant",
            message_uuid="old-row",
            parent_uuid=None,
            text="old row outside window",
        )
    finally:
        conn.close()

    rows = calibrate_dedup.load_transcript_rows(db_path, BASE_TS - timedelta(days=7))
    results = calibrate_dedup.calibrate(rows)
    markdown = calibrate_dedup.render_markdown(
        results,
        db_path=db_path,
        since=BASE_TS - timedelta(days=7),
        now=BASE_TS,
    )

    assert "C07OLD" not in markdown
    assert "| Channel | Before | 0.70 | 0.75 | 0.80 | 0.85 | 0.90 | 0.95 |" in markdown
    assert "| C07TESTA | 11 | 10 (9.1% red) | 10 (9.1% red) |" in markdown
    assert "| C07TESTB | 10 | 9 (10.0% red, LOW<10) |" in markdown
    assert "| C07TESTC | 3 | 3 (0.0% red, LOW<10) |" in markdown
    assert "Low-evidence flags:" in markdown


def test_jaccard_overlap_uses_word_sets() -> None:
    left = calibrate_dedup._tokenize("Alpha alpha beta, JSON")
    right = calibrate_dedup._tokenize("alpha beta markdown")

    assert calibrate_dedup.jaccard_overlap(left, right) == 0.5


def _seed_channel(conn, channel_id: str, count: int, prefix: str) -> None:
    for index in range(count):
        insert_transcript(
            conn,
            session_id=f"session-{channel_id}",
            channel_id=channel_id,
            ts=BASE_TS + timedelta(minutes=index),
            role="assistant",
            message_uuid=f"{channel_id}-{index}",
            parent_uuid=None,
            text=f"{prefix} row {index} token-{index}",
        )
