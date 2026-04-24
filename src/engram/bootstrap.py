"""Bootstrap the Engram runtime tree and per-channel context directories.

Two entry points:

* `ensure_project_root(home)` — idempotently seeds the project-level
  `.claude/` inheritance layer from repo templates.

* `provision_channel(channel_id, ...)` — creates a new channel's context
  directory from a template (owner-DM or safe), renders its
  CLAUDE.md with per-channel variables, and writes a ChannelManifest.
  Safe to call on existing channels: it will NOT clobber an existing
  manifest.

The idempotency rule matters because `main.py` calls `ensure_project_root`
on every boot, and the router will call `provision_channel` the first time
we see an unfamiliar channel ID.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from engram import paths
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    _apply_tier_defaults,
    dump_manifest,
    load_manifest,
)
from engram.mcp import resolve_team_mcp_servers, warn_missing_mcp_servers

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Project-layer bootstrap
# ──────────────────────────────────────────────────────────────────────


def ensure_project_root(home: Path | None = None) -> Path:
    """Ensure `~/.engram/project/.claude/` exists and is seeded.

    Copies SOUL.md, AGENTS.md, and skills/ from `templates/project/` on
    first run. On subsequent runs, adds any files the user is missing but
    NEVER overwrites existing files — the operator can edit SOUL.md freely.

    Returns the project root (cwd that the agent will use when a channel
    doesn't override it).
    """
    target = paths.project_root(home)
    source = paths.TEMPLATES_PROJECT_DIR

    if not source.exists():
        raise FileNotFoundError(
            f"Repo templates missing at {source}. "
            "Is the engram package installed correctly?"
        )

    target.mkdir(parents=True, exist_ok=True)
    _copy_tree_no_clobber(source, target)
    log.info("project_root.ready path=%s", target)
    return target


def _copy_tree_no_clobber(source: Path, target: Path) -> None:
    """Copy every file in `source` to `target`, skipping files that already
    exist. Creates subdirectories as needed."""
    for src_path in source.rglob("*"):
        rel = src_path.relative_to(source)
        dst_path = target / rel
        if src_path.is_dir():
            dst_path.mkdir(parents=True, exist_ok=True)
            continue
        if dst_path.exists():
            log.debug("bootstrap.skip existing=%s", dst_path)
            continue
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)
        log.debug("bootstrap.copied src=%s dst=%s", src_path, dst_path)


# ──────────────────────────────────────────────────────────────────────
# Channel-layer provisioning
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ProvisionResult:
    """Outcome of a `provision_channel` call."""

    channel_id: str
    manifest: ChannelManifest
    created: bool  # True if we just wrote it; False if it already existed.
    manifest_path: Path
    claude_md_path: Path


def provision_channel(
    channel_id: str,
    *,
    identity: IdentityTemplate,
    label: str | None = None,
    status: ChannelStatus | None = None,
    home: Path | None = None,
    template_vars: dict[str, str] | None = None,
) -> ProvisionResult:
    """Create (or confirm) a channel's context directory.

    If the manifest already exists, we load it, apply idempotent manifest
    migrations, and return it. If it doesn't, we render the appropriate
    manifest template and CLAUDE.md from `templates/manifests/` and
    `templates/identity/`.

    Parameters
    ----------
    channel_id:
        Slack channel ID (C07ABC, D07XYZ, etc.).
    identity:
        Which identity template governs rendering + default manifest.
    label:
        Human-readable name (e.g. "#growth"). Used in CLAUDE.md rendering.
    status:
        Override the template's default status. Owner-DMs default to
        `active`; team channels default to `pending`.
    template_vars:
        Extra substitutions for CLAUDE.md rendering. Default vars
        (channel_id, channel_label, owner_display_name, slack_workspace_name)
        are always available; `template_vars` supplements / overrides them.
    """
    manifest_path = paths.channel_manifest_path(channel_id, home)
    claude_md_path = paths.channel_claude_md_path(channel_id, home)

    if manifest_path.exists():
        manifest = apply_manifest_migrations(
            load_manifest(manifest_path),
            manifest_path,
        )
        log.info(
            "channel.already_provisioned channel_id=%s status=%s",
            channel_id,
            manifest.status,
        )
        return ProvisionResult(
            channel_id=channel_id,
            manifest=manifest,
            created=False,
            manifest_path=manifest_path,
            claude_md_path=claude_md_path,
        )

    # Fresh provisioning.
    claude_md_path.parent.mkdir(parents=True, exist_ok=True)
    paths.channel_memory_dir(channel_id, home).mkdir(
        parents=True, exist_ok=True
    )

    manifest = _render_manifest(
        channel_id=channel_id,
        identity=identity,
        label=label,
        status_override=status,
    )
    if not manifest.is_owner_dm():
        mcp_servers, mcp_allowed, missing_mcp = resolve_team_mcp_servers(manifest)
        log.info(
            "channel.mcp_allow_list channel_id=%s strict_mode=true servers=%s",
            channel_id,
            list(mcp_servers),
        )
        if missing_mcp:
            log.info(
                "channel.mcp_allow_list_declared channel_id=%s servers=%s",
                channel_id,
                mcp_allowed,
            )
        warn_missing_mcp_servers(
            channel_id,
            missing_mcp,
            logger=log,
        )
    dump_manifest(manifest, manifest_path)

    identity_body = _render_identity_md(
        identity=identity,
        channel_id=channel_id,
        label=label,
        extra_vars=template_vars,
    )
    claude_md_path.write_text(identity_body)

    log.info(
        "channel.provisioned channel_id=%s identity=%s status=%s",
        channel_id,
        identity,
        manifest.status,
    )
    return ProvisionResult(
        channel_id=channel_id,
        manifest=manifest,
        created=True,
        manifest_path=manifest_path,
        claude_md_path=claude_md_path,
    )


def apply_manifest_migrations(
    manifest: ChannelManifest,
    manifest_path: Path,
) -> ChannelManifest:
    """Deprecated wrapper. `load_manifest()` now performs load-time migrations."""
    return manifest


# ──────────────────────────────────────────────────────────────────────
# Template rendering
# ──────────────────────────────────────────────────────────────────────


def _render_manifest(
    *,
    channel_id: str,
    identity: IdentityTemplate,
    label: str | None,
    status_override: ChannelStatus | None,
) -> ChannelManifest:
    """Load the appropriate manifest template and fill in channel-specifics."""
    template_name = (
        "trusted.yaml"
        if identity == IdentityTemplate.OWNER_DM_FULL
        else "safe.yaml"
    )
    template_path = paths.TEMPLATES_MANIFESTS_DIR / template_name
    raw = template_path.read_text()

    # Simple `{{var}}` substitutions before YAML parse.
    rendered = _apply_vars(
        raw,
        {
            "channel_id": channel_id,
            "channel_label": label or channel_id,
        },
    )

    # Parse the rendered YAML into a ChannelManifest.
    import yaml

    data = yaml.safe_load(rendered)
    # Make sure channel_id wasn't accidentally left templated.
    data["channel_id"] = channel_id
    if label is not None:
        data["label"] = label
    hydrated_data, _ = _apply_tier_defaults(
        data,
        infer_legacy_tier=False,
    )
    manifest = ChannelManifest.model_validate(hydrated_data)

    if status_override is not None:
        manifest = manifest.model_copy(update={"status": status_override})
    return manifest


def _render_identity_md(
    *,
    identity: IdentityTemplate,
    channel_id: str,
    label: str | None,
    extra_vars: dict[str, str] | None,
) -> str:
    """Load the identity template and substitute channel-level variables."""
    template_path = paths.TEMPLATES_IDENTITY_DIR / f"{identity.value}.md"
    raw = template_path.read_text()

    base_vars = {
        "channel_id": channel_id,
        "channel_label": label or channel_id,
        "owner_display_name": "the operator",
        "slack_workspace_name": "this workspace",
    }
    if extra_vars:
        base_vars.update(extra_vars)
    return _apply_vars(raw, base_vars)


def _apply_vars(text: str, vars_: dict[str, str]) -> str:
    """Minimal `{{var}}` substitution with literal matching (no Jinja).

    Chose plain string replacement over Jinja2 to avoid a dependency and to
    keep templates readable by hand. Any `{{var}}` whose name isn't in
    `vars_` is left untouched so it surfaces in logs rather than silently
    becoming empty.
    """
    for key, value in vars_.items():
        text = text.replace("{{" + key + "}}", value)
    return text
