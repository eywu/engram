"""Engram setup wizard.

Walks the user through first-time configuration:
  1. Verify `claude` CLI is installed (M0-F1)
  2. Collect Slack tokens (new app — OQ4)
  3. Collect ANTHROPIC_API_KEY
  4. Collect GEMINI_API_KEY (optional; unlocks semantic memory search)
  5. Discover MCPs and echo them (zero-MCP is supported — OQ19)
  6. Write ~/.engram/config.yaml
  7. Sync ~/Library/LaunchAgents/com.engram.bridge.plist
"""
from __future__ import annotations

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
from engram.launchd import (
    find_repo_root,
    installed_bridge_plist_path,
    load_plist,
    render_bridge_plist,
    resolve_uv_bin,
    setup_bridge_plist_issues,
    write_bridge_env_file,
    write_plist,
)
from engram.mcp import load_claude_mcp_servers

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
      slash_commands:
        - command: /engram
          description: "Manage Engram permission tiers, YOLO mode, and nightly-summary inclusion"
          usage_hint: "upgrade | yolo | channels | exclude | include"
          should_escape: false
        - command: /exclude-from-nightly
          description: "Exclude this channel from the nightly cross-channel summary"
          usage_hint: ""
          should_escape: false
        - command: /include-in-nightly
          description: "Include this channel in the nightly cross-channel summary"
          usage_hint: ""
          should_escape: false
    oauth_config:
      scopes:
        bot:
          - app_mentions:read
          - channels:history
          - channels:read
          - chat:write
          - commands
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
        # Block Kit button actions arrive over Socket Mode when interactivity is enabled.
        # They are not event_subscriptions and do not use an "actions" bot scope.
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

    gemini_key = _step_gemini()
    rprint()

    _step_mcp_inventory()
    rprint()

    _write_config(slack=slack, anthropic_key=anthropic_key, gemini_key=gemini_key)
    rprint()

    _step_launchd_sync(anthropic_key=anthropic_key, gemini_key=gemini_key)
    rprint("\n[bold green]✓ Setup complete.[/bold green]")
    rprint(f"  Config written to: {DEFAULT_CONFIG_PATH}")
    rprint()
    rprint("Next steps:")
    rprint("  1. [cyan]engram status[/cyan]        — verify config + launchd health")
    rprint("  2. [cyan]engram run[/cyan]           — start the bridge in foreground if you want")
    rprint('  3. Test in Slack: DM the bot ("Hello")')
    rprint("  4. Verify slash commands: type `/engram` in any channel — Slack should autocomplete")
    rprint("     If not: re-paste the manifest at api.slack.com/apps and reinstall the app.")
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
    rprint("  [dim]This key is billed separately from any Claude subscription you have.[/dim]")
    rprint("  [dim]Get one at: https://console.anthropic.com/settings/keys[/dim]")
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


def _step_gemini() -> str | None:
    rprint("[bold]Step 4 — Gemini API Key[/bold] [dim](optional)[/dim]")
    rprint("Engram uses Gemini for semantic memory embeddings (text-embedding-004).")
    rprint("  [dim]With a key:    semantic + keyword (FTS5) memory search.[/dim]")
    rprint("  [dim]Without a key: keyword-only memory — still works, just less[/dim]")
    rprint("  [dim]              accurate for paraphrase / conceptual recall.[/dim]")
    rprint("  [dim]Get one at: https://aistudio.google.com/app/apikey (free tier is plenty).[/dim]")
    existing = os.environ.get("GEMINI_API_KEY") or os.environ.get("ENGRAM_GEMINI_API_KEY") or ""
    hint = f" [dim](found existing GEMINI_API_KEY ending {existing[-4:]})[/dim]" if existing else ""
    rprint(f"  If you have one configured in your environment, we'll use that.{hint}")
    key = Prompt.ask(
        "  GEMINI_API_KEY (leave blank to skip — keyword-only memory)",
        default=existing,
        show_default=False,
    ).strip()
    if not key:
        rprint("  [yellow]⚠[/yellow] no key provided — semantic search disabled. You can add one later")
        rprint("    by editing [cyan]~/.engram/config.yaml[/cyan] (set [italic]embeddings.api_key[/italic])")
        rprint("    or by exporting [cyan]GEMINI_API_KEY[/cyan] before [cyan]engram run[/cyan].")
        return None
    return key


def _step_mcp_inventory() -> None:
    rprint("[bold]Step 5 — MCP Inventory[/bold]")
    rprint("Engram is MCP-agnostic. It reads the same [italic]~/.claude.json[/italic] inventory that Claude Code uses.")
    found = load_claude_mcp_servers()

    if not found:
        rprint("  [dim]no MCP servers configured (zero-MCP is a supported setup)[/dim]")
    else:
        for name in found:
            rprint(f"  [green]•[/green] {name}  [dim](~/.claude.json)[/dim]")
    rprint()
    rprint("  Note: this is the shared user inventory. Channel manifests still")
    rprint("  gate which MCPs are allowed in each team channel.")


def _write_config(
    *,
    slack: dict[str, str],
    anthropic_key: str,
    gemini_key: str | None,
) -> None:
    rprint("[bold]Step 6 — Write Config[/bold]")
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    config: dict = {
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
    }
    if gemini_key:
        # Embeddings block is only written when the user supplied a key.
        # When the block is absent, EmbeddingsConfig.from_mapping falls back
        # to GEMINI_API_KEY env var (or disables semantic search entirely).
        config["embeddings"] = {
            "enabled": True,
            "provider": "gemini",
            "api_key": gemini_key,
        }

    DEFAULT_CONFIG_PATH.write_text(yaml.safe_dump(config, sort_keys=False))
    # Tight permissions — secrets live here.
    DEFAULT_CONFIG_PATH.chmod(0o600)
    rprint(f"  [green]✓[/green] wrote {DEFAULT_CONFIG_PATH} (mode 600)")


def _step_launchd_sync(*, anthropic_key: str, gemini_key: str | None) -> None:
    rprint("[bold]Step 7 — Launchd Bridge[/bold]")

    repo_root = find_repo_root()
    if repo_root is None:
        rprint("  [yellow]⚠[/yellow] repo root not found from the current directory — skipping plist sync.")
        rprint("    Run `engram setup` from the repo checkout or refresh with `scripts/install_launchd.sh`.")
        return

    uv_bin = resolve_uv_bin()
    if uv_bin is None:
        rprint("  [yellow]⚠[/yellow] uv is not on PATH — skipping launchd plist sync.")
        return

    env_file = write_bridge_env_file(
        anthropic_key=anthropic_key,
        gemini_key=gemini_key,
    )
    rprint(f"  [green]✓[/green] wrote {env_file} (mode 600)")

    expected = render_bridge_plist(
        repo_root=repo_root,
        uv_bin=uv_bin,
        env_file=env_file,
    )
    installed_path = installed_bridge_plist_path()
    if not installed_path.exists():
        if Confirm.ask("  Install launchd bridge plist now?", default=True):
            write_plist(installed_path, expected)
            rprint(f"  [green]✓[/green] wrote {installed_path}")
        else:
            rprint("  [yellow]⚠[/yellow] skipped launchd plist install.")
        return

    try:
        installed = load_plist(installed_path)
    except Exception as exc:
        rprint(
            "  [yellow]⚠[/yellow] installed plist could not be parsed "
            f"({type(exc).__name__}: {exc})."
        )
        if Confirm.ask("  Overwrite the installed launchd plist now?", default=True):
            write_plist(installed_path, expected)
            rprint(f"  [green]✓[/green] refreshed {installed_path}")
            rprint("    Restart the bridge with `launchctl unload && launchctl load` to apply it.")
        return

    issues = setup_bridge_plist_issues(installed, expected)
    if not issues:
        rprint("  [green]✓[/green] installed launchd plist is already in sync.")
        return

    categories = sorted({issue.category.replace("_", " ") for issue in issues})
    rprint(f"  [yellow]⚠[/yellow] installed launchd plist drift detected: {', '.join(categories)}.")
    if Confirm.ask("  Update the installed launchd plist now?", default=True):
        write_plist(installed_path, expected)
        rprint(f"  [green]✓[/green] refreshed {installed_path}")
        rprint("    Restart the bridge with `launchctl unload && launchctl load` to apply it.")
    else:
        rprint("  [yellow]⚠[/yellow] left the installed launchd plist unchanged.")
