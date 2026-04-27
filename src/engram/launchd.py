"""Shared helpers for Engram's launchd bridge plist."""
from __future__ import annotations

import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BRIDGE_LABEL = "com.engram.bridge"
BRIDGE_TEMPLATE_RELATIVE_PATH = Path("launchd") / "com.engram.bridge.plist"
BRIDGE_INSTALL_SCRIPT_RELATIVE_PATH = Path("scripts") / "install_launchd.sh"
BRIDGE_INSTALLED_RELATIVE_PATH = Path("Library") / "LaunchAgents" / f"{BRIDGE_LABEL}.plist"
_REPO_ROOT_MARKERS = ("pyproject.toml",)
_PLACEHOLDER_UV_BIN = "/REPLACE/WITH/ABSOLUTE/PATH/TO/uv"
_PLACEHOLDER_REPO_ROOT = "/REPLACE/WITH/ABSOLUTE/PATH/TO/engram-repo"
_PLACEHOLDER_HOME = "/REPLACE/WITH/HOME"
_PLACEHOLDER_ENV_FILE = "/REPLACE/WITH/ABSOLUTE/PATH/TO/engram.env"
_PLACEHOLDER_OPTIONAL_NODE_PATH_PREFIX = "/REPLACE/WITH/OPTIONAL/NODE/PATH/PREFIX/"
_BRIDGE_PROGRAM_ARGUMENTS = (
    "run",
    "--project",
    "python",
    "-m",
    "engram.main",
)
_BRIDGE_LOG_PATHS = {
    "StandardOutPath": "/tmp/engram.bridge.out.log",
    "StandardErrorPath": "/tmp/engram.bridge.err.log",
}
_DEFAULT_BRIDGE_PATH = (
    f"{_PLACEHOLDER_HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)
_BRIDGE_ENV_KEYS = ("PATH", "LANG", "HOME", "ENGRAM_ENV_FILE")
_BRIDGE_KEEPALIVE = {"SuccessfulExit": False, "Crashed": True}
_BRIDGE_RESOURCE_LIMITS = {
    "SoftResourceLimits": 4096,
    "HardResourceLimits": 8192,
}


@dataclass(frozen=True)
class PlistIssue:
    category: str
    path: str
    expected: Any | None = None
    actual: Any | None = None

    @property
    def missing(self) -> bool:
        return self.actual is None


def find_repo_root(start: Path | None = None) -> Path | None:
    """Return the enclosing Engram repo root if the current cwd is inside one."""
    start_path = (start or Path.cwd()).resolve()
    for candidate in (start_path, *start_path.parents):
        if all((candidate / marker).exists() for marker in _REPO_ROOT_MARKERS) and (
            candidate / BRIDGE_TEMPLATE_RELATIVE_PATH
        ).exists():
            return candidate
    return None


def bridge_template_path(repo_root: Path) -> Path:
    return repo_root / BRIDGE_TEMPLATE_RELATIVE_PATH


def bridge_install_script_path(repo_root: Path) -> Path:
    return repo_root / BRIDGE_INSTALL_SCRIPT_RELATIVE_PATH


def installed_bridge_plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / BRIDGE_INSTALLED_RELATIVE_PATH


def load_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"plist root is not a dict: {path}")
    return payload


def write_plist(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)


def resolve_uv_bin() -> Path | None:
    path = shutil.which("uv")
    return Path(path).resolve() if path else None


def write_bridge_env_file(
    *,
    anthropic_key: str,
    gemini_key: str | None,
    home: Path | None = None,
) -> Path:
    """Write ~/.engram/.env while preserving unrelated operator-managed lines."""
    target = (home or Path.home()) / ".engram" / ".env"
    preserved: list[str] = []
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY=") or line.startswith("GEMINI_API_KEY="):
                continue
            preserved.append(line)

    lines = [
        *preserved,
        f"ANTHROPIC_API_KEY={anthropic_key}",
    ]
    if gemini_key:
        lines.append(f"GEMINI_API_KEY={gemini_key}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return target


def render_bridge_plist(
    *,
    repo_root: Path,
    uv_bin: Path,
    env_file: Path,
    home: Path | None = None,
) -> dict[str, Any]:
    template = load_plist(bridge_template_path(repo_root))
    replacements = {
        _PLACEHOLDER_UV_BIN: str(uv_bin),
        _PLACEHOLDER_REPO_ROOT: str(repo_root),
        _PLACEHOLDER_HOME: str((home or Path.home()).resolve()),
        _PLACEHOLDER_ENV_FILE: str(env_file),
        _PLACEHOLDER_OPTIONAL_NODE_PATH_PREFIX: "",
    }
    rendered = _replace_placeholders(template, replacements)
    if not isinstance(rendered, dict):
        raise ValueError("rendered bridge plist must be a dict")
    return rendered


def setup_bridge_plist_issues(
    installed: dict[str, Any],
    expected: dict[str, Any],
) -> list[PlistIssue]:
    """Strict diff for setup, where current rendered values should match exactly."""
    issues: list[PlistIssue] = []

    for top_level in (
        "Label",
        "WorkingDirectory",
        "RunAtLoad",
        "ThrottleInterval",
        "StandardOutPath",
        "StandardErrorPath",
        "ProcessType",
    ):
        _check_exact(issues, "deployment_template", top_level, installed.get(top_level), expected[top_level])

    _check_exact(
        issues,
        "deployment_template",
        "ProgramArguments",
        installed.get("ProgramArguments"),
        expected["ProgramArguments"],
    )
    _check_exact(
        issues,
        "deployment_template",
        "KeepAlive",
        installed.get("KeepAlive"),
        expected["KeepAlive"],
    )

    installed_env = installed.get("EnvironmentVariables")
    expected_env = expected["EnvironmentVariables"]
    if not isinstance(installed_env, dict):
        issues.append(
            PlistIssue(
                category="env_vars",
                path="EnvironmentVariables",
                expected=expected_env,
                actual=installed_env,
            )
        )
    else:
        for key, value in expected_env.items():
            if key == "PATH":
                issues.extend(
                    _check_bridge_path(
                        "env_vars",
                        f"EnvironmentVariables.{key}",
                        installed_env.get(key),
                        value,
                    )
                )
                continue
            _check_exact(
                issues,
                "env_vars",
                f"EnvironmentVariables.{key}",
                installed_env.get(key),
                value,
            )

    for top_level, expected_limit in _BRIDGE_RESOURCE_LIMITS.items():
        actual_limit = _resource_limit(installed.get(top_level))
        issues.extend(
            _check_value(
                "resource_limits",
                f"{top_level}.NumberOfFiles",
                actual_limit,
                expected_limit,
            )
        )
    return issues


def doctor_bridge_plist_issues(installed: dict[str, Any]) -> list[PlistIssue]:
    """Lenient drift check for doctor: tolerate path values, flag missing structure."""
    issues: list[PlistIssue] = []

    _check_exact(issues, "deployment_template", "Label", installed.get("Label"), BRIDGE_LABEL)
    _check_nonempty_str(
        issues,
        "deployment_template",
        "WorkingDirectory",
        installed.get("WorkingDirectory"),
    )
    _check_exact(
        issues,
        "deployment_template",
        "RunAtLoad",
        installed.get("RunAtLoad"),
        True,
    )
    _check_exact(
        issues,
        "deployment_template",
        "ThrottleInterval",
        installed.get("ThrottleInterval"),
        30,
    )
    _check_exact(
        issues,
        "deployment_template",
        "ProcessType",
        installed.get("ProcessType"),
        "Background",
    )
    for key, expected in _BRIDGE_LOG_PATHS.items():
        _check_exact(issues, "deployment_template", key, installed.get(key), expected)

    program_arguments = installed.get("ProgramArguments")
    if not isinstance(program_arguments, list) or len(program_arguments) != 7:
        issues.append(
            PlistIssue(
                category="deployment_template",
                path="ProgramArguments",
                expected=["<uv>", *list(_BRIDGE_PROGRAM_ARGUMENTS[:2]), "<repo>", *_BRIDGE_PROGRAM_ARGUMENTS[2:]],
                actual=program_arguments,
            )
        )
    else:
        for index, expected in ((1, "run"), (2, "--project"), (4, "python"), (5, "-m"), (6, "engram.main")):
            _check_exact(
                issues,
                "deployment_template",
                f"ProgramArguments[{index}]",
                program_arguments[index],
                expected,
            )
        for index in (0, 3):
            _check_nonempty_str(
                issues,
                "deployment_template",
                f"ProgramArguments[{index}]",
                program_arguments[index],
            )

    keep_alive = installed.get("KeepAlive")
    if not isinstance(keep_alive, dict):
        issues.append(
            PlistIssue(
                category="deployment_template",
                path="KeepAlive",
                expected=_BRIDGE_KEEPALIVE,
                actual=keep_alive,
            )
        )
    else:
        for key, expected in _BRIDGE_KEEPALIVE.items():
            _check_exact(
                issues,
                "deployment_template",
                f"KeepAlive.{key}",
                keep_alive.get(key),
                expected,
            )

    env_vars = installed.get("EnvironmentVariables")
    if not isinstance(env_vars, dict):
        issues.append(
            PlistIssue(
                category="env_vars",
                path="EnvironmentVariables",
                expected=list(_BRIDGE_ENV_KEYS),
                actual=env_vars,
            )
        )
    else:
        for key in _BRIDGE_ENV_KEYS:
            _check_nonempty_str(
                issues,
                "env_vars",
                f"EnvironmentVariables.{key}",
                env_vars.get(key),
            )

    for top_level, expected_limit in _BRIDGE_RESOURCE_LIMITS.items():
        actual_limit = _resource_limit(installed.get(top_level))
        issues.extend(
            _check_value(
                "resource_limits",
                f"{top_level}.NumberOfFiles",
                actual_limit,
                expected_limit,
            )
        )
    return issues


def bridge_template_commit(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "log", "-n", "1", "--format=%h", "--", str(BRIDGE_TEMPLATE_RELATIVE_PATH)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _replace_placeholders(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_placeholders(inner, replacements) for key, inner in value.items()}
    if isinstance(value, list):
        return [_replace_placeholders(inner, replacements) for inner in value]
    if isinstance(value, str):
        rendered = value
        for placeholder, replacement in replacements.items():
            rendered = rendered.replace(placeholder, replacement)
        return rendered
    return value


def _check_exact(
    issues: list[PlistIssue],
    category: str,
    path: str,
    actual: Any,
    expected: Any,
) -> None:
    if actual == expected:
        return
    issues.append(PlistIssue(category=category, path=path, expected=expected, actual=actual))


def _check_bridge_path(
    category: str,
    path: str,
    actual: Any,
    expected: Any,
) -> list[PlistIssue]:
    if (
        isinstance(actual, str)
        and isinstance(expected, str)
        and (actual == expected or actual.endswith(f":{expected}"))
    ):
        return []
    return [PlistIssue(category=category, path=path, expected=expected, actual=actual)]


def _check_nonempty_str(
    issues: list[PlistIssue],
    category: str,
    path: str,
    actual: Any,
) -> None:
    if isinstance(actual, str) and actual.strip():
        return
    issues.append(PlistIssue(category=category, path=path, expected="<non-empty string>", actual=actual))


def _check_value(
    category: str,
    path: str,
    actual: Any,
    expected: Any,
) -> list[PlistIssue]:
    if actual == expected:
        return []
    return [PlistIssue(category=category, path=path, expected=expected, actual=actual)]


def _resource_limit(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    number_of_files = value.get("NumberOfFiles")
    if isinstance(number_of_files, int):
        return number_of_files
    return None
