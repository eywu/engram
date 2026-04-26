from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["safe", "trusted", "yolo"]
MCPAccessAction = Literal["allow", "deny"]

_TIER_RANK: dict[Tier, int] = {
    "safe": 0,
    "trusted": 1,
    "yolo": 2,
}


@dataclass(frozen=True)
class TransitionDecision:
    allowed: bool
    reason: str


def classify_mcp_access_change(
    *,
    action: MCPAccessAction,
    has_allow_list: bool,
    is_allowed: bool,
    is_disallowed: bool,
) -> Literal["grant", "revoke", "no-op"]:
    """Return whether an MCP access change grants, revokes, or is a no-op."""
    if action == "allow":
        if has_allow_list:
            return "no-op" if is_allowed and not is_disallowed else "grant"
        return "grant" if is_disallowed else "no-op"
    return "no-op" if is_disallowed else "revoke"


def classify_transition(
    from_tier: Tier,
    to_tier: Tier,
) -> Literal["upgrade", "downgrade", "no-op"]:
    """Return whether the transition is upgrade, downgrade, or no-op."""
    if from_tier == to_tier:
        return "no-op"
    return "upgrade" if _TIER_RANK[to_tier] > _TIER_RANK[from_tier] else "downgrade"


def can_change_tier(
    *,
    current_tier: Tier,
    target_tier: Tier,
    invoker_user_id: str,
    channel_owner_user_id: str,
) -> TransitionDecision:
    """Decide whether the invoker may change the tier."""
    kind = classify_transition(current_tier, target_tier)
    if kind == "no-op":
        return TransitionDecision(allowed=True, reason=f"Already on `{current_tier}`.")
    if kind == "downgrade":
        return TransitionDecision(
            allowed=True,
            reason=f"Downgrade to `{target_tier}` (anyone can downgrade).",
        )
    if invoker_user_id == channel_owner_user_id:
        return TransitionDecision(
            allowed=True,
            reason=f"Owner upgrading to `{target_tier}`.",
        )
    return TransitionDecision(
        allowed=False,
        reason=(
            f"Only the channel owner can upgrade to `{target_tier}`. "
            "Ask owner to run `/engram upgrade`."
        ),
    )


def can_change_mcp_access(
    *,
    action: MCPAccessAction,
    server_name: str,
    has_allow_list: bool,
    is_allowed: bool,
    is_disallowed: bool,
    invoker_user_id: str,
    channel_owner_user_id: str,
) -> TransitionDecision:
    """Decide whether the invoker may mutate per-channel MCP access."""
    kind = classify_mcp_access_change(
        action=action,
        has_allow_list=has_allow_list,
        is_allowed=is_allowed,
        is_disallowed=is_disallowed,
    )
    if kind == "no-op":
        state = "allowed" if action == "allow" else "denied"
        return TransitionDecision(
            allowed=True,
            reason=f"MCP server `{server_name}` is already {state}.",
        )
    if kind == "revoke":
        return TransitionDecision(
            allowed=True,
            reason=f"Denying MCP server `{server_name}` (anyone can reduce access).",
        )
    if invoker_user_id == channel_owner_user_id:
        return TransitionDecision(
            allowed=True,
            reason=f"Owner granting MCP access to `{server_name}`.",
        )
    return TransitionDecision(
        allowed=False,
        reason=(
            f"Only the channel owner can grant MCP access to `{server_name}`. "
            f"Ask owner to run `/engram mcp allow {server_name}`."
        ),
    )
