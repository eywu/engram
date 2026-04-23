from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from engram.nightly.report import ReportArtifact, write_report_and_notify


@pytest.mark.asyncio
async def test_report_writes_markdown_and_posts_aggregate_dm(tmp_path: Path) -> None:
    posts: list[str] = []
    run_date = date(2026, 4, 22)
    artifact = ReportArtifact(
        trigger="nightly",
        harvest_path=tmp_path / "nightly" / "2026-04-22" / "harvest.json",
        synthesis_path=tmp_path / "nightly" / "archive" / "2026-04-22" / "synthesis.json",
        rows_written=2,
        payload={
            "date": "2026-04-22",
            "channels": [
                {
                    "channel_id": "C07ALPHA",
                    "status": "synthesized",
                    "cost_usd": "0.010000",
                    "row_count": 3,
                    "token_count": 30,
                    "synthesis": {
                        "summary": "Alpha summary.",
                        "highlights": [{"text": "Alpha highlight"}],
                        "decisions": [{"text": "Alpha decision"}],
                        "action_items": [{"text": "Alpha action", "owner": "ops"}],
                        "open_questions": [{"text": "Alpha question"}],
                    },
                },
                {
                    "channel_id": "C07BETA",
                    "status": "synthesized",
                    "cost_usd": "0.020000",
                    "row_count": 4,
                    "token_count": 40,
                    "synthesis": {
                        "summary": "Beta summary.",
                        "highlights": [],
                        "decisions": [],
                        "action_items": [{"text": "Beta action"}],
                        "open_questions": [],
                    },
                },
            ],
            "skipped_channels": [],
            "totals": {"cost_usd": "0.030000"},
        },
    )

    async def success_dm(text: str) -> None:
        posts.append(text)

    result = await write_report_and_notify(
        run_date=run_date,
        output_root=tmp_path / "nightly",
        artifacts=[artifact],
        success_dm=success_dm,
    )

    assert result.report_path == tmp_path / "nightly" / "archive" / "2026-04-22" / "report.md"
    assert result.slack_posted is True
    assert posts == [
        f"Engram nightly — 2 channels, 3 flags, $0.0300. Full: `{result.report_path}`"
    ]
    assert "C07ALPHA" not in posts[0]
    assert "C07BETA" not in posts[0]

    report = result.report_path.read_text(encoding="utf-8")
    assert "# Engram Nightly Report - 2026-04-22" in report
    assert "| C07ALPHA | synthesized | $0.0100 |" in report
    assert "| C07BETA | synthesized | $0.0200 |" in report
    assert "| TOTAL | | | $0.0300 |" in report
    assert "- Action: Alpha action (owner: ops)" in report
    assert "- Question: Alpha question" in report


@pytest.mark.asyncio
async def test_report_suppress_skips_slack_post(tmp_path: Path) -> None:
    posts: list[str] = []
    artifact = ReportArtifact(
        trigger="nightly",
        harvest_path=None,
        synthesis_path=None,
        rows_written=0,
        payload={"channels": [], "totals": {"cost_usd": "0"}},
    )

    async def success_dm(text: str) -> None:
        posts.append(text)

    result = await write_report_and_notify(
        run_date=date(2026, 4, 22),
        output_root=tmp_path / "nightly",
        artifacts=[artifact],
        suppress_slack=True,
        success_dm=success_dm,
    )

    assert result.report_path.exists()
    assert result.slack_posted is False
    assert posts == []
