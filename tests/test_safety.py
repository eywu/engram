"""Safety invariants for sticky HITL approvals."""
from __future__ import annotations

import pytest

from engram.egress import _is_sticky_eligible
from engram.manifest import (
    ChannelManifest,
    IdentityTemplate,
    PermissionsRules,
    _assert_sticky_eligible,
    add_allow_rule,
)


def owner_dm_manifest(
    *,
    permissions: PermissionsRules | None = None,
) -> ChannelManifest:
    return ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        permissions=permissions or PermissionsRules(),
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "Bash",
        "BashOutput",
        "KillShell",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Task",
        "SlashCommand",
        "mcp__gmail__send_email",
    ],
)
def test_sticky_eligibility_rejects_high_risk_tools(tool_name: str):
    assert _is_sticky_eligible(tool_name, owner_dm_manifest()) is False


def test_manifest_allow_list_does_not_make_bash_sticky_eligible():
    manifest = owner_dm_manifest(permissions=PermissionsRules(allow=["Bash"]))

    assert _is_sticky_eligible("Bash", manifest) is False


# ---------------------------------------------------------------------------
# Defense-in-depth: persistence layer MUST reject ineligible tools even if the
# UI filter is bypassed (tampered payload, stale button, drifted code path).
# See GRO-478 review — the UI check in egress._is_sticky_eligible is the first
# line of defense; _assert_sticky_eligible in manifest.py is the second.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "Bash",
        "BashOutput",
        "KillShell",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Task",
        "SlashCommand",
    ],
)
def test_assert_sticky_eligible_rejects_high_risk_tools(tool_name: str):
    with pytest.raises(ValueError, match="ineligible tool"):
        _assert_sticky_eligible(tool_name)


def test_assert_sticky_eligible_rejects_mcp_tools():
    with pytest.raises(ValueError, match="mcp tool"):
        _assert_sticky_eligible("mcp__gmail__send_email")


def test_assert_sticky_eligible_rejects_bash_with_specifier():
    # Defense-in-depth must apply even when a caller wraps the tool name
    # with a specifier like ``Bash(git status*)`` — the base tool is the key.
    with pytest.raises(ValueError, match="ineligible tool"):
        _assert_sticky_eligible("Bash(git status*)")


def test_assert_sticky_eligible_allows_webfetch():
    # Happy path: WebFetch is the canonical sticky-eligible tool.
    _assert_sticky_eligible("WebFetch")  # must not raise


def test_add_allow_rule_refuses_to_persist_bash(tmp_path):
    # Full persistence-path smoke: even if something calls add_allow_rule
    # directly with a high-risk tool, it must refuse before touching disk.
    with pytest.raises(ValueError, match="ineligible tool"):
        add_allow_rule("D07OWNER", "Bash", home=tmp_path)


def test_add_allow_rule_refuses_mcp_tools(tmp_path):
    with pytest.raises(ValueError, match="mcp tool"):
        add_allow_rule("D07OWNER", "mcp__gmail__send_email", home=tmp_path)
