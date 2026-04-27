"""Operator-facing MCP onboarding helpers."""
from __future__ import annotations

import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rich import print as rprint
from rich.prompt import Confirm

from engram import paths
from engram.manifest import (
    ManifestError,
    MCPManifestChangePlan,
    load_manifest,
    persist_approved_mcp_manifest_change,
)
from engram.mcp import (
    MCPChannelCoverage,
    audit_mcp_channel_coverage,
    detect_new_user_mcp_servers,
    load_claude_mcp_servers,
    write_mcp_inventory_state,
)
from engram.mcp_trust import MCPTrustTier, resolve_mcp_server_trust

log = logging.getLogger(__name__)


def render_new_mcp_sync_needed_message(server_names: list[str]) -> str:
    servers = ", ".join(server_names)
    return (
        "New MCPs detected in ~/.claude.json but not yet allowed in any team channel "
        f"manifests: {servers}. Owner DMs can use them already; strict team channels "
        "cannot. Fix: run `engram doctor` or `engram setup`, or add each server under "
        "`mcp_servers.allowed` in "
        "~/.engram/contexts/<channel-id>/.claude/channel-manifest.yaml."
    )


async def sync_team_channel_mcp_allow_lists(
    configured_servers: dict[str, dict[str, object]],
    coverage: MCPChannelCoverage,
    *,
    home: Path | None = None,
    target_servers: list[str] | None = None,
    confirm: Callable[[str, bool], bool] | None = None,
    printer: Callable[[str], None] = rprint,
    prompt_to_continue: bool = True,
    audit_source: str = "setup_wizard",
    trust_resolver: Callable[..., Awaitable[Any]] = resolve_mcp_server_trust,
) -> bool:
    """Interactively add uncovered MCPs to existing strict team manifests."""
    target_filter = set(target_servers or coverage.uncovered_servers)
    servers_to_sync = [
        name for name in coverage.uncovered_servers if name in target_filter
    ]
    if not servers_to_sync:
        return False

    # GRO-532 fix: surface broken manifests to the operator instead of
    # silently skipping them. A wizard run that says "no team manifest
    # changes applied" with two corrupted manifests is silently failing.
    # The operator needs to know which manifests need repair.
    manifests: list[tuple[object, Path]] = []
    for channel_id in coverage.team_channels:
        manifest_path = coverage.team_manifest_paths.get(channel_id)
        if manifest_path is None:
            continue
        try:
            manifests.append((load_manifest(manifest_path), manifest_path))
        except ManifestError as exc:
            printer(
                f"  [yellow]⚠[/yellow] could not parse {manifest_path}: "
                f"{exc}. Skipping this channel."
            )
            continue

    if not manifests:
        return False

    ask = confirm or (lambda message, default: Confirm.ask(message, default=default))
    if prompt_to_continue and not ask(
        "  Enable these MCPs in existing team channel manifests now?",
        True,
    ):
        return False

    home_path = paths.engram_home(home)
    changed_any = False
    for server_name in servers_to_sync:
        decision = await trust_resolver(
            server_name,
            configured_servers.get(server_name),
            home=home_path,
        )
        if decision.tier == MCPTrustTier.UNKNOWN:
            summary = decision.trust_summary or decision.reason or "metadata unavailable"
            printer(
                f"  [yellow]⚠[/yellow] {server_name} is [italic]{decision.tier.value}[/italic] "
                f"({summary}). Explicit confirmation required per channel."
            )

        for idx, (manifest, manifest_path) in enumerate(manifests):
            allowed = list(manifest.mcp_servers.allowed or [])
            if server_name in allowed:
                continue

            channel_label = manifest.label or manifest.channel_id
            should_allow = ask(
                f"  Allow [cyan]{server_name}[/cyan] in {channel_label} ({manifest.channel_id})?",
                decision.tier != MCPTrustTier.UNKNOWN,
            )
            if not should_allow:
                continue

            merged_allowed = list(dict.fromkeys([*allowed, server_name]))
            updated_manifest = manifest.model_copy(
                update={
                    "mcp_servers": manifest.mcp_servers.model_copy(
                        update={"allowed": merged_allowed}
                    )
                }
            )
            try:
                current_text = manifest_path.read_text(encoding="utf-8")
                persist_approved_mcp_manifest_change(
                    MCPManifestChangePlan(
                        manifest_path=manifest_path,
                        current_manifest=manifest,
                        staged_manifest=updated_manifest,
                        staged_text="",
                        baseline_sha256=hashlib.sha256(
                            current_text.encode("utf-8")
                        ).hexdigest(),
                        additions=[server_name],
                    ),
                    audit_source=audit_source,
                )
            except ManifestError as exc:
                printer(
                    f"    [yellow]⚠[/yellow] could not update {manifest.channel_id}: {exc}"
                )
                continue

            manifests[idx] = (updated_manifest, manifest_path)
            changed_any = True
            suffix = ""
            if decision.tier == MCPTrustTier.COMMUNITY_TRUSTED:
                summary = decision.trust_summary or decision.reason or "community-trusted"
                suffix = f"  [dim](community-trusted: {summary})[/dim]"
            elif decision.tier == MCPTrustTier.UNKNOWN:
                suffix = "  [dim](unknown tier; operator confirmed)[/dim]"
            printer(
                f"    [green]✓[/green] allowed {server_name} in {channel_label} ({manifest.channel_id}){suffix}"
            )

    if not changed_any:
        printer("  [dim]no team manifest changes applied[/dim]")
    return changed_any


async def maybe_prompt_for_new_mcp_servers(
    *,
    home: Path | None = None,
    configured_servers: dict[str, dict[str, object]] | None = None,
    coverage: MCPChannelCoverage | None = None,
    interactive: bool,
    owner_alert: Callable[[str], Awaitable[None] | None] | None = None,
    confirm: Callable[[str, bool], bool] | None = None,
    printer: Callable[[str], None] = rprint,
) -> list[str]:
    """Handle post-setup user MCP additions on the next Engram startup."""
    home_path = paths.engram_home(home)
    configured = (
        load_claude_mcp_servers()
        if configured_servers is None
        else dict(configured_servers)
    )
    delta = detect_new_user_mcp_servers(configured, home=home_path)
    current_coverage = coverage or audit_mcp_channel_coverage(
        contexts_path=paths.contexts_dir(home_path),
        configured_servers=configured,
    )
    new_uncovered = [
        name for name in delta.new_servers if name in current_coverage.uncovered_servers
    ]

    if not new_uncovered or not current_coverage.team_channels:
        write_mcp_inventory_state(configured, home=home_path)
        return []

    # GRO-532 fix: only mark these MCPs as "acknowledged" in the
    # inventory state file after a successful sync OR after the
    # operator explicitly dismissed the prompt. Previously the state
    # file was always written at the end of this function, which meant
    # that if all manifests were malformed (silently skipped) or the
    # interactive sync failed for any reason, the next Engram startup
    # would not re-prompt and the new MCP would have no team-channel
    # coverage forever.
    sync_succeeded = False
    if interactive:
        servers = ", ".join(new_uncovered)
        printer("")
        printer(
            "  [yellow]⚠[/yellow] New MCPs detected in [italic]~/.claude.json[/italic] "
            f"since the last Engram run: {servers}"
        )
        printer(
            "  Owner DMs can use them already. Team channels still need "
            "[italic]mcp_servers.allowed[/italic] entries."
        )
        sync_succeeded = await sync_team_channel_mcp_allow_lists(
            configured,
            current_coverage,
            home=home_path,
            target_servers=new_uncovered,
            confirm=confirm,
            printer=printer,
            audit_source="startup_prompt",
        )
    else:
        message = render_new_mcp_sync_needed_message(new_uncovered)
        log.warning(
            "mcp.onboarding_sync_needed servers=%s team_channels=%s",
            new_uncovered,
            current_coverage.team_channels,
        )
        if owner_alert is not None:
            maybe_awaitable = owner_alert(message)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable

    # State-write policy:
    # - non-interactive (bridge daemon path): the owner_alert WAS the
    #   action. Advance the state; re-alerting on every bridge restart
    #   would be spammy. The operator can act on the alert manually.
    # - interactive: only advance when at least one manifest was actually
    #   updated. If the operator declined the prompt or the sync failed
    #   (e.g. all manifests malformed and skipped — see Fix 3 above), do
    #   NOT consume the prompt; the next startup should re-prompt so the
    #   new MCP doesn't get stuck without team-channel coverage forever.
    if not interactive or sync_succeeded:
        write_mcp_inventory_state(configured, home=home_path)
    return new_uncovered
