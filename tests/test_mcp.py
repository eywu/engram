from __future__ import annotations

import json
from pathlib import Path

import pytest

from engram.manifest import ChannelManifest, IdentityTemplate, ScopeList
from engram.mcp import (
    claude_mcp_config_path,
    legacy_claude_mcp_config_path,
    load_claude_mcp_servers,
    resolve_team_mcp_servers,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_claude_mcp_servers_reads_claude_code_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = {
        "linear": {"type": "http", "url": "https://linear.example/mcp"},
        "figma": {"command": "figma-mcp"},
    }
    _write_json(
        claude_mcp_config_path(),
        {"mcpServers": expected, "theme": "light"},
    )

    assert load_claude_mcp_servers() == expected


def test_load_claude_mcp_servers_migrates_legacy_inventory_with_backups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target_path = claude_mcp_config_path()
    legacy_path = legacy_claude_mcp_config_path()
    target_payload = {
        "theme": "dark",
        "mcpServers": {
            "linear": {"command": "linear-new"},
        },
    }
    legacy_payload = {
        "mcpServers": {
            "linear": {"command": "linear-old"},
            "github": {"command": "github-mcp"},
        },
    }
    _write_json(target_path, target_payload)
    _write_json(legacy_path, legacy_payload)

    with caplog.at_level("WARNING", logger="engram.mcp"):
        servers = load_claude_mcp_servers()

    assert servers == {
        "linear": {"command": "linear-new"},
        "github": {"command": "github-mcp"},
    }
    assert json.loads(target_path.read_text(encoding="utf-8")) == {
        "mcpServers": servers,
        "theme": "dark",
    }
    assert not legacy_path.exists()
    assert json.loads(
        target_path.with_name(f"{target_path.name}.bak").read_text(encoding="utf-8")
    ) == target_payload
    assert json.loads(
        legacy_path.with_name(f"{legacy_path.name}.bak").read_text(encoding="utf-8")
    ) == legacy_payload
    assert any(
        record.getMessage().startswith("mcp.legacy_config_migrated")
        for record in caplog.records
    )


def test_resolve_team_mcp_servers_filters_claude_code_inventory_by_allow_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_json(
        claude_mcp_config_path(),
        {
            "mcpServers": {
                "linear": {"type": "http", "url": "https://linear.example/mcp"},
                "figma": {"command": "figma-mcp"},
            }
        },
    )
    manifest = ChannelManifest(
        channel_id="C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        mcp_servers=ScopeList(allowed=["linear"]),
    )

    servers, allowed, missing = resolve_team_mcp_servers(manifest)

    assert servers == {
        "linear": {"type": "http", "url": "https://linear.example/mcp"}
    }
    assert allowed == ["linear"]
    assert missing == []
