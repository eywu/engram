"""GRO-392 SDK hook ingestion tests."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from engram import memory
from engram.hooks import make_hooks, pending_compactions
from engram.router import SessionState


@pytest.fixture(autouse=True)
def _clear_pending_compactions():
    pending_compactions.clear()
    yield
    pending_compactions.clear()


@dataclass
class _FakeClient:
    messages: list[dict[str, Any]]
    raise_on_fetch: bool = False
    calls: list[dict[str, str | None]] | None = None

    async def get_session_messages(
        self,
        *,
        session_id: str,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.raise_on_fetch:
            raise RuntimeError("boom")
        if self.calls is not None:
            self.calls.append({"session_id": session_id, "since": since})
        if since is None:
            return list(self.messages)
        for index, message in enumerate(self.messages):
            if message["message_uuid"] == since:
                return list(self.messages[index + 1 :])
        return list(self.messages)


def _db(tmp_path: Path) -> sqlite3.Connection:
    return memory.connect(tmp_path / "memory.db")


def _session(client: _FakeClient) -> SessionState:
    session = SessionState(channel_id="C07TEST123")
    session.agent_client = client
    return session


def _messages() -> list[dict[str, Any]]:
    return [
        {
            "ts": "2026-04-21T10:00:00Z",
            "role": "user",
            "message_uuid": "u1",
            "parent_uuid": None,
            "text": "hello",
        },
        {
            "ts": "2026-04-21T10:00:01Z",
            "role": "assistant",
            "message_uuid": "a1",
            "parent_uuid": "u1",
            "text": "hi",
        },
        {
            "ts": "2026-04-21T10:00:02Z",
            "role": "user",
            "message_uuid": "u2",
            "parent_uuid": "a1",
            "text": "again",
        },
    ]


async def _fire_stop(session: SessionState, conn: sqlite3.Connection) -> None:
    hook = make_hooks(session, conn)["Stop"][0].hooks[0]
    await hook(
        {"hook_event_name": "Stop", "session_id": session.session_id},
        None,
        {"signal": None},
    )


async def _fire_precompact(
    session: SessionState,
    conn: sqlite3.Connection,
    *,
    custom_instructions: str | None = "keep project facts",
) -> None:
    hook = make_hooks(session, conn)["PreCompact"][0].hooks[0]
    await hook(
        {
            "hook_event_name": "PreCompact",
            "session_id": session.session_id,
            "trigger": "manual",
            "custom_instructions": custom_instructions,
        },
        None,
        {"signal": None},
    )


@pytest.mark.asyncio
async def test_stop_hook_ingests_new_messages(tmp_path: Path):
    conn = _db(tmp_path)
    session = _session(_FakeClient(_messages()))

    await _fire_stop(session, conn)

    rows = conn.execute(
        "SELECT role, message_uuid, parent_uuid, text FROM transcripts ORDER BY id"
    ).fetchall()
    assert [row["message_uuid"] for row in rows] == ["u1", "a1", "u2"]
    assert [row["role"] for row in rows] == ["user", "assistant", "user"]
    assert rows[1]["parent_uuid"] == "u1"
    assert rows[2]["text"] == "again"


@pytest.mark.asyncio
async def test_stop_hook_idempotent_on_duplicate_fire(tmp_path: Path):
    conn = _db(tmp_path)
    calls: list[dict[str, str | None]] = []
    session = _session(_FakeClient(_messages(), calls=calls))

    await _fire_stop(session, conn)
    await _fire_stop(session, conn)

    count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    assert count == 3
    assert calls[1]["since"] == "u2"


@pytest.mark.asyncio
async def test_stop_hook_advances_watermark(tmp_path: Path):
    conn = _db(tmp_path)
    session = _session(_FakeClient(_messages()))

    await _fire_stop(session, conn)

    assert memory.get_watermark(conn, session.session_id) == "u2"


@pytest.mark.asyncio
async def test_stop_hook_across_restart(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    conn = memory.connect(db_path)
    session = _session(_FakeClient(_messages()))
    await _fire_stop(session, conn)
    conn.close()

    conn = memory.connect(db_path)
    restarted = _session(
        _FakeClient(
            [
                *_messages(),
                {
                    "ts": "2026-04-21T10:00:03Z",
                    "role": "assistant",
                    "message_uuid": "a2",
                    "parent_uuid": "u2",
                    "text": "new only",
                },
            ]
        )
    )
    restarted.session_id = session.session_id

    await _fire_stop(restarted, conn)

    rows = conn.execute(
        "SELECT message_uuid FROM transcripts ORDER BY id"
    ).fetchall()
    assert [row["message_uuid"] for row in rows] == ["u1", "a1", "u2", "a2"]


@pytest.mark.asyncio
async def test_precompact_without_summary_message_no_summary_row(tmp_path: Path):
    conn = _db(tmp_path)
    session = _session(_FakeClient([]))

    await _fire_precompact(session, conn)
    await _fire_stop(session, conn)

    count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    assert count == 0
    assert session.session_id in pending_compactions


@pytest.mark.asyncio
async def test_precompact_then_summary_message_promotes_to_summary_row(
    tmp_path: Path,
):
    conn = _db(tmp_path)
    session = _session(
        _FakeClient(
            [
                {
                    "ts": "2026-04-21T10:00:04Z",
                    "role": "summary",
                    "message_uuid": "s1",
                    "parent_uuid": "a1",
                    "text": "A compacted memory summary.",
                }
            ]
        )
    )

    await _fire_precompact(
        session,
        conn,
        custom_instructions="preserve names and preferences",
    )
    await _fire_stop(session, conn)

    row = conn.execute(
        """
        SELECT trigger, summary_text, custom_instructions, source_message_uuid
        FROM summaries
        """
    ).fetchone()
    assert row["trigger"] == "compact"
    assert row["summary_text"] == "A compacted memory summary."
    assert row["custom_instructions"] == "preserve names and preferences"
    assert row["source_message_uuid"] == "s1"
    assert session.session_id not in pending_compactions


@pytest.mark.asyncio
async def test_hook_exception_does_not_raise(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    conn = _db(tmp_path)
    session = _session(_FakeClient([], raise_on_fetch=True))

    await _fire_stop(session, conn)

    assert "hooks.stop_failed" in caplog.text
