"""Reusable trust gate for MCP allow-list manifest additions."""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from engram import paths
from engram.manifest import MCPManifestChangePlan
from engram.mcp import load_claude_mcp_servers
from engram.mcp_trust import (
    MCPTrustDecision,
    MCPTrustTier,
    render_community_notification,
    resolve_mcp_server_trust,
)


class MCPApprovalDisposition(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    PENDING = "pending"


@dataclass(frozen=True)
class MCPManifestApprovalResult:
    disposition: MCPApprovalDisposition
    plan: MCPManifestChangePlan | None
    decisions: list[MCPTrustDecision]


async def request_approved_mcp_manifest_change(
    plan: MCPManifestChangePlan,
    *,
    channel_label: str | None = None,
    owner_alert: Callable[[str], Awaitable[None] | None] | None = None,
    confirm_unknown: Callable[
        [MCPManifestChangePlan, list[MCPTrustDecision]],
        Awaitable[MCPApprovalDisposition | bool] | MCPApprovalDisposition | bool,
    ]
    | None = None,
    home: Path | None = None,
    inventory: dict[str, dict[str, object]] | None = None,
    trust_resolver: Callable[..., Awaitable[MCPTrustDecision]] | None = None,
) -> MCPManifestApprovalResult:
    """Resolve MCP trust for a staged manifest change plan."""
    additions = list(dict.fromkeys(plan.additions))
    if not additions:
        return MCPManifestApprovalResult(
            disposition=MCPApprovalDisposition.APPROVED,
            plan=plan,
            decisions=[],
        )

    home_path = paths.engram_home(home)
    configured = dict(load_claude_mcp_servers() if inventory is None else inventory)
    resolver = trust_resolver or resolve_mcp_server_trust
    decisions: list[MCPTrustDecision] = []
    for server_name in additions:
        decisions.append(
            await resolver(
                server_name,
                configured.get(server_name),
                home=home_path,
            )
        )

    unknown = [decision for decision in decisions if decision.tier == MCPTrustTier.UNKNOWN]
    community = [
        decision
        for decision in decisions
        if decision.tier == MCPTrustTier.COMMUNITY_TRUSTED
    ]
    if not unknown:
        if community and owner_alert is not None:
            maybe_awaitable = owner_alert(
                render_community_notification(
                    channel_id=plan.current_manifest.channel_id,
                    channel_label=channel_label or plan.current_manifest.label,
                    decisions=community,
                )
            )
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        return MCPManifestApprovalResult(
            disposition=MCPApprovalDisposition.APPROVED,
            plan=plan,
            decisions=decisions,
        )

    if confirm_unknown is None:
        return MCPManifestApprovalResult(
            disposition=MCPApprovalDisposition.DENIED,
            plan=None,
            decisions=decisions,
        )

    outcome = confirm_unknown(plan, decisions)
    if inspect.isawaitable(outcome):
        outcome = await outcome
    disposition = _normalize_disposition(outcome)
    return MCPManifestApprovalResult(
        disposition=disposition,
        plan=plan if disposition == MCPApprovalDisposition.APPROVED else None,
        decisions=decisions,
    )


def _normalize_disposition(
    value: MCPApprovalDisposition | bool,
) -> MCPApprovalDisposition:
    if isinstance(value, MCPApprovalDisposition):
        return value
    return MCPApprovalDisposition.APPROVED if value else MCPApprovalDisposition.DENIED
