from __future__ import annotations

import re
from datetime import timedelta

import pytest

from engram.egress import ActiveYoloGrantRow, render_active_yolo_grants
from engram.ingress import (
    CHANNELS_PAGE_ACTION_PATTERN,
    NIGHTLY_TOGGLE_ACTION_PATTERN,
    TIER_PICK_ACTION_PATTERN,
    YOLO_DURATION_ACTION_PATTERN,
    ChannelDashboardRow,
    _render_channels_dashboard,
    _render_yolo_duration_picker,
    build_tier_picker_blocks,
)
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
)


def _collect_action_ids(blocks: list[dict[str, object]]) -> list[str]:
    action_ids: list[str] = []
    for block in blocks:
        elements = block.get("elements")
        if not isinstance(elements, list):
            continue
        for element in elements:
            if not isinstance(element, dict):
                continue
            action_id = element.get("action_id")
            if isinstance(action_id, str):
                action_ids.append(action_id)
    return action_ids


def _assert_unique_action_ids(blocks: list[dict[str, object]]) -> list[str]:
    action_ids = _collect_action_ids(blocks)
    assert action_ids
    assert len(action_ids) == len(set(action_ids))
    return action_ids


def _matches_any_pattern(action_id: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.match(action_id) for pattern in patterns)


def _dashboard_row(
    channel_id: str,
    *,
    tier: PermissionTier = PermissionTier.OWNER_SCOPED,
    nightly_included: bool = True,
) -> ChannelDashboardRow:
    label = f"channel-{channel_id[-2:].lower()}"
    return ChannelDashboardRow(
        channel_id=channel_id,
        label=label,
        sort_label=label,
        manifest=ChannelManifest(
            channel_id=channel_id,
            identity=IdentityTemplate.TASK_ASSISTANT,
            status=ChannelStatus.ACTIVE,
            label=f"#{label}",
            permission_tier=tier,
            nightly_included=nightly_included,
        ),
        is_owner_dm=False,
        is_private=False,
        is_archived=False,
    )


def _dashboard_smoke_blocks() -> list[dict[str, object]]:
    rows = [
        _dashboard_row(f"C07PAGE{index:03d}")
        for index in range(41)
    ]
    _text, blocks, rendered_page = _render_channels_dashboard(rows, page=1)
    assert rendered_page == 1
    return blocks


def test_build_tier_picker_blocks_action_ids_are_unique_and_match_pattern() -> None:
    _text, blocks = build_tier_picker_blocks(
        channel_id="C07TEST123",
        current_tier=PermissionTier.TASK_ASSISTANT,
        is_owner=True,
        invoker_user_id="U07OWNER",
    )

    action_ids = _assert_unique_action_ids(blocks)

    assert all(TIER_PICK_ACTION_PATTERN.match(action_id) for action_id in action_ids)


def test_render_channels_dashboard_action_ids_are_unique_for_multiple_channels() -> None:
    rows = [
        _dashboard_row("C07TEST001"),
        _dashboard_row("C07TEST002", tier=PermissionTier.TASK_ASSISTANT),
    ]

    _text, blocks, rendered_page = _render_channels_dashboard(rows, page=0)
    action_ids = _assert_unique_action_ids(blocks)

    assert rendered_page == 0
    assert all(
        _matches_any_pattern(
            action_id,
            (TIER_PICK_ACTION_PATTERN, NIGHTLY_TOGGLE_ACTION_PATTERN),
        )
        for action_id in action_ids
    )


@pytest.mark.parametrize(
    ("builder_name", "builder", "patterns"),
    [
        (
            "tier_picker",
            lambda: build_tier_picker_blocks(
                channel_id="C07TEST123",
                current_tier=PermissionTier.OWNER_SCOPED,
                is_owner=True,
                invoker_user_id="U07OWNER",
            )[1],
            (TIER_PICK_ACTION_PATTERN,),
        ),
        (
            "channels_dashboard",
            _dashboard_smoke_blocks,
            (
                TIER_PICK_ACTION_PATTERN,
                NIGHTLY_TOGGLE_ACTION_PATTERN,
                CHANNELS_PAGE_ACTION_PATTERN,
            ),
        ),
        (
            "yolo_duration_picker",
            lambda: _render_yolo_duration_picker(channel_id="C07TEST123")[1],
            (YOLO_DURATION_ACTION_PATTERN,),
        ),
        (
            "active_yolo_grants",
            lambda: render_active_yolo_grants(
                [
                    ActiveYoloGrantRow(
                        channel_id="C07TEST001",
                        channel_label="#alpha",
                        remaining=timedelta(hours=6),
                        pre_yolo_tier=PermissionTier.OWNER_SCOPED,
                    ),
                    ActiveYoloGrantRow(
                        channel_id="C07TEST002",
                        channel_label="#beta",
                        remaining=timedelta(hours=24),
                        pre_yolo_tier=PermissionTier.TASK_ASSISTANT,
                    ),
                ]
            )[1],
            (),
        ),
    ],
)
def test_block_builders_emit_unique_action_ids_smoke(
    builder_name: str,
    builder,
    patterns: tuple[re.Pattern[str], ...],
) -> None:
    blocks = builder()
    action_ids = _assert_unique_action_ids(blocks)

    if patterns:
        assert all(
            _matches_any_pattern(action_id, patterns)
            for action_id in action_ids
        ), builder_name
