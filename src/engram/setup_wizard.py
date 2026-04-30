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

import asyncio
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent
from typing import Any

import yaml
from rich import print as rprint
from rich.console import Console
from rich.prompt import Confirm, Prompt

from engram import paths
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
from engram.mcp import (
    MCPChannelCoverage,
    audit_mcp_channel_coverage,
    load_claude_mcp_servers,
    write_mcp_inventory_state,
)
from engram.mcp_onboarding import sync_team_channel_mcp_allow_lists
from engram.mcp_trust import resolve_mcp_server_trust

console = Console()
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"

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
          description: "Manage Engram channel MCP access, permission tiers, YOLO mode, and nightly-summary inclusion"
          usage_hint: "channels | mcp | upgrade | yolo | exclude | include"
          should_escape: false
        - command: /exclude-from-nightly
          description: "Exclude this channel from the nightly cross-channel summary"
          usage_hint: "Run in this channel to exclude it from tonight's summary"
          should_escape: false
        - command: /include-in-nightly
          description: "Include this channel in the nightly cross-channel summary"
          usage_hint: "Run in this channel to include it in tonight's summary"
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


def _step_slack(
    requester: Callable[..., tuple[int, dict[str, Any]]] | None = None,
) -> dict[str, str]:
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
    workspace = _validate_slack_workspace(bot_token, requester=requester)
    team_name = workspace.get("team_name")
    team_id = workspace.get("team_id")
    workspace_url = workspace.get("workspace_url")
    if team_id and team_name:
        rprint(f"  [green]✓[/green] Connected to [bold]{team_name}[/bold] ({team_id})")
    elif team_id:
        rprint(f"  [green]✓[/green] Connected to workspace {team_id}")
    # else: _validate_slack_workspace already printed a yellow warning
    # explaining that workspace metadata could not be captured.
    if workspace_url:
        rprint(f"    Workspace URL: {workspace_url}")
    return {"bot_token": bot_token, "app_token": app_token} | workspace


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
    rprint(
        "Engram is MCP-agnostic. It reads the same [italic]~/.claude.json[/italic] "
        "inventory that Claude Code uses, not [italic]~/.claude/mcp.json[/italic]."
    )
    found = load_claude_mcp_servers()
    coverage = audit_mcp_channel_coverage()

    if not found:
        rprint("  [dim]no MCP servers configured (zero-MCP is a supported setup)[/dim]")
    else:
        for name in found:
            rprint(f"  [green]•[/green] {name}  [dim](~/.claude.json)[/dim]")
    rprint()
    rprint("  Owner DMs auto-discover from this shared user inventory.")
    rprint("  Team channels still gate MCPs per manifest with strict allow-lists.")
    if found and coverage.team_channels:
        servers = ", ".join(coverage.uncovered_servers)
        if coverage.uncovered_servers:
            rprint()
            rprint(
                "  [yellow]⚠[/yellow] Registered but not yet allowed in any team channel "
                f"manifest: {servers}"
            )
            rprint(
                "    Fix: add each server under [italic]mcp_servers.allowed[/italic] in "
                "[cyan]~/.engram/contexts/<channel-id>/.claude/channel-manifest.yaml[/cyan]"
            )
        _maybe_sync_team_channel_mcp_allow_lists(found, coverage)
    write_mcp_inventory_state(found, home=paths.engram_home())


def _maybe_sync_team_channel_mcp_allow_lists(
    configured_servers: dict[str, dict[str, object]],
    coverage: MCPChannelCoverage,
) -> None:
    asyncio.run(
        sync_team_channel_mcp_allow_lists(
            configured_servers,
            coverage,
            home=paths.engram_home(),
            confirm=lambda message, default: Confirm.ask(message, default=default),
            printer=rprint,
            trust_resolver=resolve_mcp_server_trust,
        )
    )


def _write_config(
    *,
    slack: dict[str, str],
    anthropic_key: str,
    gemini_key: str | None,
) -> None:
    rprint("[bold]Step 6 — Write Config[/bold]")
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    slack_config: dict[str, str] = {
        "bot_token": slack["bot_token"],
        "app_token": slack["app_token"],
    }
    for key in ("team_id", "team_name", "workspace_url"):
        if value := slack.get(key):
            slack_config[key] = value

    config: dict = {
        "slack": slack_config,
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


def _validate_slack_workspace(
    bot_token: str,
    *,
    requester: Callable[..., tuple[int, dict[str, Any]]] | None = None,
) -> dict[str, str]:
    """Best-effort enrichment of Slack workspace metadata at setup time.

    GRO-475 blocker 2: a transient Slack failure (HTTP error, timeout, 503,
    or missing team_id) must NOT terminate the wizard. The point of capturing
    team_id at setup is opportunistic enrichment so doctor can verify later —
    if Slack is temporarily unavailable, the user should still be able to
    finish setup. Only hard-exit if Slack explicitly rejects the bot token
    (`ok=false` with a clear auth error like invalid_auth / token_revoked /
    not_authed), since that means the token will never work.
    """
    request = requester or _post_json
    try:
        status_code, payload = request(
            SLACK_AUTH_TEST_URL,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            payload={},
        )
    except Exception as exc:
        # Transient transport failure — warn and continue. Doctor will
        # verify on next run when Slack is reachable again.
        rprint(
            "  [yellow]⚠[/yellow] Slack auth.test could not be reached "
            f"({type(exc).__name__}: {exc}). Continuing without workspace "
            "metadata; run `engram doctor` after setup completes to verify."
        )
        return {}

    if status_code != 200:
        slack_error = _optional_string(payload.get("error"))
        suffix = f" ({slack_error})" if slack_error else ""
        rprint(
            f"  [yellow]⚠[/yellow] Slack auth.test returned HTTP {status_code}{suffix}; "
            "continuing without workspace metadata. Run `engram doctor` after setup "
            "completes to verify."
        )
        return {}

    if not payload.get("ok"):
        error = _optional_string(payload.get("error")) or "unknown_error"
        # These errors mean the token will never work — hard-exit so the
        # user fixes it before continuing setup.
        auth_hard_fail = {
            "invalid_auth",
            "token_revoked",
            "token_expired",
            "not_authed",
            "account_inactive",
        }
        if error in auth_hard_fail:
            rprint(f"  [red]✗[/red] Slack bot token rejected by auth.test: {error}.")
            sys.exit(1)
        # Other ok=false reasons (rate_limited, fatal_error, internal_error)
        # are transient — warn and continue.
        rprint(
            f"  [yellow]⚠[/yellow] Slack auth.test returned ok=false ({error}); "
            "this looks transient. Continuing without workspace metadata. "
            "Run `engram doctor` after setup completes to verify."
        )
        return {}

    team_id = _optional_string(payload.get("team_id"))
    if not team_id:
        # auth.test succeeded but no team_id — unusual, but not fatal.
        # The token works, doctor will verify the rest later.
        rprint(
            "  [yellow]⚠[/yellow] Slack auth.test succeeded but did not "
            "return a team_id; continuing without workspace metadata. "
            "Run `engram doctor` after setup completes to verify."
        )
        return {}

    workspace = {"team_id": team_id}
    if team_name := _optional_string(payload.get("team")):
        workspace["team_name"] = team_name
    if workspace_url := _optional_string(payload.get("url")):
        workspace["workspace_url"] = workspace_url
    return workspace


def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = 3.0,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return response.status, _parse_json_payload(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, _parse_json_payload(text)


def _parse_json_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
