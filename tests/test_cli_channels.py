"""CLI tests for `engram channels`.

Uses typer.testing.CliRunner + monkey-patched HOME so the commands see a
tmp_path-based ~/.engram layout.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from engram.bootstrap import provision_channel
from engram.cli_channels import app
from engram.manifest import (
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
    dump_manifest,
    load_manifest,
)
from engram.paths import channel_manifest_path


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Runs the CLI with ~/.engram pointed at tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Pydantic models use pathlib.Path.home() which reads HOME at call time
    # — our paths helpers do the same — so monkeypatching HOME is enough.
    return CliRunner()


def _provision(tmp_path: Path, channel_id: str, identity: IdentityTemplate):
    return provision_channel(
        channel_id,
        identity=identity,
        label=channel_id,
        home=tmp_path / ".engram",
    )


# ── list ────────────────────────────────────────────────────────────────


def test_list_empty(cli, tmp_path: Path):
    result = cli.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No channels provisioned" in result.output


def test_list_after_provisioning(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path / ".engram",
    )
    result = cli.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "C07TEAM" in result.output
    assert "Name" in result.output
    assert "Nightly" in result.output
    assert "YOLO Expiry" in result.output
    assert "Owner DM" in result.output
    assert "pending" in result.output.lower()
    assert "safe" in result.output
    assert "excluded" in result.output


def test_list_json_has_versioned_stable_schema(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    home = tmp_path / ".engram"
    fixed_now = datetime(2026, 4, 24, 18, 0, tzinfo=UTC)
    pc(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Alice (DM)",
        home=home,
    )
    owner_path = channel_manifest_path("D07OWNER", home)
    owner_manifest = load_manifest(owner_path)
    dump_manifest(
        owner_manifest.model_copy(
            update={
                "permission_tier": PermissionTier.YOLO,
                "nightly_included": True,
                "yolo_granted_at": fixed_now - timedelta(hours=1),
                "yolo_until": fixed_now + timedelta(hours=23),
                "pre_yolo_tier": PermissionTier.OWNER_SCOPED,
            }
        ),
        owner_path,
    )
    pc(
        "C07SAFE",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#random",
        home=home,
    )

    result = cli.invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["version"] == "1"
    assert len(payload["channels"]) == 2
    owner_row = next(row for row in payload["channels"] if row["channel_id"] == "D07OWNER")
    safe_row = next(row for row in payload["channels"] if row["channel_id"] == "C07SAFE")
    assert set(owner_row) == {
        "channel_id",
        "channel_name",
        "error",
        "manifest_path",
        "nightly",
        "owner_dm",
        "status",
        "tier",
        "yolo_expires_at",
    }
    assert owner_row["channel_name"] == "owner-dm"
    assert owner_row["tier"] == "yolo"
    assert owner_row["nightly"] == "included"
    assert owner_row["owner_dm"] is True
    assert owner_row["status"] == "active"
    assert owner_row["yolo_expires_at"] == (fixed_now + timedelta(hours=23)).isoformat()
    assert owner_row["error"] is None
    assert safe_row["channel_name"] == "#random"
    assert safe_row["tier"] == "safe"
    assert safe_row["nightly"] == "excluded"
    assert safe_row["owner_dm"] is False
    assert safe_row["yolo_expires_at"] is None


# ── show ────────────────────────────────────────────────────────────────


def test_show_unknown_channel(cli):
    result = cli.invoke(app, ["show", "C_NOPE"])
    assert result.exit_code == 1
    assert "No manifest found" in result.output


def test_show_existing_channel(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path / ".engram",
    )
    result = cli.invoke(app, ["show", "C07TEAM"])
    assert result.exit_code == 0
    assert "C07TEAM" in result.output
    assert "safe" in result.output
    assert "pending" in result.output.lower()
    # tools disallowed list should surface
    assert "Bash" in result.output


# ── approve / deny / reset ──────────────────────────────────────────────


def test_approve_flips_pending_to_active(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path / ".engram",
    )
    result = cli.invoke(app, ["approve", "C07TEAM"])
    assert result.exit_code == 0
    assert "pending" in result.output.lower()
    assert "active" in result.output.lower()

    # Re-read manifest to confirm persisted
    m = load_manifest(channel_manifest_path("C07TEAM", tmp_path / ".engram"))
    assert m.status == ChannelStatus.ACTIVE


def test_approve_unknown_channel(cli):
    result = cli.invoke(app, ["approve", "C_NOPE"])
    assert result.exit_code == 1


def test_approve_no_op_when_already_active(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        home=tmp_path / ".engram",
    )
    # Owner-DM defaults to ACTIVE
    result = cli.invoke(app, ["approve", "D07OWNER"])
    assert result.exit_code == 0
    assert "already has status 'active'" in result.output


def test_deny_flips_to_denied(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        home=tmp_path / ".engram",
    )
    result = cli.invoke(app, ["deny", "C07TEAM"])
    assert result.exit_code == 0

    m = load_manifest(channel_manifest_path("C07TEAM", tmp_path / ".engram"))
    assert m.status == ChannelStatus.DENIED


def test_reset_flips_to_pending(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        home=tmp_path / ".engram",
    )
    # Owner-DM starts ACTIVE; reset it
    result = cli.invoke(app, ["reset", "D07OWNER"])
    assert result.exit_code == 0

    m = load_manifest(channel_manifest_path("D07OWNER", tmp_path / ".engram"))
    assert m.status == ChannelStatus.PENDING


def test_upgrade_sets_trusted_tier(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        home=tmp_path / ".engram",
    )

    result = cli.invoke(app, ["upgrade", "C07TEAM", "trusted"])

    assert result.exit_code == 0
    assert "safe" in result.output
    assert "trusted" in result.output
    manifest = load_manifest(channel_manifest_path("C07TEAM", tmp_path / ".engram"))
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED
    assert manifest.yolo_until is None


def test_upgrade_accepts_deprecated_tier_alias_with_warning(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        home=tmp_path / ".engram",
    )

    result = cli.invoke(app, ["upgrade", "C07TEAM", "task-assistant"])

    assert result.exit_code == 0
    assert "Deprecated tier name 'task-assistant'; use 'safe' instead." in result.output
    assert "already has tier 'safe'" in result.output


def test_upgrade_accepts_owner_scoped_alias_with_warning(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        home=tmp_path / ".engram",
    )

    result = cli.invoke(app, ["upgrade", "C07TEAM", "owner-scoped"])

    assert result.exit_code == 0
    assert "Deprecated tier name 'owner-scoped'; use 'trusted' instead." in result.output
    manifest = load_manifest(channel_manifest_path("C07TEAM", tmp_path / ".engram"))
    assert manifest.permission_tier == PermissionTier.OWNER_SCOPED


def test_upgrade_yolo_duration_and_tier_output(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    pc(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        home=tmp_path / ".engram",
    )

    upgrade_result = cli.invoke(app, ["upgrade", "C07TEAM", "yolo", "--until", "24h"])
    tier_result = cli.invoke(app, ["tier", "C07TEAM"])

    assert upgrade_result.exit_code == 0
    manifest = load_manifest(channel_manifest_path("C07TEAM", tmp_path / ".engram"))
    assert manifest.permission_tier == PermissionTier.YOLO
    assert manifest.yolo_until is not None
    assert manifest.pre_yolo_tier == PermissionTier.TASK_ASSISTANT
    assert tier_result.exit_code == 0
    assert "tier:   yolo" in tier_result.output
    assert "yolo:   active" in tier_result.output
    assert "expiry:" in tier_result.output


def test_exclude_happy_path_and_idempotent(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    home = tmp_path / ".engram"
    pc(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=home,
    )
    manifest_path = channel_manifest_path("C07TEAM", home)
    manifest = load_manifest(manifest_path)
    dump_manifest(
        manifest.model_copy(
            update={
                "permission_tier": PermissionTier.OWNER_SCOPED,
                "nightly_included": True,
            }
        ),
        manifest_path,
    )

    first = cli.invoke(app, ["exclude", "C07TEAM"])
    second = cli.invoke(app, ["exclude", "C07TEAM"])

    updated = load_manifest(manifest_path)
    assert first.exit_code == 0
    assert "excluded from nightly cross-channel summary" in first.output
    assert "Previous state: included" in first.output
    assert second.exit_code == 0
    assert "Already excluded. No change." in second.output
    assert updated.nightly_included is False


def test_include_happy_path_and_idempotent(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    home = tmp_path / ".engram"
    pc(
        "C07TEAM",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Owner DM",
        home=home,
    )
    manifest_path = channel_manifest_path("C07TEAM", home)
    manifest = load_manifest(manifest_path)
    dump_manifest(
        manifest.model_copy(update={"nightly_included": False}),
        manifest_path,
    )

    first = cli.invoke(app, ["include", "C07TEAM"])
    second = cli.invoke(app, ["include", "C07TEAM"])

    updated = load_manifest(manifest_path)
    assert first.exit_code == 0
    assert "included in nightly cross-channel summary" in first.output
    assert "Previous state: excluded" in first.output
    assert second.exit_code == 0
    assert "Already included. No change." in second.output
    assert updated.nightly_included is True


@pytest.mark.parametrize("command", ["exclude", "include"])
def test_include_exclude_not_found(cli, command: str):
    result = cli.invoke(app, [command, "C_NOPE"])

    assert result.exit_code == 1
    assert "No manifest found for channel 'C_NOPE'." in result.output


def test_exclude_manifest_load_error_returns_exit_2(cli, tmp_path: Path):
    home = tmp_path / ".engram"
    manifest_path = channel_manifest_path("C07BROKEN", home)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("channel_id: C07BROKEN\nstatus: [\n", encoding="utf-8")

    result = cli.invoke(app, ["exclude", "C07BROKEN"])

    assert result.exit_code == 2
    assert "Failed to load manifest" in result.output


def test_include_rejects_safe_channel_with_exit_3(cli, tmp_path: Path):
    from engram.bootstrap import provision_channel as pc

    home = tmp_path / ".engram"
    pc(
        "C07SAFE",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#random",
        home=home,
    )

    result = cli.invoke(app, ["include", "C07SAFE"])

    manifest = load_manifest(channel_manifest_path("C07SAFE", home))
    assert result.exit_code == 3
    assert (
        "Cannot include a `safe` channel. Safe channels are excluded by default "
        "to protect team privacy. Upgrade first: `engram channels upgrade C07SAFE trusted`."
        in result.output
    )
    assert manifest.nightly_included is False
