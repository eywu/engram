from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from engram.bootstrap import provision_channel
from engram.manifest import ChannelManifest, IdentityTemplate, ScopeList
from engram.mcp import (
    audit_mcp_channel_coverage,
    claude_mcp_config_path,
    detect_new_user_mcp_servers,
    legacy_claude_mcp_config_path,
    load_claude_mcp_servers,
    migrate_legacy_claude_mcp_config,
    resolve_team_mcp_servers,
    write_mcp_inventory_state,
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


def test_migrate_legacy_claude_mcp_config_merges_inventory_with_backups(
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

    assert load_claude_mcp_servers() == {"linear": {"command": "linear-new"}}
    assert legacy_path.exists()

    with caplog.at_level("WARNING", logger="engram.mcp"):
        migrate_legacy_claude_mcp_config()

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


def test_migration_concurrent_invocations_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            "github": {"command": "github-mcp"},
        },
    }
    _write_json(target_path, target_payload)
    _write_json(legacy_path, legacy_payload)

    with ProcessPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(migrate_legacy_claude_mcp_config)
            for _ in range(2)
        ]
        for future in futures:
            future.result()

    assert load_claude_mcp_servers() == {
        "linear": {"command": "linear-new"},
        "github": {"command": "github-mcp"},
    }
    assert not legacy_path.exists()
    assert sorted(path.name for path in target_path.parent.glob(".claude.json.bak*")) == [
        ".claude.json.bak"
    ]
    assert sorted(path.name for path in legacy_path.parent.glob("mcp.json.bak*")) == [
        "mcp.json.bak"
    ]
    assert json.loads(
        target_path.with_name(f"{target_path.name}.bak").read_text(encoding="utf-8")
    ) == target_payload
    assert json.loads(
        legacy_path.with_name(f"{legacy_path.name}.bak").read_text(encoding="utf-8")
    ) == legacy_payload


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


def test_audit_mcp_channel_coverage_finds_uncovered_user_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_json(
        claude_mcp_config_path(),
        {
            "mcpServers": {
                "linear": {"type": "http", "url": "https://linear.example/mcp"},
                "camoufox": {"command": "camoufox-mcp"},
            }
        },
    )
    home = tmp_path / ".engram"
    provision_channel(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        home=home,
    )
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=home,
    )

    coverage = audit_mcp_channel_coverage(
        contexts_path=home / "contexts",
    )

    assert coverage.configured_servers == ["linear", "camoufox"]
    assert coverage.team_channels == ["C07TEAM"]
    assert coverage.allowed_by_channel == {"C07TEAM": ["engram-memory"]}
    assert coverage.uncovered_servers == ["linear", "camoufox"]
    assert coverage.invalid_manifest_paths == []


def test_detect_new_user_mcp_servers_compares_against_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".engram"
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

    delta = detect_new_user_mcp_servers(home=home)

    assert delta.known_servers == ["linear"]
    assert delta.current_servers == ["camoufox", "linear"]
    assert delta.new_servers == ["camoufox"]


def _write_raw_manifest_yaml(
    home: Path, channel_id: str, *, allowed: list[str], disallowed: list[str]
) -> None:
    """Write a manifest YAML directly, bypassing the trust gate. For tests
    that need to set up adversarial manifest states (e.g. allow+disallow
    of the same server) without routing through approval.
    """
    import yaml

    from engram.paths import channel_manifest_path

    path = channel_manifest_path(channel_id, home)
    payload = yaml.safe_load(path.read_text())
    payload["mcp_servers"] = {"allowed": allowed, "disallowed": disallowed}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_audit_mcp_channel_coverage_subtracts_disallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRO-532 regression: a server that is allowed AND disallowed in the
    same channel must NOT count as covered. Previously
    `allowed_anywhere.update(allowed)` ignored `disallowed`, producing
    false-positive PASS in the doctor coverage check.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_json(
        claude_mcp_config_path(),
        {
            "mcpServers": {
                "camoufox": {"command": "camoufox-mcp"},
            }
        },
    )
    home = tmp_path / ".engram"
    provision_channel(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        home=home,
    )
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=home,
    )
    # Hand-write the YAML to set up the adversarial allow+disallow state
    # (the trust gate would block this via the normal write API, which is
    # the right behavior in production but blocks the test setup).
    _write_raw_manifest_yaml(
        home,
        "C07TEAM",
        allowed=["engram-memory", "camoufox"],
        disallowed=["camoufox"],
    )

    coverage = audit_mcp_channel_coverage(contexts_path=home / "contexts")

    # Effective access: camoufox is disallowed, so it must NOT be in
    # allowed_by_channel and must appear in uncovered_servers.
    assert "camoufox" not in coverage.allowed_by_channel["C07TEAM"]
    assert coverage.uncovered_servers == ["camoufox"]


def test_audit_mcp_channel_coverage_partial_coverage_across_team_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRO-532 regression: a server allowed in one team channel but
    disallowed in another should NOT cause global PASS.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_json(
        claude_mcp_config_path(),
        {
            "mcpServers": {
                "camoufox": {"command": "camoufox-mcp"},
            }
        },
    )
    home = tmp_path / ".engram"
    for cid in ("D07OWNER", "C07A", "C07B"):
        ident = (
            IdentityTemplate.OWNER_DM_FULL
            if cid.startswith("D")
            else IdentityTemplate.TASK_ASSISTANT
        )
        provision_channel(cid, identity=ident, label="#x", home=home)

    # C07A allows camoufox; C07B explicitly disallows it. Hand-write to
    # bypass the trust gate (production paths route through approval).
    _write_raw_manifest_yaml(
        home, "C07A", allowed=["engram-memory", "camoufox"], disallowed=[]
    )
    _write_raw_manifest_yaml(
        home, "C07B", allowed=["engram-memory"], disallowed=["camoufox"]
    )

    coverage = audit_mcp_channel_coverage(contexts_path=home / "contexts")
    # Per-channel effective view:
    assert "camoufox" in coverage.allowed_by_channel["C07A"]
    assert "camoufox" not in coverage.allowed_by_channel["C07B"]
    # Global coverage now: camoufox is allowed somewhere (C07A) so the
    # global audit reports it as covered. The PARTIAL coverage scenario
    # (allowed in some, disallowed in others) is intentionally still
    # reported as covered globally; per-channel exclusion evidence is
    # what the doctor check now surfaces independently (see
    # check_mcp_channel_coverage WARN-on-recent-exclusions branch).
    assert coverage.uncovered_servers == []
