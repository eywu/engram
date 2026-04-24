"""Destructive shell command detection for tier-aware confirmation flows."""
from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern
from typing import Any


@dataclass(frozen=True)
class FootgunMatch:
    pattern: Pattern[str]
    description: str
    command: str


_FLAGS = re.IGNORECASE | re.DOTALL
_SHARED_BRANCH_PREFIXES = {
    "bugfix",
    "chore",
    "dev",
    "develop",
    "development",
    "docs",
    "feature",
    "feat",
    "fix",
    "hotfix",
    "main",
    "master",
    "prod",
    "production",
    "release",
    "staging",
    "stable",
    "test",
    "tests",
    "trunk",
}

_RM_PATTERN = re.compile(
    r"\brm\s+.*(?:--recursive\b|-[^\s-]*[rR][^\s]*)",
    _FLAGS,
)
_SUDO_PATTERN = re.compile(r"\bsudo\b", _FLAGS)
_CURL_PIPE_PATTERN = re.compile(r"\bcurl\s+.*\|\s*(sh|bash|zsh)\b", _FLAGS)
_WGET_PIPE_PATTERN = re.compile(r"\bwget\s+.*\|\s*(sh|bash|zsh)\b", _FLAGS)
_DD_PATTERN = re.compile(r"\bdd\s+if=", _FLAGS)
_MKFS_PATTERN = re.compile(r"\bmkfs\b", _FLAGS)
_FDISK_PATTERN = re.compile(r"\bfdisk\b", _FLAGS)
_CHMOD_777_PATTERN = re.compile(r"\bchmod\s+-R\s+777\b", _FLAGS)
_BLOCK_DEVICE_PATTERN = re.compile(r">\s*/dev/sd[a-z](?:\d+)?\b", _FLAGS)
_GIT_FORCE_PUSH_PATTERN = re.compile(r"\bgit\s+push\s+--force\b", _FLAGS)
_SQL_PATTERN = re.compile(r"\b(drop|truncate)\s+(table|database)\b", _FLAGS)

FOOTGUN_PATTERNS: list[tuple[Pattern[str], str]] = [
    (_RM_PATTERN, "recursive rm command"),
    (_SUDO_PATTERN, "sudo command"),
    (_CURL_PIPE_PATTERN, "curl piped into a shell"),
    (_WGET_PIPE_PATTERN, "wget piped into a shell"),
    (_DD_PATTERN, "raw disk copy command"),
    (_MKFS_PATTERN, "filesystem formatting command"),
    (_FDISK_PATTERN, "disk partitioning command"),
    (_CHMOD_777_PATTERN, "recursive world-writable chmod"),
    (_BLOCK_DEVICE_PATTERN, "direct write to a block device"),
    (_GIT_FORCE_PUSH_PATTERN, "force push"),
    (_SQL_PATTERN, "destructive SQL command"),
]


def match_footgun(
    tool_name: str,
    tool_input: dict[str, Any],
) -> FootgunMatch | None:
    """Return the first destructive shell match for Bash/BashOutput input."""
    if tool_name not in {"Bash", "BashOutput"}:
        return None

    command = _extract_command(tool_input)
    if command is None:
        return None

    for pattern, description in FOOTGUN_PATTERNS:
        if not pattern.search(command):
            continue
        if pattern is _GIT_FORCE_PUSH_PATTERN and _is_personal_force_push(command):
            continue
        return FootgunMatch(
            pattern=pattern,
            description=description,
            command=command,
        )
    return None


def _extract_command(tool_input: dict[str, Any]) -> str | None:
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        command = tool_input.get("cmd")
    if not isinstance(command, str):
        return None
    stripped = command.strip()
    return stripped or None


def _is_personal_force_push(command: str) -> bool:
    branch = _extract_force_push_branch(command)
    if branch is None:
        return False

    normalized = branch.removeprefix("refs/heads/")
    if ":" in normalized:
        normalized = normalized.split(":", 1)[1]
    if normalized.startswith("origin/"):
        normalized = normalized.split("/", 1)[1]
    if "/" not in normalized:
        return False

    prefix, _rest = normalized.split("/", 1)
    return prefix.lower() not in _SHARED_BRANCH_PREFIXES


def _extract_force_push_branch(command: str) -> str | None:
    match = re.search(r"\bgit\s+push\s+--force\b(?P<rest>.*)", command, _FLAGS)
    if match is None:
        return None

    rest = match.group("rest")
    tokens = re.findall(r"[^\s]+", rest)
    positional = [token for token in tokens if not token.startswith("-")]
    if not positional:
        return None
    if len(positional) == 1:
        return positional[0]
    return positional[1]
