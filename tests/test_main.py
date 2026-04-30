from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_migration_runs_at_bridge_startup_before_first_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    from engram import config as config_module
    from engram import main
    from engram.mcp import claude_mcp_config_path, legacy_claude_mcp_config_path

    config_path = tmp_path / ".engram" / "config.yaml"
    state_dir = tmp_path / ".engram" / "state"
    contexts_dir = tmp_path / ".engram" / "contexts"
    log_dir = tmp_path / ".engram" / "logs"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "slack:",
                "  bot_token: xoxb-test",
                "  app_token: xapp-test",
                "anthropic:",
                "  api_key: sk-ant-test",
                "paths:",
                f"  state_dir: {state_dir}",
                f"  contexts_dir: {contexts_dir}",
                f"  log_dir: {log_dir}",
                "embeddings:",
                "  enabled: false",
                "observability:",
                "  fd_snapshots_enabled: false",
                "owner_dm_channel_id: D07OWNER",
                "owner_user_id: U07OWNER",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", config_path)

    target_path = claude_mcp_config_path()
    legacy_path = legacy_claude_mcp_config_path()
    target_payload = {"theme": "dark", "mcpServers": {"linear": {"command": "linear"}}}
    legacy_payload = {"mcpServers": {"github": {"command": "github-mcp"}}}
    _write_json(target_path, target_payload)
    _write_json(legacy_path, legacy_payload)

    class RouterConstructedError(Exception):
        pass

    class FakeAsyncApp:
        def __init__(self, *, token: str):
            self.token = token
            self.client = object()

    async def fake_discover_template_vars(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    def fake_ensure_project_root(*, home: Path) -> Path:
        project = home / "project"
        project.mkdir(parents=True, exist_ok=True)
        return project

    def fake_router(*_args: Any, **_kwargs: Any) -> object:
        assert json.loads(target_path.read_text(encoding="utf-8")) == {
            "mcpServers": {
                "github": {"command": "github-mcp"},
                "linear": {"command": "linear"},
            },
            "theme": "dark",
        }
        assert not legacy_path.exists()
        assert target_path.with_name(f"{target_path.name}.bak").exists()
        assert legacy_path.with_name(f"{legacy_path.name}.bak").exists()
        raise RouterConstructedError

    monkeypatch.setattr(main, "AsyncApp", FakeAsyncApp)
    monkeypatch.setattr(main, "_discover_template_vars", fake_discover_template_vars)
    monkeypatch.setattr(main, "ensure_project_root", fake_ensure_project_root)
    monkeypatch.setattr(main, "Router", fake_router)

    with pytest.raises(RouterConstructedError):
        await main.run()
