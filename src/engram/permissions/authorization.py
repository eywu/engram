from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["safe", "trusted", "yolo"]

_TIER_RANK: dict[Tier, int] = {
    "safe": 0,
    "trusted": 1,
    "yolo": 2,
}


@dataclass(frozen=True)
class TransitionDecision:
    allowed: bool
    reason: str


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
