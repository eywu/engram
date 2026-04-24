from __future__ import annotations

import re
from pathlib import Path

from engram.setup_wizard import SLACK_APP_MANIFEST, run_wizard


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
    monkeypatch.setattr("engram.setup_wizard.rprint", lambda *args, **_kwargs: output.append(" ".join(map(str, args))))
    monkeypatch.setattr("engram.setup_wizard._step_claude_cli", lambda: None)
    monkeypatch.setattr(
        "engram.setup_wizard._step_slack",
        lambda: {"bot_token": "xoxb-test", "app_token": "xapp-test"},
    )
    monkeypatch.setattr("engram.setup_wizard._step_anthropic", lambda: "sk-ant-test")
    monkeypatch.setattr("engram.setup_wizard._step_gemini", lambda: None)
    monkeypatch.setattr("engram.setup_wizard._step_mcp_inventory", lambda: None)
    monkeypatch.setattr("engram.setup_wizard._write_config", lambda **_kwargs: None)

    run_wizard()

    rendered = "\n".join(output)
    assert "Verify slash commands: type `/engram` in any channel" in rendered
    assert "api.slack.com/apps and reinstall the app." in rendered
