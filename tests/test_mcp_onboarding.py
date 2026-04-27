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


@pytest.mark.asyncio
async def test_maybe_prompt_does_not_advance_state_on_failed_interactive_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRO-532 regression: if the interactive sync fails (e.g. operator
    declined or all manifests malformed), the inventory state file must
    NOT be advanced. Otherwise the next startup will silently skip the
    re-prompt and the new MCP gets stuck without team-channel coverage
    forever.
    """
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

    async def fake_sync_failed(configured_servers, coverage, **kwargs):
        # Simulate sync that produced no manifest changes (operator
        # declined OR all manifests were malformed and skipped).
        return False

    monkeypatch.setattr(
        "engram.mcp_onboarding.sync_team_channel_mcp_allow_lists",
        fake_sync_failed,
    )

    new_servers = await maybe_prompt_for_new_mcp_servers(
        home=home,
        interactive=True,
        printer=lambda _line: None,
    )

    assert new_servers == ["camoufox"]
    # State must NOT have been advanced — next startup should re-prompt.
    delta = detect_new_user_mcp_servers(home=home)
    assert "camoufox" in delta.new_servers


@pytest.mark.asyncio
async def test_maybe_prompt_advances_state_on_successful_interactive_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRO-532: counterpart to the above — successful interactive sync DOES
    advance the state so we don't re-prompt next startup.
    """
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

    async def fake_sync_ok(configured_servers, coverage, **kwargs):
        return True

    monkeypatch.setattr(
        "engram.mcp_onboarding.sync_team_channel_mcp_allow_lists",
        fake_sync_ok,
    )

    new_servers = await maybe_prompt_for_new_mcp_servers(
        home=home,
        interactive=True,
        printer=lambda _line: None,
    )

    assert new_servers == ["camoufox"]
    delta = detect_new_user_mcp_servers(home=home)
    assert delta.new_servers == []


@pytest.mark.asyncio
async def test_sync_warns_on_malformed_manifest_instead_of_silently_skipping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRO-532 regression: malformed team manifests must surface a
    printer warning, not silent `except ManifestError: continue`.

    Scenario: TOCTOU between `audit_mcp_channel_coverage` (which records
    valid manifests in `team_manifest_paths`) and the `sync_team_channel_mcp_allow_lists`
    re-load. If the manifest is edited/corrupted in that window, the
    re-load fails and previously the operator got no warning at all
    (silent `except ManifestError: continue`). With the fix, the operator
    sees a printer line naming the unparsable file.
    """
    from engram.mcp import (
        audit_mcp_channel_coverage,
        claude_mcp_config_path,
    )
    from engram.mcp_onboarding import sync_team_channel_mcp_allow_lists
    from engram.mcp_trust import MCPTrustDecision, MCPTrustTier
    from engram.paths import channel_manifest_path

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".engram"
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=home,
    )
    _write_json(
        claude_mcp_config_path(),
        {"mcpServers": {"camoufox": {"command": "camoufox-mcp"}}},
    )

    # Audit sees a valid manifest and records it.
    coverage = audit_mcp_channel_coverage(contexts_path=home / "contexts")
    assert coverage.team_channels == ["C07TEAM"]

    # Now corrupt the manifest BETWEEN audit and sync (TOCTOU window).
    bad_path = channel_manifest_path("C07TEAM", home)
    bad_path.write_text(
        "this is: not: valid: yaml: : :\n\t- broken\n", encoding="utf-8"
    )

    output: list[str] = []

    async def trust_fake(name, cfg, *, home=None):
        return MCPTrustDecision(
            tier=MCPTrustTier.OFFICIAL,
            reason="test",
            trust_summary=None,
        )

    changed = await sync_team_channel_mcp_allow_lists(
        {"camoufox": {"command": "camoufox-mcp"}},
        coverage,
        home=home,
        printer=output.append,
        trust_resolver=trust_fake,
        prompt_to_continue=False,
    )

    assert changed is False
    # The warning line must surface to the operator naming the bad path.
    assert any(
        "could not parse" in line.lower() and "C07TEAM" in line for line in output
    )
