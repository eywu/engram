"""Permission-tier foundation tests."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from engram.manifest import (
    _TIER_DEFAULTS,
    ABSOLUTE_DENY_RULES,
    OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES,
    ChannelManifest,
    IdentityTemplate,
    PermissionTier,
    load_manifest,
)
from engram.scope import build_scope_decision


def test_permission_tier_enum_round_trip():
    for tier in PermissionTier:
        manifest = ChannelManifest(
            channel_id="C07TEST123",
            identity=IdentityTemplate.TASK_ASSISTANT,
            permission_tier=tier,
        )

        dumped = manifest.model_dump(mode="json")

        assert dumped["permission_tier"] == tier.value
        assert ChannelManifest.model_validate(dumped).permission_tier == tier


def test_tier_defaults_exposed():
    assert set(_TIER_DEFAULTS) == set(PermissionTier)
    assert _TIER_DEFAULTS[PermissionTier.TASK_ASSISTANT]["hitl_max_per_day"] == 1000
    assert tuple(_TIER_DEFAULTS[PermissionTier.OWNER_SCOPED]["allow_rules"]) == (
        OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES
    )
    assert tuple(_TIER_DEFAULTS[PermissionTier.YOLO]["deny_rules"]) == (
        ABSOLUTE_DENY_RULES
    )


def test_load_manifest_migration_idempotent(tmp_path: Path):
    path = tmp_path / "channel-manifest.yaml"
    path.write_text(
        """
channel_id: D07OWNER
identity: owner-dm-full
label: DM
status: active
setting_sources: [user]
permissions:
  allow: []
"""
    )

    first = load_manifest(path)
    second = load_manifest(path)

    assert first == second
    assert first.permission_tier == PermissionTier.OWNER_SCOPED
    assert first.permissions.allow == list(OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES)

    persisted = yaml.safe_load(path.read_text())
    assert persisted["permission_tier"] == "trusted"
    assert persisted["permissions"]["allow"] == list(
        OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES
    )


def test_load_manifest_migrates_deprecated_tier_alias_fixture(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "manifests"
        / "legacy-permission-tiers.yaml"
    )
    path = tmp_path / "channel-manifest.yaml"
    path.write_text(fixture.read_text())

    with caplog.at_level(logging.INFO, logger="engram.manifest"):
        manifest = load_manifest(path)

    assert manifest.permission_tier == PermissionTier.TASK_ASSISTANT
    assert manifest.pre_yolo_tier == PermissionTier.OWNER_SCOPED
    assert "channel.permission_tier_migrated" in caplog.text
    persisted = yaml.safe_load(path.read_text())
    assert persisted["permission_tier"] == "safe"
    assert persisted["pre_yolo_tier"] == "trusted"


def test_tier_effective_lazy_yolo_demotion():
    expired = datetime.now(UTC) - timedelta(minutes=5)
    active = datetime.now(UTC) + timedelta(minutes=5)

    expired_manifest = ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        permission_tier=PermissionTier.YOLO,
        yolo_until=expired,
        pre_yolo_tier=PermissionTier.OWNER_SCOPED,
    )
    active_manifest = ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        permission_tier=PermissionTier.YOLO,
        yolo_until=active,
        pre_yolo_tier=PermissionTier.OWNER_SCOPED,
    )

    assert expired_manifest.permission_tier == PermissionTier.YOLO
    assert expired_manifest.tier_effective() == PermissionTier.OWNER_SCOPED
    assert active_manifest.tier_effective() == PermissionTier.YOLO


def test_load_manifest_leaves_expired_yolo_for_lazy_demotion(
    tmp_path: Path,
):
    path = tmp_path / "channel-manifest.yaml"
    expired = datetime(2026, 4, 22, 12, 0, tzinfo=UTC).isoformat()
    path.write_text(
        f"""
channel_id: D07OWNER
identity: owner-dm-full
label: DM
status: active
permission_tier: yolo
yolo_until: "{expired}"
pre_yolo_tier: owner-scoped
setting_sources: [user]
"""
    )

    manifest = load_manifest(path)

    assert manifest.permission_tier == PermissionTier.YOLO
    assert manifest.yolo_until == datetime.fromisoformat(expired)
    assert manifest.pre_yolo_tier == PermissionTier.OWNER_SCOPED


def test_absolute_deny_list_enforced_on_load(tmp_path: Path):
    path = tmp_path / "channel-manifest.yaml"
    path.write_text(
        """
channel_id: D07OWNER
identity: owner-dm-full
label: DM
status: active
permission_tier: trusted
setting_sources: [user]
permissions:
  deny:
    - "Read(./tmp/**)"
"""
    )

    manifest = load_manifest(path)
    decision = build_scope_decision(manifest)

    assert "Read(./tmp/**)" in manifest.permissions.deny
    for rule in ABSOLUTE_DENY_RULES:
        assert rule in manifest.permissions.deny
        assert rule in decision.disallowed_tools


def test_task_assistant_tier_denies_survive_user_additions(tmp_path: Path):
    path = tmp_path / "channel-manifest.yaml"
    path.write_text(
        """
channel_id: C07TEST
identity: task-assistant
status: active
permission_tier: safe
setting_sources: [project]
permissions:
  deny:
    - "Read(./secrets/**)"
"""
    )

    manifest = load_manifest(path)

    assert "Read(./secrets/**)" in manifest.permissions.deny
    for rule in _TIER_DEFAULTS[PermissionTier.TASK_ASSISTANT]["deny_rules"]:
        assert rule in manifest.permissions.deny, f"tier rule dropped: {rule}"
