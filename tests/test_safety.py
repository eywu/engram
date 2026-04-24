"""Safety invariants for sticky HITL approvals."""
from __future__ import annotations

import pytest

from engram.egress import _is_sticky_eligible
from engram.manifest import ChannelManifest, IdentityTemplate, PermissionsRules


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
