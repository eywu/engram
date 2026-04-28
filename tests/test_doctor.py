from __future__ import annotations

import datetime
import json
import plistlib
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from engram.bootstrap import provision_channel
from engram.cli import app
from engram.config import AnthropicConfig, EmbeddingsConfig, EngramConfig, PathsConfig, SlackConfig
from engram.doctor import (
    CheckStatus,
    DoctorCheck,
    DoctorReport,
    HttpResult,
    check_anthropic_api_key,
    check_claude_on_path,
    check_config_file,
    check_config_loads,
    check_disk_space,
    check_gemini_api_key,
    check_launchd_bridge_plist_drift,
    check_launchd_job,
    check_launchd_nightly_env_file,
    check_log_dir_writable,
    check_mcp_channel_coverage,
    check_mcp_commands_on_bridge_path,
    check_owner_dm_channel_id,
    check_owner_user_id,
    check_python_version,
    check_slack_app_token,
    check_slack_bot_token,
    check_slack_slash_commands,
    check_uv_on_path,
)
from engram.launchd import render_bridge_plist, write_bridge_env_file
from engram.manifest import IdentityTemplate


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ENGRAM_SLACK_BOT_TOKEN",
        "ENGRAM_SLACK_APP_TOKEN",
        "ENGRAM_SLACK_SIGNING_SECRET",
        "ENGRAM_SLACK_TEAM_ID",
        "ENGRAM_ANTHROPIC_API_KEY",
        "ENGRAM_MODEL",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
        "SLACK_TEAM_ID",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "ENGRAM_OWNER_DM_CHANNEL_ID",
        "ENGRAM_OWNER_USER_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def _config(tmp_path: Path, *, gemini_api_key: str | None = None) -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test", model="claude-test-model"),
        paths=PathsConfig(
            state_dir=tmp_path / "state",
            contexts_dir=tmp_path / "contexts",
            log_dir=tmp_path / "logs",
        ),
        embeddings=EmbeddingsConfig(api_key=gemini_api_key),
    )


def _write_bridge_log(
    log_dir: Path,
    *,
    log_date: datetime.date,
    rows: list[dict[str, object]],
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"engram-{log_date.isoformat()}.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_check_uv_on_path_reports_version() -> None:
    check = check_uv_on_path(
        which=lambda name: "/usr/local/bin/uv" if name == "uv" else None,
        version_runner=lambda path: "uv 0.7.0",
    )

    assert check.status == CheckStatus.PASS
    assert check.details["path"] == "/usr/local/bin/uv"
    assert check.details["version"] == "uv 0.7.0"


def test_check_claude_on_path_missing_fails() -> None:
    check = check_claude_on_path(which=lambda _name: None)

    assert check.status == CheckStatus.FAIL
    assert "not found" in check.message


def test_check_mcp_commands_on_bridge_path_fails_when_npx_is_missing_from_bridge_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "camoufox": {
                        "command": "npx",
                        "args": ["-y", "mcp-camoufox@latest"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    installed_path = tmp_path / "Library" / "LaunchAgents" / "com.engram.bridge.plist"
    installed_path.parent.mkdir(parents=True)
    with installed_path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.engram.bridge",
                "EnvironmentVariables": {
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                },
            },
            handle,
            sort_keys=False,
        )

    check = check_mcp_commands_on_bridge_path(home=tmp_path)

    assert check.status == CheckStatus.FAIL
    assert "camoufox -> npx" in check.message
    assert "install_launchd.sh" in check.message
    assert "brew install node" in check.message
    assert check.details["unreachable"] == [{"server": "camoufox", "command": "npx"}]


def test_check_python_version_requires_312() -> None:
    passing = check_python_version((3, 12, 0))
    failing = check_python_version((3, 11, 9))

    assert passing.status == CheckStatus.PASS
    assert failing.status == CheckStatus.FAIL


def test_check_config_file_requires_mode_600(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("slack: {}\n", encoding="utf-8")
    config_path.chmod(0o600)

    check = check_config_file(config_path)

    assert check.status == CheckStatus.PASS
    assert check.details["mode"] == "0o600"


def test_check_config_loads_uses_engram_loader(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    check, loaded = check_config_loads(tmp_path / "config.yaml", loader=lambda _path: cfg)

    assert check.status == CheckStatus.PASS
    assert loaded is cfg


def test_check_slack_bot_token_validates_team_id(tmp_path: Path) -> None:
    def requester(*_args, **_kwargs) -> HttpResult:
        return HttpResult(
            200,
            {
                "ok": True,
                "team_id": "T123",
                "team": "Example",
                "url": "https://example.slack.com/",
            },
        )

    check = check_slack_bot_token(
        _config(tmp_path),
        expected_team_id="T123",
        requester=requester,
    )

    assert check.status == CheckStatus.PASS
    assert check.details["team_id"] == "T123"
    assert check.details["team_name"] == "Example"
    assert check.details["url"] == "https://example.slack.com/"
    assert (
        check.message
        == "Slack token is valid for workspace Example (T123) at https://example.slack.com/."
    )


def test_check_slack_bot_token_passes_without_expected_team_id(tmp_path: Path) -> None:
    def requester(*_args, **_kwargs) -> HttpResult:
        return HttpResult(
            200,
            {
                "ok": True,
                "team_id": "T123",
                "team": "Growth Gauge",
                "url": "https://growthgauge.slack.com/",
            },
        )

    check = check_slack_bot_token(_config(tmp_path), requester=requester)

    assert check.status == CheckStatus.PASS
    assert check.details["expected_team_id"] is None
    assert check.details["team_id"] == "T123"
    assert "Growth Gauge" in check.message


def test_check_slack_bot_token_fails_on_expected_team_mismatch(tmp_path: Path) -> None:
    def requester(*_args, **_kwargs) -> HttpResult:
        return HttpResult(
            200,
            {
                "ok": True,
                "team_id": "T999",
                "team": "Other Workspace",
                "url": "https://other-workspace.slack.com/",
            },
        )

    check = check_slack_bot_token(
        _config(tmp_path),
        expected_team_id="T123",
        requester=requester,
    )

    assert check.status == CheckStatus.FAIL
    assert check.details["team_id"] == "T999"
    assert check.details["expected_team_id"] == "T123"
    assert "Other Workspace" in check.message
    assert "auth.test returned team_id T999" in check.message
    assert "config expects T123" in check.message


def test_owner_approval_checks_warn_when_missing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    owner_dm = check_owner_dm_channel_id(cfg)
    owner_user = check_owner_user_id(cfg)

    assert owner_dm.status == CheckStatus.WARN
    assert owner_user.status == CheckStatus.WARN


def test_check_mcp_channel_coverage_warns_on_uncovered_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "camoufox": {"command": "camoufox-mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    home = tmp_path / ".engram"
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=home,
    )
    _write_bridge_log(
        home / "logs",
        log_date=datetime.date(2026, 4, 25),
        rows=[
            {
                "timestamp": "2026-04-25T12:00:00Z",
                "event": "mcp.excluded_by_manifest",
                "channel_id": "C07TEAM",
                "mcp_name": "camoufox",
                "reason": "not_in_allowed",
            }
        ],
    )

    check = check_mcp_channel_coverage(
        contexts_path=home / "contexts",
        log_dir=home / "logs",
        now=lambda: datetime.datetime(2026, 4, 25, 12, 30, tzinfo=datetime.UTC),
    )

    assert check.status == CheckStatus.WARN
    assert "camoufox" in check.message
    assert "mcp_servers.allowed" in check.message
    assert "mcp.excluded_by_manifest" in check.message
    assert check.details["uncovered_servers"] == ["camoufox"]
    assert check.details["recent_exclusions"] == [
        {
            "channel_id": "C07TEAM",
            "mcp_name": "camoufox",
            "reason": "not_in_allowed",
            "timestamp": "2026-04-25T12:00:00+00:00",
        }
    ]


def test_check_mcp_channel_coverage_warns_on_invalid_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRO-532 regression: invalid team manifests must produce WARN, not
    silent PASS. Previously `coverage.invalid_manifest_paths` was
    collected into details but never branched on, so a corrupted manifest
    fell through to the global PASS branches below.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"camoufox": {"command": "camoufox-mcp"}}}),
        encoding="utf-8",
    )
    home = tmp_path / ".engram"
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=home,
    )
    # Corrupt the team manifest on disk.
    from engram.paths import channel_manifest_path

    bad_path = channel_manifest_path("C07TEAM", home)
    bad_path.write_text("this is: not: valid: yaml: : :\n\t- broken\n", encoding="utf-8")

    check = check_mcp_channel_coverage(
        contexts_path=home / "contexts",
        log_dir=home / "logs",
        now=lambda: datetime.datetime(2026, 4, 25, 12, 30, tzinfo=datetime.UTC),
    )

    assert check.status == CheckStatus.WARN
    assert "Could not parse" in check.message
    assert "C07TEAM" in check.message or str(bad_path) in check.message


def test_check_mcp_channel_coverage_warns_on_recent_exclusions_when_globally_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRO-532 regression: if global coverage looks fine but per-channel
    exclusions were recently logged, escalate to WARN. Without this branch,
    a per-channel exclusion would be hidden behind global PASS.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"camoufox": {"command": "camoufox-mcp"}}}),
        encoding="utf-8",
    )
    home = tmp_path / ".engram"
    # Two team channels: A allows camoufox (so global coverage passes),
    # B silently filtered it (recent exclusion logged).
    for cid in ("C07A", "C07B"):
        provision_channel(
            cid,
            identity=IdentityTemplate.TASK_ASSISTANT,
            label="#x",
            home=home,
        )
    # Hand-write to bypass the trust gate (the gate exists for a reason
    # in production; tests need adversarial setup states).
    import yaml

    from engram.paths import channel_manifest_path

    pa = channel_manifest_path("C07A", home)
    payload_a = yaml.safe_load(pa.read_text())
    payload_a["mcp_servers"] = {
        "allowed": ["engram-memory", "camoufox"],
        "disallowed": [],
    }
    pa.write_text(yaml.safe_dump(payload_a, sort_keys=False), encoding="utf-8")
    _write_bridge_log(
        home / "logs",
        log_date=datetime.date(2026, 4, 25),
        rows=[
            {
                "timestamp": "2026-04-25T12:00:00Z",
                "event": "mcp.excluded_by_manifest",
                "channel_id": "C07B",
                "mcp_name": "camoufox",
                "reason": "not_in_allowed",
            }
        ],
    )

    check = check_mcp_channel_coverage(
        contexts_path=home / "contexts",
        log_dir=home / "logs",
        now=lambda: datetime.datetime(2026, 4, 25, 12, 30, tzinfo=datetime.UTC),
    )

    # Global coverage shows camoufox covered (by C07A), but recent
    # exclusions show C07B filtering it. The check must escalate to WARN.
    assert check.status == CheckStatus.WARN
    assert "recent" in check.message.lower() or "excluded" in check.message.lower()
    assert check.details["uncovered_servers"] == []
    assert any(
        ev["channel_id"] == "C07B" for ev in check.details["recent_exclusions"]
    )


def test_check_slack_app_token_requires_xapp_prefix(tmp_path: Path) -> None:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="bad-token"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )

    check = check_slack_app_token(cfg)

    assert check.status == CheckStatus.FAIL
    assert "xapp-" in check.message


def test_check_slack_slash_commands_passes_when_recent_logs_cover_all_commands(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    now = datetime.datetime(2026, 4, 24, 12, 0, tzinfo=datetime.UTC)
    _write_bridge_log(
        cfg.paths.log_dir,
        log_date=now.date(),
        rows=[
            {
                "timestamp": "2026-04-24T11:00:00Z",
                "event": "ingress.slash_command_received",
                "slash_command": "/engram",
            },
            {
                "timestamp": "2026-04-24T11:05:00Z",
                "event": "ingress.slash_command_received",
                "slash_command": "/exclude-from-nightly",
            },
            {
                "timestamp": "2026-04-24T11:10:00Z",
                "event": "ingress.slash_command_received",
                "slash_command": "/include-in-nightly",
            },
        ],
    )

    check = check_slack_slash_commands(cfg, now=lambda: now)

    assert check.status == CheckStatus.PASS
    assert check.details["verdict"] == "present"
    assert check.details["observed_commands"] == [
        "/engram",
        "/exclude-from-nightly",
        "/include-in-nightly",
    ]


def test_check_slack_slash_commands_warns_when_recent_logs_show_missing_signal(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    now = datetime.datetime(2026, 4, 24, 12, 0, tzinfo=datetime.UTC)
    _write_bridge_log(
        cfg.paths.log_dir,
        log_date=now.date(),
        rows=[
            {
                "timestamp": "2026-04-24T11:15:00Z",
                "event": "slack.ui_error",
                "message": "\"/engram\" is not a valid command.",
            },
        ],
    )

    check = check_slack_slash_commands(cfg, now=lambda: now)

    assert check.status == CheckStatus.WARN
    assert check.details["verdict"] == "missing"
    assert "Upgrading an existing install" in check.message


def test_check_slack_slash_commands_warns_when_recent_logs_are_inconclusive(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    now = datetime.datetime(2026, 4, 24, 12, 0, tzinfo=datetime.UTC)
    _write_bridge_log(
        cfg.paths.log_dir,
        log_date=now.date(),
        rows=[
            {
                "timestamp": "2026-04-24T11:00:00Z",
                "event": "ingress.slash_command_received",
                "slash_command": "/engram",
            },
        ],
    )

    check = check_slack_slash_commands(cfg, now=lambda: now)

    assert check.status == CheckStatus.WARN
    assert check.details["verdict"] == "unknown"
    assert "should autocomplete" in check.message


def test_check_anthropic_api_key_validates_configured_model_access(tmp_path: Path) -> None:
    def requester(url: str, **kwargs) -> HttpResult:
        assert url == "https://api.anthropic.com/v1/models"
        assert kwargs["headers"]["x-api-key"] == "sk-ant-test"
        assert "payload" not in kwargs
        return HttpResult(
            200,
            {
                "data": [
                    {"id": "claude-test-model"},
                    {"id": "claude-sonnet-4-6"},
                ]
            },
        )

    check = check_anthropic_api_key(_config(tmp_path), requester=requester)

    assert check.status == CheckStatus.PASS
    assert check.details["status_code"] == 200
    assert check.details["available_models"] == [
        "claude-sonnet-4-6",
        "claude-test-model",
    ]


def test_check_anthropic_api_key_fails_when_configured_model_not_accessible(
    tmp_path: Path,
) -> None:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test", model="claude-missing"),
        paths=PathsConfig(
            state_dir=tmp_path / "state",
            contexts_dir=tmp_path / "contexts",
            log_dir=tmp_path / "logs",
        ),
    )

    def requester(*_args, **_kwargs) -> HttpResult:
        return HttpResult(
            200,
            {
                "data": [
                    {"id": "claude-opus-4-6"},
                    {"id": "claude-sonnet-4-6"},
                ]
            },
        )

    check = check_anthropic_api_key(cfg, requester=requester)

    assert check.status == CheckStatus.FAIL
    assert "claude-missing" in check.message
    assert "claude-opus-4-6, claude-sonnet-4-6" in check.message
    assert check.details["available_models"] == [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    ]


def test_check_gemini_api_key_absent_is_keyword_only_memory(tmp_path: Path) -> None:
    check = check_gemini_api_key(_config(tmp_path))

    assert check.status == CheckStatus.PASS
    assert "keyword-only memory" in check.message
    assert check.details["configured"] is False


def test_check_launchd_bridge_job_running() -> None:
    check = check_launchd_job(
        "launchd_bridge",
        "launchd bridge job",
        "com.engram.bridge",
        launchctl_list=lambda: "PID\tStatus\tLabel\n123\t0\tcom.engram.bridge\n",
    )

    assert check.status == CheckStatus.PASS
    assert check.details["pid"] == "123"


def test_check_launchd_nightly_job_not_installed_fails() -> None:
    check = check_launchd_job(
        "launchd_nightly",
        "launchd nightly job",
        "com.engram.v3.nightly",
        launchctl_list=lambda: "PID\tStatus\tLabel\n123\t0\tcom.engram.bridge\n",
    )

    assert check.status == CheckStatus.FAIL
    assert check.details["state"] == "not_installed"


def test_check_launchd_nightly_env_file_warns_when_missing(tmp_path: Path) -> None:
    installed_path = tmp_path / "Library" / "LaunchAgents" / "com.engram.v3.nightly.plist"
    installed_path.parent.mkdir(parents=True)
    with installed_path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.engram.v3.nightly",
                "EnvironmentVariables": {
                    "HOME": str(tmp_path),
                    "LANG": "en_US.UTF-8",
                },
            },
            handle,
            sort_keys=False,
        )

    check = check_launchd_nightly_env_file(home=tmp_path)

    assert check.status == CheckStatus.WARN
    assert "EnvironmentVariables.ENGRAM_ENV_FILE" in check.message


def test_check_launchd_bridge_plist_drift_warns_on_missing_soft_resource_limits(
    tmp_path: Path,
) -> None:
    installed_path = tmp_path / "Library" / "LaunchAgents" / "com.engram.bridge.plist"
    installed_path.parent.mkdir(parents=True)
    with installed_path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.engram.bridge",
                "ProgramArguments": ["/tmp/engram", "run"],
                "WorkingDirectory": "/tmp/repo",
                "EnvironmentVariables": {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "LANG": "en_US.UTF-8",
                },
                "RunAtLoad": True,
                "StandardOutPath": "/tmp/engram.bridge.out.log",
                "StandardErrorPath": "/tmp/engram.bridge.err.log",
                "ProcessType": "Background",
            },
            handle,
            sort_keys=False,
        )

    check = check_launchd_bridge_plist_drift(
        repo_root=Path.cwd(),
        home=tmp_path,
        commit_resolver=lambda _repo_root: "abc1234",
    )

    assert check.status == CheckStatus.WARN
    assert "SoftResourceLimits.NumberOfFiles" in check.message
    assert check.details["template_commit"] == "abc1234"
    assert "SoftResourceLimits.NumberOfFiles" in check.details["issues"]


def test_check_disk_space_requires_one_gb(tmp_path: Path) -> None:
    check = check_disk_space(tmp_path, disk_usage=lambda _path: (2_000_000_000, 0, 1_500_000_000))

    assert check.status == CheckStatus.PASS
    assert check.details["free_bytes"] == 1_500_000_000


def test_check_log_dir_writable_probes_directory(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    check = check_log_dir_writable(log_dir)

    assert check.status == CheckStatus.PASS


def test_doctor_json_schema_is_stable() -> None:
    report = DoctorReport(
        checks=[
            DoctorCheck("one", "One", CheckStatus.PASS, "ok", {"value": 1}),
            DoctorCheck("two", "Two", CheckStatus.WARN, "warn", {}),
        ]
    )

    assert report.to_json() == {
        "schema_version": 1,
        "summary": {
            "total": 2,
            "passed": 1,
            "warnings": 1,
            "failed": 0,
            "exit_code": 0,
        },
        "checks": [
            {
                "id": "one",
                "name": "One",
                "status": "pass",
                "emoji": "✅",
                "message": "ok",
                "details": {"value": 1},
            },
            {
                "id": "two",
                "name": "Two",
                "status": "warn",
                "emoji": "⚠️",
                "message": "warn",
                "details": {},
            },
        ],
    }


def test_doctor_cli_json_against_tmp_config(
    tmp_path: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    home = tmp_path / ".engram"
    logs = home / "logs"
    logs.mkdir(parents=True)
    config_path = home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "slack": {
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "team_id": "T123",
                },
                "owner_dm_channel_id": "D07OWNER",
                "owner_user_id": "U07OWNER",
                "anthropic": {
                    "api_key": "sk-ant-test",
                    "model": "claude-test-model",
                },
                "paths": {
                    "state_dir": str(home / "state"),
                    "contexts_dir": str(home / "contexts"),
                    "log_dir": str(logs),
                },
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    monkeypatch.setattr(
        "engram.doctor.shutil.which",
        lambda name: f"/tmp/{name}" if name in {"uv", "claude"} else None,
    )
    monkeypatch.setattr("engram.doctor._run_version", lambda path: f"{Path(path).name} 1.0.0")
    monkeypatch.setattr(
        "engram.doctor._launchctl_list",
        lambda: (
            "PID\tStatus\tLabel\n"
            "123\t0\tcom.engram.bridge\n"
            "456\t0\tcom.engram.v3.nightly\n"
        ),
    )
    monkeypatch.setattr(
        "engram.doctor.shutil.disk_usage",
        lambda _path: (2_000_000_000, 0, 1_500_000_000),
    )
    monkeypatch.setattr(
        "engram.doctor.fd_usage_snapshot",
        lambda: {"in_use": 42, "soft_limit": 256, "hard_limit": 1024},
    )
    monkeypatch.setattr("engram.doctor.read_latest_fd_snapshot", lambda _path: None)
    _write_bridge_log(
        logs,
        log_date=datetime.datetime.now(datetime.UTC).date(),
        rows=[
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "event": "ingress.slash_command_received",
                "slash_command": "/engram",
            },
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "event": "ingress.slash_command_received",
                "slash_command": "/exclude-from-nightly",
            },
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "event": "ingress.slash_command_received",
                "slash_command": "/include-in-nightly",
            },
        ],
    )

    def post_json(url: str, **_kwargs) -> HttpResult:
        if "slack.com" in url:
            return HttpResult(200, {"ok": True, "team_id": "T123"})
        return HttpResult(200, {"ok": True})

    def get_json(url: str, **_kwargs) -> HttpResult:
        if url == "https://api.anthropic.com/v1/models":
            return HttpResult(200, {"data": [{"id": "claude-test-model"}]})
        raise AssertionError(f"unexpected GET url: {url}")

    monkeypatch.setattr("engram.doctor._post_json", post_json)
    monkeypatch.setattr("engram.doctor._get_json", get_json)

    env_file = write_bridge_env_file(
        anthropic_key="sk-ant-test",
        gemini_key="gemini-test",
        home=tmp_path,
    )
    installed_plist = tmp_path / "Library" / "LaunchAgents" / "com.engram.bridge.plist"
    installed_plist.parent.mkdir(parents=True, exist_ok=True)
    payload = render_bridge_plist(
        repo_root=Path.cwd(),
        uv_bin=Path("/tmp/uv"),
        env_file=env_file,
        home=tmp_path,
    )
    with installed_plist.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    nightly_plist = tmp_path / "Library" / "LaunchAgents" / "com.engram.v3.nightly.plist"
    with nightly_plist.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.engram.v3.nightly",
                "EnvironmentVariables": {
                    "HOME": str(tmp_path),
                    "LANG": "en_US.UTF-8",
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "ENGRAM_ENV_FILE": str(env_file),
                    "ENGRAM_REPO_ROOT": str(Path.cwd()),
                    "ENGRAM_UV_BIN": "/tmp/uv",
                },
            },
            handle,
            sort_keys=False,
        )

    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["summary"] == {
        "total": 21,
        "passed": 21,
        "warnings": 0,
        "failed": 0,
        "exit_code": 0,
    }
    assert [check["id"] for check in payload["checks"]] == [
        "uv_path",
        "claude_path",
        "mcp_bridge_path",
        "python_version",
        "config_file",
        "config_load",
        "mcp_channel_coverage",
        "owner_dm_channel_id",
        "owner_user_id",
        "slack_bot_token",
        "slack_app_token",
        "slack_slash_commands",
        "anthropic_api_key",
        "gemini_api_key",
        "launchd_bridge",
        "launchd_bridge_plist",
        "launchd_nightly",
        "launchd_nightly_env_file",
        "fd_pressure",
        "memory_db_disk_space",
        "log_dir_writable",
    ]
