"""MCP configuration helpers for channel isolation."""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from engram import paths
from engram.manifest import ChannelManifest, ManifestError, load_manifest
from engram.mcp_tools import (
    MEMORY_SEARCH_SERVER_NAME,
    make_memory_search_server,
)

log = logging.getLogger(__name__)
MCP_INVENTORY_STATE_FILE = "mcp_inventory_state.json"
MCP_MIGRATION_LOCK_FILE = ".migrate-mcp.lock"


@dataclass(frozen=True)
class MCPChannelCoverage:
    """Audit of user MCP inventory coverage across strict team manifests."""

    inventory_path: Path
    configured_servers: list[str]
    team_channels: list[str]
    team_manifest_paths: dict[str, Path]
    allowed_by_channel: dict[str, list[str]]
    uncovered_servers: list[str]
    invalid_manifest_paths: list[Path]


@dataclass(frozen=True)
class MCPInventoryDelta:
    """Diff between the current Claude MCP inventory and Engram's snapshot."""

    state_path: Path
    known_servers: list[str]
    current_servers: list[str]
    new_servers: list[str]


@dataclass(frozen=True)
class ChannelMCPAccessSummary:
    """Resolved MCP access view for one channel manifest."""

    mode: Literal["inherit-all", "allow-list"]
    inventory: list[str]
    allowed: list[str] | None
    disallowed: list[str]
    effective: list[str]
    missing: list[str]


def claude_mcp_config_path() -> Path:
    """Return Claude Code's documented user MCP config path."""
    return Path.home() / ".claude.json"


def legacy_claude_mcp_config_path() -> Path:
    """Return Engram's deprecated legacy MCP inventory path."""
    return Path.home() / ".claude" / "mcp.json"


def mcp_inventory_state_path(home: Path | None = None) -> Path:
    return paths.state_dir(home) / MCP_INVENTORY_STATE_FILE


def _load_mcp_config_root(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("mcp.config_invalid_json path=%s", path)
        return None

    if not isinstance(data, dict):
        log.warning("mcp.config_invalid_root path=%s", path)
        return None
    return data


def _extract_mcp_servers(
    data: dict[str, Any] | None,
    *,
    path: Path,
) -> dict[str, dict[str, Any]]:
    if data is None:
        return {}

    servers = data.get("mcpServers") or {}
    if not isinstance(servers, dict):
        log.warning("mcp.config_invalid_servers path=%s", path)
        return {}
    return dict(servers)


def _next_backup_path(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.bak")
    if not backup.exists():
        return backup

    suffix = 1
    while True:
        candidate = path.with_name(f"{path.name}.bak.{suffix}")
        if not candidate.exists():
            return candidate
        suffix += 1


def _mcp_migration_lock_path() -> Path:
    return paths.state_dir() / MCP_MIGRATION_LOCK_FILE


def migrate_legacy_claude_mcp_config() -> None:
    """One-time merge from deprecated ~/.claude/mcp.json into ~/.claude.json."""
    lock_path = _mcp_migration_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            _migrate_legacy_claude_mcp_config_locked()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _migrate_legacy_claude_mcp_config_locked() -> None:
    target_path = claude_mcp_config_path()
    legacy_path = legacy_claude_mcp_config_path()
    if not legacy_path.exists():
        return

    legacy_root = _load_mcp_config_root(legacy_path)
    if legacy_root is None:
        return
    raw_legacy_servers = legacy_root.get("mcpServers")
    if raw_legacy_servers is not None and not isinstance(raw_legacy_servers, dict):
        log.warning("mcp.config_invalid_servers path=%s", legacy_path)
        return
    legacy_servers = _extract_mcp_servers(legacy_root, path=legacy_path)

    target_root = _load_mcp_config_root(target_path)
    target_servers = _extract_mcp_servers(target_root, path=target_path)

    merged_servers = dict(target_servers)
    added_names: list[str] = []
    skipped_names: list[str] = []
    for name, config in legacy_servers.items():
        if name in merged_servers:
            skipped_names.append(name)
            continue
        merged_servers[name] = config
        added_names.append(name)

    needs_target_write = not target_path.exists() or target_root is None
    if target_root is not None:
        raw_target_servers = target_root.get("mcpServers")
        if raw_target_servers is None or not isinstance(raw_target_servers, dict):
            needs_target_write = True
    if added_names:
        needs_target_write = True

    target_backup: Path | None = None
    if needs_target_write:
        target_root = dict(target_root or {})
        target_root["mcpServers"] = merged_servers
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_backup = _next_backup_path(target_path)
            shutil.copy2(target_path, target_backup)
        target_path.write_text(
            json.dumps(target_root, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    legacy_backup = _next_backup_path(legacy_path)
    legacy_path.replace(legacy_backup)
    log.warning(
        "mcp.legacy_config_migrated legacy=%s target=%s legacy_backup=%s "
        "target_backup=%s added=%s skipped=%s",
        legacy_path,
        target_path,
        legacy_backup,
        target_backup,
        added_names,
        skipped_names,
    )


def hash_inventory_config(config: dict[str, Any] | None) -> str:
    """Return a stable sha256 hash of an MCP server inventory entry.

    Used by the trust gate to bind owner approval to the specific package
    config (command, args, env, etc.) the owner reviewed. Hash semantics
    (not raw config) keep env values out of plan logs and audit trails.

    Returns an empty string for missing/None config so callers can use
    "empty hash" as the sentinel for "no snapshot, no enforcement" — see
    GRO-544.
    """
    if not config:
        return ""
    canonical = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_claude_mcp_servers(
    config_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load MCP server configs from ~/.claude.json.

    Malformed or absent config is treated as an empty inventory. The caller
    decides whether missing manifest references should warn or fail.
    """
    path = config_path or claude_mcp_config_path()
    data = _load_mcp_config_root(path)
    return _extract_mcp_servers(data, path=path)


def load_known_mcp_servers(
    *,
    home: Path | None = None,
) -> list[str]:
    """Load the last MCP inventory snapshot Engram recorded."""
    path = mcp_inventory_state_path(home)
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("mcp.inventory_state_invalid_json path=%s", path)
        return []

    if not isinstance(payload, dict):
        log.warning("mcp.inventory_state_invalid_root path=%s", path)
        return []

    names = payload.get("known_servers")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        log.warning("mcp.inventory_state_invalid_servers path=%s", path)
        return []
    return list(dict.fromkeys(names))


def write_mcp_inventory_state(
    server_names: dict[str, dict[str, Any]] | list[str],
    *,
    home: Path | None = None,
) -> Path:
    """Persist the current Claude MCP inventory for later delta checks."""
    names = list(server_names)

    normalized = sorted(
        name for name in dict.fromkeys(names) if isinstance(name, str)
    )
    path = mcp_inventory_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "known_servers": normalized,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def detect_new_user_mcp_servers(
    configured_servers: dict[str, dict[str, Any]] | None = None,
    *,
    home: Path | None = None,
) -> MCPInventoryDelta:
    """Compare the current Claude MCP inventory to Engram's last snapshot."""
    configured = (
        load_claude_mcp_servers()
        if configured_servers is None
        else dict(configured_servers)
    )
    current_servers = sorted(configured)
    known_servers = load_known_mcp_servers(home=home)
    known = set(known_servers)
    new_servers = [name for name in current_servers if name not in known]
    return MCPInventoryDelta(
        state_path=mcp_inventory_state_path(home),
        known_servers=known_servers,
        current_servers=current_servers,
        new_servers=new_servers,
    )


def audit_mcp_channel_coverage(
    *,
    contexts_path: Path | None = None,
    configured_servers: dict[str, dict[str, Any]] | None = None,
) -> MCPChannelCoverage:
    """Compare Claude Code's user MCP inventory to strict team manifests.

    Owner DMs use ``setting_sources=["user"]`` and are intentionally
    ignored here. The question this audit answers is narrower: which user
    MCPs are registered in ``~/.claude.json`` but not allowed anywhere in
    Engram's strict team-channel manifest layer?
    """

    configured = (
        load_claude_mcp_servers()
        if configured_servers is None
        else dict(configured_servers)
    )
    context_root = contexts_path or paths.contexts_dir()
    team_channels: list[str] = []
    team_manifest_paths: dict[str, Path] = {}
    allowed_by_channel: dict[str, list[str]] = {}
    invalid_manifest_paths: list[Path] = []
    allowed_anywhere: set[str] = set()

    if context_root.exists():
        for manifest_path in sorted(context_root.glob("*/.claude/channel-manifest.yaml")):
            try:
                manifest = load_manifest(manifest_path)
            except ManifestError:
                invalid_manifest_paths.append(manifest_path)
                continue
            if manifest.is_owner_dm():
                continue

            # GRO-532 fix: subtract disallowed BEFORE recording channel
            # coverage. A channel with `allowed: [foo]` AND `disallowed: [foo]`
            # has zero effective access to foo (see resolve_team_mcp_servers
            # below which applies the same filter). Without this subtraction,
            # the audit reports false-positive PASS when an MCP is allowed in
            # one team channel but disallowed in another, or even when allowed
            # and disallowed in the same channel.
            allowed_raw = list(manifest.mcp_servers.allowed or [])
            disallowed = list(manifest.mcp_servers.disallowed or [])
            effective_allowed = [
                name for name in allowed_raw if name not in disallowed
            ]
            team_channels.append(manifest.channel_id)
            team_manifest_paths[manifest.channel_id] = manifest_path
            allowed_by_channel[manifest.channel_id] = effective_allowed
            allowed_anywhere.update(effective_allowed)

    uncovered_servers = [
        name for name in configured if name not in allowed_anywhere
    ]
    return MCPChannelCoverage(
        inventory_path=claude_mcp_config_path(),
        configured_servers=list(configured),
        team_channels=team_channels,
        team_manifest_paths=team_manifest_paths,
        allowed_by_channel=allowed_by_channel,
        uncovered_servers=uncovered_servers,
        invalid_manifest_paths=invalid_manifest_paths,
    )


def resolve_team_mcp_servers(
    manifest: ChannelManifest,
    *,
    configured_servers: dict[str, dict[str, Any]] | None = None,
    embedder: Any | None = None,
    log_exclusions: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Resolve a team-channel manifest to an explicit MCP config map.

    Team channels are strict by default: only names in `mcp_servers.allowed`
    are eligible. If the allow-list is absent, the effective set is empty.

    Returns `(servers, allowed_names, missing_names)`.
    """
    configured = (
        load_claude_mcp_servers()
        if configured_servers is None
        else configured_servers
    )
    allowed_names = list(manifest.mcp_servers.allowed or [])
    disallowed = set(manifest.mcp_servers.disallowed)

    if log_exclusions:
        allowed = set(allowed_names)
        for name in configured:
            reason: str | None = None
            if name in disallowed:
                reason = "in_disallowed"
            elif name not in allowed:
                reason = "not_in_allowed"
            if reason is None:
                continue
            log.info(
                "mcp.excluded_by_manifest",
                extra={
                    "channel_id": manifest.channel_id,
                    "mcp_name": name,
                    "reason": reason,
                    "available_in_inventory": True,
                },
            )

    effective_names = [name for name in allowed_names if name not in disallowed]

    servers: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name in effective_names:
        if name == MEMORY_SEARCH_SERVER_NAME:
            servers[name] = make_memory_search_server(
                manifest.channel_id,
                embedder=embedder,
                excluded_channels=manifest.memory.excluded_channels,
            )
            continue
        config = configured.get(name)
        if config is None:
            missing.append(name)
            continue
        servers[name] = config
    return servers, effective_names, missing


def summarize_channel_mcp_access(
    manifest: ChannelManifest,
    *,
    configured_servers: dict[str, dict[str, Any]] | None = None,
) -> ChannelMCPAccessSummary:
    """Return the effective MCP access picture for a channel manifest."""
    configured = (
        load_claude_mcp_servers()
        if configured_servers is None
        else configured_servers
    )
    inventory = sorted(configured)
    available = list(dict.fromkeys([*inventory, MEMORY_SEARCH_SERVER_NAME]))
    allowed = (
        list(manifest.mcp_servers.allowed)
        if manifest.mcp_servers.allowed is not None
        else None
    )
    disallowed = list(manifest.mcp_servers.disallowed)
    disallowed_set = set(disallowed)

    if allowed is None:
        effective = [name for name in available if name not in disallowed_set]
        missing: list[str] = []
        mode: Literal["inherit-all", "allow-list"] = "inherit-all"
    else:
        effective = [
            name for name in allowed if name in available and name not in disallowed_set
        ]
        missing = [
            name for name in allowed if name not in available and name not in disallowed_set
        ]
        mode = "allow-list"

    return ChannelMCPAccessSummary(
        mode=mode,
        inventory=inventory,
        allowed=allowed,
        disallowed=disallowed,
        effective=effective,
        missing=missing,
    )


def render_channel_mcp_access(
    manifest: ChannelManifest,
    *,
    configured_servers: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Render a human-readable MCP access summary for CLI and Slack."""
    summary = summarize_channel_mcp_access(
        manifest,
        configured_servers=configured_servers,
    )

    def _fmt(values: list[str] | None, *, empty: str) -> str:
        if values is None:
            return "inherit-all"
        if not values:
            return empty
        return ", ".join(values)

    lines = [
        f"MCP access for {manifest.label or manifest.channel_id} ({manifest.channel_id})",
        f"Tier: {manifest.tier_effective().value}",
        f"Mode: {summary.mode}",
        f"Allowed: {_fmt(summary.allowed, empty='(none)')}",
        f"Denied: {_fmt(summary.disallowed, empty='(none)')}",
        f"Effective: {_fmt(summary.effective, empty='(none)')}",
    ]
    if summary.missing:
        lines.append(f"Missing from ~/.claude.json: {', '.join(summary.missing)}")
    return "\n".join(lines)


def warn_missing_mcp_servers(
    channel_id: str,
    missing: list[str],
    *,
    logger: logging.Logger,
    config_path: Path | None = None,
) -> None:
    """Log missing manifest MCP references without failing provisioning."""
    path = config_path or claude_mcp_config_path()
    for name in missing:
        logger.warning(
            "channel.mcp_server_missing channel_id=%s server=%s config=%s",
            channel_id,
            name,
            path,
        )
