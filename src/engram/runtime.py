"""Runtime health marker and status snapshot helpers."""
from __future__ import annotations

import datetime
import json
import logging
import os
import resource
from pathlib import Path
from typing import Any

from engram.costs import CostDatabase
from engram.mcp_tools import memory_tool_metrics
from engram.router import Router, SessionState
from engram.telemetry import write_json

log = logging.getLogger(__name__)
_FD_WARNING_THRESHOLD = 0.5
_fd_warning_active = False


def pid_path(state_dir: Path) -> Path:
    return state_dir / "engram.pid"


def health_path(state_dir: Path) -> Path:
    return state_dir / "health.json"


def status_path(state_dir: Path) -> Path:
    return state_dir / "status.json"


async def write_runtime_snapshot(
    *,
    state_dir: Path,
    router: Router,
    cost_db: CostDatabase | None,
) -> dict[str, Any]:
    """Write health.json and status.json for CLI probes."""
    state_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.UTC).isoformat()
    pid = os.getpid()
    fd_usage = fd_usage_snapshot()
    pid_path(state_dir).write_text(str(pid), encoding="utf-8")
    health = {"ok": True, "pid": pid, "ts": now}
    if fd_usage is not None:
        health["fds"] = fd_usage
    write_json(health_path(state_dir), health)

    channels = []
    for session in router.list_sessions():
        channels.append(await _channel_snapshot(session, cost_db))

    snapshot = {
        "bridge": {"up": True, "pid": pid, "ts": now},
        "channels": channels,
        "memory": memory_tool_metrics(),
    }
    if fd_usage is not None:
        snapshot["bridge"]["fds"] = fd_usage
        _warn_if_fd_usage_high(fd_usage)
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
