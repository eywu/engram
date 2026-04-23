from __future__ import annotations

from datetime import date

from typer.testing import CliRunner

from engram.cli import app


def test_nightly_cli_passes_nightly_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Result:
        exit_code = 0

    async def fake_run_configured_nightly(
        *,
        dry_run: bool,
        weekly: bool,
        verbose: bool,
        target_date,
    ):
        captured["dry_run"] = dry_run
        captured["weekly"] = weekly
        captured["verbose"] = verbose
        captured["target_date"] = target_date
        return _Result()

    monkeypatch.setattr(
        "engram.nightly.run_configured_nightly",
        fake_run_configured_nightly,
    )

    result = CliRunner().invoke(
        app,
        ["nightly", "--dry-run", "--weekly", "--verbose", "--date", "2026-04-20"],
    )

    assert result.exit_code == 0
    assert captured == {
        "dry_run": True,
        "weekly": True,
        "verbose": True,
        "target_date": date(2026, 4, 20),
    }
