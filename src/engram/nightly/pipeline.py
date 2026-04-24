"""End-to-end nightly pipeline orchestration."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from engram.config import DEFAULT_CONFIG_PATH, NightlyConfig, load_nightly_config
from engram.nightly.apply import ApplyResult, apply_synthesis
from engram.nightly.harvest import HarvestResult, run_harvest, run_weekly_harvest
from engram.nightly.report import (
    ReportArtifact,
    SuccessDMPoster,
    post_configured_success_dm,
    write_report_and_notify,
)
from engram.nightly.synthesize import SynthesisResult, synthesize
from engram.nightly.yolo import sweep_expired_yolo
from engram.paths import engram_home
from engram.telemetry import write_json


class HarvestFunc(Protocol):
    def __call__(
        self,
        *,
        db_path: Path,
        output_root: Path,
        target_date: date | None,
        config: NightlyConfig,
    ) -> HarvestResult:
        ...


class SynthesizeFunc(Protocol):
    def __call__(
        self,
        harvest_json: Path,
        *,
        config_path: Path | None,
        output_root: Path,
        weekly: bool,
        config: NightlyConfig,
    ) -> Awaitable[SynthesisResult]:
        ...


class ApplyFunc(Protocol):
    def __call__(
        self,
        synthesis_json: Path,
        *,
        db_path: Path,
        config_path: Path,
        dry_run: bool,
        summary_trigger: str,
        clock: Callable[[], datetime],
    ) -> Awaitable[ApplyResult]:
        ...


class YoloSweepFunc(Protocol):
    def __call__(
        self,
        *,
        home: Path | None,
        config_path: Path,
        now: datetime,
    ) -> Awaitable[object]:
        ...


async def run_nightly_pipeline(
    *,
    dry_run: bool = False,
    weekly: bool = False,
    verbose: bool = False,
    target_date: date | None = None,
    db_path: Path | None = None,
    output_root: Path | None = None,
    config_path: Path = DEFAULT_CONFIG_PATH,
    clock: Callable[[], datetime] | None = None,
    harvest_func: HarvestFunc = run_harvest,
    weekly_harvest_func: HarvestFunc = run_weekly_harvest,
    synthesize_func: SynthesizeFunc = synthesize,
    apply_func: ApplyFunc = apply_synthesis,
    success_dm: SuccessDMPoster | None = None,
    yolo_sweep_func: YoloSweepFunc = sweep_expired_yolo,
) -> dict[str, Any]:
    """Run daily nightly work, optionally followed by weekly meta-synthesis."""
    clock = clock or (lambda: datetime.now(UTC))
    cfg_path = config_path.expanduser()
    root = cfg_path.parent if cfg_path.name else engram_home()
    memory_db = (db_path or root / "memory.db").expanduser()
    nightly_root = (output_root or root / "nightly").expanduser()
    cfg = load_nightly_config(cfg_path)
    run_date = target_date or clock().date()
    run_date_text = run_date.isoformat()

    cost_usd = 0.0
    channels_covered = 0

    await yolo_sweep_func(
        home=root,
        config_path=cfg_path,
        now=clock(),
    )

    _verbose_event(
        verbose,
        "nightly.harvest_started",
        phase="harvest",
        date=run_date_text,
        trigger="nightly",
        dry_run=dry_run,
    )
    daily_harvest = harvest_func(
        db_path=memory_db,
        output_root=nightly_root,
        target_date=run_date,
        config=cfg,
    )
    _verbose_event(
        verbose,
        "nightly.harvest_completed",
        phase="harvest",
        date=run_date_text,
        trigger="nightly",
        output_path=str(daily_harvest.output_path),
        channels=len(daily_harvest.payload.get("channels") or []),
        skipped_channels=len(daily_harvest.payload.get("skipped_channels") or []),
        dry_run=dry_run,
    )
    _verbose_event(
        verbose,
        "nightly.synthesis_started",
        phase="synthesis",
        date=run_date_text,
        trigger="nightly",
        dry_run=dry_run,
    )
    daily_synthesis = await synthesize_func(
        daily_harvest.output_path,
        config_path=cfg_path,
        output_root=nightly_root,
        weekly=False,
        config=cfg,
    )
    _verbose_event(
        verbose,
        "nightly.synthesis_completed",
        phase="synthesis",
        date=run_date_text,
        trigger="nightly",
        output_path=str(daily_synthesis.output_path),
        channels=len(daily_synthesis.payload.get("channels") or []),
        skipped_channels=len(daily_synthesis.payload.get("skipped_channels") or []),
        dry_run=dry_run,
    )
    _verbose_event(
        verbose,
        "nightly.apply_started",
        phase="apply",
        date=run_date_text,
        trigger="nightly",
        dry_run=dry_run,
    )
    daily_apply = await apply_func(
        daily_synthesis.output_path,
        db_path=memory_db,
        config_path=cfg_path,
        dry_run=dry_run,
        summary_trigger="nightly",
        clock=clock,
    )
    _verbose_event(
        verbose,
        "nightly.apply_completed",
        phase="apply",
        date=run_date_text,
        trigger="nightly",
        rows_written=daily_apply.rows_written,
        rows_queued=daily_apply.rows_queued,
        dry_run=dry_run,
    )
    cost_usd += _payload_cost(daily_synthesis.payload)
    channels_covered += daily_apply.rows_written
    report_artifacts = [
        ReportArtifact(
            trigger="nightly",
            harvest_path=daily_harvest.output_path,
            synthesis_path=daily_synthesis.output_path,
            rows_written=daily_apply.rows_written,
            payload=daily_synthesis.payload,
        )
    ]

    weekly_payload: dict[str, Any] | None = None
    if weekly:
        _verbose_event(
            verbose,
            "nightly.harvest_started",
            phase="weekly-harvest",
            date=run_date_text,
            trigger="nightly-weekly",
            dry_run=dry_run,
        )
        weekly_harvest = weekly_harvest_func(
            db_path=memory_db,
            output_root=nightly_root,
            target_date=run_date,
            config=cfg,
        )
        _verbose_event(
            verbose,
            "nightly.harvest_completed",
            phase="weekly-harvest",
            date=run_date_text,
            trigger="nightly-weekly",
            output_path=str(weekly_harvest.output_path),
            channels=len(weekly_harvest.payload.get("channels") or []),
            skipped_channels=len(weekly_harvest.payload.get("skipped_channels") or []),
            dry_run=dry_run,
        )
        _verbose_event(
            verbose,
            "nightly.synthesis_started",
            phase="synthesis",
            date=run_date_text,
            trigger="nightly-weekly",
            dry_run=dry_run,
        )
        weekly_synthesis = await synthesize_func(
            weekly_harvest.output_path,
            config_path=cfg_path,
            output_root=nightly_root,
            weekly=True,
            config=cfg,
        )
        _attach_weekly_source_row_ids(weekly_synthesis.payload, weekly_harvest.payload)
        write_json(weekly_synthesis.output_path, weekly_synthesis.payload)
        _verbose_event(
            verbose,
            "nightly.synthesis_completed",
            phase="synthesis",
            date=run_date_text,
            trigger="nightly-weekly",
            output_path=str(weekly_synthesis.output_path),
            channels=len(weekly_synthesis.payload.get("channels") or []),
            skipped_channels=len(weekly_synthesis.payload.get("skipped_channels") or []),
            dry_run=dry_run,
        )
        _verbose_event(
            verbose,
            "nightly.apply_started",
            phase="apply",
            date=run_date_text,
            trigger="nightly-weekly",
            dry_run=dry_run,
        )
        weekly_apply = await apply_func(
            weekly_synthesis.output_path,
            db_path=memory_db,
            config_path=cfg_path,
            dry_run=dry_run,
            summary_trigger="nightly-weekly",
            clock=clock,
        )
        _verbose_event(
            verbose,
            "nightly.apply_completed",
            phase="apply",
            date=run_date_text,
            trigger="nightly-weekly",
            rows_written=weekly_apply.rows_written,
            rows_queued=weekly_apply.rows_queued,
            dry_run=dry_run,
        )
        cost_usd += _payload_cost(weekly_synthesis.payload)
        channels_covered += weekly_apply.rows_written
        report_artifacts.append(
            ReportArtifact(
                trigger="nightly-weekly",
                harvest_path=weekly_harvest.output_path,
                synthesis_path=weekly_synthesis.output_path,
                rows_written=weekly_apply.rows_written,
                payload=weekly_synthesis.payload,
            )
        )
        weekly_payload = {
            "harvest_path": str(weekly_harvest.output_path),
            "synthesis_path": str(weekly_synthesis.output_path),
            "rows_written": weekly_apply.rows_written,
        }

    configured_success_dm = success_dm
    if configured_success_dm is None:
        async def configured_success_dm(text: str) -> None:
            await post_configured_success_dm(text, config_path=cfg_path)

    report = await write_report_and_notify(
        run_date=run_date,
        output_root=nightly_root,
        artifacts=report_artifacts,
        suppress_slack=cfg.report.suppress or dry_run,
        success_dm=configured_success_dm,
    )

    result: dict[str, Any] = {
        "cost_usd": cost_usd,
        "channels_covered": channels_covered,
        "flags": report.flag_count,
        "report_path": str(report.report_path),
        "daily": {
            "harvest_path": str(daily_harvest.output_path),
            "synthesis_path": str(daily_synthesis.output_path),
            "rows_written": daily_apply.rows_written,
        },
    }
    if weekly_payload is not None:
        result["weekly"] = weekly_payload
    return result


def _verbose_event(enabled: bool, event: str, **fields: Any) -> None:
    if not enabled:
        return
    logging.getLogger("engram.nightly").info(event, extra=fields)


def _payload_cost(payload: dict[str, Any]) -> float:
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        return 0.0
    try:
        return float(totals.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _attach_weekly_source_row_ids(
    synthesis_payload: dict[str, Any],
    harvest_payload: dict[str, object],
) -> None:
    source_ids_by_channel = {
        str(channel.get("channel_id")): [
            int(row["id"])
            for row in channel.get("rows", [])
            if isinstance(row, dict) and "id" in row
        ]
        for channel in harvest_payload.get("channels", [])
        if isinstance(channel, dict)
    }
    for channel in synthesis_payload.get("channels", []):
        if not isinstance(channel, dict) or channel.get("status") != "synthesized":
            continue
        channel_id = str(channel.get("channel_id") or "")
        source_ids = source_ids_by_channel.get(channel_id)
        synthesis = channel.get("synthesis")
        if not source_ids or not isinstance(synthesis, dict):
            continue
        synthesis["source_row_ids"] = source_ids
        channel["source_row_ids"] = source_ids
