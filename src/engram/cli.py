"""Engram CLI — `engram status`, `engram run`, `engram setup`."""
from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import typer
from rich import print as rprint
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from engram import __version__
from engram.cli_channels import app as channels_app
from engram.config import DEFAULT_CONFIG_PATH, EngramConfig, PathsConfig
from engram.costs import CostDatabase
from engram.manifest import ChannelManifest, ManifestError, load_manifest
from engram.mcp import resolve_team_mcp_servers
from engram.mcp_tools import (
    MEMORY_SEARCH_FULL_TOOL_NAMES,
    MEMORY_SEARCH_SERVER_NAME,
    memory_tool_metrics,
)
from engram.paths import contexts_dir, engram_home, nightly_heartbeat_path
from engram.runtime import health_path, pid_path, status_path
from engram.telemetry import process_exists, read_json

app = typer.Typer(
    name="engram",
    help="Personal AI agent for Slack.",
    no_args_is_help=True,
    add_completion=False,
)
scope_app = typer.Typer(
    name="scope",
    help="Audit per-channel scope and memory eligibility.",
    no_args_is_help=True,
)
app.add_typer(
    channels_app,
    name="channels",
    help="List and manage per-channel manifests.",
)
app.add_typer(
    scope_app,
    name="scope",
    help="Audit per-channel scope and memory eligibility.",
)
console = Console()


@app.command()
def version() -> None:
    """Print Engram version."""
    rprint(f"engram [bold]{__version__}[/bold]")


@app.command()
def status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show bridge health, live channels, memory counts, and rate limits."""
    snapshot = _build_status_snapshot()
    if json_output:
        typer.echo(json.dumps(snapshot, sort_keys=True))
        return

    rprint(f"[bold]Engram[/bold] version {__version__}")
    bridge = snapshot["bridge"]
    if bridge["up"]:
        rprint(f"[bold]Bridge[/bold] running (pid {bridge['pid']})")
    else:
        rprint("[bold]Bridge[/bold] not running")
    if snapshot.get("config_error"):
        rprint(f"[yellow]Config[/yellow] {snapshot['config_error']}")
    nightly = snapshot["nightly"]
    rprint(f"nightly: {nightly['summary']}")
    rprint()

    memory = snapshot["memory"]
    rprint("[bold]Memory[/bold]")
    rprint(
        f"  transcripts={memory['transcripts_count']} "
        f"summaries={memory['summaries_count']}"
    )
    rprint()

    table = Table(title="Channels")
    table.add_column("channel")
    table.add_column("live")
    table.add_column("rate limit")
    table.add_column("context")
    table.add_column("mcp")
    for channel in snapshot["channels"]:
        context = channel.get("context_usage") or {}
        mcp = channel.get("mcp_status") or {}
        total_tokens = context.get("totalTokens")
        mcp_servers = mcp.get("mcpServers") if isinstance(mcp, dict) else None
        table.add_row(
            channel["channel_id"],
            "yes" if channel.get("live") else "no",
            str(channel.get("rate_limit", {}).get("status", "allowed")),
            str(total_tokens if total_tokens is not None else "-"),
            str(len(mcp_servers) if isinstance(mcp_servers, list) else "-"),
        )
    console.print(table)


@app.command()
def cost(
    month: bool = typer.Option(False, "--month", help="Show month-to-date spend."),
    today: bool = typer.Option(False, "--today", help="Show today's spend."),
    by_channel: bool = typer.Option(False, "--by-channel", help="Break spend down by channel."),
    since: str | None = typer.Option(None, "--since", help="Show spend since YYYY-MM-DD."),
) -> None:
    """Query the SQLite cost ledger."""
    cfg, _ = _load_config_optional()
    paths = cfg.paths if cfg else _fallback_paths()
    db = CostDatabase(_cost_db_path(paths))
    start, label = _cost_window(month=month, today=today, since=since)
    result = db.query(since=start, by_channel=by_channel)
    if by_channel:
        table = Table(title=f"Cost By Channel ({label})")
        table.add_column("channel")
        table.add_column("turns", justify="right")
        table.add_column("cost", justify="right")
        for channel_id, total in result.per_channel.items():
            table.add_row(
                escape(_cost_channel_label(channel_id)),
                str(total.turn_count),
                f"${total.total_cost_usd:.4f}",
            )
        table.add_row(
            "TOTAL",
            str(result.turns),
            f"${result.total_cost_usd:.4f}",
        )
        console.print(table)
        return
    typer.echo(f"{label}: {result.turns} turns ${result.total_cost_usd:.4f}")


@app.command()
def logs(
    tail: int = typer.Option(100, "--tail", "-n", help="Number of matching log lines to print."),
    channel: str | None = typer.Option(None, "--channel", help="Filter by channel id."),
    level: str | None = typer.Option(None, "--level", help="Filter by level, e.g. err."),
) -> None:
    """Tail the most recent structured log file."""
    cfg, _ = _load_config_optional()
    paths = cfg.paths if cfg else _fallback_paths()
    log_file = _latest_log_file(paths.log_dir)
    if log_file is None:
        typer.echo("No Engram log files found.")
        return
    matches = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        if _log_line_matches(line, channel=channel, level=level):
            matches.append(line)
    for line in matches[-tail:]:
        typer.echo(line)


@app.command()
def health(
    max_age_seconds: int = typer.Option(
        120,
        "--max-age-seconds",
        help="Maximum health marker age.",
    ),
) -> None:
    """Health check for launchd watchdogs."""
    cfg, _ = _load_config_optional()
    paths = cfg.paths if cfg else _fallback_paths()
    pid = _pid_from_file(paths.state_dir)
    marker = read_json(health_path(paths.state_dir)) or {}
    now = time.time()
    marker_ts = _parse_iso_ts(marker.get("ts"))
    healthy = (
        pid is not None
        and process_exists(pid)
        and marker_ts is not None
        and now - marker_ts <= max_age_seconds
    )
    if not healthy:
        typer.echo("unhealthy", err=True)
        raise typer.Exit(1)
    typer.echo("ok")


@app.command()
def run() -> None:
    """Start the Engram bridge (Socket Mode, foreground)."""
    from engram.main import run as run_bridge

    sys.exit(asyncio.run(run_bridge()))


@app.command()
def nightly(
    weekly: bool = typer.Option(
        False,
        "--weekly",
        help="After the daily run, synthesize the seven daily rows ending on this date.",
    ),
    target_date: str | None = typer.Option(
        None,
        "--date",
        help="Target date as YYYY-MM-DD. Defaults to the current UTC date.",
    ),
) -> None:
    """Run nightly synthesis with heartbeat/log observability."""
    from engram.nightly import run_configured_nightly

    parsed_date = _parse_cli_date(target_date)
    result = asyncio.run(run_configured_nightly(weekly=weekly, target_date=parsed_date))
    raise typer.Exit(result.exit_code)


@scope_app.command("audit")
def scope_audit(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show manifest scope posture for every provisioned channel."""
    rows = _manifest_audit_rows(engram_home())
    if json_output:
        typer.echo(json.dumps(rows, sort_keys=True))
        return

    table = Table(title="Scope Audit")
    table.add_column("channel")
    table.add_column("label")
    table.add_column("identity")
    table.add_column("status")
    table.add_column("meta eligible")
    table.add_column("mcp")
    table.add_column("tools")
    for row in rows:
        table.add_row(
            row["channel_id"],
            row["label"] or "-",
            row["identity"],
            row["status"],
            "yes" if row["meta_eligible"] else "no",
            row["mcp"],
            row["tools"],
        )
    console.print(table)


@app.command()
def setup() -> None:
    """Interactive setup wizard for first-time configuration."""
    from engram.setup_wizard import run_wizard

    run_wizard()


def _build_status_snapshot() -> dict[str, Any]:
    cfg, config_error = _load_config_optional()
    paths = cfg.paths if cfg else _fallback_paths()
    home = engram_home()
    cost_db = CostDatabase(_cost_db_path(paths))
    runtime = read_json(status_path(paths.state_dir)) or {}
    pid = _pid_from_file(paths.state_dir) or _bridge_pid()
    bridge_up = bool(pid and process_exists(pid))

    channels = _merge_channels(
        runtime_channels=runtime.get("channels") or [],
        cost_db=cost_db,
        home=home,
    )
    memory = _memory_counts(home / "memory.db")
    memory.update(memory_tool_metrics())
    runtime_memory = runtime.get("memory") if isinstance(runtime, dict) else None
    if isinstance(runtime_memory, dict):
        memory.update(runtime_memory)
    memory.pop("embedding_queue", None)

    return {
        "version": __version__,
        "config_file": str(DEFAULT_CONFIG_PATH),
        "config_error": config_error,
        "bridge": {
            "up": bridge_up,
            "pid": pid,
            "health": read_json(health_path(paths.state_dir)),
        },
        "nightly": _nightly_status(home),
        "channels": channels,
        "memory": memory,
    }


def _load_config_optional() -> tuple[EngramConfig | None, str | None]:
    try:
        return EngramConfig.load(), None
    except RuntimeError as e:
        return None, str(e)


def _fallback_paths() -> PathsConfig:
    home = Path.home() / ".engram"
    return PathsConfig(
        state_dir=home / "state",
        contexts_dir=home / "contexts",
        log_dir=home / "logs",
    )


def _parse_cli_date(raw: str | None) -> datetime.date | None:
    if raw is None:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError as exc:
        raise typer.BadParameter("--date must be YYYY-MM-DD") from exc


def _nightly_status(home: Path) -> dict[str, Any]:
    path = nightly_heartbeat_path(home)
    heartbeat = read_json(path)
    base: dict[str, Any] = {
        "heartbeat_path": str(path),
        "heartbeat": heartbeat,
        "state": "missing",
        "stale": False,
        "age_hours": None,
        "summary": "no heartbeat",
    }
    if heartbeat is None:
        return base

    completed_at = _parse_iso_datetime(heartbeat.get("completed_at"))
    if completed_at is None:
        phase = heartbeat.get("phase_reached") or "unknown"
        exit_code = heartbeat.get("exit_code")
        if exit_code not in (None, 0):
            base.update(
                {
                    "state": "failed",
                    "summary": f"failed at phase={phase} exit={exit_code} ⚠️",
                }
            )
        else:
            base.update({"state": "incomplete", "summary": "incomplete ⚠️"})
        return base

    age = max(
        0.0,
        (_utc_now() - completed_at.astimezone(datetime.UTC)).total_seconds(),
    )
    age_hours = age / 3600
    base["age_hours"] = round(age_hours, 2)
    if age_hours > 36:
        base.update(
            {
                "state": "stale",
                "stale": True,
                "summary": f"stale ({_format_age(age)}) ⚠️",
            }
        )
    else:
        base.update(
            {
                "state": "ok",
                "summary": f"last ran {_format_age(age)} ago ✓",
            }
        )
    return base


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _parse_iso_datetime(raw: object) -> datetime.datetime | None:
    if not isinstance(raw, str):
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _format_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


def _cost_db_path(paths: PathsConfig) -> Path:
    if paths.log_dir.name == "logs":
        return paths.log_dir.parent / "cost.db"
    return paths.state_dir.parent / "cost.db"


def _cost_channel_label(channel_id: str) -> str:
    if channel_id == "__nightly__":
        return "[nightly-synthesis]"
    return channel_id


def _merge_channels(
    *,
    runtime_channels: list[Any],
    cost_db: CostDatabase,
    home: Path,
) -> list[dict[str, Any]]:
    by_channel: dict[str, dict[str, Any]] = {}
    for raw in runtime_channels:
        if not isinstance(raw, dict):
            continue
        channel_id = raw.get("channel_id")
        if not channel_id:
            continue
        channel = dict(raw)
        channel.setdefault("rate_limit", cost_db.latest_rate_limit(channel_id))
        by_channel[channel_id] = channel

    for manifest_path in sorted(contexts_dir(home).glob("*/.claude/channel-manifest.yaml")):
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError:
            continue
        channel = by_channel.setdefault(
            manifest.channel_id,
            {
                "channel_id": manifest.channel_id,
                "label": manifest.label,
                "live": False,
                "turn_count": 0,
                "mcp_status": None,
                "context_usage": None,
                "rate_limit": cost_db.latest_rate_limit(manifest.channel_id),
            },
        )
        channel.setdefault("manifest_status", str(manifest.status))
        channel.setdefault("identity", str(manifest.identity))
        channel.setdefault("meta_eligible", manifest.meta_eligible)
        channel["mcp"] = _manifest_mcp_policy(manifest)
        channel["tools"] = _merge_registered_tools(
            channel.get("tools"),
            _manifest_registered_tools(manifest),
        )

    for channel_id, channel in by_channel.items():
        channel.setdefault("rate_limit", cost_db.latest_rate_limit(channel_id))
        channel.setdefault("mcp_status", None)
        channel.setdefault("mcp", {"strict_mode": None, "servers": []})
        channel.setdefault("tools", {"registered": []})
        channel.setdefault("context_usage", None)
        channel.setdefault("live", False)
    return sorted(by_channel.values(), key=lambda item: item["channel_id"])


def _manifest_audit_rows(home: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for manifest_path in sorted(contexts_dir(home).glob("*/.claude/channel-manifest.yaml")):
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            rows.append(
                {
                    "channel_id": manifest_path.parents[1].name,
                    "label": None,
                    "identity": "invalid",
                    "status": "invalid",
                    "meta_eligible": None,
                    "mcp": f"invalid: {exc}",
                    "tools": "invalid",
                }
            )
            continue
        rows.append(
            {
                "channel_id": manifest.channel_id,
                "label": manifest.label,
                "identity": str(manifest.identity),
                "status": str(manifest.status),
                "meta_eligible": manifest.meta_eligible,
                "mcp": _scope_summary(manifest.mcp_servers.allowed, manifest.mcp_servers.disallowed),
                "tools": _scope_summary(manifest.tools.allowed, manifest.tools.disallowed),
            }
        )
    return rows


def _scope_summary(allowed: list[str] | None, disallowed: list[str]) -> str:
    base = f"allow:{','.join(allowed) or '-'}" if allowed is not None else "inherit"
    if disallowed:
        return f"{base} deny:{','.join(disallowed)}"
    return base


def _manifest_mcp_policy(manifest: ChannelManifest) -> dict[str, Any]:
    strict_mode = not manifest.is_owner_dm()
    servers: list[str] = []
    if strict_mode:
        resolved, _allowed, _missing = resolve_team_mcp_servers(manifest)
        servers = list(resolved)
    return {"strict_mode": strict_mode, "servers": servers}


def _manifest_registered_tools(manifest: ChannelManifest) -> list[str]:
    if manifest.is_owner_dm():
        return list(MEMORY_SEARCH_FULL_TOOL_NAMES)
    mcp = manifest.mcp_servers
    if MEMORY_SEARCH_SERVER_NAME in mcp.disallowed:
        return []
    if mcp.allowed is not None and MEMORY_SEARCH_SERVER_NAME in mcp.allowed:
        return list(MEMORY_SEARCH_FULL_TOOL_NAMES)
    return []


def _merge_registered_tools(raw_tools: Any, registered: list[str]) -> dict[str, Any]:
    tools = dict(raw_tools) if isinstance(raw_tools, dict) else {}
    existing = tools.get("registered")
    if isinstance(existing, list):
        registered = [*existing, *registered]
    tools["registered"] = sorted(set(registered))
    return tools


def _memory_counts(path: Path) -> dict[str, int]:
    counts = {"transcripts_count": 0, "summaries_count": 0}
    if not path.exists():
        return counts
    try:
        with sqlite3.connect(path) as conn:
            for table, key in (
                ("transcripts", "transcripts_count"),
                ("summaries", "summaries_count"),
            ):
                try:
                    counts[key] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
                except sqlite3.Error:
                    counts[key] = 0
    except sqlite3.Error:
        return counts
    return counts


def _cost_window(
    *,
    month: bool,
    today: bool,
    since: str | None,
) -> tuple[datetime.datetime, str]:
    now = datetime.datetime.now(datetime.UTC)
    if since:
        date = datetime.date.fromisoformat(since)
        start = datetime.datetime.combine(date, datetime.time.min, tzinfo=datetime.UTC)
        return start, f"since {since}"
    if today:
        start = datetime.datetime.combine(now.date(), datetime.time.min, tzinfo=datetime.UTC)
        return start, "today"
    # Default and --month both mean month-to-date.
    start = datetime.datetime(
        year=now.year,
        month=now.month,
        day=1,
        tzinfo=datetime.UTC,
    )
    return start, "month"


def _latest_log_file(log_dir: Path) -> Path | None:
    files = sorted(log_dir.glob("engram-*.jsonl"))
    return files[-1] if files else None


def _log_line_matches(
    line: str,
    *,
    channel: str | None,
    level: str | None,
) -> bool:
    if not channel and not level:
        return True
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return False
    if channel and payload.get("channel_id") != channel:
        return False
    if level:
        wanted = level.lower()
        actual = str(payload.get("level", "")).lower()
        if wanted in {"err", "error"}:
            return actual in {"error", "critical"}
        if actual != wanted:
            return False
    return True


def _pid_from_file(state_dir: Path) -> int | None:
    path = pid_path(state_dir)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _parse_iso_ts(raw: object) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _safe_run(argv: list[str], timeout: float = 10.0) -> str:
    """Run a command, return stdout (or stderr) as text. Timeout-bounded.

    Default 10s tolerates `claude mcp list` doing its health-check sweep
    across multiple hosted MCPs. Short commands still return promptly.
    """
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        return (out.stdout or out.stderr).strip()
    except Exception:
        return ""


def _bridge_pid() -> int | None:
    """Best-effort: find the running bridge via pgrep. M1 placeholder."""
    out = _safe_run(["pgrep", "-f", "engram.main"])
    if not out:
        return None
    try:
        return int(out.splitlines()[0])
    except (ValueError, IndexError):
        return None


if __name__ == "__main__":
    app()
