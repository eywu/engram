"""Tests for centralized tier transition authorization."""
from __future__ import annotations

from itertools import product

import pytest

from engram.permissions.authorization import can_change_tier, classify_transition

TIERS = ("safe", "trusted", "yolo")
CHANNEL_KINDS = ("owner-dm", "private", "public")
INVOKER_KINDS = ("owner", "non-owner")
TIER_RANK = {"safe": 0, "trusted": 1, "yolo": 2}


def _expected_transition(
    from_tier: str,
    to_tier: str,
) -> str:
    if from_tier == to_tier:
        return "no-op"
    return "upgrade" if TIER_RANK[to_tier] > TIER_RANK[from_tier] else "downgrade"


def _owner_user_id(channel_kind: str) -> str:
    return {
        "owner-dm": "U07OWNERDM",
        "private": "U07PRIVATE",
        "public": "U07PUBLIC",
    }[channel_kind]


@pytest.mark.parametrize(
    ("from_tier", "to_tier", "invoker_kind", "channel_kind"),
    [
        pytest.param(from_tier, to_tier, invoker_kind, channel_kind)
        for from_tier, to_tier, invoker_kind, channel_kind in product(
            TIERS,
            TIERS,
            INVOKER_KINDS,
            CHANNEL_KINDS,
        )
    ],
)
def test_tier_authorization_matrix(
    from_tier: str,
    to_tier: str,
    invoker_kind: str,
    channel_kind: str,
) -> None:
    owner_user_id = _owner_user_id(channel_kind)
    invoker_user_id = (
        owner_user_id if invoker_kind == "owner" else f"{owner_user_id}_OTHER"
    )
    expected_transition = _expected_transition(from_tier, to_tier)

    decision = can_change_tier(
        current_tier=from_tier,
        target_tier=to_tier,
        invoker_user_id=invoker_user_id,
        channel_owner_user_id=owner_user_id,
    )

    assert classify_transition(from_tier, to_tier) == expected_transition

    if expected_transition == "no-op":
        assert decision.allowed is True
        assert decision.reason == f"Already on `{from_tier}`."
        return

    if expected_transition == "downgrade":
        assert decision.allowed is True
        assert decision.reason == (
            f"Downgrade to `{to_tier}` (anyone can downgrade)."
        )
        return

    if invoker_kind == "owner":
        assert decision.allowed is True
        assert decision.reason == f"Owner upgrading to `{to_tier}`."
        return

    assert decision.allowed is False
    assert decision.reason == (
        f"Only the channel owner can upgrade to `{to_tier}`. "
        "Ask owner to run `/engram upgrade`."
    )
