"""Engram setup wizard.

Walks the user through first-time configuration:
  1. Verify `claude` CLI is installed (M0-F1)
  2. Collect Slack tokens (new app — OQ4)
  3. Collect ANTHROPIC_API_KEY
  4. Discover MCPs and echo them (zero-MCP is supported — OQ19)
  5. Write ~/.engram/config.yaml
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from textwrap import dedent

import yaml
from rich import print as rprint
from rich.console import Console
from rich.prompt import Confirm, Prompt

from engram.config import DEFAULT_CONFIG_PATH

console = Console()

SLACK_APP_MANIFEST = dedent(
    """\
    display_information:
      name: Engram
      description: Personal AI agent — per-channel memory and skills.
      background_color: "#1a1a1a"
    features:
      bot_user:
        display_name: Engram
        always_online: true
      app_home:
        home_tab_enabled: false
        messages_tab_enabled: true
        messages_tab_read_only_enabled: false
    oauth_config:
      scopes:
        bot:
          - app_mentions:read
          - channels:history
          - channels:read
          - chat:write
          - files:read
          - files:write
          - groups:history
          - groups:read
          - im:history
          - im:read
          - im:write
          - mpim:history
          - mpim:read
          - reactions:read
          - reactions:write
          - users:read
    settings:
      event_subscriptions:
        bot_events:
          - app_mention
          - message.channels
          - message.groups
          - message.im
          - message.mpim
      interactivity:
        is_enabled: true
      org_deploy_enabled: false
      socket_mode_enabled: true
      token_rotation_enabled: false
    """
)


def run_wizard() -> None:
    rprint("[bold cyan]Engram Setup[/bold cyan]\n")

    _step_claude_cli()
    rprint()

    slack = _step_slack()
    rprint()

    anthropic_key = _step_anthropic()
    rprint()

    _step_mcp_inventory()
    rprint()

    _write_config(slack=slack, anthropic_key=anthropic_key)
    rprint("\n[bold green]✓ Setup complete.[/bold green]")
    rprint(f"  Config written to: {DEFAULT_CONFIG_PATH}")
    rprint()
    rprint("Next:")
    rprint("  [cyan]engram status[/cyan]   — verify config")
    rprint("  [cyan]engram run[/cyan]      — start the bridge (foreground)")
    rprint()


def _step_claude_cli() -> None:
    rprint("[bold]Step 1 — Claude CLI[/bold]")
    rprint("The Claude Agent SDK manages the [italic]claude[/italic] CLI as a subprocess, so it must be installed.")
    claude = shutil.which("claude")
    if claude:
        rprint(f"  [green]✓[/green] found at {claude}")
    else:
        rprint("  [red]✗[/red] claude CLI not found on PATH.")
        rprint("  Install with: [cyan]npm install -g @anthropic-ai/claude-code[/cyan]")
        if not Confirm.ask("  Continue anyway? (will fail at runtime)", default=False):
            rprint("  Aborting. Install Claude CLI and re-run `engram setup`.")
            sys.exit(1)


def _step_slack() -> dict[str, str]:
    rprint("[bold]Step 2 — Slack App[/bold]")
    rprint("Engram needs its own Slack app (Socket Mode + Bot User).")
    rprint()
    rprint("If you haven't created one yet:")
    rprint("  1. Go to [cyan]https://api.slack.com/apps[/cyan]")
    rprint("  2. Click 'Create New App' → 'From an app manifest'")
    rprint("  3. Pick your workspace")
    rprint(f"  4. Paste the manifest from: [cyan]{_write_manifest_tempfile()}[/cyan]")
    rprint("  5. Create App → Install to Workspace → Allow")
    rprint("  6. Under 'Basic Information' → 'App-Level Tokens', create a token")
    rprint("     with the [italic]connections:write[/italic] scope. Copy that token.")
    rprint("  7. Under 'OAuth & Permissions', copy the [italic]Bot User OAuth Token[/italic].")
    rprint()

    bot_token = Prompt.ask(
        "  Bot User OAuth Token (xoxb-…)",
        default=os.environ.get("ENGRAM_SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN") or "",
        show_default=False,
    ).strip()
    if not bot_token.startswith("xoxb-"):
        rprint("  [yellow]⚠[/yellow] expected prefix 'xoxb-' — double-check this token.")
    app_token = Prompt.ask(
        "  App-Level Token       (xapp-…)",
        default=os.environ.get("ENGRAM_SLACK_APP_TOKEN") or os.environ.get("SLACK_APP_TOKEN") or "",
        show_default=False,
    ).strip()
    if not app_token.startswith("xapp-"):
        rprint("  [yellow]⚠[/yellow] expected prefix 'xapp-' — double-check this token.")
    return {"bot_token": bot_token, "app_token": app_token}


def _write_manifest_tempfile() -> Path:
    p = Path("/tmp/engram-slack-manifest.yaml")
    p.write_text(SLACK_APP_MANIFEST)
    return p


def _step_anthropic() -> str:
    rprint("[bold]Step 3 — Anthropic API Key[/bold]")
    rprint("Engram uses a separate Anthropic API key (not your Claude Code OAuth session).")
    existing = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ENGRAM_ANTHROPIC_API_KEY") or ""
    hint = f" [dim](found existing ANTHROPIC_API_KEY ending {existing[-4:]})[/dim]" if existing else ""
    rprint(f"  If you have one configured in your environment, we'll use that.{hint}")
    key = Prompt.ask(
        "  ANTHROPIC_API_KEY (leave blank to use env)", default=existing, show_default=False
    ).strip()
    if not key:
        rprint("  [red]✗[/red] no API key provided. Set ANTHROPIC_API_KEY before running.")
        sys.exit(1)
    return key


def _step_mcp_inventory() -> None:
    rprint("[bold]Step 4 — MCP Inventory[/bold]")
    rprint("Engram is MCP-agnostic. Whatever [italic]claude[/italic] sees, Engram can use.")
    found: dict[str, str] = {}
    mcp_json = Path.home() / ".claude" / "mcp.json"
    if mcp_json.exists():
        try:
            data = json.loads(mcp_json.read_text())
            for n in data.get("mcpServers") or {}:
                found[n] = "~/.claude/mcp.json"
        except json.JSONDecodeError:
            pass
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text())
            for n in data.get("mcpServers") or {}:
                found.setdefault(n, "~/.claude.json")
        except json.JSONDecodeError:
            pass

    if not found:
        rprint("  [dim]no MCP servers configured (zero-MCP is a supported setup)[/dim]")
    else:
        for name, src in found.items():
            rprint(f"  [green]•[/green] {name}  [dim]({src})[/dim]")
    rprint()
    rprint("  Note: per-MCP scoping per channel is an M2 feature. For now, Engram")
    rprint("  sees everything claude sees.")


def _write_config(*, slack: dict[str, str], anthropic_key: str) -> None:
    rprint("[bold]Step 5 — Write Config[/bold]")
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "slack": {
            "bot_token": slack["bot_token"],
            "app_token": slack["app_token"],
        },
        "anthropic": {
            "api_key": anthropic_key,
            "model": "claude-sonnet-4-6",
        },
        "allowed_channels": [],  # DMs always allowed; team channels opt-in
        "max_turns_per_message": 8,
        "embeddings": {
            "enabled": True,
            "provider": "gemini",
            "model": "text-embedding-004",
            "dimensions": 768,
            "sample_rate_transcripts": 0.3,
        },
    }

    DEFAULT_CONFIG_PATH.write_text(yaml.safe_dump(config, sort_keys=False))
    # Tight permissions — secrets live here.
    DEFAULT_CONFIG_PATH.chmod(0o600)
    rprint(f"  [green]✓[/green] wrote {DEFAULT_CONFIG_PATH} (mode 600)")
