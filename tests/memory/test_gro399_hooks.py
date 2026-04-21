from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SessionMessage

import engram.memory_hooks as memory_hooks
from engram.memory import get_watermark, open_memory_db, set_watermark
from engram.memory_hooks import make_memory_hooks, pending_compactions
from engram.router import Router, SessionState

CHANNEL_ID = "C07TEST123"
BASE_TS = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_pending_compactions():
    pending_compactions.clear()
    yield
    pending_compactions.clear()


@pytest.fixture
def memory_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "memory.db"

    def _open_memory_db(path: Path | None = None) -> sqlite3.Connection:
        return open_memory_db(path or db_path)

    monkeypatch.setattr(memory_hooks, "open_memory_db", _open_memory_db)
    return db_path


@pytest.fixture
async def router_session() -> tuple[Router, SessionState]:
    router = Router()
    session = await router.get(CHANNEL_ID)
    return router, session


def _stop_hook(router: Router):
    return make_memory_hooks(router)[0].hooks[0]


def _precompact_hook(router: Router):
    return make_memory_hooks(router)[1].hooks[0]


def _hook_input(session_id: str, transcript_path: Path, tmp_path: Path) -> dict[str, Any]:
    return {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "stop_hook_active": False,
    }


def _precompact_input(
    session_id: str,
    transcript_path: Path,
    tmp_path: Path,
    *,
    trigger: str = "manual",
    custom_instructions: str | None = "keep preferences",
) -> dict[str, Any]:
    return {
        "hook_event_name": "PreCompact",
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "trigger": trigger,
        "custom_instructions": custom_instructions,
    }


def _message(
    session_id: str,
    uuid: str,
    *,
    role: str = "user",
    content: object | None = None,
) -> SessionMessage:
    return SessionMessage(
        type="assistant" if role in {"assistant", "summary"} else "user",
        uuid=uuid,
        session_id=session_id,
        message={
            "role": role,
            "content": f"text {uuid}" if content is None else content,
        },
    )


def _write_jsonl(path: Path, uuids: list[str]) -> None:
    lines = []
    parent_uuid = None
    for index, uuid in enumerate(uuids):
        lines.append(
            json.dumps(
                {
                    "uuid": uuid,
                    "timestamp": (BASE_TS + timedelta(seconds=index)).isoformat(),
                    "parentUuid": parent_uuid,
                }
            )
        )
        parent_uuid = uuid
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fetch_all(db_path: Path, query: str) -> list[sqlite3.Row]:
    conn = open_memory_db(db_path)
    try:
        return list(conn.execute(query).fetchall())
    finally:
        conn.close()


def _fetch_one(db_path: Path, query: str) -> sqlite3.Row:
    conn = open_memory_db(db_path)
    try:
        row = conn.execute(query).fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


def _seed_watermark(db_path: Path, session_id: str, message_uuid: str) -> None:
    conn = open_memory_db(db_path)
    try:
        set_watermark(conn, session_id, message_uuid, BASE_TS)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_precompact_hook_records_pending_state(
    router_session: tuple[Router, SessionState],
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"

    await _precompact_hook(router)(
        _precompact_input(
            session.session_id,
            transcript_path,
            tmp_path,
            trigger="auto",
            custom_instructions="keep project preferences",
        ),
        None,
        {},
    )

    pending = pending_compactions[session.session_id]
    assert pending["trigger"] == "auto"
    assert pending["custom_instructions"] == "keep project preferences"
    assert isinstance(pending["ts"], datetime)


@pytest.mark.asyncio
async def test_precompact_hook_without_summary_no_summary_row_yet(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"

    await _precompact_hook(router)(
        _precompact_input(session.session_id, transcript_path, tmp_path),
        None,
        {},
    )

    row = _fetch_one(memory_db_path, "SELECT COUNT(*) AS count FROM summaries")
    assert row["count"] == 0
    assert session.session_id in pending_compactions


@pytest.mark.asyncio
async def test_stop_hook_ingests_new_messages(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"
    _write_jsonl(transcript_path, ["u1", "u2", "u3"])
    messages = [_message(session.session_id, uuid) for uuid in ["u1", "u2", "u3"]]
    monkeypatch.setattr(memory_hooks, "get_session_messages", lambda **_kwargs: messages)

    await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})

    rows = _fetch_all(memory_db_path, "SELECT message_uuid FROM transcripts ORDER BY ts")
    assert [row["message_uuid"] for row in rows] == ["u1", "u2", "u3"]
    conn = open_memory_db(memory_db_path)
    try:
        assert get_watermark(conn, session.session_id)[0] == "u3"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_stop_hook_idempotent_on_duplicate_fire(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"
    _write_jsonl(transcript_path, ["u1", "u2", "u3"])
    messages = [_message(session.session_id, uuid) for uuid in ["u1", "u2", "u3"]]
    monkeypatch.setattr(memory_hooks, "get_session_messages", lambda **_kwargs: messages)

    await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})
    await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})

    row = _fetch_one(memory_db_path, "SELECT COUNT(*) AS count FROM transcripts")
    assert row["count"] == 3


@pytest.mark.asyncio
async def test_stop_hook_advances_watermark(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"
    _write_jsonl(transcript_path, ["u1", "u2"])
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: [_message(session.session_id, "u1"), _message(session.session_id, "u2")],
    )

    await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})

    conn = open_memory_db(memory_db_path)
    try:
        assert get_watermark(conn, session.session_id) == ("u2", BASE_TS + timedelta(seconds=1))
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_stop_hook_across_restart(
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    router1 = Router()
    session1 = await router1.get(CHANNEL_ID)
    transcript_path = tmp_path / "session.jsonl"
    _write_jsonl(transcript_path, ["u1", "u2", "u3"])
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: [_message(session1.session_id, uuid) for uuid in ["u1", "u2", "u3"]],
    )
    await _stop_hook(router1)(_hook_input(session1.session_id, transcript_path, tmp_path), None, {})

    router2 = Router()
    session2 = await router2.get(CHANNEL_ID)
    _write_jsonl(transcript_path, ["u1", "u2", "u3", "u4"])
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: [_message(session2.session_id, uuid) for uuid in ["u1", "u2", "u3", "u4"]],
    )
    await _stop_hook(router2)(_hook_input(session2.session_id, transcript_path, tmp_path), None, {})

    rows = _fetch_all(memory_db_path, "SELECT message_uuid FROM transcripts ORDER BY ts")
    assert [row["message_uuid"] for row in rows] == ["u1", "u2", "u3", "u4"]


@pytest.mark.asyncio
async def test_stop_hook_slices_from_watermark(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"
    uuids = ["u1", "u2", "u3", "u4", "u5"]
    _write_jsonl(transcript_path, uuids)
    _seed_watermark(memory_db_path, session.session_id, "u3")
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: [_message(session.session_id, uuid) for uuid in uuids],
    )

    await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})

    rows = _fetch_all(memory_db_path, "SELECT message_uuid FROM transcripts ORDER BY ts")
    assert [row["message_uuid"] for row in rows] == ["u4", "u5"]


@pytest.mark.asyncio
async def test_stop_hook_watermark_not_found_ingests_all(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"
    _write_jsonl(transcript_path, ["u1", "u2"])
    _seed_watermark(memory_db_path, session.session_id, "missing-uuid")
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: [_message(session.session_id, "u1"), _message(session.session_id, "u2")],
    )

    with caplog.at_level("WARNING", logger="engram.telemetry.memory_hooks"):
        await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})

    assert "memory.stop_watermark_not_found" in caplog.text
    row = _fetch_one(memory_db_path, "SELECT COUNT(*) AS count FROM transcripts")
    assert row["count"] == 2


@pytest.mark.asyncio
async def test_stop_hook_unknown_session_is_noop(
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    router = Router()
    transcript_path = tmp_path / "session.jsonl"
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: pytest.fail("unknown sessions must not fetch messages"),
    )

    with caplog.at_level("WARNING", logger="engram.telemetry.memory_hooks"):
        await _stop_hook(router)(_hook_input("unknown-session", transcript_path, tmp_path), None, {})

    assert "memory.stop_unknown_session" in caplog.text
    row = _fetch_one(memory_db_path, "SELECT COUNT(*) AS count FROM transcripts")
    assert row["count"] == 0


@pytest.mark.asyncio
async def test_precompact_then_stop_promotes_to_summary(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"
    _write_jsonl(transcript_path, ["summary-1"])
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: [
            _message(
                session.session_id,
                "summary-1",
                role="summary",
                content="Summary: the project prefers concise replies.",
            )
        ],
    )

    await _precompact_hook(router)(
        _precompact_input(
            session.session_id,
            transcript_path,
            tmp_path,
            custom_instructions="keep project preferences",
        ),
        None,
        {},
    )
    await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})

    row = _fetch_one(
        memory_db_path,
        "SELECT trigger, custom_instructions, summary_text FROM summaries",
    )
    assert row["trigger"] == "compact"
    assert row["custom_instructions"] == "keep project preferences"
    assert row["summary_text"]
    assert session.session_id not in pending_compactions


@pytest.mark.asyncio
async def test_precompact_then_stop_without_summary_message_keeps_pending(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"
    _write_jsonl(transcript_path, ["u1"])
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: [_message(session.session_id, "u1", content="ordinary reply")],
    )

    await _precompact_hook(router)(
        _precompact_input(session.session_id, transcript_path, tmp_path),
        None,
        {},
    )
    await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})

    row = _fetch_one(memory_db_path, "SELECT COUNT(*) AS count FROM summaries")
    assert row["count"] == 0
    assert session.session_id in pending_compactions


@pytest.mark.asyncio
async def test_hook_exception_is_swallowed(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"

    def _raise(**_kwargs):
        raise RuntimeError("sdk unavailable")

    monkeypatch.setattr(memory_hooks, "get_session_messages", _raise)

    with caplog.at_level("ERROR", logger="engram.telemetry.memory_hooks"):
        output = await _stop_hook(router)(
            _hook_input(session.session_id, transcript_path, tmp_path),
            None,
            {},
        )

    assert output["continue_"] is True
    assert "memory.stop_failed" in caplog.text
    row = _fetch_one(memory_db_path, "SELECT COUNT(*) AS count FROM transcripts")
    assert row["count"] == 0


@pytest.mark.asyncio
async def test_message_text_extraction_user_role(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"
    _write_jsonl(transcript_path, ["u1"])
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: [_message(session.session_id, "u1", role="user", content="hello")],
    )

    await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})

    row = _fetch_one(memory_db_path, "SELECT role, text FROM transcripts")
    assert row["role"] == "user"
    assert row["text"] == "hello"


@pytest.mark.asyncio
async def test_message_text_extraction_assistant_blocks(
    router_session: tuple[Router, SessionState],
    memory_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    router, session = router_session
    transcript_path = tmp_path / "session.jsonl"
    _write_jsonl(transcript_path, ["a1"])
    monkeypatch.setattr(
        memory_hooks,
        "get_session_messages",
        lambda **_kwargs: [
            _message(
                session.session_id,
                "a1",
                role="assistant",
                content=[
                    {"type": "text", "text": "Hi!"},
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {"type": "text", "text": " How are you?"},
                ],
            )
        ],
    )

    await _stop_hook(router)(_hook_input(session.session_id, transcript_path, tmp_path), None, {})

    row = _fetch_one(memory_db_path, "SELECT role, text FROM transcripts")
    assert row["role"] == "assistant"
    assert row["text"] == "Hi! How are you?"


def test_jsonl_metadata_enrichment(tmp_path: Path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "uuid": "u1",
                        "timestamp": "2026-04-21T12:00:00Z",
                        "parentUuid": None,
                    }
                ),
                json.dumps(
                    {
                        "uuid": "u2",
                        "timestamp": "2026-04-21T12:00:01Z",
                        "parentUuid": "u1",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    metadata = memory_hooks._parse_jsonl_for_metadata(str(transcript_path))

    assert metadata["u1"]["ts"] == datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    assert metadata["u1"]["parent_uuid"] is None
    assert metadata["u2"]["ts"] == datetime(2026, 4, 21, 12, 0, 1, tzinfo=UTC)
    assert metadata["u2"]["parent_uuid"] == "u1"
