from __future__ import annotations

import json
from pathlib import Path

import pytest

from engram.nightly import run_nightly


@pytest.mark.asyncio
async def test_nightly_failure_writes_heartbeat_log_and_failure_dm(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    posts: list[str] = []

    async def synthesize() -> dict[str, object]:
        raise RuntimeError("boom")

    async def failure_dm(text: str) -> None:
        posts.append(text)

    result = await run_nightly(
        home=home,
        logs_dir=home / "logs",
        synthesize=synthesize,
        failure_dm=failure_dm,
    )

    assert result.exit_code == 1
    heartbeat = json.loads(result.heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["started_at"]
    assert heartbeat["completed_at"] is None
    assert heartbeat["phase_reached"] == "synthesize"
    assert heartbeat["exit_code"] == 1
    assert heartbeat["cost_usd"] == 0.0
    assert heartbeat["channels_covered"] == 0
    assert heartbeat["error_msg"] == "RuntimeError: boom"
    assert posts == [
        f"⚠️ Engram nightly FAILED at phase=synthesize, exit=1. Logs: `{result.log_path}`."
    ]

    nightly_logs = sorted((home / "logs").glob("nightly-*.jsonl"))
    bridge_logs = sorted((home / "logs").glob("engram-*.jsonl"))
    assert nightly_logs
    assert bridge_logs == []
    assert any("nightly.failed" in line for line in nightly_logs[-1].read_text().splitlines())


@pytest.mark.asyncio
async def test_nightly_success_records_completion_cost_and_channels(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    posts: list[str] = []

    async def synthesize() -> dict[str, object]:
        return {"cost_usd": 0.1234, "channels_covered": 3}

    async def failure_dm(text: str) -> None:
        posts.append(text)

    result = await run_nightly(
        home=home,
        logs_dir=home / "logs",
        synthesize=synthesize,
        failure_dm=failure_dm,
    )

    assert result.exit_code == 0
    heartbeat = json.loads(result.heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["started_at"]
    assert heartbeat["completed_at"]
    assert heartbeat["phase_reached"] == "synthesize"
    assert heartbeat["exit_code"] == 0
    assert heartbeat["cost_usd"] == pytest.approx(0.1234)
    assert heartbeat["channels_covered"] == 3
    assert heartbeat["error_msg"] is None
    assert posts == []
