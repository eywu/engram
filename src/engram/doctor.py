"""Pre-flight diagnostics for an Engram installation."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from rich.console import Console
from rich.table import Table

from engram.config import EngramConfig
from engram.launchd import (
    bridge_template_commit,
    doctor_bridge_plist_issues,
    find_repo_root,
    installed_bridge_plist_path,
    load_plist,
)
from engram.mcp import audit_mcp_channel_coverage, load_claude_mcp_servers
from engram.runtime import fd_usage_snapshot, read_latest_fd_snapshot

SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
GEMINI_EMBED_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent?key={api_key}"
)
MIN_MEMORY_DB_FREE_BYTES = 1_000_000_000
SLACK_SLASH_COMMANDS = (
    "/engram",
    "/exclude-from-nightly",
    "/include-in-nightly",
)
SLACK_SLASH_COMMAND_MISSING_PATTERNS = (
    "not a valid command",
    "isn't a valid command",
    "unknown slash command",
)
SLACK_SLASH_COMMAND_LOG_WINDOW = datetime.timedelta(hours=24)
MCP_EXCLUSION_LOG_WINDOW = datetime.timedelta(hours=24)


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


STATUS_EMOJI = {
    CheckStatus.PASS: "✅",
    CheckStatus.WARN: "⚠️",
    CheckStatus.FAIL: "❌",
}


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    name: str
    status: CheckStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def emoji(self) -> str:
        return STATUS_EMOJI[self.status]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "emoji": self.emoji,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck]
    schema_version: int = 1

    @property
    def exit_code(self) -> int:
        return 1 if any(check.status == CheckStatus.FAIL for check in self.checks) else 0

    @property
    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.checks),
            "passed": sum(1 for check in self.checks if check.status == CheckStatus.PASS),
            "warnings": sum(1 for check in self.checks if check.status == CheckStatus.WARN),
            "failed": sum(1 for check in self.checks if check.status == CheckStatus.FAIL),
            "exit_code": self.exit_code,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "summary": self.summary,
            "checks": [check.to_json() for check in self.checks],
        }


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    payload: dict[str, Any]
    text: str = ""


def default_config_path() -> Path:
    return Path.home() / ".engram" / "config.yaml"


def run_doctor(config_path: Path | None = None) -> DoctorReport:
    path = (config_path or default_config_path()).expanduser()
    config_check, config = check_config_loads(path)
    contexts_path = (
        config.paths.contexts_dir if config is not None else path.parent / "contexts"
    )
    log_dir = config.paths.log_dir if config is not None else path.parent / "logs"

    checks = [
        check_uv_on_path(),
        check_claude_on_path(),
        check_mcp_commands_on_bridge_path(),
        check_python_version(),
        check_config_file(path),
        config_check,
        check_mcp_channel_coverage(
            contexts_path=contexts_path,
            log_dir=log_dir,
        ),
        check_owner_dm_channel_id(config),
        check_owner_user_id(config),
        check_slack_bot_token(
            config,
            expected_team_id=_configured_slack_team_id(path),
        ),
        check_slack_app_token(config),
        check_slack_slash_commands(config, log_dir=log_dir),
        check_anthropic_api_key(config),
        check_gemini_api_key(config),
        check_launchd_job(
            "launchd_bridge",
            "launchd bridge job",
            "com.engram.bridge",
        ),
        check_launchd_bridge_plist_drift(),
        check_launchd_job(
            "launchd_nightly",
            "launchd nightly job",
            "com.engram.v3.nightly",
        ),
        check_fd_pressure(log_dir),
        check_disk_space(path.parent),
        check_log_dir_writable(log_dir),
    ]
    return DoctorReport(checks=checks)


def render_report(report: DoctorReport, console: Console | None = None) -> None:
    target = console or Console()
    table = Table(title="Engram Doctor")
    table.add_column("Check")
    table.add_column("Status", justify="center")
    table.add_column("Hint")
    for check in report.checks:
        table.add_row(check.name, check.emoji, check.message)
    target.print(table)
    summary = report.summary
    target.print(
        f"{summary['passed']} passed, "
        f"{summary['warnings']} warnings, "
        f"{summary['failed']} failed"
    )


def check_uv_on_path(
    *,
    which: Callable[[str], str | None] | None = None,
    version_runner: Callable[[str], str | None] | None = None,
) -> DoctorCheck:
    return _check_binary_on_path(
        check_id="uv_path",
        name="uv on PATH",
        binary="uv",
        install_hint="Install uv and make sure it is on PATH.",
        which=which,
        version_runner=version_runner,
    )


def check_claude_on_path(
    *,
    which: Callable[[str], str | None] | None = None,
    version_runner: Callable[[str], str | None] | None = None,
) -> DoctorCheck:
    return _check_binary_on_path(
        check_id="claude_path",
        name="claude CLI on PATH",
        binary="claude",
        install_hint="Install the Claude CLI or add it to PATH.",
        which=which,
        version_runner=version_runner,
    )


def check_mcp_commands_on_bridge_path(
    *,
    home: Path | None = None,
    configured_servers: dict[str, dict[str, Any]] | None = None,
    plist_loader: Callable[[Path], dict[str, Any]] | None = None,
) -> DoctorCheck:
    base_home = (home or Path.home()).expanduser()
    inventory_path = base_home / ".claude.json"
    installed_path = installed_bridge_plist_path(base_home)
    servers = (
        load_claude_mcp_servers(config_path=inventory_path)
        if configured_servers is None
        else dict(configured_servers)
    )
    details: dict[str, Any] = {
        "inventory_path": str(inventory_path),
        "installed_path": str(installed_path),
        "configured_servers": sorted(servers),
    }

    if not servers:
        return DoctorCheck(
            id="mcp_bridge_path",
            name="MCP bridge PATH",
            status=CheckStatus.PASS,
            message="No user MCP servers are registered in ~/.claude.json.",
            details=details,
        )

    if not installed_path.exists():
        return DoctorCheck(
            id="mcp_bridge_path",
            name="MCP bridge PATH",
            status=CheckStatus.WARN,
            message=(
                f"{installed_path} is missing; run `./scripts/install_launchd.sh` "
                "before checking MCP command resolution under launchd."
            ),
            details=details,
        )

    try:
        installed = (plist_loader or load_plist)(installed_path)
    except Exception as exc:
        return DoctorCheck(
            id="mcp_bridge_path",
            name="MCP bridge PATH",
            status=CheckStatus.WARN,
            message=f"Installed bridge plist could not be parsed: {type(exc).__name__}: {exc}",
            details=details | {"error_class": type(exc).__name__},
        )

    env_vars = installed.get("EnvironmentVariables")
    bridge_path = env_vars.get("PATH") if isinstance(env_vars, dict) else None
    if not isinstance(bridge_path, str) or not bridge_path.strip():
        return DoctorCheck(
            id="mcp_bridge_path",
            name="MCP bridge PATH",
            status=CheckStatus.WARN,
            message="Installed bridge plist is missing EnvironmentVariables.PATH.",
            details=details,
        )

    details["bridge_path"] = bridge_path
    unreachable: list[dict[str, str]] = []
    checked: list[dict[str, str]] = []
    for name in sorted(servers):
        command = _mcp_server_command(servers[name])
        if not command:
            continue
        resolved = _resolve_command_on_bridge_path(command, bridge_path)
        checked.append(
            {
                "server": name,
                "command": command,
                "resolved_path": resolved or "",
            }
        )
        if resolved is None:
            unreachable.append({"server": name, "command": command})

    details["checked_commands"] = checked
    details["unreachable"] = unreachable

    if not unreachable:
        return DoctorCheck(
            id="mcp_bridge_path",
            name="MCP bridge PATH",
            status=CheckStatus.PASS,
            message="Every configured MCP command resolves under the bridge PATH.",
            details=details,
        )

    summary = ", ".join(f"{item['server']} -> {item['command']}" for item in unreachable[:3])
    if len(unreachable) > 3:
        summary = f"{summary}, +{len(unreachable) - 3} more"
    node_runtime_hint = any(item["command"] in {"npx", "node"} for item in unreachable)
    hint = (
        " Reinstall the bridge with `./scripts/install_launchd.sh` after `nvm use --lts`, "
        "or install Node via Homebrew (`brew install node`)."
        if node_runtime_hint
        else " Reinstall the bridge with `./scripts/install_launchd.sh` after fixing the missing binary."
    )
    return DoctorCheck(
        id="mcp_bridge_path",
        name="MCP bridge PATH",
        status=CheckStatus.FAIL,
        message=f"Bridge PATH cannot resolve configured MCP commands: {summary}.{hint}",
        details=details,
    )


def check_python_version(version_info: tuple[Any, ...] | None = None) -> DoctorCheck:
    raw = version_info or sys.version_info
    major = int(raw[0])
    minor = int(raw[1])
    micro = int(raw[2])
    version = f"{major}.{minor}.{micro}"
    if (major, minor) >= (3, 12):
        return DoctorCheck(
            id="python_version",
            name="Python version",
            status=CheckStatus.PASS,
            message=f"Python {version} satisfies 3.12+.",
            details={"version": version},
        )
    return DoctorCheck(
        id="python_version",
        name="Python version",
        status=CheckStatus.FAIL,
        message=f"Python {version} is too old; install Python 3.12+.",
        details={"version": version},
    )


def check_config_file(config_path: Path | None = None) -> DoctorCheck:
    path = (config_path or default_config_path()).expanduser()
    details = {"path": str(path)}
    if not path.exists():
        return DoctorCheck(
            id="config_file",
            name="Config file",
            status=CheckStatus.FAIL,
            message=f"{path} is missing; run `engram setup` or create config.yaml.",
            details=details,
        )
    if not path.is_file():
        return DoctorCheck(
            id="config_file",
            name="Config file",
            status=CheckStatus.FAIL,
            message=f"{path} exists but is not a file.",
            details=details,
        )

    mode = stat.S_IMODE(path.stat().st_mode)
    details["mode"] = oct(mode)
    if mode != 0o600:
        return DoctorCheck(
            id="config_file",
            name="Config file",
            status=CheckStatus.WARN,
            message=f"{path} mode is {mode:o}; run `chmod 600 {path}`.",
            details=details,
        )
    return DoctorCheck(
        id="config_file",
        name="Config file",
        status=CheckStatus.PASS,
        message=f"{path} exists with mode 600.",
        details=details,
    )


def check_config_loads(
    config_path: Path | None = None,
    *,
    loader: Callable[[Path], EngramConfig] | None = None,
) -> tuple[DoctorCheck, EngramConfig | None]:
    path = (config_path or default_config_path()).expanduser()
    load = loader or EngramConfig.load
    try:
        config = load(path)
    except Exception as exc:
        return (
            DoctorCheck(
                id="config_load",
                name="Config loads",
                status=CheckStatus.FAIL,
                message=f"Config failed to load: {type(exc).__name__}: {exc}",
                details={"path": str(path), "error_class": type(exc).__name__},
            ),
            None,
        )
    return (
        DoctorCheck(
            id="config_load",
            name="Config loads",
            status=CheckStatus.PASS,
            message="EngramConfig.load() completed cleanly.",
            details={"path": str(path)},
        ),
        config,
    )


def check_owner_dm_channel_id(config: EngramConfig | None) -> DoctorCheck:
    if config is None:
        return _blocked_by_config("owner_dm_channel_id", "Owner DM channel")
    channel_id = _optional_str(config.owner_dm_channel_id)
    details = {"channel_id": channel_id}
    if not channel_id:
        return DoctorCheck(
            id="owner_dm_channel_id",
            name="Owner DM channel",
            status=CheckStatus.WARN,
            message="owner_dm_channel_id is unset; upgrade approvals cannot reach the owner DM.",
            details=details,
        )
    if not channel_id.startswith("D"):
        return DoctorCheck(
            id="owner_dm_channel_id",
            name="Owner DM channel",
            status=CheckStatus.WARN,
            message=f"owner_dm_channel_id={channel_id} does not look like a Slack DM channel ID.",
            details=details,
        )
    return DoctorCheck(
        id="owner_dm_channel_id",
        name="Owner DM channel",
        status=CheckStatus.PASS,
        message=f"owner_dm_channel_id is configured as {channel_id}.",
        details=details,
    )


def check_owner_user_id(config: EngramConfig | None) -> DoctorCheck:
    if config is None:
        return _blocked_by_config("owner_user_id", "Owner user ID")
    user_id = _optional_str(config.owner_user_id)
    details = {"user_id": user_id}
    if not user_id:
        return DoctorCheck(
            id="owner_user_id",
            name="Owner user ID",
            status=CheckStatus.WARN,
            message="owner_user_id is unset; upgrade approval buttons cannot verify the owner.",
            details=details,
        )
    if not user_id.startswith("U"):
        return DoctorCheck(
            id="owner_user_id",
            name="Owner user ID",
            status=CheckStatus.WARN,
            message=f"owner_user_id={user_id} does not look like a Slack user ID.",
            details=details,
        )
    return DoctorCheck(
        id="owner_user_id",
        name="Owner user ID",
        status=CheckStatus.PASS,
        message=f"owner_user_id is configured as {user_id}.",
        details=details,
    )


def check_slack_bot_token(
    config: EngramConfig | None,
    *,
    expected_team_id: str | None = None,
    requester: Callable[..., HttpResult] | None = None,
) -> DoctorCheck:
    if config is None:
        return _blocked_by_config("slack_bot_token", "Slack bot token")

    request = requester or _post_json
    try:
        response = request(
            SLACK_AUTH_TEST_URL,
            headers={
                "Authorization": f"Bearer {config.slack.bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            payload={},
        )
    except Exception as exc:
        return DoctorCheck(
            id="slack_bot_token",
            name="Slack bot token",
            status=CheckStatus.WARN,
            message=f"Could not reach Slack auth.test: {type(exc).__name__}: {exc}",
            details={"error_class": type(exc).__name__},
        )

    details = {
        "status_code": response.status_code,
        "team_id": response.payload.get("team_id"),
        "expected_team_id": expected_team_id,
    }
    if response.status_code != 200:
        return DoctorCheck(
            id="slack_bot_token",
            name="Slack bot token",
            status=CheckStatus.FAIL,
            message=f"Slack auth.test returned HTTP {response.status_code}.",
            details=details,
        )
    if not response.payload.get("ok"):
        error = response.payload.get("error") or "unknown_error"
        return DoctorCheck(
            id="slack_bot_token",
            name="Slack bot token",
            status=CheckStatus.FAIL,
            message=f"Slack bot token rejected by auth.test: {error}.",
            details=details | {"slack_error": error},
        )

    team_id = _optional_str(response.payload.get("team_id"))
    if expected_team_id and team_id != expected_team_id:
        return DoctorCheck(
            id="slack_bot_token",
            name="Slack bot token",
            status=CheckStatus.FAIL,
            message=f"Slack token is for team {team_id}; expected {expected_team_id}.",
            details=details,
        )
    if not team_id:
        return DoctorCheck(
            id="slack_bot_token",
            name="Slack bot token",
            status=CheckStatus.WARN,
            message="Slack auth.test succeeded but did not return a team_id.",
            details=details,
        )
    return DoctorCheck(
        id="slack_bot_token",
        name="Slack bot token",
        status=CheckStatus.PASS,
        message=f"Slack auth.test succeeded for team {team_id}.",
        details=details,
    )


def check_slack_app_token(config: EngramConfig | None) -> DoctorCheck:
    if config is None:
        return _blocked_by_config("slack_app_token", "Slack app token")
    token = config.slack.app_token
    if token.startswith("xapp-"):
        return DoctorCheck(
            id="slack_app_token",
            name="Slack app token",
            status=CheckStatus.PASS,
            message="Slack app token has the required xapp- prefix.",
            details={"prefix": "xapp-"},
        )
    return DoctorCheck(
        id="slack_app_token",
        name="Slack app token",
        status=CheckStatus.FAIL,
        message="Slack app token must start with xapp- for Socket Mode.",
        details={"prefix": token[:5]},
    )


def check_slack_slash_commands(
    config: EngramConfig | None,
    *,
    log_dir: Path | None = None,
    now: Callable[[], datetime.datetime] | None = None,
) -> DoctorCheck:
    details: dict[str, Any] = {
        "required_commands": list(SLACK_SLASH_COMMANDS),
        "window_hours": int(SLACK_SLASH_COMMAND_LOG_WINDOW.total_seconds() // 3600),
    }
    if config is None:
        return DoctorCheck(
            id="slack_slash_commands",
            name="Slack slash commands",
            status=CheckStatus.WARN,
            message="Slack slash-command check skipped because config did not load.",
            details=details | {"verdict": "unknown", "reason": "config_unavailable"},
        )

    if not _optional_str(config.slack.bot_token):
        return DoctorCheck(
            id="slack_slash_commands",
            name="Slack slash commands",
            status=CheckStatus.WARN,
            message="Slack slash-command check skipped because the bot token is unavailable.",
            details=details | {"verdict": "unknown", "reason": "missing_bot_token"},
        )

    clock = now or (lambda: datetime.datetime.now(datetime.UTC))
    window_end = clock()
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=datetime.UTC)
    cutoff = window_end - SLACK_SLASH_COMMAND_LOG_WINDOW
    path = (log_dir or config.paths.log_dir).expanduser()
    evidence = _collect_slack_slash_command_evidence(
        path,
        cutoff=cutoff,
        window_end=window_end,
    )
    details |= {
        "verdict": evidence["verdict"],
        "log_dir": str(path),
        "log_files": evidence["log_files"],
        "observed_commands": evidence["observed_commands"],
        "missing_signals": evidence["missing_signals"],
    }
    if evidence["verdict"] == "present":
        return DoctorCheck(
            id="slack_slash_commands",
            name="Slack slash commands",
            status=CheckStatus.PASS,
            message="Recent bridge logs show all three slash commands reaching Engram.",
            details=details,
        )
    if evidence["verdict"] == "missing":
        return DoctorCheck(
            id="slack_slash_commands",
            name="Slack slash commands",
            status=CheckStatus.WARN,
            message=(
                "Recent bridge logs suggest Slack slash commands are missing. "
                "See the 'Upgrading an existing install' section in docs/INSTALL.md."
            ),
            details=details,
        )
    return DoctorCheck(
        id="slack_slash_commands",
        name="Slack slash commands",
        status=CheckStatus.WARN,
        message=(
            "No recent bridge evidence confirmed all slash commands. "
            "Type `/engram` in Slack; it should autocomplete."
        ),
        details=details,
    )


def check_mcp_channel_coverage(
    *,
    contexts_path: Path | None = None,
    log_dir: Path | None = None,
    configured_servers: dict[str, dict[str, Any]] | None = None,
    now: Callable[[], datetime.datetime] | None = None,
) -> DoctorCheck:
    coverage = audit_mcp_channel_coverage(
        contexts_path=contexts_path,
        configured_servers=configured_servers,
    )
    details: dict[str, Any] = {
        "inventory_path": str(coverage.inventory_path),
        "contexts_path": str(contexts_path or (Path.home() / ".engram" / "contexts")),
        "configured_servers": coverage.configured_servers,
        "team_channels": coverage.team_channels,
        "team_manifest_paths": {
            channel_id: str(path)
            for channel_id, path in coverage.team_manifest_paths.items()
        },
        "allowed_by_channel": coverage.allowed_by_channel,
        "uncovered_servers": coverage.uncovered_servers,
        "invalid_manifest_paths": [
            str(path) for path in coverage.invalid_manifest_paths
        ],
    }

    clock = now or (lambda: datetime.datetime.now(datetime.UTC))
    window_end = clock()
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=datetime.UTC)
    cutoff = window_end - MCP_EXCLUSION_LOG_WINDOW
    recent_exclusions = _collect_recent_mcp_exclusion_evidence(
        (log_dir or (Path.home() / ".engram" / "logs")).expanduser(),
        cutoff=cutoff,
        window_end=window_end,
    )
    if recent_exclusions:
        details["recent_exclusions"] = recent_exclusions

    # GRO-532 fix: invalid manifests must surface as WARN, not silent PASS.
    # Previously check_mcp_channel_coverage collected
    # `coverage.invalid_manifest_paths` into details but never branched on it,
    # so a corrupted team manifest would fall through to one of the PASS
    # branches below and the operator would be told everything is fine.
    if coverage.invalid_manifest_paths:
        bad = ", ".join(str(p) for p in coverage.invalid_manifest_paths)
        return DoctorCheck(
            id="mcp_channel_coverage",
            name="MCP channel coverage",
            status=CheckStatus.WARN,
            message=(
                f"Could not parse {len(coverage.invalid_manifest_paths)} "
                f"team channel manifest(s): {bad}. "
                "Coverage analysis is incomplete until they are repaired "
                "or removed."
            ),
            details=details,
        )

    if not coverage.configured_servers:
        return DoctorCheck(
            id="mcp_channel_coverage",
            name="MCP channel coverage",
            status=CheckStatus.PASS,
            message="No user MCP servers are registered in ~/.claude.json.",
            details=details,
        )

    if not coverage.team_channels:
        return DoctorCheck(
            id="mcp_channel_coverage",
            name="MCP channel coverage",
            status=CheckStatus.PASS,
            message=(
                "No team channel manifests exist yet. Owner DMs auto-discover "
                "user MCPs from ~/.claude.json."
            ),
            details=details,
        )

    # GRO-532 fix: surface recent_exclusions independently of uncovered_servers.
    # If the audit thinks coverage is globally fine but a real per-channel
    # exclusion was recently logged, the operator needs to see it. Without
    # this branch, exclusions only surfaced when global coverage already
    # failed, hiding per-channel issues behind global PASS.
    if not coverage.uncovered_servers:
        if recent_exclusions:
            return DoctorCheck(
                id="mcp_channel_coverage",
                name="MCP channel coverage",
                status=CheckStatus.WARN,
                message=(
                    "Coverage looks complete globally, but recent bridge "
                    "logs recorded mcp.excluded_by_manifest events. Some "
                    "team channels may still be filtering MCPs you expect "
                    "to be available; check `recent_exclusions` in details."
                ),
                details=details,
            )
        return DoctorCheck(
            id="mcp_channel_coverage",
            name="MCP channel coverage",
            status=CheckStatus.PASS,
            message="Every user MCP is allowed in at least one strict team channel manifest.",
            details=details,
        )

    servers = ", ".join(coverage.uncovered_servers)
    suffix = ""
    if recent_exclusions:
        suffix = " Recent bridge logs already recorded mcp.excluded_by_manifest."
    return DoctorCheck(
        id="mcp_channel_coverage",
        name="MCP channel coverage",
        status=CheckStatus.WARN,
        message=(
            f"Registered in ~/.claude.json but allowed in no team channel manifests: {servers}. "
            "Fix: add each server under mcp_servers.allowed in "
            "~/.engram/contexts/<channel-id>/.claude/channel-manifest.yaml."
            f"{suffix}"
        ),
        details=details,
    )


def check_anthropic_api_key(
    config: EngramConfig | None,
    *,
    requester: Callable[..., HttpResult] | None = None,
) -> DoctorCheck:
    if config is None:
        return _blocked_by_config("anthropic_api_key", "Anthropic API key")

    request = requester or _get_json
    try:
        response = request(
            ANTHROPIC_MODELS_URL,
            headers={
                "x-api-key": config.anthropic.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
    except Exception as exc:
        return DoctorCheck(
            id="anthropic_api_key",
            name="Anthropic API key",
            status=CheckStatus.WARN,
            message=f"Could not reach Anthropic models API: {type(exc).__name__}: {exc}",
            details={
                "error_class": type(exc).__name__,
                "configured_model": config.anthropic.model,
            },
        )

    details = {
        "status_code": response.status_code,
        "configured_model": config.anthropic.model,
    }
    if response.status_code == 401:
        return DoctorCheck(
            id="anthropic_api_key",
            name="Anthropic API key",
            status=CheckStatus.FAIL,
            message="Anthropic API key was rejected with 401; check ANTHROPIC_API_KEY.",
            details=details,
        )
    if response.status_code == 429:
        return DoctorCheck(
            id="anthropic_api_key",
            name="Anthropic API key",
            status=CheckStatus.WARN,
            message="Anthropic models API is rate limited; the key reached the service.",
            details=details,
        )
    if response.status_code != 200:
        return DoctorCheck(
            id="anthropic_api_key",
            name="Anthropic API key",
            status=CheckStatus.WARN,
            message=f"Anthropic models API returned unexpected HTTP {response.status_code}.",
            details=details,
        )

    models = response.payload.get("data")
    available_models = sorted(
        str(model_id)
        for item in models
        if isinstance(item, dict) and (model_id := item.get("id"))
    ) if isinstance(models, list) else []
    details["available_models"] = available_models

    configured_model = config.anthropic.model
    if configured_model not in available_models:
        available_text = ", ".join(available_models) if available_models else "(none returned)"
        return DoctorCheck(
            id="anthropic_api_key",
            name="Anthropic API key",
            status=CheckStatus.FAIL,
            message=(
                f"Configured Anthropic model '{configured_model}' is not accessible with this key. "
                f"Available: {available_text}"
            ),
            details=details,
        )

    return DoctorCheck(
        id="anthropic_api_key",
        name="Anthropic API key",
        status=CheckStatus.PASS,
        message=f"Anthropic key valid; configured model '{configured_model}' is accessible.",
        details=details,
    )


def check_gemini_api_key(
    config: EngramConfig | None,
    *,
    requester: Callable[..., HttpResult] | None = None,
) -> DoctorCheck:
    if config is None:
        return _blocked_by_config("gemini_api_key", "Gemini API key")
    if not config.embeddings.api_key:
        return DoctorCheck(
            id="gemini_api_key",
            name="Gemini API key",
            status=CheckStatus.PASS,
            message="No key configured; Engram will use keyword-only memory.",
            details={"configured": False},
        )
    if config.embeddings.provider != "gemini":
        return DoctorCheck(
            id="gemini_api_key",
            name="Gemini API key",
            status=CheckStatus.WARN,
            message=f"Embeddings provider is {config.embeddings.provider}; Gemini check skipped.",
            details={"provider": config.embeddings.provider},
        )

    request = requester or _post_json
    url = GEMINI_EMBED_URL_TEMPLATE.format(
        model=config.embeddings.model,
        api_key=config.embeddings.api_key,
    )
    try:
        response = request(
            url,
            headers={"Content-Type": "application/json"},
            payload={"content": {"parts": [{"text": "engram doctor"}]}},
            timeout=config.embeddings.api_timeout_s,
        )
    except Exception as exc:
        return DoctorCheck(
            id="gemini_api_key",
            name="Gemini API key",
            status=CheckStatus.WARN,
            message=f"Could not reach Gemini embedding API: {type(exc).__name__}: {exc}",
            details={"error_class": type(exc).__name__, "model": config.embeddings.model},
        )

    details = {"status_code": response.status_code, "model": config.embeddings.model}
    if response.status_code == 200:
        return DoctorCheck(
            id="gemini_api_key",
            name="Gemini API key",
            status=CheckStatus.PASS,
            message=f"Gemini embedding API accepted the key for {config.embeddings.model}.",
            details=details | {"configured": True},
        )
    if response.status_code in {401, 403}:
        message = "Gemini API key was rejected; unset it or provide a valid key."
    elif response.status_code == 429:
        return DoctorCheck(
            id="gemini_api_key",
            name="Gemini API key",
            status=CheckStatus.WARN,
            message="Gemini API is rate limited; the key reached the service.",
            details=details | {"configured": True},
        )
    else:
        message = f"Gemini embedding API returned HTTP {response.status_code}."
    return DoctorCheck(
        id="gemini_api_key",
        name="Gemini API key",
        status=CheckStatus.FAIL,
        message=message,
        details=details | {"configured": True},
    )


def check_launchd_job(
    check_id: str,
    name: str,
    label: str,
    *,
    launchctl_list: Callable[[], str] | None = None,
) -> DoctorCheck:
    list_jobs = launchctl_list or _launchctl_list
    try:
        output = list_jobs()
    except FileNotFoundError:
        return DoctorCheck(
            id=check_id,
            name=name,
            status=CheckStatus.WARN,
            message="launchctl is not available on this system.",
            details={"label": label},
        )
    except Exception as exc:
        return DoctorCheck(
            id=check_id,
            name=name,
            status=CheckStatus.WARN,
            message=f"Could not inspect launchd: {type(exc).__name__}: {exc}",
            details={"label": label, "error_class": type(exc).__name__},
        )

    row = _find_launchd_row(output, label)
    if row is None:
        return DoctorCheck(
            id=check_id,
            name=name,
            status=CheckStatus.FAIL,
            message=f"{label} is not installed; load the launchd plist.",
            details={"label": label, "state": "not_installed"},
        )
    pid, status_code = row
    if pid != "-":
        return DoctorCheck(
            id=check_id,
            name=name,
            status=CheckStatus.PASS,
            message=f"{label} is running with pid {pid}.",
            details={"label": label, "pid": pid, "status_code": status_code},
        )
    return DoctorCheck(
        id=check_id,
        name=name,
        status=CheckStatus.WARN,
        message=f"{label} is installed but not running (status {status_code}).",
        details={"label": label, "pid": None, "status_code": status_code},
    )


def check_launchd_bridge_plist_drift(
    *,
    repo_root: Path | None = None,
    home: Path | None = None,
    commit_resolver: Callable[[Path], str | None] | None = None,
) -> DoctorCheck:
    installed_path = installed_bridge_plist_path(home)
    details = {"installed_path": str(installed_path)}
    if not installed_path.exists():
        return DoctorCheck(
            id="launchd_bridge_plist",
            name="launchd bridge plist",
            status=CheckStatus.WARN,
            message=f"{installed_path} is missing; run `engram setup` to install the current plist.",
            details=details,
        )

    root = repo_root or find_repo_root()
    if root is None:
        return DoctorCheck(
            id="launchd_bridge_plist",
            name="launchd bridge plist",
            status=CheckStatus.WARN,
            message="Repo launchd template not found from the current directory; cannot compare plist drift.",
            details=details,
        )
    details["repo_root"] = str(root)

    try:
        installed = load_plist(installed_path)
    except Exception as exc:
        return DoctorCheck(
            id="launchd_bridge_plist",
            name="launchd bridge plist",
            status=CheckStatus.WARN,
            message=f"Installed plist could not be parsed: {type(exc).__name__}: {exc}",
            details=details | {"error_class": type(exc).__name__},
        )

    issues = doctor_bridge_plist_issues(installed)
    if not issues:
        commit = (commit_resolver or bridge_template_commit)(root)
        if commit:
            details["template_commit"] = commit
        return DoctorCheck(
            id="launchd_bridge_plist",
            name="launchd bridge plist",
            status=CheckStatus.PASS,
            message=f"{installed_path} matches the canonical launchd bridge template.",
            details=details,
        )

    commit = (commit_resolver or bridge_template_commit)(root)
    if commit:
        details["template_commit"] = commit
    details["issues"] = [issue.path for issue in issues]
    return DoctorCheck(
        id="launchd_bridge_plist",
        name="launchd bridge plist",
        status=CheckStatus.WARN,
        message=_launchd_bridge_plist_drift_message(issues, template_commit=commit),
        details=details,
    )


def check_disk_space(
    engram_dir: Path | None = None,
    *,
    min_free_bytes: int = MIN_MEMORY_DB_FREE_BYTES,
    disk_usage: Callable[[str], Any] | None = None,
) -> DoctorCheck:
    target = (engram_dir or (Path.home() / ".engram")).expanduser()
    probe = _nearest_existing_parent(target)
    usage_fn = disk_usage or shutil.disk_usage
    try:
        usage = usage_fn(str(probe))
    except Exception as exc:
        return DoctorCheck(
            id="memory_db_disk_space",
            name="Memory DB disk space",
            status=CheckStatus.WARN,
            message=f"Could not inspect free disk space at {probe}: {exc}.",
            details={"path": str(target), "probe_path": str(probe)},
        )
    free = _free_bytes(usage)
    details = {
        "path": str(target),
        "probe_path": str(probe),
        "free_bytes": free,
        "min_free_bytes": min_free_bytes,
    }
    if free >= min_free_bytes:
        return DoctorCheck(
            id="memory_db_disk_space",
            name="Memory DB disk space",
            status=CheckStatus.PASS,
            message=f"{_format_bytes(free)} free for memory.db.",
            details=details,
        )
    return DoctorCheck(
        id="memory_db_disk_space",
        name="Memory DB disk space",
        status=CheckStatus.FAIL,
        message=f"Only {_format_bytes(free)} free; keep at least 1 GB available.",
        details=details,
    )


def check_log_dir_writable(
    log_dir: Path | None = None,
    *,
    write_probe: Callable[[Path], None] | None = None,
) -> DoctorCheck:
    path = (log_dir or (Path.home() / ".engram" / "logs")).expanduser()
    details = {"path": str(path)}
    if not path.exists():
        return DoctorCheck(
            id="log_dir_writable",
            name="Log directory writable",
            status=CheckStatus.FAIL,
            message=f"{path} is missing; create it or run `engram setup`.",
            details=details,
        )
    if not path.is_dir():
        return DoctorCheck(
            id="log_dir_writable",
            name="Log directory writable",
            status=CheckStatus.FAIL,
            message=f"{path} exists but is not a directory.",
            details=details,
        )
    probe = write_probe or _write_probe
    try:
        probe(path)
    except Exception as exc:
        return DoctorCheck(
            id="log_dir_writable",
            name="Log directory writable",
            status=CheckStatus.FAIL,
            message=f"{path} is not writable: {type(exc).__name__}: {exc}",
            details=details | {"error_class": type(exc).__name__},
        )
    return DoctorCheck(
        id="log_dir_writable",
        name="Log directory writable",
        status=CheckStatus.PASS,
        message=f"{path} is writable.",
        details=details,
    )


def check_fd_pressure(
    log_dir: Path | None = None,
    *,
    usage_reader: Callable[[], dict[str, int | None] | None] | None = None,
    snapshot_reader: Callable[[Path], dict[str, Any] | None] | None = None,
) -> DoctorCheck:
    path = (log_dir or (Path.home() / ".engram" / "logs")).expanduser()
    usage = (usage_reader or fd_usage_snapshot)()
    snapshot = (snapshot_reader or read_latest_fd_snapshot)(path)
    top_patterns, other_count = _fd_snapshot_pattern_summary(snapshot)
    details: dict[str, Any] = {"log_dir": str(path), "top_path_patterns": top_patterns}
    if other_count is not None:
        details["other_count"] = other_count

    if usage is None or usage.get("in_use") is None:
        return DoctorCheck(
            id="fd_pressure",
            name="FD pressure",
            status=CheckStatus.WARN,
            message=_fd_pressure_message(
                "FD usage is unavailable on this system.",
                top_patterns,
                other_count,
            ),
            details=details,
        )

    in_use_raw = usage.get("in_use")
    if in_use_raw is None:
        return DoctorCheck(
            id="fd_pressure",
            name="FD pressure",
            status=CheckStatus.WARN,
            message=_fd_pressure_message(
                "FD usage is unavailable on this system.",
                top_patterns,
                other_count,
            ),
            details=details,
        )

    in_use = int(in_use_raw)
    soft_limit = usage.get("soft_limit")
    details |= usage
    if in_use >= 200:
        status = CheckStatus.FAIL
        summary = f"{in_use} FDs in use; pressure is critical."
    elif in_use >= 100:
        status = CheckStatus.WARN
        summary = f"{in_use} FDs in use; pressure is elevated."
    else:
        status = CheckStatus.PASS
        summary = f"{in_use} FDs in use; pressure is normal."

    if soft_limit is not None:
        summary = f"{summary} Soft limit {soft_limit}."
    return DoctorCheck(
        id="fd_pressure",
        name="FD pressure",
        status=status,
        message=_fd_pressure_message(summary, top_patterns, other_count),
        details=details,
    )


def _launchd_bridge_plist_drift_message(
    issues: list[Any],
    *,
    template_commit: str | None,
) -> str:
    soft_limit_issue = next(
        (issue for issue in issues if issue.path == "SoftResourceLimits.NumberOfFiles"),
        None,
    )
    if soft_limit_issue is not None:
        suffix = (
            f" (introduced in GRO-481; canonical template {template_commit})."
            if template_commit
            else " (introduced in GRO-481)."
        )
        return (
            "installed plist missing or outdated SoftResourceLimits.NumberOfFiles; "
            "restart bridge with corrected plist to apply"
            f"{suffix}"
        )

    summarized = ", ".join(issue.path for issue in issues[:3])
    if len(issues) > 3:
        summarized = f"{summarized}, +{len(issues) - 3} more"
    suffix = f" Canonical template {template_commit}." if template_commit else ""
    return (
        f"installed plist drift detected ({summarized}); "
        "restart bridge with corrected plist to apply."
        f"{suffix}"
    )


def _mcp_server_command(server_config: dict[str, Any] | None) -> str | None:
    if not isinstance(server_config, dict):
        return None
    command = server_config.get("command")
    if not isinstance(command, str):
        return None
    stripped = command.strip()
    return stripped or None


def _resolve_command_on_bridge_path(command: str, bridge_path: str) -> str | None:
    expanded = Path(command).expanduser()
    if os.sep in command:
        return str(expanded) if expanded.exists() and os.access(expanded, os.X_OK) else None
    resolved = shutil.which(command, path=bridge_path)
    return str(Path(resolved)) if resolved else None


def _check_binary_on_path(
    *,
    check_id: str,
    name: str,
    binary: str,
    install_hint: str,
    which: Callable[[str], str | None] | None,
    version_runner: Callable[[str], str | None] | None,
) -> DoctorCheck:
    resolver = which or shutil.which
    path = resolver(binary)
    if path is None:
        return DoctorCheck(
            id=check_id,
            name=name,
            status=CheckStatus.FAIL,
            message=f"{binary} was not found on PATH. {install_hint}",
            details={"binary": binary},
        )
    runner = version_runner or _run_version
    version = runner(path)
    if not version:
        return DoctorCheck(
            id=check_id,
            name=name,
            status=CheckStatus.WARN,
            message=f"{binary} found at {path}, but `--version` did not return cleanly.",
            details={"binary": binary, "path": path},
        )
    return DoctorCheck(
        id=check_id,
        name=name,
        status=CheckStatus.PASS,
        message=f"{binary} found at {path}: {version}",
        details={"binary": binary, "path": path, "version": version},
    )


def _run_version(path: str) -> str | None:
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout.strip() or completed.stderr.strip()) or None


def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = 3.0,
) -> HttpResult:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return HttpResult(
                status_code=response.status,
                payload=_parse_json_payload(text),
                text=text,
            )
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return HttpResult(
            status_code=exc.code,
            payload=_parse_json_payload(text),
            text=text,
        )


def _get_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float = 3.0,
) -> HttpResult:
    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return HttpResult(
                status_code=response.status,
                payload=_parse_json_payload(text),
                text=text,
            )
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return HttpResult(
            status_code=exc.code,
            payload=_parse_json_payload(text),
            text=text,
        )


def _launchctl_list() -> str:
    completed = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "launchctl list failed")
    return completed.stdout


def _find_launchd_row(output: str, label: str) -> tuple[str, str] | None:
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] == label:
            return parts[0], parts[1]
    return None


def _collect_slack_slash_command_evidence(
    log_dir: Path,
    *,
    cutoff: datetime.datetime,
    window_end: datetime.datetime,
) -> dict[str, Any]:
    observed_commands: set[str] = set()
    missing_signals: list[str] = []
    log_files = [str(path) for path in _recent_engram_log_paths(log_dir, cutoff=cutoff, window_end=window_end)]
    for path_str in log_files:
        path = Path(path_str)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            payload = _parse_json_payload(line)
            timestamp = _parse_doctor_log_timestamp(payload.get("timestamp"))
            if timestamp is None or timestamp < cutoff or timestamp > window_end:
                continue
            slash_command = _optional_str(payload.get("slash_command"))
            if slash_command in SLACK_SLASH_COMMANDS:
                observed_commands.add(slash_command)
            haystack = json.dumps(payload, sort_keys=True).lower()
            for pattern in SLACK_SLASH_COMMAND_MISSING_PATTERNS:
                if pattern in haystack and pattern not in missing_signals:
                    missing_signals.append(pattern)

    if missing_signals:
        verdict = "missing"
    elif set(SLACK_SLASH_COMMANDS).issubset(observed_commands):
        verdict = "present"
    else:
        verdict = "unknown"
    return {
        "verdict": verdict,
        "log_files": log_files,
        "observed_commands": sorted(observed_commands),
        "missing_signals": missing_signals,
    }


def _collect_recent_mcp_exclusion_evidence(
    log_dir: Path,
    *,
    cutoff: datetime.datetime,
    window_end: datetime.datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in _recent_engram_log_paths(log_dir, cutoff=cutoff, window_end=window_end):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            payload = _parse_json_payload(line)
            if payload.get("event") != "mcp.excluded_by_manifest":
                continue
            timestamp = _parse_doctor_log_timestamp(payload.get("timestamp"))
            if timestamp is None or timestamp < cutoff or timestamp > window_end:
                continue
            channel_id = _optional_str(payload.get("channel_id"))
            mcp_name = _optional_str(payload.get("mcp_name"))
            reason = _optional_str(payload.get("reason"))
            if channel_id is None or mcp_name is None or reason is None:
                continue
            key = (channel_id, mcp_name, reason)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "channel_id": channel_id,
                    "mcp_name": mcp_name,
                    "reason": reason,
                    "timestamp": timestamp.isoformat(),
                }
            )
    rows.sort(key=lambda item: (item["channel_id"], item["mcp_name"], item["reason"]))
    return rows


def _recent_engram_log_paths(
    log_dir: Path,
    *,
    cutoff: datetime.datetime,
    window_end: datetime.datetime,
) -> list[Path]:
    if not log_dir.exists() or not log_dir.is_dir():
        return []
    paths: list[Path] = []
    start_date = cutoff.date()
    end_date = window_end.date()
    for path in sorted(log_dir.glob("engram-*.jsonl")):
        try:
            file_date = datetime.date.fromisoformat(path.stem.removeprefix("engram-"))
        except ValueError:
            continue
        if start_date <= file_date <= end_date:
            paths.append(path)
    return paths


def _configured_slack_team_id(config_path: Path) -> str | None:
    for env_key in ("ENGRAM_SLACK_TEAM_ID", "SLACK_TEAM_ID"):
        if value := os.environ.get(env_key):
            return value
    if not config_path.exists():
        return None
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    slack = raw.get("slack") if isinstance(raw, dict) else None
    if not isinstance(slack, dict):
        return None
    return _optional_str(slack.get("team_id")) or _optional_str(slack.get("workspace_id"))


def _blocked_by_config(check_id: str, name: str) -> DoctorCheck:
    return DoctorCheck(
        id=check_id,
        name=name,
        status=CheckStatus.FAIL,
        message="Config did not load; fix config.yaml before validating this dependency.",
        details={"blocked_by": "config_load"},
    )


def _nearest_existing_parent(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _write_probe(path: Path) -> None:
    probe = path / f".engram-doctor-write-test-{os.getpid()}"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def _parse_json_payload(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_doctor_log_timestamp(value: object) -> datetime.datetime | None:
    text = _optional_str(value)
    if text is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _format_bytes(value: int) -> str:
    gb = value / 1_000_000_000
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = value / 1_000_000
    return f"{mb:.0f} MB"


def _free_bytes(usage: Any) -> int:
    free = getattr(usage, "free", None)
    if free is not None:
        return int(free)
    return int(usage[2])


def _fd_snapshot_pattern_summary(
    snapshot: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(snapshot, dict):
        return [], None
    raw_patterns = snapshot.get("by_path_pattern")
    if not isinstance(raw_patterns, dict):
        return [], None
    patterns: list[dict[str, Any]] = []
    other_count: int | None = None
    for name, count in raw_patterns.items():
        if not isinstance(name, str):
            continue
        try:
            value = int(count)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        if name == "other":
            other_count = value
            continue
        patterns.append({"pattern": name, "count": value})
    patterns.sort(key=lambda item: (-item["count"], item["pattern"]))
    return patterns[:3], other_count


def _fd_pressure_message(
    summary: str,
    top_patterns: list[dict[str, Any]],
    other_count: int | None,
) -> str:
    suffixes: list[str] = []
    if top_patterns:
        rendered = ", ".join(
            f"{item['pattern']}={item['count']}" for item in top_patterns
        )
        suffixes.append(f"Top snapshot patterns: {rendered}.")
    if other_count is not None:
        suffixes.append(f"Uncategorized (other)={other_count}.")
    if not suffixes:
        return summary
    return f"{summary} {' '.join(suffixes)}"
