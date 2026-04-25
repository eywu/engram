from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from engram.budget import Budget, BudgetConfig
from engram.smoketest import (
    SMOKE_CHANNEL_ID,
    SMOKE_PROMPT,
    AnthropicRuntime,
    CliResolution,
    run_smoke,
)


class _FakeClient:
    options_seen: ClaudeAgentOptions | None = None
    prompt_seen: str | None = None
    session_seen: str | None = None
    disconnected = False

    def __init__(self, options: ClaudeAgentOptions):
        type(self).options_seen = options

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str, *, session_id: str | None = None) -> None:
        type(self).prompt_seen = prompt
        type(self).session_seen = session_id

    async def receive_response(self):
        yield AssistantMessage(
            content=[TextBlock("smoke-test-ok")],
            model="claude-test-model",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=type(self).session_seen or "session-test",
            total_cost_usd=0.012345,
            usage={
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 0,
            },
            model_usage={"claude-test-model": {"input_tokens": 10}},
        )

    async def disconnect(self) -> None:
        type(self).disconnected = True


@pytest.mark.asyncio
async def test_run_smoke_records_budget_and_structured_success_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    smoke_cwd = tmp_path / ".engram" / "contexts" / "owner-dm"
    (smoke_cwd / ".claude").mkdir(parents=True)
    log_path = tmp_path / ".engram" / "logs" / "smoketest-test.jsonl"
    budget = Budget(BudgetConfig(), db_path=tmp_path / ".engram" / "cost.db")

    _FakeClient.options_seen = None
    _FakeClient.prompt_seen = None
    _FakeClient.session_seen = None
    _FakeClient.disconnected = False

    code = await run_smoke(
        cwd=smoke_cwd,
        log_path=log_path,
        client_factory=_FakeClient,
        budget=budget,
        cli_resolver=lambda _path: CliResolution(
            resolved=True,
            cli_path="/tmp/claude",
            source="path",
            path_cli="/tmp/claude",
        ),
        anthropic_loader=lambda: AnthropicRuntime(
            api_key="sk-test",
            model="claude-test-model",
        ),
    )

    assert code == 0
    assert _FakeClient.prompt_seen == SMOKE_PROMPT
    assert _FakeClient.disconnected is True
    assert _FakeClient.options_seen is not None
    assert _FakeClient.options_seen.setting_sources == ["project"]
    assert _FakeClient.options_seen.cwd == smoke_cwd
    assert _FakeClient.options_seen.can_use_tool is None
    assert _FakeClient.options_seen.hooks == {}
    assert _FakeClient.options_seen.env == {"ANTHROPIC_API_KEY": "sk-test"}

    with sqlite3.connect(budget.db_path) as conn:
        row = conn.execute(
            "SELECT channel_id, cost_usd, cache_creation_input_tokens "
            "FROM turns"
        ).fetchone()
    assert row == (SMOKE_CHANNEL_ID, "0.012345", 7)

    events = _read_jsonl(log_path)
    success = _event(events, "smoketest.success")
    assert success["hitl_disabled"] is True
    assert success["cli_resolved"] is True
    assert success["project_found"] is True
    assert success["budget_recorded"] is True
    assert success["budget_channel_id"] == SMOKE_CHANNEL_ID
    assert success["prompt_cache_status"] == "created"
    assert success["write_edit_hitl_guard_fired"] is False
    assert success["hitl_guard_invocations"] == 0


@pytest.mark.asyncio
async def test_run_smoke_fails_before_sdk_when_project_context_missing(tmp_path):
    log_path = tmp_path / "smoketest.jsonl"

    code = await run_smoke(
        cwd=tmp_path / ".engram" / "contexts" / "owner-dm",
        log_path=log_path,
        client_factory=_FakeClient,
        budget=Budget(BudgetConfig(), db_path=tmp_path / "cost.db"),
        cli_resolver=lambda _path: CliResolution(
            resolved=True,
            cli_path="/tmp/claude",
            source="path",
            path_cli="/tmp/claude",
        ),
        anthropic_loader=lambda: AnthropicRuntime(api_key=None, model=None),
    )

    assert code == 1
    failure = _event(_read_jsonl(log_path), "smoketest.failure")
    assert failure["reason"] == "project_not_found"
    assert failure["project_found"] is False


def test_launchd_smoketest_plist_is_manual_one_shot_and_copies_bridge_env():
    bridge = _plist(Path("launchd/com.engram.bridge.plist"))
    smoke = _plist(Path("launchd/com.engram.v3.smoketest.plist"))

    assert smoke["Label"] == "com.engram.v3.smoketest"
    assert bridge["SoftResourceLimits"]["NumberOfFiles"] == 4096
    assert bridge["HardResourceLimits"]["NumberOfFiles"] == 8192
    assert smoke["RunAtLoad"] is False
    assert "StartInterval" not in smoke
    assert "StartCalendarInterval" not in smoke
    assert "KeepAlive" not in smoke
    assert smoke["ProgramArguments"][-2:] == ["-m", "engram.smoketest"]

    bridge_env = bridge["EnvironmentVariables"]
    smoke_env = smoke["EnvironmentVariables"]
    assert smoke_env["PATH"] == bridge_env["PATH"]
    assert smoke_env["LANG"] == bridge_env["LANG"]
    assert smoke_env["HOME"] == "/REPLACE/WITH/HOME"


def test_launchd_nightly_plist_is_daily_2am_and_copies_smoketest_env():
    smoke = _plist(Path("launchd/com.engram.v3.smoketest.plist"))
    nightly = _plist(Path("launchd/com.engram.v3.nightly.plist"))

    assert nightly["Label"] == "com.engram.v3.nightly"
    assert nightly["RunAtLoad"] is False
    assert "KeepAlive" not in nightly
    assert nightly["StartCalendarInterval"] == {"Hour": 2, "Minute": 0}
    assert nightly["ProgramArguments"][0].endswith("engram_nightly_launchd.sh")

    smoke_env = smoke["EnvironmentVariables"]
    nightly_env = nightly["EnvironmentVariables"]
    assert nightly_env["PATH"] == smoke_env["PATH"]
    assert nightly_env["LANG"] == smoke_env["LANG"]
    assert nightly_env["HOME"] == "/REPLACE/WITH/HOME"
    assert nightly_env["ENGRAM_REPO_ROOT"] == "/REPLACE/WITH/ABSOLUTE/PATH/TO/engram-repo"
    assert nightly_env["ENGRAM_UV_BIN"] == "/REPLACE/WITH/ABSOLUTE/PATH/TO/uv"
    assert "nightly-stdio-" in nightly["StandardOutPath"]
    assert nightly["StandardOutPath"] == nightly["StandardErrorPath"]


def test_nightly_launchd_wrapper_adds_weekly_on_monday(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$UV_ARGS\"\n")
    date = bin_dir / "date"
    date.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"+%F\" ]; then echo 2026-04-20; exit 0; fi\n"
        "if [ \"$1\" = \"+%u\" ]; then echo 1; exit 0; fi\n"
        "exec /bin/date \"$@\"\n"
    )
    uv.chmod(0o755)
    date.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "ENGRAM_REPO_ROOT": str(Path.cwd()),
        "ENGRAM_UV_BIN": str(uv),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UV_ARGS": str(args_path),
    }

    subprocess.run(["scripts/engram_nightly_launchd.sh"], env=env, check=True)

    assert (tmp_path / ".engram" / "logs" / "nightly-stdio-2026-04-20.log").exists()
    assert args_path.read_text(encoding="utf-8").splitlines() == [
        "run",
        "--project",
        str(Path.cwd()),
        "engram",
        "nightly",
        "--verbose",
        "--weekly",
    ]


def test_install_launchd_install_nightly_is_idempotent(tmp_path: Path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    calls = tmp_path / "launchctl-calls.txt"
    state = tmp_path / "launchctl-state.txt"

    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "cmd=\"$1\"\n"
        "shift || true\n"
        "case \"$cmd\" in\n"
        "  list)\n"
        "    [ -f \"$LAUNCHCTL_STATE\" ] && cat \"$LAUNCHCTL_STATE\"\n"
        "    ;;\n"
        "  bootstrap|load)\n"
        "    printf '%s\\n' '- 0 com.engram.v3.nightly' > \"$LAUNCHCTL_STATE\"\n"
        "    printf '%s %s\\n' \"$cmd\" \"$*\" >> \"$LAUNCHCTL_CALLS\"\n"
        "    ;;\n"
        "  bootout|unload)\n"
        "    rm -f \"$LAUNCHCTL_STATE\"\n"
        "    printf '%s %s\\n' \"$cmd\" \"$*\" >> \"$LAUNCHCTL_CALLS\"\n"
        "    ;;\n"
        "  enable)\n"
        "    printf '%s %s\\n' \"$cmd\" \"$*\" >> \"$LAUNCHCTL_CALLS\"\n"
        "    ;;\n"
        "esac\n"
    )
    uv.chmod(0o755)
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAUNCHCTL_CALLS": str(calls),
        "LAUNCHCTL_STATE": str(state),
    }

    subprocess.run(["scripts/install_launchd.sh", "--install-nightly"], env=env, check=True)
    subprocess.run(["scripts/install_launchd.sh", "--install-nightly"], env=env, check=True)

    installed = _plist(home / "Library" / "LaunchAgents" / "com.engram.v3.nightly.plist")
    assert installed["Label"] == "com.engram.v3.nightly"
    assert installed["EnvironmentVariables"]["HOME"] == str(home)
    assert installed["EnvironmentVariables"]["ENGRAM_UV_BIN"] == str(uv)
    assert installed["ProgramArguments"] == [
        str(Path.cwd() / "scripts" / "engram_nightly_launchd.sh")
    ]

    call_text = calls.read_text(encoding="utf-8")
    assert call_text.count("bootstrap ") == 2
    assert "bootout " in call_text


def test_install_launchd_bridge_writes_explicit_env_file_into_plist(tmp_path: Path):
    script, repo_root, home, env = _bridge_install_fixture(tmp_path)
    secrets_dir = home / "secrets"
    secrets_dir.mkdir()
    env_file = secrets_dir / "engram.env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-test\n", encoding="utf-8")
    env["ENGRAM_ENV_FILE"] = "~/secrets/engram.env"

    subprocess.run([str(script)], cwd=repo_root, env=env, check=True)

    installed = _plist(home / "Library" / "LaunchAgents" / "com.engram.bridge.plist")
    assert installed["Label"] == "com.engram.bridge"
    assert installed["EnvironmentVariables"]["ENGRAM_ENV_FILE"] == str(env_file)
    assert installed["SoftResourceLimits"]["NumberOfFiles"] == 4096
    assert installed["HardResourceLimits"]["NumberOfFiles"] == 8192


def test_install_launchd_bridge_prefers_home_env_file_over_repo_env(tmp_path: Path):
    script, repo_root, home, env = _bridge_install_fixture(tmp_path)
    home_env = home / ".engram" / ".env"
    home_env.parent.mkdir(parents=True)
    home_env.write_text("ANTHROPIC_API_KEY=sk-home\n", encoding="utf-8")
    repo_env = repo_root / ".env"
    repo_env.write_text("ANTHROPIC_API_KEY=sk-repo\n", encoding="utf-8")

    subprocess.run([str(script)], cwd=repo_root, env=env, check=True)

    installed = _plist(home / "Library" / "LaunchAgents" / "com.engram.bridge.plist")
    assert installed["EnvironmentVariables"]["ENGRAM_ENV_FILE"] == str(home_env)


def test_install_launchd_bridge_falls_back_to_repo_env_file(tmp_path: Path):
    script, repo_root, home, env = _bridge_install_fixture(tmp_path)
    repo_env = repo_root / ".env"
    repo_env.write_text("ANTHROPIC_API_KEY=sk-repo\n", encoding="utf-8")

    subprocess.run([str(script)], cwd=repo_root, env=env, check=True)

    installed = _plist(home / "Library" / "LaunchAgents" / "com.engram.bridge.plist")
    assert installed["EnvironmentVariables"]["ENGRAM_ENV_FILE"] == str(repo_env)


def test_install_launchd_bridge_errors_before_overwriting_plist_when_env_missing(
    tmp_path: Path,
):
    script, repo_root, home, env = _bridge_install_fixture(tmp_path)
    plist_path = home / "Library" / "LaunchAgents" / "com.engram.bridge.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("manual fix", encoding="utf-8")

    result = subprocess.run(
        [str(script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Set ENGRAM_ENV_FILE" in result.stderr
    assert "engram setup" in result.stderr
    assert plist_path.read_text(encoding="utf-8") == "manual fix"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _event(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for event in events:
        if event["event"] == name:
            return event
    raise AssertionError(f"missing event: {name}")


def _plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return plistlib.load(fh)


def _bridge_install_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    repo_root = tmp_path / "repo"
    script_dir = repo_root / "scripts"
    launchd_dir = repo_root / "launchd"
    script_dir.mkdir(parents=True)
    launchd_dir.mkdir(parents=True)
    script = script_dir / "install_launchd.sh"
    script.write_text(
        Path("scripts/install_launchd.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    (launchd_dir / "com.engram.bridge.plist").write_text(
        Path("launchd/com.engram.bridge.plist").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    calls = tmp_path / "launchctl-calls.txt"
    state = tmp_path / "launchctl-state.txt"

    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    grep = bin_dir / "grep"
    grep.write_text(
        "#!/bin/sh\n"
        "if [ \"$#\" -eq 3 ] && [ \"$1\" = \"-q\" ] && [ \"$2\" = \"engram.ready\" ] "
        "&& [ \"$3\" = \"/tmp/engram.bridge.out.log\" ]; then\n"
        "  exit 0\n"
        "fi\n"
        "exec /usr/bin/grep \"$@\"\n",
        encoding="utf-8",
    )
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "cmd=\"$1\"\n"
        "shift || true\n"
        "case \"$cmd\" in\n"
        "  list)\n"
        "    printf '%s\\n' 'PID Status Label'\n"
        "    [ -f \"$LAUNCHCTL_STATE\" ] && cat \"$LAUNCHCTL_STATE\"\n"
        "    ;;\n"
        "  load)\n"
        "    printf '%s\\n' '123 0 com.engram.bridge' > \"$LAUNCHCTL_STATE\"\n"
        "    printf '%s %s\\n' \"$cmd\" \"$*\" >> \"$LAUNCHCTL_CALLS\"\n"
        "    ;;\n"
        "  unload)\n"
        "    rm -f \"$LAUNCHCTL_STATE\"\n"
        "    printf '%s %s\\n' \"$cmd\" \"$*\" >> \"$LAUNCHCTL_CALLS\"\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    for binary in (uv, grep, launchctl):
        binary.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAUNCHCTL_CALLS": str(calls),
        "LAUNCHCTL_STATE": str(state),
    }
    return script, repo_root, home, env
