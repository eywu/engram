from __future__ import annotations

from datetime import date

from typer.testing import CliRunner

from engram.cli import app


def test_nightly_cli_passes_weekly_flag(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Result:
        exit_code = 0

    async def fake_run_configured_nightly(*, weekly: bool, target_date):
        captured["weekly"] = weekly
        captured["target_date"] = target_date
        return _Result()

    monkeypatch.setattr(
        "engram.nightly.run_configured_nightly",
        fake_run_configured_nightly,
    )

    result = CliRunner().invoke(app, ["nightly", "--weekly", "--date", "2026-04-20"])

    assert result.exit_code == 0
    assert captured == {
        "weekly": True,
        "target_date": date(2026, 4, 20),
    }
