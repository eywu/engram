"""CLI tests for `engram status`."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from engram import paths
from engram.bootstrap import provision_channel
from engram.cli import app
from engram.manifest import IdentityTemplate, ScopeList, dump_manifest, load_manifest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("engram.cli._bridge_pid", lambda: None)
    for key in (
        "ENGRAM_SLACK_BOT_TOKEN",
        "ENGRAM_SLACK_APP_TOKEN",
        "ENGRAM_ANTHROPIC_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / ".engram"


def test_status_json_includes_channel_mcp_policy(isolated_home: Path):
    mcp_dir = Path.home() / ".claude"
    mcp_dir.mkdir()
    (mcp_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "linear": {
                        "type": "http",
                        "url": "https://linear.example/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=isolated_home,
    )
    manifest_path = paths.channel_manifest_path("C07TEAM", isolated_home)
    manifest = load_manifest(manifest_path)
    dump_manifest(
        manifest.model_copy(
            update={"mcp_servers": ScopeList(allowed=["linear"])}
        ),
        manifest_path,
    )

    result = CliRunner().invoke(app, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    channel = next(
        c for c in payload["channels"] if c["channel_id"] == "C07TEAM"
    )
    assert channel["mcp"]["strict_mode"] is True
    assert channel["mcp"]["servers"] == ["linear"]
    assert channel["meta_eligible"] is True


def test_scope_audit_surfaces_meta_eligibility(isolated_home: Path):
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=isolated_home,
    )
    manifest_path = paths.channel_manifest_path("C07TEAM", isolated_home)
    manifest = load_manifest(manifest_path)
    dump_manifest(manifest.model_copy(update={"meta_eligible": False}), manifest_path)

    result = CliRunner().invoke(app, ["scope", "audit", "--json"])

    assert result.exit_code == 0
    rows = json.loads(result.output)
    row = next(item for item in rows if item["channel_id"] == "C07TEAM")
    assert row["label"] == "#growth"
    assert row["meta_eligible"] is False


def test_status_surfaces_recent_nightly_heartbeat(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("engram.cli._utc_now", lambda: now)
    heartbeat_dir = isolated_home / "nightly"
    heartbeat_dir.mkdir(parents=True)
    (heartbeat_dir / "last-run.json").write_text(
        json.dumps(
            {
                "started_at": (now - timedelta(hours=2, minutes=5)).isoformat(),
                "completed_at": (now - timedelta(hours=2)).isoformat(),
                "phase_reached": "synthesize",
                "exit_code": 0,
                "cost_usd": 0.01,
                "channels_covered": 2,
                "error_msg": None,
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 0
    assert "nightly: last ran 2h ago ✓" in result.output

    json_result = CliRunner().invoke(app, ["status", "--json"])
    payload = json.loads(json_result.output)
    assert payload["nightly"]["state"] == "ok"
    assert payload["nightly"]["stale"] is False
    assert payload["nightly"]["age_hours"] == 2.0


def test_status_surfaces_stale_nightly_heartbeat(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("engram.cli._utc_now", lambda: now)
    heartbeat_dir = isolated_home / "nightly"
    heartbeat_dir.mkdir(parents=True)
    (heartbeat_dir / "last-run.json").write_text(
        json.dumps(
            {
                "started_at": (now - timedelta(hours=72, minutes=5)).isoformat(),
                "completed_at": (now - timedelta(hours=72)).isoformat(),
                "phase_reached": "synthesize",
                "exit_code": 0,
                "cost_usd": 0.01,
                "channels_covered": 2,
                "error_msg": None,
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 0
    assert "nightly: stale (72h) ⚠️" in result.output

    json_result = CliRunner().invoke(app, ["status", "--json"])
    payload = json.loads(json_result.output)
    assert payload["nightly"]["state"] == "stale"
    assert payload["nightly"]["stale"] is True
    assert payload["nightly"]["age_hours"] == 72.0
