"""`engram channels ...` — manifest-driven channel lifecycle CLI.

Sub-commands:
  engram channels list              — all provisioned channels + status
  engram channels show <id>         — full manifest + CLAUDE.md preview
  engram channels approve <id>      — flip PENDING → ACTIVE
  engram channels deny <id>         — flip any → DENIED (silently ignored)
  engram channels reset <id>        — back to PENDING (owner must re-approve)
  engram channels new <id>          — start a fresh SDK conversation
  engram channels mcp ...           — manage per-channel MCP allow/deny state

The CLI is the human-friendly front-end for the `status` field in
ChannelManifest. It edits YAML directly (round-trip via dump_manifest),
so changes take effect on the bridge's next router cache miss — i.e. on
next restart, or when a channel hasn't been resolved yet this run.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from engram import paths
from engram.manifest import (
    ChannelManifest,
    ChannelMCPChangeStatus,
    ChannelStatus,
    ManifestError,
    PermissionTier,
    apply_channel_mcp_change,
    load_manifest,
    parse_permission_tier,
    permission_tier_choices_text,
    set_channel_mcp_server_access,
    set_channel_nightly_included,
    set_channel_permission_tier,
    set_channel_status,
    validate_upgrade_duration,
)
from engram.mcp import render_channel_mcp_access
from engram.mcp_manifest_gate import MCPApprovalDisposition
from engram.router import archive_session_transcript, derive_session_id

app = typer.Typer(
    name="channels",
    help="List and manage per-channel Engram manifests.",
    no_args_is_help=True,
    add_completion=False,
)
mcp_app = typer.Typer(
    name="mcp",
    help="Manage per-channel MCP access without editing YAML by hand.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
log = logging.getLogger(__name__)
CHANNEL_LIST_SCHEMA_VERSION = "1"
app.add_typer(
    mcp_app,
    name="mcp",
    help="Manage per-channel MCP access.",
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _iter_manifest_paths(home: Path | None = None) -> list[Path]:
    ctx = paths.contexts_dir(home)
    if not ctx.exists():
        return []
    return sorted(ctx.glob("*/.claude/channel-manifest.yaml"))


def _status_style(status: ChannelStatus) -> str:
    return {
        ChannelStatus.ACTIVE: "green",
        ChannelStatus.PENDING: "yellow",
        ChannelStatus.DENIED: "red",
    }[status]


def _flip_status(
    channel_id: str,
    new_status: ChannelStatus,
    home: Path | None = None,
) -> None:
    """Load manifest, set status, write back. Raises typer.Exit on error."""
    manifest_path = paths.channel_manifest_path(channel_id, home)
    if not manifest_path.exists():
        rprint(f"[red]No manifest found for channel '{channel_id}'.[/red]")
        rprint(f"  Expected at: {manifest_path}")
        raise typer.Exit(code=1)
    try:
        manifest, _updated, _manifest_path = set_channel_status(
            channel_id,
            new_status,
            home=home,
        )
    except ManifestError as e:
        rprint(f"[red]Failed to load manifest: {e}[/red]")
        raise typer.Exit(code=2) from e

    old_status = manifest.status
    if old_status == new_status:
        rprint(
            f"[dim]Channel '{channel_id}' already has status '{new_status}'.[/dim]"
        )
        return

    rprint(
        f"[{_status_style(new_status)}]✓[/] "
        f"[bold]{channel_id}[/bold]: "
        f"[{_status_style(old_status)}]{old_status}[/] "
        "→ "
        f"[{_status_style(new_status)}]{new_status}[/]"
    )
    rprint(
        "[dim]Note: already-cached sessions in the running bridge keep "
        "their old status until next restart.[/dim]"
    )


def _load_manifest_or_exit(
    channel_id: str,
    *,
    home: Path | None = None,
) -> tuple[Path, ChannelManifest]:
    manifest_path = paths.channel_manifest_path(channel_id, home)
    if not manifest_path.exists():
        rprint(f"[red]No manifest found for channel '{channel_id}'.[/red]")
        rprint(f"  Expected at: {manifest_path}")
        raise typer.Exit(code=1)
    try:
        return manifest_path, load_manifest(manifest_path)
    except ManifestError as exc:
        rprint(f"[red]Failed to load manifest: {exc}[/red]")
        raise typer.Exit(code=2) from exc


def _channel_name(manifest) -> str:
    if manifest.is_owner_dm():
        return "owner-dm"
    return manifest.label or manifest.channel_id


def _mcp_access_noop_text(
    *,
    action: str,
    manifest,
    server_name: str,
) -> str:
    if action == "allow":
        if manifest.mcp_servers.allowed is None and server_name not in manifest.mcp_servers.disallowed:
            return (
                f"[dim]MCP server '{server_name}' already inherits into "
                f"'{manifest.channel_id}'. No change.[/dim]"
            )
        return (
            f"[dim]MCP server '{server_name}' is already allowed in "
            f"'{manifest.channel_id}'. No change.[/dim]"
        )
    return (
        f"[dim]MCP server '{server_name}' is already denied in "
        f"'{manifest.channel_id}'. No change.[/dim]"
    )


def _channel_list_record(manifest_path: Path) -> dict[str, object]:
    channel_id = manifest_path.parent.parent.name
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        return {
            "channel_id": channel_id,
            "channel_name": None,
            "tier": "broken",
            "nightly": "unknown",
            "yolo_expires_at": None,
            "owner_dm": False,
            "status": "broken",
            "manifest_path": str(manifest_path),
            "error": str(exc),
        }

    effective_tier = manifest.tier_effective()
    yolo_expires_at = (
        manifest.yolo_until.isoformat()
        if effective_tier == PermissionTier.YOLO and manifest.yolo_until is not None
        else None
    )
    return {
        "channel_id": manifest.channel_id,
        "channel_name": _channel_name(manifest),
        "tier": effective_tier.value,
        "nightly": "included" if manifest.nightly_included else "excluded",
        "yolo_expires_at": yolo_expires_at,
        "owner_dm": manifest.is_owner_dm(),
        "status": str(manifest.status),
        "manifest_path": str(manifest_path),
        "error": None,
    }


# ── Commands ────────────────────────────────────────────────────────────


@app.command("list")
def list_channels(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a versioned machine-readable channel inventory.",
    ),
) -> None:
    """List every provisioned channel and its status."""
    manifest_paths = _iter_manifest_paths()
    if not manifest_paths:
        if json_output:
            typer.echo(
                json.dumps(
                    {"version": CHANNEL_LIST_SCHEMA_VERSION, "channels": []},
                    sort_keys=True,
                )
            )
            return
        rprint(
            "[dim]No channels provisioned yet. "
            "They appear here after the bot first sees a message "
            "in a new channel.[/dim]"
        )
        return

    records = [_channel_list_record(path) for path in manifest_paths]
    if json_output:
        typer.echo(
            json.dumps(
                {"version": CHANNEL_LIST_SCHEMA_VERSION, "channels": records},
                sort_keys=True,
            )
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Channel ID")
    table.add_column("Name")
    table.add_column("Tier")
    table.add_column("Nightly")
    table.add_column("YOLO Expiry")
    table.add_column("Owner DM")
    table.add_column("Status")

    for record in records:
        if record["status"] == "broken":
            table.add_row(
                str(record["channel_id"]),
                "—",
                "[red]BROKEN[/red]",
                "unknown",
                "—",
                "no",
                "[red]broken[/red]",
            )
            continue
        table.add_row(
            str(record["channel_id"]),
            str(record["channel_name"] or "—"),
            str(record["tier"]),
            str(record["nightly"]),
            str(record["yolo_expires_at"] or "—"),
            "yes" if bool(record["owner_dm"]) else "no",
            (
                f"[{_status_style(ChannelStatus(str(record['status'])))}]"
                f"{record['status']}[/]"
            ),
        )

    console.print(table)


@app.command()
def show(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
) -> None:
    """Show the manifest + rendered CLAUDE.md for a channel."""
    manifest_path = paths.channel_manifest_path(channel_id)
    claude_md_path = paths.channel_claude_md_path(channel_id)
    if not manifest_path.exists():
        rprint(f"[red]No manifest found for '{channel_id}'.[/red]")
        raise typer.Exit(code=1)

    try:
        m = load_manifest(manifest_path)
    except ManifestError as e:
        rprint(f"[red]Failed to load manifest: {e}[/red]")
        raise typer.Exit(code=2) from e

    rprint(f"[bold]{m.channel_id}[/bold] — {m.label or '(no label)'}")
    rprint(
        f"  status:   [{_status_style(m.status)}]{m.status}[/]"
    )
    rprint(f"  tier:     {m.tier_effective().value}")
    rprint(f"  setting:  {m.setting_sources}")
    rprint(f"  behavior: style={m.behavior.style} max_turns={m.behavior.max_turns}")

    if not m.tools.is_unrestricted():
        rprint(f"  tools:    allowed={m.tools.allowed} disallowed={m.tools.disallowed}")
    if not m.mcp_servers.is_unrestricted():
        rprint(
            f"  mcp:      allowed={m.mcp_servers.allowed} "
            f"disallowed={m.mcp_servers.disallowed}"
        )
    if not m.skills.is_unrestricted():
        rprint(
            f"  skills:   allowed={m.skills.allowed} "
            f"disallowed={m.skills.disallowed}"
        )

    rprint()
    rprint(f"[bold]Manifest[/bold] [dim]({manifest_path})[/dim]")
    rprint(f"  {manifest_path}")
    rprint()
    rprint(f"[bold]CLAUDE.md[/bold] [dim]({claude_md_path})[/dim]")
    if claude_md_path.exists():
        preview = claude_md_path.read_text().splitlines()[:15]
        for line in preview:
            rprint(f"  {line}")
        if len(claude_md_path.read_text().splitlines()) > 15:
            rprint("  [dim]...[/dim]")
    else:
        rprint("  [red]missing[/red]")


@app.command()
def approve(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
) -> None:
    """Approve a channel. Flips status → ACTIVE; bot starts responding."""
    _flip_status(channel_id, ChannelStatus.ACTIVE)


@app.command()
def deny(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
) -> None:
    """Deny a channel. Flips status → DENIED; bot stays silent."""
    _flip_status(channel_id, ChannelStatus.DENIED)


@app.command()
def reset(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
) -> None:
    """Reset a channel to PENDING; requires re-approval before bot responds."""
    _flip_status(channel_id, ChannelStatus.PENDING)


@app.command("new")
def new(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation.",
    ),
) -> None:
    """Start a fresh SDK conversation while preserving manifest and memory."""
    _manifest_path, manifest = _load_manifest_or_exit(channel_id)
    if not yes and not Confirm.ask(
        f"Start a new conversation in '{channel_id}'?",
        default=False,
    ):
        rprint("[dim]Canceled.[/dim]")
        return

    request_path = paths.new_session_request_path(channel_id)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps(
            {
                "channel_id": channel_id,
                "requested_at": datetime.datetime.now(datetime.UTC).isoformat(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    archived = archive_session_transcript(
        derive_session_id(channel_id),
        paths.project_root(),
    )
    rprint(
        f"[green]✓[/] [bold]{channel_id}[/bold]: fresh conversation requested "
        f"(tier: {manifest.tier_effective().value})."
    )
    if archived is not None:
        rprint(f"[dim]Archived transcript: {archived}[/dim]")
    rprint(
        "[dim]The running bridge will drop any live SDK client before the next "
        "message in this channel.[/dim]"
    )


@app.command("upgrade")
def upgrade(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
    tier: str = typer.Argument(..., help="Target tier."),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Upgrade duration: 6h, 24h, 72h, 30d, or permanent.",
    ),
) -> None:
    """Upgrade a channel tier immediately, bypassing the Slack approval flow."""
    try:
        target_tier, deprecated_alias = parse_permission_tier(tier)
    except ValueError as exc:
        rprint(f"[red]Unknown permission tier: {tier}[/red]")
        rprint(
            f"  Expected one of: {permission_tier_choices_text()} "
            "(deprecated aliases still accepted here)."
        )
        raise typer.Exit(code=2) from exc

    if deprecated_alias is not None:
        log.info(
            "permission_tier.deprecated_alias_used source=cli alias=%s canonical=%s channel_id=%s",
            deprecated_alias,
            target_tier.value,
            channel_id,
        )
        rprint(
            f"[yellow]Deprecated tier name '{deprecated_alias}'; "
            f"use '{target_tier.value}' instead.[/yellow]"
        )

    try:
        normalized_duration = validate_upgrade_duration(
            until or ("24h" if target_tier == PermissionTier.YOLO else "permanent")
        )
    except ValueError as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    manifest_path = paths.channel_manifest_path(channel_id)
    if not manifest_path.exists():
        rprint(f"[red]No manifest found for channel '{channel_id}'.[/red]")
        rprint(f"  Expected at: {manifest_path}")
        raise typer.Exit(code=1)

    try:
        previous, updated, _manifest_path, normalized_duration = (
            set_channel_permission_tier(
                channel_id,
                target_tier,
                duration=normalized_duration,
            )
        )
    except ValueError as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except ManifestError as exc:
        rprint(f"[red]Failed to load manifest: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if previous == updated:
        rprint(
            "[dim]Channel "
            f"'{channel_id}' already has tier '{updated.permission_tier.value}' "
            f"with duration '{normalized_duration}'.[/dim]"
        )
        return

    log.info(
        "permission.upgrade_granted",
        extra={
            "channel": channel_id,
            "approver": "cli",
            "duration": normalized_duration,
        },
    )
    rprint(
        f"[green]✓[/] [bold]{channel_id}[/bold]: "
        f"{previous.permission_tier.value} → {updated.permission_tier.value} "
        f"({normalized_duration})"
    )
    if updated.yolo_until is not None:
        rprint(f"  expires: {updated.yolo_until.isoformat()}")
        if updated.pre_yolo_tier is not None:
            rprint(f"  restores_to: {updated.pre_yolo_tier.value}")
    else:
        rprint("  expires: permanent")
    rprint(
        "[dim]Note: already-cached sessions in the running bridge keep "
        "their old tier until next restart.[/dim]"
    )


@app.command("tier")
def tier(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
) -> None:
    """Show a channel's current tier, YOLO status, and expiry."""
    _manifest_path, manifest = _load_manifest_or_exit(channel_id)

    expiry = manifest.yolo_until.isoformat() if manifest.yolo_until is not None else "none"
    yolo_status = "active" if manifest.tier_effective() == PermissionTier.YOLO else "inactive"
    rprint(f"[bold]{channel_id}[/bold]")
    rprint(f"  tier:   {manifest.tier_effective().value}")
    rprint(f"  yolo:   {yolo_status}")
    rprint(f"  expiry: {expiry}")


def _set_nightly_state(
    channel_id: str,
    *,
    nightly_included: bool,
) -> None:
    _manifest_path, manifest = _load_manifest_or_exit(channel_id)
    previous_state = "included" if manifest.nightly_included else "excluded"
    desired_state = "included" if nightly_included else "excluded"
    if previous_state == desired_state:
        rprint(
            f"[dim]Already {desired_state}. No change.[/dim]"
        )
        return

    try:
        _previous, _updated, _saved_path = set_channel_nightly_included(
            channel_id,
            nightly_included,
        )
    except ValueError as exc:
        typer.echo(
            "Cannot include a `safe` channel. Safe channels are excluded by default "
            "to protect team privacy. Upgrade first: "
            f"`engram channels upgrade {channel_id} trusted`."
        )
        raise typer.Exit(code=3) from exc
    except ManifestError as exc:
        rprint(f"[red]Failed to load manifest: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if nightly_included:
        rprint(
            f"Channel '{channel_id}' included in nightly cross-channel summary."
        )
    else:
        rprint(
            f"Channel '{channel_id}' excluded from nightly cross-channel summary."
        )
    rprint(f"Previous state: {previous_state}")


@app.command("exclude")
def exclude(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
) -> None:
    """Exclude a channel from the nightly cross-channel summary."""
    _set_nightly_state(channel_id, nightly_included=False)


@app.command("include")
def include(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
) -> None:
    """Include a channel in the nightly cross-channel summary."""
    _set_nightly_state(channel_id, nightly_included=True)


@mcp_app.command("allow")
def mcp_allow(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
    server_name: str = typer.Argument(..., help="MCP server name."),
) -> None:
    """Allow one MCP server in a channel manifest."""
    try:
        async def _confirm_unknown(_plan, decisions) -> MCPApprovalDisposition:
            decision = decisions[0]
            summary = decision.trust_summary or decision.reason or "metadata unavailable"
            approved = Confirm.ask(
                f"Allow unknown-tier MCP '{decision.server_name}' ({summary})?",
                default=False,
            )
            return (
                MCPApprovalDisposition.APPROVED
                if approved
                else MCPApprovalDisposition.DENIED
            )

        result = asyncio.run(
            apply_channel_mcp_change(
                channel_id,
                server_name,
                action="allow",
                confirm_unknown=_confirm_unknown,
            )
        )
    except ValueError as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except ManifestError as exc:
        rprint(f"[red]Failed to load manifest: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if result.status == ChannelMCPChangeStatus.UNCHANGED:
        rprint(
            _mcp_access_noop_text(
                action="allow",
                manifest=result.previous_manifest,
                server_name=result.normalized_name,
            )
        )
        return
    if result.status == ChannelMCPChangeStatus.APPROVAL_DENIED:
        rprint(
            f"[yellow]Skipped MCP server [bold]{result.normalized_name}[/bold]; "
            "owner confirmation was declined.[/yellow]"
        )
        raise typer.Exit(code=1)

    rprint(
        f"[green]✓[/] Allowed MCP server [bold]{result.normalized_name}[/bold] "
        f"in [bold]{channel_id}[/bold]."
    )
    rprint(render_channel_mcp_access(result.updated_manifest))


@mcp_app.command("deny")
def mcp_deny(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
    server_name: str = typer.Argument(..., help="MCP server name."),
) -> None:
    """Deny one MCP server in a channel manifest."""
    try:
        previous, updated, _manifest_path, normalized_name = (
            set_channel_mcp_server_access(
                channel_id,
                server_name,
                action="deny",
            )
        )
    except ValueError as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except ManifestError as exc:
        rprint(f"[red]Failed to load manifest: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if previous == updated:
        rprint(
            _mcp_access_noop_text(
                action="deny",
                manifest=previous,
                server_name=normalized_name,
            )
        )
        return

    rprint(
        f"[green]✓[/] Denied MCP server [bold]{normalized_name}[/bold] "
        f"in [bold]{channel_id}[/bold]."
    )
    rprint(render_channel_mcp_access(updated))


@mcp_app.command("list")
def mcp_list(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
) -> None:
    """Show the effective MCP access for one channel."""
    _manifest_path, manifest = _load_manifest_or_exit(channel_id)
    rprint(render_channel_mcp_access(manifest))


if __name__ == "__main__":
    app()
