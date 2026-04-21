"""CLI tests for `engram channels`.

Uses typer.testing.CliRunner + monkey-patched HOME so the commands see a
tmp_path-based ~/.engram layout.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from engram.bootstrap import provision_channel
from engram.cli_channels import app
from engram.manifest import (
    ChannelStatus,
    IdentityTemplate,
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
    _provision(tmp_path / ".engram".lstrip("/"), "C07TEAM", IdentityTemplate.TASK_ASSISTANT)
    # Easier: use the exact pattern the CLI looks up.
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
    assert "pending" in result.output.lower()


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
    assert "task-assistant" in result.output
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
