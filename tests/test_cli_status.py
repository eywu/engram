"""CLI tests for `engram status`."""
from __future__ import annotations

import json
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
