from __future__ import annotations

import json
from pathlib import Path

import pytest

from engram.bootstrap import provision_channel
from engram.manifest import IdentityTemplate
from engram.mcp import (
    claude_mcp_config_path,
    detect_new_user_mcp_servers,
    write_mcp_inventory_state,
)
from engram.mcp_onboarding import maybe_prompt_for_new_mcp_servers


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_maybe_prompt_for_new_mcp_servers_alerts_owner_without_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".engram"
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=home,
    )
    write_mcp_inventory_state(["linear"], home=home)
    _write_json(
        claude_mcp_config_path(),
        {
            "mcpServers": {
                "linear": {"type": "http", "url": "https://linear.example/mcp"},
                "camoufox": {"command": "camoufox-mcp"},
            }
        },
    )
    alerts: list[str] = []

    new_servers = await maybe_prompt_for_new_mcp_servers(
        home=home,
        interactive=False,
        owner_alert=alerts.append,
    )

    assert new_servers == ["camoufox"]
    assert alerts
    assert "camoufox" in alerts[0]
    assert "engram doctor" in alerts[0]
    assert "mcp_servers.allowed" in alerts[0]
    assert detect_new_user_mcp_servers(home=home).new_servers == []


@pytest.mark.asyncio
async def test_maybe_prompt_for_new_mcp_servers_reuses_sync_flow_with_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".engram"
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=home,
    )
    write_mcp_inventory_state(["linear"], home=home)
    _write_json(
        claude_mcp_config_path(),
        {
            "mcpServers": {
                "linear": {"type": "http", "url": "https://linear.example/mcp"},
                "camoufox": {"command": "camoufox-mcp"},
            }
        },
    )
    calls: dict[str, object] = {}
    output: list[str] = []

    async def fake_sync(configured_servers, coverage, **kwargs):
        calls["configured_servers"] = configured_servers
        calls["coverage"] = coverage
        calls["target_servers"] = kwargs["target_servers"]
        calls["audit_source"] = kwargs["audit_source"]
        return True

    monkeypatch.setattr("engram.mcp_onboarding.sync_team_channel_mcp_allow_lists", fake_sync)

    new_servers = await maybe_prompt_for_new_mcp_servers(
        home=home,
        interactive=True,
        printer=output.append,
    )

    assert new_servers == ["camoufox"]
    assert calls["target_servers"] == ["camoufox"]
    assert calls["audit_source"] == "startup_prompt"
    assert any("New MCPs detected" in line for line in output)
