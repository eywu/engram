from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from engram.cli import app


def test_uninstall_dry_run_outputs_plan_without_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    def fail_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected command: {args} {kwargs}")

    monkeypatch.setattr("engram.uninstall.subprocess.run", fail_run)

    result = CliRunner().invoke(app, ["uninstall", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry run: no changes will be made." in result.output
    assert "unload launchd bridge job (com.engram.bridge)" in result.output
    assert "unload launchd nightly job (com.engram.v3.nightly)" in result.output
    assert "launchctl bootout gui/" in result.output
    assert "uv tool uninstall engram" in result.output


def test_uninstall_purge_skips_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".engram"
    home.mkdir()
    (home / "config.yaml").write_text("slack: {}\n", encoding="utf-8")
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.engram.bridge.plist").write_text("bridge", encoding="utf-8")
    (launch_agents / "com.engram.v3.nightly.plist").write_text("nightly", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("engram.uninstall.os.getuid", lambda: 501)

    commands: list[list[str]] = []

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(args))
        return subprocess.CompletedProcess(args=list(args), returncode=0)

    monkeypatch.setattr("engram.uninstall.subprocess.run", fake_run)

    def fail_confirm(*args: object, **kwargs: object) -> bool:
        raise AssertionError(f"unexpected prompt: {args} {kwargs}")

    monkeypatch.setattr("engram.uninstall.typer.confirm", fail_confirm)

    result = CliRunner().invoke(app, ["uninstall", "--purge"])

    assert result.exit_code == 0
    assert not home.exists()
    assert ["uv", "tool", "uninstall", "engram"] in commands


def test_uninstall_launchctl_bootout_falls_back_to_unload_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    bridge_plist = launch_agents / "com.engram.bridge.plist"
    nightly_plist = launch_agents / "com.engram.v3.nightly.plist"
    bridge_plist.write_text("bridge", encoding="utf-8")
    nightly_plist.write_text("nightly", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("engram.uninstall.os.getuid", lambda: 501)

    commands: list[list[str]] = []

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(args)
        commands.append(command)
        returncode = 1 if command[:2] == ["launchctl", "bootout"] else 0
        return subprocess.CompletedProcess(args=command, returncode=returncode)

    monkeypatch.setattr("engram.uninstall.subprocess.run", fake_run)

    result = CliRunner().invoke(app, ["uninstall"], input="y\nn\nn\nn\n")

    assert result.exit_code == 0
    assert commands == [
        ["launchctl", "bootout", "gui/501/com.engram.bridge"],
        ["launchctl", "unload", str(bridge_plist)],
        ["launchctl", "bootout", "gui/501/com.engram.v3.nightly"],
        ["launchctl", "unload", str(nightly_plist)],
    ]
    assert not bridge_plist.exists()
    assert not nightly_plist.exists()
