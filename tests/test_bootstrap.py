"""Tests for bootstrap / provisioning."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from engram import paths
from engram.bootstrap import ensure_project_root, provision_channel
from engram.manifest import (
    OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES,
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
    ScopeList,
    dump_manifest,
    load_manifest,
)
from engram.notifications import notify_pending_channel


class FakeSlackClient:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, object]] = []
        self.chat_postMessage = self._chat_post_message

    async def _chat_post_message(self, **kwargs):
        self.post_calls.append(kwargs)
        return {"ok": True, "ts": "1713800000.000200"}

# ── Project-root bootstrap ──────────────────────────────────────────────


def test_ensure_project_root_creates_claude_dir(tmp_path: Path):
    root = ensure_project_root(home=tmp_path)
    assert root == tmp_path / "project"
    assert (root / ".claude" / "SOUL.md").exists()
    assert (root / ".claude" / "AGENTS.md").exists()
    assert (root / ".claude" / "skills").is_dir()


def test_ensure_project_root_is_idempotent(tmp_path: Path):
    ensure_project_root(home=tmp_path)
    soul = tmp_path / "project" / ".claude" / "SOUL.md"
    # Operator edits SOUL.md
    soul.write_text("# My custom soul\n")
    # Running bootstrap again must NOT clobber it
    ensure_project_root(home=tmp_path)
    assert soul.read_text() == "# My custom soul\n"


def test_ensure_project_root_adds_missing_files(tmp_path: Path):
    """If operator deleted a file, re-bootstrap restores it."""
    ensure_project_root(home=tmp_path)
    agents_md = tmp_path / "project" / ".claude" / "AGENTS.md"
    agents_md.unlink()
    ensure_project_root(home=tmp_path)
    assert agents_md.exists()


# ── Channel provisioning: owner-DM ──────────────────────────────────────


def test_provision_owner_dm_creates_manifest_and_claude_md(tmp_path: Path):
    result = provision_channel(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="DM",
        home=tmp_path,
    )
    assert result.created
    assert result.manifest_path.exists()
    assert result.claude_md_path.exists()

    m = load_manifest(result.manifest_path)
    assert m.channel_id == "D07OWNER"
    assert m.identity == IdentityTemplate.OWNER_DM_FULL
    assert m.permission_tier == PermissionTier.OWNER_SCOPED
    assert m.status == ChannelStatus.ACTIVE  # owner-DM template defaults active
    assert m.setting_sources == ["user"]
    assert m.tools.is_unrestricted()
    assert m.mcp_servers.is_unrestricted()


def test_owner_dm_claude_md_substitutes_vars(tmp_path: Path):
    provision_channel(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="Alice (DM)",
        home=tmp_path,
        template_vars={
            "owner_display_name": "Alice",
            "slack_workspace_name": "acme-corp",
        },
    )
    claude_md = paths.channel_claude_md_path("D07OWNER", tmp_path).read_text()
    assert "Alice" in claude_md
    assert "acme-corp" in claude_md
    assert "D07OWNER" in claude_md
    # No unsubstituted template variables should leak through.
    assert "{{owner_display_name}}" not in claude_md
    assert "{{slack_workspace_name}}" not in claude_md
    assert "{{channel_id}}" not in claude_md


# ── Channel provisioning: task-assistant ────────────────────────────────


def test_provision_task_assistant_defaults_pending(tmp_path: Path):
    result = provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path,
    )
    assert result.created
    m = load_manifest(result.manifest_path)
    assert m.permission_tier == PermissionTier.TASK_ASSISTANT
    assert m.status == ChannelStatus.PENDING  # must be approved before use
    assert m.setting_sources == ["project"]
    # Default exclusions from template
    assert "Bash" in m.tools.disallowed
    assert "Write" in m.tools.disallowed
    assert "Edit" in m.tools.disallowed
    # Team channels start strict; Engram's own memory recall is allowed by default.
    assert m.mcp_servers.allowed == ["engram-memory"]
    assert m.skills.is_unrestricted()


def test_provision_supports_status_override(tmp_path: Path):
    result = provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        status=ChannelStatus.ACTIVE,
        home=tmp_path,
    )
    assert result.manifest.status == ChannelStatus.ACTIVE


def test_task_assistant_claude_md_substitutes_label(tmp_path: Path):
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path,
    )
    claude_md = paths.channel_claude_md_path("C07TEAM", tmp_path).read_text()
    assert "#growth" in claude_md
    assert "C07TEAM" in claude_md


# ── Idempotency ─────────────────────────────────────────────────────────


def test_provision_channel_idempotent(tmp_path: Path):
    """Second call should return the existing manifest, not overwrite."""
    first = provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path,
    )
    assert first.created

    # Operator edits the manifest between boots
    manifest_path = first.manifest_path
    m = load_manifest(manifest_path)
    m2 = ChannelManifest.model_validate(
        {**m.model_dump(mode="json"), "status": "active"}
    )
    dump_manifest(m2, manifest_path)

    second = provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path,
    )
    assert not second.created
    assert second.manifest.status == ChannelStatus.ACTIVE  # preserved


def test_provision_channel_migrates_owner_dm_empty_permission_allow(
    tmp_path: Path,
):
    result = provision_channel(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="DM",
        home=tmp_path,
    )
    manifest = load_manifest(result.manifest_path)
    migrated = manifest.model_copy(
        update={
            "permissions": manifest.permissions.model_copy(update={"allow": []})
        }
    )
    dump_manifest(migrated, result.manifest_path)

    reloaded = provision_channel(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="DM",
        home=tmp_path,
    )

    assert not reloaded.created
    assert reloaded.manifest.permissions.allow == list(
        OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES
    )
    assert load_manifest(reloaded.manifest_path).permissions.allow == list(
        OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES
    )


def test_provision_channel_leaves_owner_dm_custom_permission_allow_alone(
    tmp_path: Path,
):
    result = provision_channel(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="DM",
        home=tmp_path,
    )
    manifest = load_manifest(result.manifest_path)
    customized = manifest.model_copy(
        update={
            "permissions": manifest.permissions.model_copy(
                update={"allow": ["Read"]}
            )
        }
    )
    dump_manifest(customized, result.manifest_path)

    reloaded = provision_channel(
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        label="DM",
        home=tmp_path,
    )

    assert not reloaded.created
    assert reloaded.manifest.permissions.allow == ["Read"]


def test_provision_channel_preserves_custom_claude_md(tmp_path: Path):
    """If someone hand-edits CLAUDE.md, re-provision must not clobber it."""
    first = provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path,
    )
    custom = "# My custom prompt\nDo the thing.\n"
    first.claude_md_path.write_text(custom)
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path,
    )
    assert first.claude_md_path.read_text() == custom


def test_provision_creates_memory_dir(tmp_path: Path):
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path,
    )
    assert paths.channel_memory_dir("C07TEAM", tmp_path).is_dir()


def test_unknown_mcp_in_manifest_warns_but_allows_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    def fake_render_manifest(**_kwargs):
        return ChannelManifest(
            channel_id="C07TEAM",
            identity=IdentityTemplate.TASK_ASSISTANT,
            status=ChannelStatus.PENDING,
            mcp_servers=ScopeList(allowed=["nonexistent-mcp"]),
        )

    monkeypatch.setattr(
        "engram.bootstrap._render_manifest",
        fake_render_manifest,
    )
    caplog.set_level(logging.WARNING)

    result = provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path,
    )

    assert result.created
    assert result.manifest.mcp_servers.allowed == ["nonexistent-mcp"]
    assert "channel.mcp_server_missing" in caplog.text
    assert "nonexistent-mcp" in caplog.text


@pytest.mark.asyncio
async def test_pending_channel_notification_posts_owner_dm_card(tmp_path: Path):
    result = provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#engram-self",
        home=tmp_path,
    )
    slack = FakeSlackClient()

    await notify_pending_channel(
        slack_client=slack,
        owner_dm_channel_id="D07OWNER",
        channel_id=result.channel_id,
        channel_label=result.manifest.label or result.channel_id,
        invited_by_user_id="U07REQUESTER",
        tier=result.manifest.permission_tier.value,
        first_message="hey engram, what's the status?",
        source_thread_ts="1713800000.000100",
    )

    assert len(slack.post_calls) == 1
    post = slack.post_calls[0]
    assert post["channel"] == "D07OWNER"
    assert post["text"] == "New channel awaiting approval"
    assert "#engram-self (C07TEAM)" in post["blocks"][1]["text"]["text"]
    assert "<@U07REQUESTER>" in post["blocks"][1]["text"]["text"]
    assert "safe" in post["blocks"][1]["text"]["text"]
    assert "hey engram, what's the status?" in post["blocks"][1]["text"]["text"]


# ── Path helpers sanity ─────────────────────────────────────────────────


def test_paths_layout(tmp_path: Path):
    """Smoke-test the path helpers produce what we'd expect."""
    assert paths.engram_home(tmp_path) == tmp_path
    assert paths.project_root(tmp_path) == tmp_path / "project"
    assert paths.contexts_dir(tmp_path) == tmp_path / "contexts"
    assert paths.channel_dir("C07X", tmp_path) == tmp_path / "contexts" / "C07X"
    assert (
        paths.channel_manifest_path("C07X", tmp_path)
        == tmp_path / "contexts" / "C07X" / ".claude" / "channel-manifest.yaml"
    )


# ── Edge: missing template dir raises clearly ──────────────────────────


def test_missing_template_dir_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        paths, "TEMPLATES_PROJECT_DIR", tmp_path / "does-not-exist"
    )
    with pytest.raises(FileNotFoundError, match="Repo templates missing"):
        ensure_project_root(home=tmp_path)
