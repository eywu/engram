"""Runtime health marker and status snapshot helpers."""
from __future__ import annotations

import datetime
import json
import logging
import os
import resource
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from engram.costs import CostDatabase
from engram.mcp_tools import memory_tool_metrics
from engram.router import Router, SessionState
from engram.telemetry import write_json

log = logging.getLogger(__name__)
_FD_WARNING_THRESHOLD = 0.5
_FD_SNAPSHOT_RETENTION_DAYS = 30
_fd_warning_active = False


def pid_path(state_dir: Path) -> Path:
    return state_dir / "engram.pid"


def health_path(state_dir: Path) -> Path:
    return state_dir / "health.json"


def status_path(state_dir: Path) -> Path:
    return state_dir / "status.json"


def fd_snapshot_dir(log_dir: Path) -> Path:
    return log_dir / "fd-snapshots"


async def write_runtime_snapshot(
    *,
    state_dir: Path,
    router: Router,
    cost_db: CostDatabase | None,
    fd_usage: dict[str, int | None] | None = None,
    fd_high_water: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write health.json and status.json for CLI probes."""
    state_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.UTC).isoformat()
    pid = os.getpid()
    current_fd_usage = fd_usage if fd_usage is not None else fd_usage_snapshot()
    pid_path(state_dir).write_text(str(pid), encoding="utf-8")
    health = {"ok": True, "pid": pid, "ts": now}
    if current_fd_usage is not None:
        health["fds"] = _fd_payload(current_fd_usage, fd_high_water)
    write_json(health_path(state_dir), health)

    channels = []
    for session in router.list_sessions():
        channels.append(await _channel_snapshot(session, cost_db))

    snapshot: dict[str, Any] = {
        "bridge": {"up": True, "pid": pid, "ts": now},
        "channels": channels,
        "memory": memory_tool_metrics(),
    }
    if current_fd_usage is not None:
        snapshot["bridge"]["fds"] = _fd_payload(current_fd_usage, fd_high_water)
        _warn_if_fd_usage_high(current_fd_usage)
    write_json(status_path(state_dir), snapshot)
    return snapshot


async def _channel_snapshot(
    session: SessionState,
    cost_db: CostDatabase | None,
) -> dict[str, Any]:
    channel: dict[str, Any] = {
        "channel_id": session.channel_id,
        "label": session.label(),
        "live": session.agent_client is not None,
        "turn_count": session.turn_count,
        "rate_limit": session.rate_limit_state(),
        "mcp_status": None,
        "context_usage": None,
    }
    if cost_db is not None:
        latest = cost_db.latest_rate_limit(session.channel_id)
        if latest.get("status") != "allowed":
            channel["rate_limit"] = latest

    if session.agent_client is None:
        return channel

    async with session.agent_lock:
        client = session.agent_client
        if client is None:
            return channel
        try:
            mcp_status = await client.get_mcp_status()
            channel["mcp_status"] = _jsonable(mcp_status)
            await _reconnect_failed_mcp_servers(client, mcp_status, session)
        except Exception as e:
            channel["mcp_status"] = {
                "error": f"{type(e).__name__}: {e}",
            }
            log.warning(
                "runtime.mcp_status_failed session=%s error_class=%s",
                session.label(),
                type(e).__name__,
                exc_info=True,
            )
        try:
            channel["context_usage"] = _jsonable(await client.get_context_usage())
        except Exception as e:
            channel["context_usage"] = {
                "error": f"{type(e).__name__}: {e}",
            }
            log.warning(
                "runtime.context_usage_failed session=%s error_class=%s",
                session.label(),
                type(e).__name__,
                exc_info=True,
            )
    return channel


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        pass
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        try:
            json.dumps(dumped)
            return dumped
        except TypeError:
            pass
    return str(value)


async def _reconnect_failed_mcp_servers(
    client: Any,
    mcp_status: Any,
    session: SessionState,
) -> None:
    servers = []
    if isinstance(mcp_status, dict):
        servers = list(mcp_status.get("mcpServers") or [])
    for server in servers:
        if not isinstance(server, dict):
            continue
        if server.get("status") != "failed":
            continue
        name = server.get("name")
        if not name:
            continue
        # GRO-555: respect the per-channel circuit-breaker ban list.
        # Without this, the runtime status snapshot's periodic retry loop
        # would re-trigger the very reconnect storm we just disabled at
        # the agent layer.
        if name in session.disabled_mcp_servers:
            log.debug(
                "runtime.mcp_reconnect_skipped_disabled session=%s server=%s",
                session.label(),
                name,
            )
            continue
        try:
            await client.reconnect_mcp_server(name)
        except Exception:
            log.warning(
                "runtime.mcp_reconnect_failed session=%s server=%s",
                session.label(),
                name,
                exc_info=True,
            )
        else:
            log.info(
                "runtime.mcp_reconnect_attempted session=%s server=%s",
                session.label(),
                name,
            )


def fd_usage_snapshot() -> dict[str, int | None] | None:
    in_use = _open_fd_count()
    if in_use is None:
        return None

    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    return {
        "in_use": in_use,
        "soft_limit": None if soft_limit == resource.RLIM_INFINITY else int(soft_limit),
        "hard_limit": None if hard_limit == resource.RLIM_INFINITY else int(hard_limit),
    }


def _open_fd_count() -> int | None:
    for path in ("/proc/self/fd", "/dev/fd"):
        if not os.path.isdir(path):
            continue
        try:
            return len(os.listdir(path))
        except OSError:
            continue
    return None


def _warn_if_fd_usage_high(fd_usage: dict[str, int | None]) -> None:
    global _fd_warning_active

    in_use = fd_usage.get("in_use")
    soft_limit = fd_usage.get("soft_limit")
    if in_use is None or soft_limit is None or soft_limit <= 0:
        return

    over_threshold = in_use > (soft_limit * _FD_WARNING_THRESHOLD)
    if over_threshold and not _fd_warning_active:
        log.warning(
            "runtime.fd_usage_high in_use=%s soft_limit=%s hard_limit=%s threshold_pct=%s",
            in_use,
            soft_limit,
            fd_usage.get("hard_limit"),
            int(_FD_WARNING_THRESHOLD * 100),
        )
        _fd_warning_active = True
        return

    if not over_threshold:
        _fd_warning_active = False


def _fd_payload(
    current: dict[str, int | None],
    high_water: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(current)
    if high_water is None and current.get("in_use") is not None:
        payload["high_water"] = {
            "in_use": current.get("in_use"),
            "soft_limit": current.get("soft_limit"),
            "hard_limit": current.get("hard_limit"),
            "window_started_at": None,
            "observed_at": None,
        }
        return payload
    if high_water is not None:
        payload["high_water"] = dict(high_water)
    return payload


def write_fd_snapshot(
    *,
    log_dir: Path,
    pid: int | None = None,
    now: datetime.datetime | None = None,
    runner: Any = None,
) -> dict[str, Any] | None:
    snapshot_pid = pid or os.getpid()
    timestamp = now or datetime.datetime.now(datetime.UTC)
    run = runner or _run_lsof
    try:
        output = run(snapshot_pid)
    except FileNotFoundError:
        log.warning("runtime.fd_snapshot_unavailable reason=lsof_missing")
        return None
    except Exception:
        log.warning("runtime.fd_snapshot_failed pid=%s", snapshot_pid, exc_info=True)
        return None

    total_fds, by_type, by_path_pattern = _parse_lsof_output(output)
    record = {
        "ts": _iso_utc(timestamp),
        "pid": snapshot_pid,
        "total_fds": total_fds,
        "by_type": dict(by_type),
        "by_path_pattern": dict(by_path_pattern),
    }
    target_dir = fd_snapshot_dir(log_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{timestamp:%Y-%m-%d}.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")
    return record


def prune_fd_snapshot_files(
    *,
    log_dir: Path,
    now: datetime.datetime | None = None,
    retention_days: int = _FD_SNAPSHOT_RETENTION_DAYS,
) -> int:
    removed = 0
    cutoff_date = (now or datetime.datetime.now(datetime.UTC)).date() - datetime.timedelta(
        days=retention_days
    )
    for path in fd_snapshot_dir(log_dir).glob("*.jsonl"):
        file_date = _fd_snapshot_file_date(path)
        if file_date is None or file_date >= cutoff_date:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def read_latest_fd_snapshot(log_dir: Path) -> dict[str, Any] | None:
    snapshot_dir = fd_snapshot_dir(log_dir)
    if not snapshot_dir.exists():
        return None
    for path in sorted(snapshot_dir.glob("*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return None


def _run_lsof(pid: int) -> str:
    completed = subprocess.run(
        ["lsof", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "lsof failed")
    return completed.stdout


def _parse_lsof_output(output: str) -> tuple[int, Counter[str], Counter[str]]:
    by_type: Counter[str] = Counter()
    by_path_pattern: Counter[str] = Counter()
    total_fds = 0
    for line in output.splitlines():
        if not line or line.startswith("COMMAND"):
            continue
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        fd = parts[3]
        if not fd or not fd[0].isdigit():
            continue
        fd_type = parts[4]
        name = parts[8]
        total_fds += 1
        by_type[fd_type] += 1
        by_path_pattern[_fd_path_pattern(fd_type, name)] += 1
    if not by_path_pattern:
        by_path_pattern["other"] = 0
    return total_fds, by_type, by_path_pattern


def _fd_path_pattern(fd_type: str, name: str) -> str:
    normalized = name.lower()
    basename = Path(name).name.lower() if "/" in name else name.lower()
    if basename.endswith(".db"):
        return basename
    if basename.endswith(".jsonl") and "logs" in normalized:
        return "*.jsonl (logs)"
    if fd_type in {"IPv4", "IPv6"}:
        for host in ("slack.com", "anthropic.com", "googleapis.com"):
            if host in normalized:
                return f"{host} TCP"
    return "other"


def _fd_snapshot_file_date(path: Path) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(path.stem)
    except ValueError:
        return None


def _iso_utc(value: datetime.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
