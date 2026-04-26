from __future__ import annotations

import json
import plistlib
import re
from pathlib import Path

import pytest

from engram.bootstrap import provision_channel
from engram.manifest import IdentityTemplate
from engram.setup_wizard import (
    SLACK_APP_MANIFEST,
    _step_launchd_sync,
    _step_mcp_inventory,
    run_wizard,
)


def _docs_manifest_block() -> str:
    docs_path = Path("docs/slack-app-setup.md")
    match = re.search(
        r"## 2\. Manifest\n\n```yaml\n(?P<manifest>.*?)\n```",
        docs_path.read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group("manifest") + "\n"


def test_setup_wizard_manifest_matches_install_doc() -> None:
    assert _docs_manifest_block() == SLACK_APP_MANIFEST


def test_run_wizard_prints_slash_command_verification_hint(monkeypatch) -> None:
    output: list[str] = []
    monkeypatch.setattr(
        "engram.setup_wizard.rprint",
        lambda *args, **_kwargs: output.append(" ".join(map(str, args))),
    )
    monkeypatch.setattr("engram.setup_wizard._step_claude_cli", lambda: None)
    monkeypatch.setattr(
        "engram.setup_wizard._step_slack",
        lambda: {"bot_token": "xoxb-test", "app_token": "xapp-test"},
    )
    monkeypatch.setattr("engram.setup_wizard._step_anthropic", lambda: "sk-ant-test")
    monkeypatch.setattr("engram.setup_wizard._step_gemini", lambda: None)
    monkeypatch.setattr("engram.setup_wizard._step_mcp_inventory", lambda: None)
    monkeypatch.setattr("engram.setup_wizard._write_config", lambda **_kwargs: None)
    monkeypatch.setattr("engram.setup_wizard._step_launchd_sync", lambda **_kwargs: None)

    run_wizard()

    rendered = "\n".join(output)
    assert "Verify slash commands: type `/engram` in any channel" in rendered
    assert "api.slack.com/apps and reinstall the app." in rendered


def test_step_mcp_inventory_reads_claude_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "linear": {"type": "http", "url": "https://linear.example/mcp"}
                }
            }
        ),
        encoding="utf-8",
    )
    output: list[str] = []
    monkeypatch.setattr(
        "engram.setup_wizard.rprint",
        lambda *args, **_kwargs: output.append(" ".join(map(str, args))),
    )

    _step_mcp_inventory()

    rendered = "\n".join(output)
    assert "~/.claude.json" in rendered
    assert "~/.claude/mcp.json" in rendered
    assert "linear" in rendered
    assert "Team channels still gate MCPs per manifest with strict allow-lists" in rendered


def test_step_mcp_inventory_warns_when_existing_team_manifests_exclude_user_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "camoufox": {"command": "camoufox-mcp"}
                }
            }
        ),
        encoding="utf-8",
    )
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=tmp_path / ".engram",
    )
    output: list[str] = []
    monkeypatch.setattr(
        "engram.setup_wizard.rprint",
        lambda *args, **_kwargs: output.append(" ".join(map(str, args))),
    )

    _step_mcp_inventory()

    rendered = "\n".join(output)
    assert "Registered but not yet allowed in any team channel manifest" in rendered
    assert "camoufox" in rendered
    assert "~/.engram/contexts/<channel-id>/.claude/channel-manifest.yaml" in rendered


def test_step_launchd_sync_refreshes_drifted_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path.cwd())
    monkeypatch.setenv("HOME", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr("engram.setup_wizard.Confirm.ask", lambda *_args, **_kwargs: True)

    installed_path = tmp_path / "Library" / "LaunchAgents" / "com.engram.bridge.plist"
    installed_path.parent.mkdir(parents=True)
    with installed_path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.engram.bridge",
                "ProgramArguments": ["/tmp/old-engram", "run"],
                "WorkingDirectory": "/tmp/old-repo",
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

    _step_launchd_sync(anthropic_key="sk-ant-test", gemini_key="gemini-test")

    with installed_path.open("rb") as handle:
        installed = plistlib.load(handle)
    assert installed["SoftResourceLimits"]["NumberOfFiles"] == 4096
    assert installed["HardResourceLimits"]["NumberOfFiles"] == 8192
    assert installed["ProgramArguments"][1:] == [
        "run",
        "--project",
        str(Path.cwd()),
        "python",
        "-m",
        "engram.main",
    ]
    assert installed["EnvironmentVariables"]["ENGRAM_ENV_FILE"] == str(
        tmp_path / ".engram" / ".env"
    )

    env_file = tmp_path / ".engram" / ".env"
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        "ANTHROPIC_API_KEY=sk-ant-test",
        "GEMINI_API_KEY=gemini-test",
    ]
