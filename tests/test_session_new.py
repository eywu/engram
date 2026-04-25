"""Unit tests for archive_session_transcript and the /engram new flow."""
from __future__ import annotations

import re
from pathlib import Path

from engram.agent import _claude_cli_jsonl_for, archive_session_transcript
from engram.router import derive_session_id

# ── archive_session_transcript ───────────────────────────────────────────────


def test_archive_returns_none_when_no_transcript(tmp_path: Path) -> None:
    """archive_session_transcript returns None when the JSONL file is absent."""
    session_id = derive_session_id("C07TEST001")
    result = archive_session_transcript(session_id, cwd=tmp_path)
    assert result is None


def test_archive_renames_file_with_timestamp(tmp_path: Path, monkeypatch) -> None:
    """archive_session_transcript renames the JSONL to .archived-<UTC-timestamp>."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))

    session_id = derive_session_id("C07TEST001")
    jsonl_path = _claude_cli_jsonl_for(session_id, cwd=tmp_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text('{"type":"test"}\n', encoding="utf-8")
    assert jsonl_path.exists()

    archived = archive_session_transcript(session_id, cwd=tmp_path)

    assert archived is not None
    assert not jsonl_path.exists(), "original file should be gone"
    assert archived.exists(), "archived file should exist"
    # Name format: <session_id>.jsonl.archived-<timestamp>
    assert archived.name.startswith(f"{session_id}.jsonl.archived-")
    ts_part = archived.name.split(".archived-")[-1]
    # Timestamp is YYYYmmddTHHMMSSZ
    assert re.fullmatch(r"\d{8}T\d{6}Z", ts_part), f"unexpected timestamp: {ts_part!r}"


def test_archive_preserves_content(tmp_path: Path, monkeypatch) -> None:
    """Archived file must have the same content as the original."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))

    session_id = derive_session_id("C07TEST002")
    jsonl_path = _claude_cli_jsonl_for(session_id, cwd=tmp_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    content = '{"type":"text","text":"hello"}\n'
    jsonl_path.write_text(content, encoding="utf-8")

    archived = archive_session_transcript(session_id, cwd=tmp_path)

    assert archived is not None
    assert archived.read_text(encoding="utf-8") == content


def test_archive_is_idempotent_when_file_gone(tmp_path: Path, monkeypatch) -> None:
    """Second call returns None (file already archived by first call)."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))

    session_id = derive_session_id("C07TEST003")
    jsonl_path = _claude_cli_jsonl_for(session_id, cwd=tmp_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text("{}\n", encoding="utf-8")

    first = archive_session_transcript(session_id, cwd=tmp_path)
    assert first is not None
    second = archive_session_transcript(session_id, cwd=tmp_path)
    assert second is None
