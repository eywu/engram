"""Tests for native Claude Code permission rules.

Covers the `PermissionsRules` schema, YAML round-tripping, and the path
through `build_scope_decision` into the static SDK options. Runtime
enforcement of `Tool(specifier)` rules is the CLI's job, not ours — we
verify that rules make it from manifest to CLI flag intact.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from pydantic import ValidationError

from engram.bootstrap import provision_channel
from engram.manifest import (
    ABSOLUTE_DENY_RULES,
    ChannelManifest,
    IdentityTemplate,
    PermissionsRules,
    dump_manifest,
    load_manifest,
)
from engram.paths import channel_manifest_path
from engram.scope import build_scope_decision, build_tool_guard

# ── Schema validation ──────────────────────────────────────────────────


def test_empty_permissions_is_valid():
    p = PermissionsRules()
    assert p.deny == []
    assert p.allow == []
    assert p.is_empty()


def test_bare_tool_name_is_valid():
    p = PermissionsRules(deny=["Bash"], allow=["Read"])
    assert p.deny == ["Bash"]


def test_tool_with_specifier_is_valid():
    p = PermissionsRules(
        deny=[
            "Read(~/.ssh/**)",
            "Read(**/.env*)",
            "Bash(curl *)",
        ],
        allow=[
            "Edit(./src/**)",
            "Bash(npm *)",
        ],
    )
    assert "Read(~/.ssh/**)" in p.deny
    assert "Bash(npm *)" in p.allow


@pytest.mark.parametrize(
    "bad_rule",
    [
        "",
        "   ",
        "(missing-tool-name)",
        "Read(unclosed",
        "1Bad(starts-with-digit)",
        "Tool-with-dash",
    ],
)
def test_invalid_rule_shape_rejected(bad_rule: str):
    with pytest.raises(ValidationError):
        PermissionsRules(deny=[bad_rule])


def test_comma_in_rule_rejected():
    """Commas would break the SDK's comma-joined CLI flag."""
    with pytest.raises(ValidationError, match="comma"):
        PermissionsRules(deny=['Bash(git commit -m "a,b")'])


def test_rule_whitespace_stripped():
    p = PermissionsRules(deny=["  Read(~/.ssh/**)  "])
    assert p.deny == ["Read(~/.ssh/**)"]


# ── YAML round-trip ────────────────────────────────────────────────────


def test_permissions_survive_yaml_round_trip(tmp_path: Path):
    m = ChannelManifest(
        channel_id="C07TEST",
        identity=IdentityTemplate.TASK_ASSISTANT,
        permissions=PermissionsRules(
            deny=["Read(~/.ssh/**)", "Bash(curl *)"],
            allow=["Bash(git status)"],
        ),
    )
    path = tmp_path / "m.yaml"
    dump_manifest(m, path)
    reloaded = load_manifest(path)
    assert reloaded.permissions.deny == [
        *ABSOLUTE_DENY_RULES,
        "Bash(curl *)",
    ]
    assert reloaded.permissions.allow == ["Bash(git status)"]


# ── Integration with ScopeDecision ─────────────────────────────────────


def test_deny_rules_merge_into_disallowed_tools():
    m = ChannelManifest(
        channel_id="C07",
        identity=IdentityTemplate.TASK_ASSISTANT,
        permissions=PermissionsRules(
            deny=["Read(~/.ssh/**)", "Read(**/.env*)"],
        ),
    )
    # Also add a plain tool-name exclusion to prove both flow through.
    m.tools.disallowed = ["Bash"]

    d = build_scope_decision(m)
    assert "Bash" in d.disallowed_tools
    assert "Read(~/.ssh/**)" in d.disallowed_tools
    assert "Read(**/.env*)" in d.disallowed_tools


def test_allow_rules_merge_into_allowed_tools():
    m = ChannelManifest(
        channel_id="C07",
        identity=IdentityTemplate.TASK_ASSISTANT,
        permissions=PermissionsRules(
            allow=["Bash(npm *)", "Edit(./src/**)"],
        ),
    )
    d = build_scope_decision(m)
    assert "Bash(npm *)" in d.allowed_tools
    assert "Edit(./src/**)" in d.allowed_tools


# ── Runtime guard + specifier rules ────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_guard_ignores_specifier_rules_in_tools_disallowed():
    """If a specifier rule ends up in tools.disallowed (wrong place, but
    tolerable), our runtime guard should NOT block bare calls. The CLI
    is the matcher for specifier format."""
    m = ChannelManifest(
        channel_id="C07",
        identity=IdentityTemplate.TASK_ASSISTANT,
    )
    m.tools.disallowed = ["Read(~/.ssh/**)"]
    guard = build_tool_guard(m)
    result = await guard("Read", {"file_path": "/tmp/safe"}, Mock())
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_runtime_guard_still_blocks_bare_tool_names():
    """Regression: bare tool-name denies must still work unchanged."""
    m = ChannelManifest(
        channel_id="C07",
        identity=IdentityTemplate.TASK_ASSISTANT,
    )
    m.tools.disallowed = ["Bash"]
    guard = build_tool_guard(m)
    result = await guard("Bash", {"cmd": "ls"}, Mock())
    assert isinstance(result, PermissionResultDeny)


# ── End-to-end: provisioned templates carry real defaults ──────────────


def test_task_assistant_template_has_secret_denies(tmp_path: Path):
    """Fresh team-channel provision ships with credential denies."""
    result = provision_channel(
        "C07NEW",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#test",
        home=tmp_path,
    )
    deny = result.manifest.permissions.deny
    # Spot-check the high-value rules.
    assert any("/.ssh/" in r for r in deny)
    assert any(".env" in r for r in deny)
    assert any("/.aws/" in r for r in deny)


def test_owner_dm_template_has_minimal_denies(tmp_path: Path):
    """Owner-DM ships with a lighter deny list; still covers credentials."""
    result = provision_channel(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="DM",
        home=tmp_path,
    )
    deny = result.manifest.permissions.deny
    assert any("/.ssh/" in r for r in deny)
    assert any(".env" in r for r in deny)


def test_provisioned_manifest_flows_to_scope_decision(tmp_path: Path):
    """Full integration: provision → load → ScopeDecision contains rules."""
    provision_channel(
        "C07FULL",
        identity=IdentityTemplate.TASK_ASSISTANT,
        home=tmp_path,
    )
    m = load_manifest(channel_manifest_path("C07FULL", tmp_path))
    d = build_scope_decision(m)
    # Plain tool-name denies (Bash, Write, etc.) should be there.
    assert "Bash" in d.disallowed_tools
    # Specifier-format denies should also be there, verbatim.
    assert any("Read(~/.ssh/" in r for r in d.disallowed_tools)
    assert any("/.env" in r for r in d.disallowed_tools)
