from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

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
    check_launchd_job,
    check_log_dir_writable,
    check_python_version,
    check_slack_app_token,
    check_slack_bot_token,
    check_uv_on_path,
)


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
        return HttpResult(200, {"ok": True, "team_id": "T123", "team": "Example"})

    check = check_slack_bot_token(
        _config(tmp_path),
        expected_team_id="T123",
        requester=requester,
    )

    assert check.status == CheckStatus.PASS
    assert check.details["team_id"] == "T123"


def test_check_slack_app_token_requires_xapp_prefix(tmp_path: Path) -> None:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="bad-token"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )

    check = check_slack_app_token(cfg)

    assert check.status == CheckStatus.FAIL
    assert "xapp-" in check.message


def test_check_anthropic_api_key_validates_messages_api(tmp_path: Path) -> None:
    def requester(*_args, **kwargs) -> HttpResult:
        assert kwargs["payload"]["max_tokens"] == 1
        assert kwargs["payload"]["model"] == "claude-3-5-haiku-latest"
        return HttpResult(200, {"id": "msg_test"})

    check = check_anthropic_api_key(_config(tmp_path), requester=requester)

    assert check.status == CheckStatus.PASS
    assert check.details["status_code"] == 200


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
                "anthropic": {
                    "api_key": "sk-ant-test",
                    "model": "claude-test-model",
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

    def post_json(url: str, **_kwargs) -> HttpResult:
        if "slack.com" in url:
            return HttpResult(200, {"ok": True, "team_id": "T123"})
        return HttpResult(200, {"ok": True})

    monkeypatch.setattr("engram.doctor._post_json", post_json)

    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["summary"] == {
        "total": 14,
        "passed": 14,
        "warnings": 0,
        "failed": 0,
        "exit_code": 0,
    }
    assert [check["id"] for check in payload["checks"]] == [
        "uv_path",
        "claude_path",
        "python_version",
        "config_file",
        "config_load",
        "slack_bot_token",
        "slack_app_token",
        "anthropic_api_key",
        "gemini_api_key",
        "launchd_bridge",
        "launchd_nightly",
        "fd_pressure",
        "memory_db_disk_space",
        "log_dir_writable",
    ]
