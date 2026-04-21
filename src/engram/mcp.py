"""MCP configuration helpers for channel isolation."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from engram.manifest import ChannelManifest

log = logging.getLogger(__name__)


def claude_mcp_config_path() -> Path:
    """Return Claude Code's documented user MCP config path."""
    return Path.home() / ".claude" / "mcp.json"


def load_claude_mcp_servers(
    config_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load MCP server configs from ~/.claude/mcp.json.

    Malformed or absent config is treated as an empty inventory. The caller
    decides whether missing manifest references should warn or fail.
    """
    path = config_path or claude_mcp_config_path()
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("mcp.config_invalid_json path=%s", path)
        return {}

    servers = data.get("mcpServers") or {}
    if not isinstance(servers, dict):
        log.warning("mcp.config_invalid_servers path=%s", path)
        return {}
    return dict(servers)


def resolve_team_mcp_servers(
    manifest: ChannelManifest,
    *,
    configured_servers: dict[str, dict[str, Any]] | None = None,
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
    effective_names = [name for name in allowed_names if name not in disallowed]

    servers: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name in effective_names:
        config = configured.get(name)
        if config is None:
            missing.append(name)
            continue
        servers[name] = config
    return servers, effective_names, missing


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
