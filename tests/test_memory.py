"""Memory database schema and helper tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from engram.memory import (
    get_watermark,
    insert_summary,
    insert_transcript,
    migrate,
    open_memory_db,
    search_keyword,
    set_watermark,
)


@pytest.fixture
def conn(tmp_path: Path):
    db = open_memory_db(tmp_path / "memory.db")
    try:
        yield db
    finally:
        db.close()


def _insert_transcript(
    conn: sqlite3.Connection,
    *,
    channel_id: str = "C07TEST123",
    message_uuid: str = "msg-1",
    text: str = "hello world",
    ts: str = "2026-04-21T12:00:00Z",
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


def _insert_summary(
    conn: sqlite3.Connection,
    *,
    channel_id: str = "C07TEST123",
    trigger: str = "compact",
    summary_text: str = "hello summary",
    ts: str = "2026-04-21T12:05:00Z",
) -> None:
    insert_summary(
        conn,
        session_id=f"session-{channel_id}",
        channel_id=channel_id,
        ts=ts,
        trigger=trigger,
        day="2026-04-21",
        custom_instructions=None,
        summary_text=summary_text,
    )


def test_migration_is_idempotent(tmp_path):
    conn = open_memory_db(tmp_path / "memory.db")
    try:
        migrate(conn)
        migrate(conn)
        rows = conn.execute(
            """
            SELECT type, name
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    finally:
        conn.close()

    names = [(row["type"], row["name"]) for row in rows]
    assert len(names) == len(set(names))
    assert ("table", "transcripts") in names
    assert ("table", "summaries") in names
    assert ("table", "ingest_state") in names
    assert ("table", "transcripts_fts") in names
    assert ("table", "summaries_fts") in names
    assert ("trigger", "transcripts_ai") in names
    assert ("trigger", "summaries_ai") in names


def test_insert_transcript_idempotent_on_message_uuid(conn):
    _insert_transcript(conn, message_uuid="msg-dup")
    _insert_transcript(conn, message_uuid="msg-dup")

    count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    assert count == 1


def test_insert_summary_trigger_values(conn):
    for trigger in ("compact", "nightly", "nightly-weekly", "manual"):
        _insert_summary(conn, trigger=trigger, ts=f"2026-04-21T12:0{len(trigger)}:00Z")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_summary(conn, trigger="unsupported")


def test_fts_auto_index_on_insert(conn):
    _insert_transcript(conn, text="hello world", message_uuid="msg-hello")

    results = search_keyword(conn, query="hello", channel_id="C07TEST123")

    assert len(results) == 1
    assert results[0]["kind"] == "transcript"
    assert results[0]["message_uuid"] == "msg-hello"
    assert "hello" in results[0]["snippet"]


def test_search_scope_this_channel_filters(conn):
    _insert_transcript(conn, channel_id="C07TEST123", message_uuid="msg-a1", text="needle one")
    _insert_transcript(conn, channel_id="C07TEST123", message_uuid="msg-a2", text="needle two")
    _insert_transcript(conn, channel_id="C07OTHER123", message_uuid="msg-b1", text="needle three")

    results = search_keyword(
        conn,
        query="needle",
        scope="this_channel",
        channel_id="C07TEST123",
        limit=5,
    )

    assert len(results) == 2
    assert {result["channel_id"] for result in results} == {"C07TEST123"}


def test_search_kind_filters(conn):
    _insert_transcript(conn, text="sharedkeyword transcript", message_uuid="msg-transcript")
    _insert_summary(conn, summary_text="sharedkeyword summary")

    transcript_results = search_keyword(
        conn,
        query="sharedkeyword",
        channel_id="C07TEST123",
        kind="transcripts",
    )
    summary_results = search_keyword(
        conn,
        query="sharedkeyword",
        channel_id="C07TEST123",
        kind="summaries",
    )
    both_results = search_keyword(conn, query="sharedkeyword", channel_id="C07TEST123", kind="both")

    assert len(transcript_results) == 1
    assert transcript_results[0]["kind"] == "transcript"
    assert len(summary_results) == 1
    assert summary_results[0]["kind"] == "summary"
    assert len(both_results) == 2


def test_watermark_roundtrip(conn):
    assert get_watermark(conn, "session-C07TEST123") is None

    set_watermark(conn, "session-C07TEST123", "msg-1", "2026-04-21T12:00:00Z")
    assert get_watermark(conn, "session-C07TEST123") == "msg-1"

    set_watermark(conn, "session-C07TEST123", "msg-2", "2026-04-21T12:05:00Z")
    assert get_watermark(conn, "session-C07TEST123") == "msg-2"
