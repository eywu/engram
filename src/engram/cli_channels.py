"""`engram channels ...` — manifest-driven channel lifecycle CLI.

Sub-commands:
  engram channels list              — all provisioned channels + status
  engram channels show <id>         — full manifest + CLAUDE.md preview
  engram channels approve <id>      — flip PENDING → ACTIVE
  engram channels deny <id>         — flip any → DENIED (silently ignored)
  engram channels reset <id>        — back to PENDING (owner must re-approve)

The CLI is the human-friendly front-end for the `status` field in
ChannelManifest. It edits YAML directly (round-trip via dump_manifest),
so changes take effect on the bridge's next router cache miss — i.e. on
next restart, or when a channel hasn't been resolved yet this run.
"""
from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from engram import paths
from engram.manifest import (
    ChannelStatus,
    ManifestError,
    PermissionTier,
    load_manifest,
    set_channel_permission_tier,
    set_channel_status,
    validate_upgrade_duration,
)

app = typer.Typer(
    name="channels",
    help="List and manage per-channel Engram manifests.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
log = logging.getLogger(__name__)


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


# ── Commands ────────────────────────────────────────────────────────────


@app.command("list")
def list_channels() -> None:
    """List every provisioned channel and its status."""
    manifest_paths = _iter_manifest_paths()
    if not manifest_paths:
        rprint(
            "[dim]No channels provisioned yet. "
            "They appear here after the bot first sees a message "
            "in a new channel.[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Channel ID")
    table.add_column("Status")
    table.add_column("Identity")
    table.add_column("Label")
    table.add_column("Setting", overflow="fold")

    for mp in manifest_paths:
        try:
            m = load_manifest(mp)
        except ManifestError:
            table.add_row(
                mp.parent.parent.name,
                "[red]BROKEN[/red]",
                "—",
                "—",
                f"[dim]{mp}[/dim]",
            )
            continue
        table.add_row(
            m.channel_id,
            f"[{_status_style(m.status)}]{m.status}[/]",
            m.identity.value,
            m.label or "—",
            ",".join(m.setting_sources),
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
    rprint(f"  identity: {m.identity.value}")
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


@app.command("upgrade")
def upgrade(
    channel_id: str = typer.Argument(..., help="Slack channel ID."),
    tier: str = typer.Argument(..., help="Target tier."),
    until: str = typer.Option(
        "permanent",
        "--until",
        help="Upgrade duration: 24h, 30d, or permanent.",
    ),
) -> None:
    """Upgrade a channel tier immediately, bypassing the Slack approval flow."""
    try:
        target_tier = PermissionTier(tier.strip().lower())
    except ValueError as exc:
        rprint(f"[red]Unknown permission tier: {tier}[/red]")
        raise typer.Exit(code=2) from exc

    try:
        normalized_duration = validate_upgrade_duration(until)
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
    manifest_path = paths.channel_manifest_path(channel_id)
    if not manifest_path.exists():
        rprint(f"[red]No manifest found for '{channel_id}'.[/red]")
        raise typer.Exit(code=1)

    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        rprint(f"[red]Failed to load manifest: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    expiry = manifest.yolo_until.isoformat() if manifest.yolo_until is not None else "none"
    yolo_status = "active" if manifest.tier_effective() == PermissionTier.YOLO else "inactive"
    rprint(f"[bold]{channel_id}[/bold]")
    rprint(f"  tier:   {manifest.tier_effective().value}")
    rprint(f"  yolo:   {yolo_status}")
    rprint(f"  expiry: {expiry}")


if __name__ == "__main__":
    app()
