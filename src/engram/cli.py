"""Engram CLI — `engram status`, `engram run`, `engram setup`."""
from __future__ import annotations

import asyncio
import json as jsonlib
import subprocess
import sys
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from engram import __version__
from engram.cli_channels import app as channels_app
from engram.config import DEFAULT_CONFIG_PATH, EngramConfig
from engram.costs import CostLedger
from engram.tools import registered_tool_names

app = typer.Typer(
    name="engram",
    help="Personal AI agent for Slack.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(
    channels_app,
    name="channels",
    help="List and manage per-channel manifests.",
)
console = Console()


@app.command()
def version() -> None:
    """Print Engram version."""
    rprint(f"engram [bold]{__version__}[/bold]")


@app.command()
def status(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable status JSON.",
    )
) -> None:
    """Show runtime + config status and MCP inventory."""
    if json_output:
        typer.echo(jsonlib.dumps(_status_payload(), indent=2, sort_keys=True))
        return

    rprint(f"[bold]Engram[/bold] version {__version__}")
    rprint()

    # --- Config ---
    rprint("[bold]Config[/bold]")
    try:
        cfg = EngramConfig.load()
    except RuntimeError as e:
        rprint(f"  [red]✗[/red] config incomplete: {e}")
        cfg = None

    if cfg:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column()
        table.add_column()
        table.add_row("  config file:", str(DEFAULT_CONFIG_PATH))
        table.add_row("  model:", cfg.anthropic.model)
        table.add_row(
            "  slack:",
            f"bot={_mask(cfg.slack.bot_token)}, app={_mask(cfg.slack.app_token)}",
        )
        table.add_row("  state_dir:", str(cfg.paths.state_dir))
        table.add_row("  allowed_channels:", ", ".join(cfg.allowed_channels) or "(none)")
        console.print(table)
    rprint()

    # --- Claude CLI presence (SDK requirement, M0-F1) ---
    rprint("[bold]Claude CLI[/bold] (SDK subprocess dependency)")
    claude = _which("claude")
    if claude:
        ver = _safe_run([claude, "--version"]).strip()
        rprint(f"  [green]✓[/green] {claude}  [dim]{ver}[/dim]")
    else:
        rprint(
            "  [red]✗[/red] claude CLI not found on PATH. "
            "Install with: [cyan]npm i -g @anthropic-ai/claude-code[/cyan]"
        )
    rprint()

    # --- MCP inventory (M1 done criterion) ---
    rprint("[bold]MCP Inventory[/bold]")
    mcps = _discover_mcps()
    if not mcps:
        rprint("  (no MCP servers configured — zero-MCP setup is supported)")
    else:
        for name, src in mcps.items():
            rprint(f"  [green]•[/green] {name}  [dim]from {src}[/dim]")
    rprint()

    # --- Cost ledger (M1 promoted from M3) ---
    rprint("[bold]Costs[/bold] (from JSONL ledger)")
    if cfg:
        ledger = CostLedger(cfg.paths.log_dir / "costs.jsonl")
        summary = ledger.summarize()
        if summary.total_turns == 0:
            rprint("  [dim](no turns recorded yet)[/dim]")
        else:
            ctable = Table(show_header=False, box=None, padding=(0, 1))
            ctable.add_column()
            ctable.add_column(justify="right")
            ctable.add_column(justify="right")
            ctable.add_row("  period", "turns", "spend")
            ctable.add_row("  today", str(summary.today_turns), f"${summary.today_cost_usd:.4f}")
            ctable.add_row("  month-to-date", str(summary.month_turns), f"${summary.month_cost_usd:.4f}")
            ctable.add_row("  all-time", str(summary.total_turns), f"${summary.total_cost_usd:.4f}")
            console.print(ctable)
            if summary.total_turns > 0:
                avg = summary.total_cost_usd / summary.total_turns
                rprint(f"  [dim]avg/turn: ${avg:.4f}[/dim]")
    rprint()

    # --- Bridge process ---
    rprint("[bold]Bridge[/bold]")
    pid = _bridge_pid()
    if pid:
        rprint(f"  [green]✓[/green] running (pid {pid})")
    else:
        rprint("  [dim](not running — start with `engram run` or via launchd)[/dim]")
    rprint()


@app.command()
def run() -> None:
    """Start the Engram bridge (Socket Mode, foreground)."""
    from engram.main import run as run_bridge

    sys.exit(asyncio.run(run_bridge()))


@app.command()
def setup() -> None:
    """Interactive setup wizard for first-time configuration."""
    from engram.setup_wizard import run_wizard

    run_wizard()


def _mask(token: str | None) -> str:
    if not token:
        return "(unset)"
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}…{token[-4:]}"


def _which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)


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


def _discover_mcps() -> dict[str, str]:
    """Discover MCPs from Claude Code config.

    M0-F5: the SDK reads more than just ~/.claude/mcp.json. We union a few
    likely sources to give the user a realistic preview.
    """
    found: dict[str, str] = {}

    # Primary: ~/.claude/mcp.json (the documented location)
    mcp_json = Path.home() / ".claude" / "mcp.json"
    if mcp_json.exists():
        try:
            data = jsonlib.loads(mcp_json.read_text())
            for name in (data.get("mcpServers") or {}):
                found[name] = "~/.claude/mcp.json"
        except jsonlib.JSONDecodeError:
            pass

    # Secondary: ~/.claude.json top-level user config
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data = jsonlib.loads(claude_json.read_text())
            for name in (data.get("mcpServers") or {}):
                found.setdefault(name, "~/.claude.json")
        except jsonlib.JSONDecodeError:
            pass

    # Tertiary: `claude mcp list` if available.
    # Output format (Claude CLI 2.1.x):
    #   <name>: <url-or-path> [args...] - <status>
    # Plus header lines like "Checking MCP server health...".
    if _which("claude"):
        raw = _safe_run(["claude", "mcp", "list"])
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("Checking ") or line.startswith("#"):
                continue
            if ": " not in line:
                continue  # not a server row
            name_part, _, _rest = line.partition(": ")
            name = name_part.strip()
            if name and name not in found:
                found[name] = "claude mcp list"

    return found


def _bridge_pid() -> int | None:
    """Best-effort: find the running bridge via pgrep. M1 placeholder."""
    out = _safe_run(["pgrep", "-f", "engram.main"])
    if not out:
        return None
    try:
        return int(out.splitlines()[0])
    except (ValueError, IndexError):
        return None


def _status_payload() -> dict:
    """Machine-readable status used by `engram status --json`."""
    try:
        cfg = EngramConfig.load()
    except RuntimeError as e:
        cfg = None
        config_error = str(e)
    else:
        config_error = None

    payload = {
        "version": __version__,
        "config": {
            "ok": cfg is not None,
            "path": str(DEFAULT_CONFIG_PATH),
        },
        "tools": {
            "registered": registered_tool_names(),
        },
    }
    if config_error:
        payload["config"]["error"] = config_error
    if cfg is not None:
        payload["config"].update(
            {
                "model": cfg.anthropic.model,
                "state_dir": str(cfg.paths.state_dir),
                "allowed_channels": list(cfg.allowed_channels),
            }
        )
    return payload


if __name__ == "__main__":
    app()
