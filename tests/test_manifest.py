"""Tests for the channel manifest schema."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from engram import paths
from engram.manifest import (
    _TIER_DEFAULTS,
    OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES,
    AskUserQuestion,
    Behavior,
    ChannelManifest,
    ChannelNightly,
    ChannelStatus,
    CostBudget,
    IdentityTemplate,
    ManifestError,
    MemoryScope,
    PermissionsRules,
    PermissionTier,
    ScopeList,
    dump_manifest,
    load_manifest,
)

# ── Defaults & inheritance model ────────────────────────────────────────


def test_scope_list_defaults_to_unrestricted():
    s = ScopeList()
    assert s.allowed is None
    assert s.disallowed == []
    assert s.is_unrestricted()


def test_scope_list_with_disallowed_is_restricted():
    s = ScopeList(disallowed=["Bash"])
    assert not s.is_unrestricted()


def test_scope_list_with_allowed_is_restricted():
    s = ScopeList(allowed=["Read", "Grep"])
    assert not s.is_unrestricted()


def test_owner_dm_minimal_manifest_full_inheritance():
    """Owner-DM should be expressible as a near-empty manifest."""
    m = ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        setting_sources=["user"],
    )
    assert m.tools.is_unrestricted()
    assert m.mcp_servers.is_unrestricted()
    assert m.skills.is_unrestricted()
    assert m.is_owner_dm()


def test_team_channel_typical_exclusions():
    """Typical team channel: exclude write-side tools, inherit everything else."""
    m = ChannelManifest(
        channel_id="C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        tools=ScopeList(disallowed=["Bash", "Write", "Edit"]),
    )
    assert "Bash" in m.tools.disallowed
    assert m.mcp_servers.is_unrestricted()  # MCPs still inherited
    assert not m.is_owner_dm()


# ── Validation ──────────────────────────────────────────────────────────


def test_empty_channel_id_rejected():
    with pytest.raises(ValueError, match="channel_id"):
        ChannelManifest(
            channel_id="",
            identity=IdentityTemplate.TASK_ASSISTANT,
        )


def test_whitespace_channel_id_normalized():
    m = ChannelManifest(
        channel_id="  C07ABC  ",
        identity=IdentityTemplate.TASK_ASSISTANT,
    )
    assert m.channel_id == "C07ABC"


def test_setting_sources_cannot_be_empty():
    with pytest.raises(ValueError, match="setting_sources"):
        ChannelManifest(
            channel_id="C07ABC",
            identity=IdentityTemplate.TASK_ASSISTANT,
            setting_sources=[],
        )


def test_setting_sources_invalid_value_rejected():
    with pytest.raises(ValueError):
        ChannelManifest(
            channel_id="C07ABC",
            identity=IdentityTemplate.TASK_ASSISTANT,
            setting_sources=["bogus"],
        )


def test_invalid_identity_rejected():
    with pytest.raises(ValueError):
        ChannelManifest(
            channel_id="C07ABC",
            identity="not-a-template",  # type: ignore[arg-type]
        )


def test_behavior_max_turns_must_be_positive():
    with pytest.raises(ValueError, match="max_turns"):
        Behavior(max_turns=0)


def test_cost_budget_rejects_negative():
    with pytest.raises(ValueError, match="must be >= 0"):
        CostBudget(daily_usd=-1.0)


def test_cost_budget_warn_at_percent_bounded():
    with pytest.raises(ValueError):
        CostBudget(warn_at_percent=0)
    with pytest.raises(ValueError):
        CostBudget(warn_at_percent=101)


# ── Defaults across the M3/M4-deferred fields ───────────────────────────


def test_status_defaults_to_pending():
    m = ChannelManifest(
        channel_id="C07ABC", identity=IdentityTemplate.TASK_ASSISTANT
    )
    assert m.status == ChannelStatus.PENDING


def test_meta_eligible_defaults_to_true():
    m = ChannelManifest(
        channel_id="C07ABC", identity=IdentityTemplate.TASK_ASSISTANT
    )
    assert m.meta_eligible is True


def test_acknowledged_pending_defaults_to_false():
    m = ChannelManifest(
        channel_id="C07ABC", identity=IdentityTemplate.TASK_ASSISTANT
    )
    assert m.acknowledged_pending is False


def test_setting_sources_default_is_project():
    """Team channels default to project-level priming, not user-level."""
    m = ChannelManifest(
        channel_id="C07ABC", identity=IdentityTemplate.TASK_ASSISTANT
    )
    assert m.setting_sources == ["project"]


def test_ask_user_question_defaults():
    a = AskUserQuestion()
    assert a.enabled is True
    assert a.fallback == "escalate-to-owner"


def test_manifest_loads_hitl_section(tmp_path: Path):
    templates = [
        ("owner-dm.yaml", "D07OWNER", "DM", PermissionTier.OWNER_SCOPED, 1000),
        (
            "task-assistant.yaml",
            "C07TEAM",
            "#test-team",
            PermissionTier.TASK_ASSISTANT,
            1000,
        ),
    ]

    for template_name, channel_id, label, permission_tier, max_per_day in templates:
        template = paths.TEMPLATES_MANIFESTS_DIR / template_name
        rendered = (
            template.read_text()
            .replace("{{channel_id}}", channel_id)
            .replace("{{channel_label}}", label)
        )
        manifest_path = tmp_path / template_name
        manifest_path.write_text(rendered)

        manifest = load_manifest(manifest_path)

        assert manifest.permission_tier == permission_tier
        assert manifest.hitl.enabled is True
        assert manifest.hitl.timeout_s == 300
        assert manifest.hitl.max_per_day == max_per_day
        assert manifest.meta_eligible is True


def test_manifest_loads_nightly_model_from_templates(tmp_path: Path):
    templates = [
        ("owner-dm.yaml", "D07OWNER", "DM", "opus"),
        ("task-assistant.yaml", "C07TEAM", "#test-team", "sonnet"),
    ]

    for template_name, channel_id, label, expected_model in templates:
        template = paths.TEMPLATES_MANIFESTS_DIR / template_name
        rendered = (
            template.read_text()
            .replace("{{channel_id}}", channel_id)
            .replace("{{channel_label}}", label)
        )
        manifest_path = tmp_path / template_name
        manifest_path.write_text(rendered)

        manifest = load_manifest(manifest_path)

        assert manifest.nightly.model == expected_model


def test_owner_dm_template_has_read_only_allow_defaults(tmp_path: Path):
    template = paths.TEMPLATES_MANIFESTS_DIR / "owner-dm.yaml"
    rendered = (
        template.read_text()
        .replace("{{channel_id}}", "D07OWNER")
        .replace("{{channel_label}}", "DM")
    )
    manifest_path = tmp_path / "owner-dm.yaml"
    manifest_path.write_text(rendered)

    manifest = load_manifest(manifest_path)

    assert manifest.permissions.allow == list(
        OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES
    )


def test_no_dont_ask_rule_in_templates():
    pattern = re.compile(r"don.t ask", re.IGNORECASE)
    matches = []

    for template in paths.TEMPLATES_DIR.rglob("*"):
        if template.is_file() and pattern.search(template.read_text()):
            matches.append(str(template.relative_to(paths.TEMPLATES_DIR)))

    assert matches == []


def test_subagents_default_empty():
    m = ChannelManifest(
        channel_id="C07ABC", identity=IdentityTemplate.TASK_ASSISTANT
    )
    assert m.subagents == []


def test_memory_scope_defaults_to_no_exclusions():
    m = ChannelManifest(
        channel_id="C07ABC", identity=IdentityTemplate.TASK_ASSISTANT
    )
    assert m.memory.excluded_channels == []


def test_memory_scope_normalizes_excluded_channels():
    scope = MemoryScope(excluded_channels=[" C07A ", "C07B", "C07A"])
    assert scope.excluded_channels == ["C07A", "C07B"]


def test_channel_nightly_model_normalized():
    nightly = ChannelNightly(model=" sonnet ")
    assert nightly.model == "sonnet"

    empty = ChannelNightly(model=" ")
    assert empty.model is None


# ── YAML I/O round-trip ─────────────────────────────────────────────────


def test_load_manifest_owner_dm(tmp_path: Path):
    p = tmp_path / "manifest.yaml"
    p.write_text(
        """
channel_id: D07OWNER
identity: owner-dm-full
label: Alice (DM)
status: active
setting_sources: [user]
"""
    )
    m = load_manifest(p)
    assert m.channel_id == "D07OWNER"
    assert m.identity == IdentityTemplate.OWNER_DM_FULL
    assert m.permission_tier == PermissionTier.OWNER_SCOPED
    assert m.status == ChannelStatus.ACTIVE
    assert m.setting_sources == ["user"]
    assert m.tools.is_unrestricted()


def test_load_manifest_team_channel(tmp_path: Path):
    p = tmp_path / "manifest.yaml"
    p.write_text(
        """
channel_id: C07TEAM
identity: task-assistant
label: "#growth"
status: active
setting_sources: [project]
meta_eligible: false
tools:
  disallowed: [Bash, Write, Edit]
mcp_servers:
  disallowed: [personal-notes]
memory:
  excluded_channels: [C07OPTEDOUT]
behavior:
  style: concise
  max_turns: 6
cost_budget:
  daily_usd: 5.0
  monthly_usd: 50.0
"""
    )
    m = load_manifest(p)
    assert m.permission_tier == PermissionTier.TASK_ASSISTANT
    assert m.meta_eligible is False
    assert m.tools.disallowed == ["Bash", "Write", "Edit"]
    assert m.mcp_servers.disallowed == ["personal-notes"]
    assert m.memory.excluded_channels == ["C07OPTEDOUT"]
    assert m.behavior.style == "concise"
    assert m.behavior.max_turns == 6
    assert m.cost_budget.daily_usd == 5.0


def test_round_trip(tmp_path: Path):
    """Dump then load must produce identical manifest."""
    original = ChannelManifest(
        channel_id="C07ROUND",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#round",
        status=ChannelStatus.ACTIVE,
        permission_tier=PermissionTier.TASK_ASSISTANT,
        tools=ScopeList(disallowed=["Bash"]),
        permissions=PermissionsRules(
            deny=list(_TIER_DEFAULTS[PermissionTier.TASK_ASSISTANT]["deny_rules"])
        ),
        behavior=Behavior(style="thorough", max_turns=10),
        cost_budget=CostBudget(monthly_usd=20.0),
    )
    p = tmp_path / "round.yaml"
    dump_manifest(original, p)
    reloaded = load_manifest(p)
    assert reloaded == original


def test_load_missing_file_raises_manifest_error(tmp_path: Path):
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "nope.yaml")


def test_load_empty_file_raises_manifest_error(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ManifestError, match="empty"):
        load_manifest(p)


def test_load_non_mapping_raises_manifest_error(tmp_path: Path):
    p = tmp_path / "list.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(ManifestError, match="mapping"):
        load_manifest(p)


def test_load_malformed_yaml_raises_manifest_error(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text("key: : :\n  bad\n  - indent\n")
    with pytest.raises(ManifestError, match="parse"):
        load_manifest(p)


def test_load_invalid_schema_raises_manifest_error(tmp_path: Path):
    p = tmp_path / "invalid.yaml"
    p.write_text(
        """
channel_id: C07
identity: not-a-real-template
"""
    )
    with pytest.raises(ManifestError, match="validation failed"):
        load_manifest(p)


# ── Edge cases for the escape-hatch allow-list ──────────────────────────


def test_allowed_list_takes_precedence_in_intent():
    """When both allowed and disallowed are set, both are stored.

    The agent layer (Phase B) is responsible for interpreting:
    `allowed` defines the universe; `disallowed` further filters.
    """
    s = ScopeList(allowed=["Read", "Grep"], disallowed=["Bash"])
    assert s.allowed == ["Read", "Grep"]
    assert s.disallowed == ["Bash"]
    assert not s.is_unrestricted()


def test_yaml_serialization_omits_default_values_cleanly(tmp_path: Path):
    """A minimal manifest should round-trip without polluting YAML with defaults."""
    m = ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        setting_sources=["user"],
    )
    p = tmp_path / "minimal.yaml"
    dump_manifest(m, p)
    raw = yaml.safe_load(p.read_text())
    # Required fields present
    assert raw["channel_id"] == "D07OWNER"
    assert raw["identity"] == "owner-dm-full"
    # Defaults are still serialized (we want explicit, not magical)
    assert raw["status"] == "pending"
    assert raw["acknowledged_pending"] is False
    assert raw["setting_sources"] == ["user"]
