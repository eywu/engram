"""Operator-facing nightly report and owner-DM templates."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from engram.config import DEFAULT_CONFIG_PATH, EngramConfig

log = logging.getLogger(__name__)

SuccessDMPoster = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ReportArtifact:
    trigger: str
    harvest_path: Path | None
    synthesis_path: Path | None
    rows_written: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class NightlyReportResult:
    report_path: Path
    slack_text: str
    slack_posted: bool
    channel_count: int
    flag_count: int
    cost_usd: float


async def write_report_and_notify(
    *,
    run_date: date,
    output_root: Path,
    artifacts: Sequence[ReportArtifact],
    suppress_slack: bool = False,
    success_dm: SuccessDMPoster | None = None,
) -> NightlyReportResult:
    """Write ``report.md`` and optionally post the concise owner-DM summary."""
    archive_dir = output_root.expanduser() / "archive" / run_date.isoformat()
    archive_dir.mkdir(parents=True, exist_ok=True)
    report_path = archive_dir / "report.md"

    channel_count = _channel_count(artifacts)
    flag_count = _flag_count(artifacts)
    cost_usd = _aggregate_cost(artifacts)
    markdown = render_report(
        run_date=run_date,
        artifacts=artifacts,
        report_path=report_path,
        channel_count=channel_count,
        flag_count=flag_count,
        cost_usd=cost_usd,
    )
    report_path.write_text(markdown, encoding="utf-8")

    slack_text = format_success_dm(
        channel_count=channel_count,
        flag_count=flag_count,
        cost_usd=cost_usd,
        report_path=report_path,
    )
    slack_posted = False
    if not suppress_slack and success_dm is not None:
        try:
            await success_dm(slack_text)
            slack_posted = True
            log.info("nightly.report_dm_posted", extra={"report_path": str(report_path)})
        except Exception:
            log.warning("nightly.report_dm_failed", exc_info=True)
    return NightlyReportResult(
        report_path=report_path,
        slack_text=slack_text,
        slack_posted=slack_posted,
        channel_count=channel_count,
        flag_count=flag_count,
        cost_usd=cost_usd,
    )


def render_report(
    *,
    run_date: date,
    artifacts: Sequence[ReportArtifact],
    report_path: Path,
    channel_count: int,
    flag_count: int,
    cost_usd: float,
) -> str:
    lines = [
        f"# Engram Nightly Report - {run_date.isoformat()}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Channels covered | {channel_count} |",
        f"| Flags | {flag_count} |",
        f"| Aggregate cost | ${_format_cost(cost_usd)} |",
        f"| Report path | `{report_path}` |",
        "",
        "## Artifacts",
        "",
        "| Trigger | Harvest | Synthesis | Rows written |",
        "| --- | --- | --- | ---: |",
    ]
    for artifact in artifacts:
        lines.append(
            "| "
            f"{_cell(artifact.trigger)} | "
            f"{_path_cell(artifact.harvest_path)} | "
            f"{_path_cell(artifact.synthesis_path)} | "
            f"{artifact.rows_written} |"
        )

    lines.extend(
        [
            "",
            "## Per-Channel Cost",
            "",
            "| Trigger | Channel | Status | Cost |",
            "| --- | --- | --- | ---: |",
        ]
    )
    cost_rows = _cost_rows(artifacts)
    if cost_rows:
        for row in cost_rows:
            lines.append(
                "| "
                f"{_cell(row['trigger'])} | "
                f"{_cell(row['channel_id'])} | "
                f"{_cell(row['status'])} | "
                f"${_format_cost(row['cost_usd'])} |"
            )
    else:
        lines.append("| - | - | none | $0.0000 |")
    lines.append(f"| TOTAL | | | ${_format_cost(cost_usd)} |")

    lines.extend(["", "## Channel Details", ""])
    detail_sections = _channel_detail_sections(artifacts)
    if detail_sections:
        for section in detail_sections:
            lines.extend(section)
            lines.append("")
    else:
        lines.append("_No synthesized channels._")
        lines.append("")

    skipped = _skipped_sections(artifacts)
    if skipped:
        lines.extend(["## Skipped Channels", ""])
        lines.extend(skipped)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def format_success_dm(
    *,
    channel_count: int,
    flag_count: int,
    cost_usd: float,
    report_path: Path,
) -> str:
    return (
        f"Engram nightly — {channel_count} channels, {flag_count} flags, "
        f"${_format_cost(cost_usd)}. Full: `{report_path}`"
    )


def format_failure_dm(*, phase: str | None, exit_code: int | None, log_path: Path) -> str:
    phase_text = phase or "unknown"
    exit_text = exit_code if exit_code is not None else "unknown"
    return (
        f"⚠️ Engram nightly FAILED at phase={phase_text}, exit={exit_text}. "
        f"Logs: `{log_path}`."
    )


async def post_configured_success_dm(
    text: str,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> None:
    try:
        config = EngramConfig.load(config_path)
    except RuntimeError:
        log.warning("nightly.report_dm_dropped reason=config_error", exc_info=True)
        return

    if not config.owner_dm_channel_id:
        log.warning("nightly.report_dm_dropped reason=no_owner_dm")
        return

    client = AsyncWebClient(token=config.slack.bot_token)
    await client.chat_postMessage(channel=config.owner_dm_channel_id, text=text)


def _channel_count(artifacts: Sequence[ReportArtifact]) -> int:
    return len(
        {
            str(channel.get("channel_id"))
            for artifact in artifacts
            for channel in _channels(artifact.payload)
            if channel.get("status") == "synthesized" and channel.get("channel_id")
        }
    )


def _flag_count(artifacts: Sequence[ReportArtifact]) -> int:
    total = 0
    for artifact in artifacts:
        for channel in _channels(artifact.payload):
            if channel.get("status") != "synthesized":
                continue
            synthesis = channel.get("synthesis")
            if not isinstance(synthesis, dict):
                continue
            total += _list_len(synthesis.get("action_items"))
            total += _list_len(synthesis.get("open_questions"))
    return total


def _aggregate_cost(artifacts: Sequence[ReportArtifact]) -> float:
    total = 0.0
    for artifact in artifacts:
        totals = artifact.payload.get("totals")
        if isinstance(totals, dict) and "cost_usd" in totals:
            total += _float(totals.get("cost_usd"))
        else:
            total += sum(_float(channel.get("cost_usd")) for channel in _channels(artifact.payload))
    return total


def _cost_rows(artifacts: Sequence[ReportArtifact]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for artifact in artifacts:
        for channel in _channels(artifact.payload):
            rows.append(
                {
                    "trigger": artifact.trigger,
                    "channel_id": str(channel.get("channel_id") or "?"),
                    "status": str(channel.get("status") or "unknown"),
                    "cost_usd": _float(channel.get("cost_usd")),
                }
            )
    return rows


def _channel_detail_sections(artifacts: Sequence[ReportArtifact]) -> list[list[str]]:
    sections: list[list[str]] = []
    for artifact in artifacts:
        for channel in _channels(artifact.payload):
            channel_id = str(channel.get("channel_id") or "?")
            status = str(channel.get("status") or "unknown")
            section = [
                f"### {channel_id} ({artifact.trigger})",
                "",
                (
                    f"Status: `{status}` · Cost: ${_format_cost(_float(channel.get('cost_usd')))}"
                    f" · Rows: {channel.get('row_count', 0)}"
                    f" · Tokens: {channel.get('token_count', 0)}"
                ),
            ]
            synthesis = channel.get("synthesis")
            if status == "synthesized" and isinstance(synthesis, dict):
                summary = str(synthesis.get("summary") or "").strip()
                if summary:
                    section.extend(["", summary])
                section.extend(["", "**Flags**"])
                section.extend(_flag_lines(synthesis))
                section.extend(["", "**Decisions**"])
                section.extend(_item_lines(synthesis.get("decisions")))
                section.extend(["", "**Highlights**"])
                section.extend(_item_lines(synthesis.get("highlights")))
            elif isinstance(channel.get("error"), dict):
                error = channel["error"]
                section.extend(
                    [
                        "",
                        f"Error: `{error.get('error_class', 'Error')}` {error.get('error', '')}",
                    ]
                )
            sections.append(section)
    return sections


def _flag_lines(synthesis: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for item in _as_list(synthesis.get("action_items")):
        owner = item.get("owner") if isinstance(item, dict) else None
        suffix = f" (owner: {owner})" if owner else ""
        lines.append(f"- Action: {_item_text(item)}{suffix}")
    for item in _as_list(synthesis.get("open_questions")):
        lines.append(f"- Question: {_item_text(item)}")
    return lines or ["_None._"]


def _item_lines(raw: Any) -> list[str]:
    lines = [f"- {_item_text(item)}" for item in _as_list(raw)]
    return lines or ["_None._"]


def _skipped_sections(artifacts: Sequence[ReportArtifact]) -> list[str]:
    lines: list[str] = []
    for artifact in artifacts:
        skipped = artifact.payload.get("skipped_channels")
        if not isinstance(skipped, list):
            continue
        for item in skipped:
            if not isinstance(item, dict):
                continue
            channel_id = str(item.get("channel_id") or "?")
            reason = str(item.get("reason") or "unknown")
            lines.append(f"- `{channel_id}` ({artifact.trigger}): {reason}")
    return lines


def _channels(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("channels")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _item_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item).strip()
    return str(item).strip()


def _as_list(raw: Any) -> list[Any]:
    return raw if isinstance(raw, list) else []


def _list_len(raw: Any) -> int:
    return len(raw) if isinstance(raw, list) else 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _format_cost(value: float) -> str:
    return f"{value:.4f}"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|")


def _path_cell(path: Path | None) -> str:
    return f"`{path}`" if path is not None else "-"
