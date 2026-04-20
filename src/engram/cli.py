"""Engram CLI — `engram status`, `engram run`, `engram setup`."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from engram import __version__
from engram.config import DEFAULT_CONFIG_PATH, EngramConfig

app = typer.Typer(
    name="engram",
    help="Personal AI agent for Slack.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print Engram version."""
    rprint(f"engram [bold]{__version__}[/bold]")


@app.command()
def status() -> None:
    """Show runtime + config status and MCP inventory."""
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


def _safe_run(argv: list[str]) -> str:
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, timeout=5, check=False
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
            data = json.loads(mcp_json.read_text())
            for name in (data.get("mcpServers") or {}):
                found[name] = "~/.claude/mcp.json"
        except json.JSONDecodeError:
            pass

    # Secondary: ~/.claude.json top-level user config
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text())
            for name in (data.get("mcpServers") or {}):
                found.setdefault(name, "~/.claude.json")
        except json.JSONDecodeError:
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


if __name__ == "__main__":
    app()
