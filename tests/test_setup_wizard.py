from __future__ import annotations

import json
import plistlib
import re
from pathlib import Path

import pytest
import yaml

from engram.bootstrap import provision_channel
from engram.manifest import (
    IdentityTemplate,
    build_mcp_manifest_change_plan,
    load_manifest,
    persist_approved_mcp_manifest_change,
)
from engram.mcp import detect_new_user_mcp_servers
from engram.mcp_trust import MCPTrustDecision, MCPTrustTier
from engram.paths import channel_manifest_path
from engram.setup_wizard import (
    SLACK_APP_MANIFEST,
    _step_launchd_sync,
    _step_mcp_inventory,
    _step_slack,
    _write_config,
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


def _allow_mcp_for_test(home: Path, channel_id: str, server_name: str) -> None:
    manifest_path = channel_manifest_path(channel_id, home)
    manifest = load_manifest(manifest_path)
    updated = manifest.model_copy(
        update={
            "mcp_servers": manifest.mcp_servers.model_copy(
                update={
                    "allowed": list(
                        dict.fromkeys([*(manifest.mcp_servers.allowed or []), server_name])
                    )
                }
            )
        }
    )
    plan = build_mcp_manifest_change_plan(manifest_path, updated)
    assert plan is not None
    persist_approved_mcp_manifest_change(plan)


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


def test_step_slack_captures_workspace_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["xoxb-test", "xapp-test"])
    monkeypatch.setattr(
        "engram.setup_wizard.Prompt.ask",
        lambda *_args, **_kwargs: next(answers),
    )
    output: list[str] = []
    monkeypatch.setattr(
        "engram.setup_wizard.rprint",
        lambda *args, **_kwargs: output.append(" ".join(map(str, args))),
    )

    def requester(url: str, **kwargs) -> tuple[int, dict[str, object]]:
        assert url == "https://slack.com/api/auth.test"
        assert kwargs["headers"]["Authorization"] == "Bearer xoxb-test"
        return (
            200,
            {
                "ok": True,
                "team_id": "T02G507JU",
                "team": "Growth Gauge",
                "url": "https://growthgauge.slack.com/",
            },
        )

    slack = _step_slack(requester=requester)

    assert slack == {
        "bot_token": "xoxb-test",
        "app_token": "xapp-test",
        "team_id": "T02G507JU",
        "team_name": "Growth Gauge",
        "workspace_url": "https://growthgauge.slack.com/",
    }
    rendered = "\n".join(output)
    assert "Connected to" in rendered
    assert "Growth Gauge" in rendered
    assert "T02G507JU" in rendered
    assert "https://growthgauge.slack.com/" in rendered


def test_write_config_persists_slack_workspace_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("engram.setup_wizard.DEFAULT_CONFIG_PATH", config_path)

    _write_config(
        slack={
            "bot_token": "xoxb-test",
            "app_token": "xapp-test",
            "team_id": "T02G507JU",
            "team_name": "Growth Gauge",
            "workspace_url": "https://growthgauge.slack.com/",
        },
        anthropic_key="sk-ant-test",
        gemini_key=None,
    )

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert payload["slack"] == {
        "bot_token": "xoxb-test",
        "app_token": "xapp-test",
        "team_id": "T02G507JU",
        "team_name": "Growth Gauge",
        "workspace_url": "https://growthgauge.slack.com/",
    }


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
    monkeypatch.setattr(
        "engram.setup_wizard.Confirm.ask",
        lambda *_args, **_kwargs: False,
    )

    _step_mcp_inventory()

    rendered = "\n".join(output)
    assert "~/.claude.json" in rendered
    assert "~/.claude/mcp.json" in rendered
    assert "linear" in rendered
    assert "Team channels still gate MCPs per manifest with strict allow-lists" in rendered
    assert detect_new_user_mcp_servers(home=tmp_path / ".engram").new_servers == []


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
    monkeypatch.setattr(
        "engram.setup_wizard.Confirm.ask",
        lambda *_args, **_kwargs: False,
    )

    _step_mcp_inventory()

    rendered = "\n".join(output)
    assert "Registered but not yet allowed in any team channel manifest" in rendered
    assert "camoufox" in rendered
    assert "~/.engram/contexts/<channel-id>/.claude/channel-manifest.yaml" in rendered


def test_step_mcp_inventory_can_allow_official_mcp_into_existing_team_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github@1.2.3"],
                    }
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

    answers = iter([True, True])
    monkeypatch.setattr(
        "engram.setup_wizard.Confirm.ask",
        lambda *_args, **_kwargs: next(answers),
    )

    async def fake_resolve(server_name, server_config, *, home=None):
        assert server_name == "github"
        assert server_config == {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github@1.2.3"],
        }
        assert home is not None
        return MCPTrustDecision(
            server_name="github",
            tier=MCPTrustTier.OFFICIAL,
            registry="npm",
            package_name="@modelcontextprotocol/server-github",
            version="1.2.3",
            trust_summary="official server",
            reason="official package",
        )

    monkeypatch.setattr("engram.setup_wizard.resolve_mcp_server_trust", fake_resolve)
    output: list[str] = []
    monkeypatch.setattr(
        "engram.setup_wizard.rprint",
        lambda *args, **_kwargs: output.append(" ".join(map(str, args))),
    )

    _step_mcp_inventory()

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert manifest.mcp_servers.allowed == ["engram-memory", "github"]
    assert "allowed github in #growth (C07TEAM)" in "\n".join(output)


def test_step_mcp_inventory_requires_explicit_confirmation_for_unknown_mcp(
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
    home = tmp_path / ".engram"
    provision_channel(
        "C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#growth",
        home=home,
    )

    answers = iter([True, True, False])
    monkeypatch.setattr(
        "engram.setup_wizard.Confirm.ask",
        lambda *_args, **_kwargs: next(answers),
    )

    async def fake_resolve(server_name, server_config, *, home=None):
        assert server_name == "camoufox"
        assert server_config == {"command": "camoufox-mcp"}
        assert home is not None
        return MCPTrustDecision(
            server_name="camoufox",
            tier=MCPTrustTier.UNKNOWN,
            registry="custom",
            package_name="camoufox-browser[mcp]",
            version="0.1.1",
            trust_summary="metadata lookup failed",
            reason="metadata lookup failed",
        )

    monkeypatch.setattr("engram.setup_wizard.resolve_mcp_server_trust", fake_resolve)
    output: list[str] = []
    monkeypatch.setattr(
        "engram.setup_wizard.rprint",
        lambda *args, **_kwargs: output.append(" ".join(map(str, args))),
    )

    _step_mcp_inventory()

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert manifest.mcp_servers.allowed == ["engram-memory"]
    rendered = "\n".join(output)
    assert "camoufox is [italic]unknown[/italic]" in rendered
    assert "no team manifest changes applied" in rendered


def test_step_mcp_inventory_syncs_partial_coverage_across_team_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github@1.2.3"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    home = tmp_path / ".engram"
    provision_channel(
        "C07A",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#alpha",
        home=home,
    )
    provision_channel(
        "C07B",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#beta",
        home=home,
    )
    _allow_mcp_for_test(home, "C07A", "github")

    answers = iter([True, True])
    monkeypatch.setattr(
        "engram.setup_wizard.Confirm.ask",
        lambda *_args, **_kwargs: next(answers),
    )

    async def fake_resolve(server_name, server_config, *, home=None):
        assert server_name == "github"
        assert server_config == {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github@1.2.3"],
        }
        return MCPTrustDecision(
            server_name="github",
            tier=MCPTrustTier.OFFICIAL,
            registry="npm",
            package_name="@modelcontextprotocol/server-github",
            version="1.2.3",
            trust_summary="official server",
            reason="official package",
        )

    monkeypatch.setattr("engram.setup_wizard.resolve_mcp_server_trust", fake_resolve)

    _step_mcp_inventory()

    assert load_manifest(channel_manifest_path("C07A", home)).mcp_servers.allowed == [
        "engram-memory",
        "github",
    ]
    assert load_manifest(channel_manifest_path("C07B", home)).mcp_servers.allowed == [
        "engram-memory",
        "github",
    ]


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
