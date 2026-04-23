"""Nightly entrypoint observability.

GRO-437 owns the process wrapper: dedicated JSONL logs, heartbeat state, and
failure-path owner DMs. The synthesis implementation plugs into the injectable
``synthesize`` phase when M5 nightly behavior lands.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from engram.config import EngramConfig
from engram.paths import engram_home, log_dir, nightly_heartbeat_path
from engram.telemetry import configure_logging, write_json

NightlyPhase = Callable[[], Awaitable[dict[str, Any] | None]]
FailureDMPoster = Callable[[str], Awaitable[None]]

SYNTHESIZE_PHASE = "synthesize"


@dataclass(frozen=True)
class NightlyRunResult:
    exit_code: int
    heartbeat: dict[str, Any]
    heartbeat_path: Path
    log_path: Path


def nightly_log_path(
    logs_dir: Path | None = None,
    *,
    now: datetime.datetime | None = None,
) -> Path:
    ts = now or datetime.datetime.now(datetime.UTC)
    return (logs_dir or log_dir()) / f"nightly-{ts.astimezone(datetime.UTC).date().isoformat()}.jsonl"


def format_failure_dm(*, phase: str | None, exit_code: int | None, log_path: Path) -> str:
    phase_text = phase or "unknown"
    exit_text = exit_code if exit_code is not None else "unknown"
    return (
        f"⚠️ Engram nightly FAILED at phase={phase_text}, exit={exit_text}. "
        f"Logs: `{log_path}`."
    )


async def run_configured_nightly() -> NightlyRunResult:
    return await run_nightly(failure_dm=post_configured_failure_dm)


async def run_nightly(
    *,
    synthesize: NightlyPhase | None = None,
    home: Path | None = None,
    logs_dir: Path | None = None,
    failure_dm: FailureDMPoster | None = None,
    now: Callable[[], datetime.datetime] | None = None,
) -> NightlyRunResult:
    clock = now or (lambda: datetime.datetime.now(datetime.UTC))
    root = engram_home(home)
    logs = logs_dir or log_dir(root)
    configure_logging(logs, force=True, file_prefix="nightly")
    log = logging.getLogger("engram.nightly")

    heartbeat_path = nightly_heartbeat_path(root)
    log_path = nightly_log_path(logs, now=clock())
    heartbeat: dict[str, Any] = {
        "started_at": _iso(clock()),
        "completed_at": None,
        "phase_reached": None,
        "exit_code": None,
        "cost_usd": 0.0,
        "channels_covered": 0,
        "error_msg": None,
    }
    write_json(heartbeat_path, heartbeat)
    log.info("nightly.started heartbeat=%s log_path=%s", heartbeat_path, log_path)

    exit_code = 0
    try:
        heartbeat["phase_reached"] = SYNTHESIZE_PHASE
        write_json(heartbeat_path, heartbeat)
        log.info("nightly.phase_started phase=%s", SYNTHESIZE_PHASE)

        phase_result = await (synthesize or _default_synthesize)()
        if phase_result:
            heartbeat["cost_usd"] = float(phase_result.get("cost_usd") or 0.0)
            heartbeat["channels_covered"] = int(phase_result.get("channels_covered") or 0)

        heartbeat["exit_code"] = 0
        heartbeat["completed_at"] = _iso(clock())
        log.info(
            "nightly.completed phase=%s cost_usd=%s channels_covered=%s",
            heartbeat["phase_reached"],
            heartbeat["cost_usd"],
            heartbeat["channels_covered"],
        )
    except Exception as exc:
        exit_code = 1
        heartbeat["exit_code"] = exit_code
        heartbeat["error_msg"] = f"{type(exc).__name__}: {exc}"
        log.exception("nightly.failed phase=%s", heartbeat["phase_reached"])
    finally:
        if heartbeat["exit_code"] is None:
            heartbeat["exit_code"] = exit_code
        write_json(heartbeat_path, heartbeat)
        if heartbeat["completed_at"] is None:
            text = format_failure_dm(
                phase=heartbeat.get("phase_reached"),
                exit_code=heartbeat.get("exit_code"),
                log_path=log_path,
            )
            if failure_dm is not None:
                try:
                    await failure_dm(text)
                    log.info("nightly.failure_dm_posted")
                except Exception:
                    log.warning("nightly.failure_dm_failed", exc_info=True)

    return NightlyRunResult(
        exit_code=exit_code,
        heartbeat=heartbeat,
        heartbeat_path=heartbeat_path,
        log_path=log_path,
    )


async def post_configured_failure_dm(text: str) -> None:
    try:
        config = EngramConfig.load()
    except RuntimeError:
        logging.getLogger("engram.nightly").warning(
            "nightly.failure_dm_dropped reason=config_error",
            exc_info=True,
        )
        return

    if not config.owner_dm_channel_id:
        logging.getLogger("engram.nightly").warning(
            "nightly.failure_dm_dropped reason=no_owner_dm"
        )
        return

    client = AsyncWebClient(token=config.slack.bot_token)
    await client.chat_postMessage(channel=config.owner_dm_channel_id, text=text)


async def _default_synthesize() -> dict[str, Any]:
    logging.getLogger("engram.nightly").info(
        "nightly.synthesize_noop reason=phase_not_implemented"
    )
    return {"cost_usd": 0.0, "channels_covered": 0}


def _iso(value: datetime.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).isoformat()


def main() -> None:
    result = asyncio.run(run_configured_nightly())
    sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
