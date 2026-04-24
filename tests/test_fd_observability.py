from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path

import pytest

from engram import main, runtime
from engram.doctor import CheckStatus, check_fd_pressure
from engram.router import Router


def _bridge_snapshot(
    in_use: int,
    *,
    soft_limit: int = 256,
    hard_limit: int = 1024,
    pid: int = 64900,
) -> dict[str, object]:
    return {
        "bridge": {
            "pid": pid,
            "fds": {
                "in_use": in_use,
                "soft_limit": soft_limit,
                "hard_limit": hard_limit,
            },
        }
    }


async def _run_runtime_snapshot_loop(
    monkeypatch: pytest.MonkeyPatch,
    snapshots: list[dict[str, object]],
    *,
    owner_alert=None,
) -> tuple[list[str], int]:
    alerts: list[str] = []
    processed = 0
    iterator = iter(snapshots)

    async def fake_write_runtime_snapshot(**_kwargs):
        nonlocal processed
        try:
            snapshot = next(iterator)
        except StopIteration as exc:
            raise asyncio.CancelledError from exc
        processed += 1
        return snapshot

    async def fake_sleep(_interval: float) -> None:
        return None

    async def collecting_owner_alert(text: str) -> None:
        alerts.append(text)

    monkeypatch.setattr(main, "write_runtime_snapshot", fake_write_runtime_snapshot)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main._runtime_snapshot_loop(
            state_dir=Path("/tmp"),
            router=Router(),
            cost_db=None,
            interval_seconds=0,
            owner_alert=owner_alert or collecting_owner_alert,
            fd_snapshots_enabled=False,
        )

    return alerts, processed


@pytest.mark.asyncio
async def test_runtime_snapshot_loop_warns_after_two_consecutive_threshold_crossings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts, _ = await _run_runtime_snapshot_loop(
        monkeypatch,
        [_bridge_snapshot(149), _bridge_snapshot(150), _bridge_snapshot(150)],
    )

    assert alerts == [
        "⚠️ Engram FD pressure: 150 / 256 in use. Monotonic growth may indicate "
        "a resource leak.\nRun `lsof -p 64900` to inspect. See GRO-481 for prior "
        "incident pattern."
    ]


@pytest.mark.asyncio
async def test_runtime_snapshot_loop_ignores_single_fd_spike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts, _ = await _run_runtime_snapshot_loop(
        monkeypatch,
        [_bridge_snapshot(149), _bridge_snapshot(150), _bridge_snapshot(149)],
    )

    assert alerts == []


@pytest.mark.asyncio
async def test_runtime_snapshot_loop_rearms_warn_after_fd_usage_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts, _ = await _run_runtime_snapshot_loop(
        monkeypatch,
        [
            _bridge_snapshot(150),
            _bridge_snapshot(150),
            _bridge_snapshot(160),
            _bridge_snapshot(149),
            _bridge_snapshot(150),
            _bridge_snapshot(150),
        ],
    )

    assert len(alerts) == 2
    assert all(message.startswith("⚠️ Engram FD pressure") for message in alerts)


@pytest.mark.asyncio
async def test_runtime_snapshot_loop_sends_critical_alert_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts, _ = await _run_runtime_snapshot_loop(
        monkeypatch,
        [
            _bridge_snapshot(150),
            _bridge_snapshot(150),
            _bridge_snapshot(200),
            _bridge_snapshot(200),
        ],
    )

    assert len(alerts) == 2
    assert alerts[0].startswith("⚠️ Engram FD pressure")
    assert alerts[1].startswith("🚨 Engram FD pressure CRITICAL")


def test_write_fd_snapshot_writes_expected_jsonl_record(tmp_path: Path) -> None:
    record = runtime.write_fd_snapshot(
        log_dir=tmp_path,
        pid=64900,
        now=datetime.datetime(2026, 4, 24, 2, 0, tzinfo=datetime.UTC),
        runner=lambda _pid: "\n".join(
            [
                "COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME",
                "Python  64900  ey  10u    REG    1,4      128  111 /Users/ey/.engram/cost.db",
                "Python  64900  ey  11u    REG    1,4      256  222 /Users/ey/.engram/logs/bridge.jsonl",
                "Python  64900  ey  12u   IPv4    0,0        0  333 TCP edge.slack.com:https->127.0.0.1:51515",
                "Python  64900  ey  13u    CHR    3,2        0  444 /dev/null",
                "Python  64900  ey  cwd    DIR    1,4      512  555 /Users/ey",
            ]
        ),
    )

    assert record == {
        "ts": "2026-04-24T02:00:00Z",
        "pid": 64900,
        "total_fds": 4,
        "by_type": {"REG": 2, "IPv4": 1, "CHR": 1},
        "by_path_pattern": {
            "cost.db": 1,
            "*.jsonl (logs)": 1,
            "slack.com TCP": 1,
            "other": 1,
        },
    }

    snapshot_path = tmp_path / "fd-snapshots" / "2026-04-24.jsonl"
    assert snapshot_path.exists()
    lines = snapshot_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_prune_fd_snapshot_files_removes_entries_older_than_30_days(tmp_path: Path) -> None:
    snapshot_dir = runtime.fd_snapshot_dir(tmp_path)
    snapshot_dir.mkdir(parents=True)
    old_file = snapshot_dir / "2026-03-23.jsonl"
    keep_file = snapshot_dir / "2026-03-24.jsonl"
    old_file.write_text("{}\n", encoding="utf-8")
    keep_file.write_text("{}\n", encoding="utf-8")

    removed = runtime.prune_fd_snapshot_files(
        log_dir=tmp_path,
        now=datetime.datetime(2026, 4, 23, 9, 0, tzinfo=datetime.UTC),
    )

    assert removed == 1
    assert not old_file.exists()
    assert keep_file.exists()


@pytest.mark.asyncio
async def test_runtime_snapshot_loop_ignores_alert_delivery_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts: list[str] = []

    async def failing_owner_alert(text: str) -> None:
        attempts.append(text)
        raise RuntimeError("slack unavailable")

    with caplog.at_level("WARNING", logger="engram.main"):
        _, processed = await _run_runtime_snapshot_loop(
            monkeypatch,
            [_bridge_snapshot(150), _bridge_snapshot(150), _bridge_snapshot(149)],
            owner_alert=failing_owner_alert,
        )

    assert len(attempts) == 1
    assert processed == 3
    assert "engram.fd_pressure_alert_failed" in caplog.text


def test_doctor_fd_pressure_uses_thresholds_and_top_snapshot_patterns(tmp_path: Path) -> None:
    check = check_fd_pressure(
        tmp_path,
        usage_reader=lambda: {"in_use": 205, "soft_limit": 256, "hard_limit": 1024},
        snapshot_reader=lambda _path: {
            "by_path_pattern": {
                "other": 23,
                "cost.db": 4,
                "*.jsonl (logs)": 3,
                "memory.db": 2,
            }
        },
    )

    assert check.status == CheckStatus.FAIL
    assert check.details["top_path_patterns"] == [
        {"pattern": "cost.db", "count": 4},
        {"pattern": "*.jsonl (logs)", "count": 3},
        {"pattern": "memory.db", "count": 2},
    ]
    assert "205 FDs in use; pressure is critical." in check.message
    assert "Soft limit 256." in check.message
    assert "cost.db=4" in check.message
    assert "*.jsonl (logs)=3" in check.message
    assert "memory.db=2" in check.message


@pytest.mark.parametrize(
    ("in_use", "expected_status"),
    [
        (99, CheckStatus.PASS),
        (100, CheckStatus.WARN),
        (200, CheckStatus.FAIL),
    ],
)
def test_doctor_fd_pressure_thresholds(
    tmp_path: Path,
    in_use: int,
    expected_status: CheckStatus,
) -> None:
    check = check_fd_pressure(
        tmp_path,
        usage_reader=lambda: {"in_use": in_use, "soft_limit": 256, "hard_limit": 1024},
        snapshot_reader=lambda _path: None,
    )

    assert check.status == expected_status
