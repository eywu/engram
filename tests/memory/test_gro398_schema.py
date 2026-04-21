from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

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

BASE_TS = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    db = open_memory_db(tmp_path / "memory.db")
    try:
        yield db
    finally:
        db.close()


def _insert_transcript(
    db: sqlite3.Connection,
    *,
    channel_id: str = "C07TEST123",
    message_uuid: str = "msg-1",
    text: str = "hello memory",
    ts: datetime = BASE_TS,
) -> bool:
    return insert_transcript(
        db,
        session_id=f"session-{channel_id}",
        channel_id=channel_id,
        ts=ts,
        role="user",
        message_uuid=message_uuid,
        parent_uuid=None,
        text=text,
    )


def _insert_summary(
    db: sqlite3.Connection,
    *,
    channel_id: str = "C07TEST123",
    trigger: str = "manual",
    summary_text: str = "summary memory",
    ts: datetime = BASE_TS,
    session_id: str | None = "session-C07TEST123",
    day: date | None = date(2026, 4, 21),
) -> int:
    return insert_summary(
        db,
        session_id=session_id,
        channel_id=channel_id,
        ts=ts,
        trigger=trigger,
        day=day,
        custom_instructions=None,
        summary_text=summary_text,
        embedding=None,
    )


def test_migration_creates_all_tables_and_indexes():
    db = sqlite3.connect(":memory:")
    try:
        migrate(db)
        rows = db.execute("SELECT name, type, sql FROM sqlite_master").fetchall()
        names = {row[0] for row in rows}

        assert {
            "transcripts",
            "summaries",
            "ingest_state",
            "transcripts_fts",
            "summaries_fts",
        }.issubset(names)
        assert {
            "idx_transcripts_channel_ts",
            "idx_summaries_channel_day",
            "idx_summaries_channel_trigger",
        }.issubset(names)
        assert {
            "trg_transcripts_fts_ai",
            "trg_transcripts_fts_ad",
            "trg_transcripts_fts_au",
            "trg_summaries_fts_ai",
            "trg_summaries_fts_ad",
            "trg_summaries_fts_au",
        }.issubset(names)

        summary_columns = {
            row[1]: row[3] for row in db.execute("PRAGMA table_info(summaries)").fetchall()
        }
        ingest_columns = {
            row[1]: row[3] for row in db.execute("PRAGMA table_info(ingest_state)").fetchall()
        }
        assert summary_columns["session_id"] == 0
        assert summary_columns["day"] == 0
        assert ingest_columns["last_ingested_uuid"] == 0
        assert ingest_columns["last_ingested_ts"] == 0
    finally:
        db.close()


def test_migration_is_idempotent():
    db = sqlite3.connect(":memory:")
    try:
        migrate(db)
        before = set(db.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
        migrate(db)
        after = set(db.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
        assert after == before
    finally:
        db.close()


def test_open_memory_db_creates_dir_and_file(tmp_path):
    db_path = tmp_path / "missing" / "nested" / "memory.db"
    db = open_memory_db(db_path)
    try:
        assert db_path.parent.is_dir()
        assert db_path.is_file()
    finally:
        db.close()


def test_insert_transcript_idempotent_on_message_uuid(conn):
    assert _insert_transcript(conn, message_uuid="dupe-msg") is True
    assert _insert_transcript(conn, message_uuid="dupe-msg") is False
    count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    assert count == 1


def test_insert_transcript_returns_true_on_first_insert(conn):
    assert _insert_transcript(conn, message_uuid="first-msg") is True


def test_insert_summary_accepts_all_four_triggers(conn):
    for trigger in {"compact", "nightly", "nightly-weekly", "manual"}:
        summary_id = _insert_summary(
            conn,
            trigger=trigger,
            summary_text=f"{trigger} summary",
            session_id=None if trigger.startswith("nightly") else "session-C07TEST123",
        )
        assert summary_id > 0


def test_insert_summary_rejects_invalid_trigger(conn):
    with pytest.raises(ValueError):
        _insert_summary(conn, trigger="garbage")


def test_insert_summary_allows_null_session_id_and_day(conn):
    summary_id = _insert_summary(conn, session_id=None, day=None, trigger="compact")
    row = conn.execute(
        "SELECT session_id, day FROM summaries WHERE id = ?",
        (summary_id,),
    ).fetchone()
    assert row["session_id"] is None
    assert row["day"] is None


def test_insert_summary_returns_generated_id(conn):
    summary_id = _insert_summary(conn)
    assert isinstance(summary_id, int)
    assert summary_id > 0


def test_fts_auto_updates_on_insert(conn):
    _insert_transcript(conn, message_uuid="octopus-msg", text="the secret is octopus")
    results = search_keyword(conn, query="octopus", scope="all_channels")
    assert len(results) == 1
    assert results[0]["message_uuid"] == "octopus-msg"


def test_fts_auto_updates_on_delete(conn):
    _insert_transcript(conn, message_uuid="delete-msg", text="the secret is octopus")
    conn.execute("DELETE FROM transcripts WHERE message_uuid = ?", ("delete-msg",))
    assert search_keyword(conn, query="octopus", scope="all_channels") == []


def test_search_scope_this_channel_filters(conn):
    _insert_transcript(
        conn,
        channel_id="C07TESTA",
        message_uuid="a-1",
        text="needle alpha",
        ts=BASE_TS,
    )
    _insert_transcript(
        conn,
        channel_id="C07TESTA",
        message_uuid="a-2",
        text="needle beta",
        ts=BASE_TS + timedelta(seconds=1),
    )
    _insert_transcript(
        conn,
        channel_id="C07TESTB",
        message_uuid="b-1",
        text="needle gamma",
        ts=BASE_TS + timedelta(seconds=2),
    )

    results = search_keyword(
        conn,
        query="needle",
        scope="this_channel",
        channel_id="C07TESTA",
        limit=10,
    )
    assert len(results) == 2
    assert {result["channel_id"] for result in results} == {"C07TESTA"}


def test_search_scope_this_channel_requires_channel_id(conn):
    with pytest.raises(ValueError):
        search_keyword(conn, query="needle", scope="this_channel", channel_id=None)


def test_search_kind_filters(conn):
    _insert_transcript(conn, message_uuid="shared-msg", text="shared keyword transcript")
    _insert_summary(conn, summary_text="shared keyword summary")

    transcript_results = search_keyword(conn, query="shared", scope="all_channels", kind="transcripts")
    summary_results = search_keyword(conn, query="shared", scope="all_channels", kind="summaries")
    both_results = search_keyword(conn, query="shared", scope="all_channels", kind="both")

    assert len(transcript_results) == 1
    assert transcript_results[0]["kind"] == "transcript"
    assert len(summary_results) == 1
    assert summary_results[0]["kind"] == "summary"
    assert len(both_results) == 2


def test_search_limit_respected_and_clamped(conn):
    for index in range(150):
        _insert_transcript(
            conn,
            message_uuid=f"limit-msg-{index}",
            text=f"limitterm row {index}",
            ts=BASE_TS + timedelta(seconds=index),
        )

    assert len(search_keyword(conn, query="limitterm", scope="all_channels", limit=200)) <= 100
    assert len(search_keyword(conn, query="limitterm", scope="all_channels", limit=3)) == 3


def test_search_empty_query_returns_empty_list(conn):
    assert search_keyword(conn, query="", scope="all_channels") == []


def test_search_snippet_includes_query_term(conn):
    _insert_transcript(conn, message_uuid="snippet-msg", text="this sentence has a needle inside")
    results = search_keyword(conn, query="needle", scope="all_channels")
    assert "needle" in results[0]["snippet"].lower()


def test_watermark_none_before_set(conn):
    assert get_watermark(conn, "new-session") == (None, None)


def test_watermark_roundtrip_and_overwrite(conn):
    first_ts = BASE_TS
    second_ts = BASE_TS + timedelta(minutes=5)

    set_watermark(conn, "session-1", "uuid-1", first_ts)
    assert get_watermark(conn, "session-1") == ("uuid-1", first_ts)

    set_watermark(conn, "session-1", "uuid-2", second_ts)
    assert get_watermark(conn, "session-1") == ("uuid-2", second_ts)


def test_connection_has_wal_and_row_factory(tmp_path):
    db = open_memory_db(tmp_path / "memory.db")
    try:
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode == "wal"
        assert db.row_factory is sqlite3.Row
    finally:
        db.close()
