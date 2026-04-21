"""Engram bridge entrypoint.

Starts a Bolt AsyncApp in Socket Mode, wires router + agent, blocks forever.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from engram import __version__, paths
from engram.agent import Agent
from engram.bootstrap import ensure_project_root
from engram.config import EngramConfig
from engram.costs import CostLedger
from engram.ingress import register_listeners
from engram.router import Router

READY_LOG_LINE = "engram.ready"  # stable string for health probes / test harnesses


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


async def run() -> int:
    _configure_logging()
    log = logging.getLogger("engram.main")
    log.info("engram.boot version=%s", __version__)

    try:
        config = EngramConfig.load()
    except RuntimeError as e:
        log.error("engram.config_error %s", e)
        print(f"\nConfig error: {e}\n", file=sys.stderr)
        print("Run `engram setup` to configure, or set the missing env vars.", file=sys.stderr)
        return 2

    config.ensure_dirs()
    log.info(
        "engram.config_loaded model=%s allowed_channels=%d state_dir=%s",
        config.anthropic.model,
        len(config.allowed_channels),
        config.paths.state_dir,
    )

    # M2: seed the project-level inheritance layer (~/.engram/project/.claude/)
    # before the router resolves any channels. Idempotent — preserves operator
    # edits to SOUL.md / AGENTS.md / skills/.
    engram_home = paths.engram_home()
    project_root_path = ensure_project_root(home=engram_home)
    log.info(
        "engram.project_root_ready path=%s owner_dm=%s",
        project_root_path,
        config.owner_dm_channel_id or "(unset)",
    )

    app = AsyncApp(token=config.slack.bot_token)
    router = Router(
        shared_cwd=project_root_path,
        home=engram_home,
        owner_dm_channel_id=config.owner_dm_channel_id,
    )
    agent = Agent(config)
    cost_ledger = CostLedger(config.paths.log_dir / "costs.jsonl")
    register_listeners(app, config, router, agent, cost_ledger=cost_ledger)

    handler = AsyncSocketModeHandler(app, config.slack.app_token)

    stop_event = asyncio.Event()

    def _graceful(*_):
        if not stop_event.is_set():
            log.info("engram.signal_received initiating_shutdown")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _graceful)
        except NotImplementedError:
            # Windows / some embedded loops
            signal.signal(sig, _graceful)

    log.info("engram.starting socket_mode=True")
    # Use connect_async (returns after WS handshake) instead of start_async
    # (which blocks on asyncio.sleep(inf)); the stop_event below is what
    # keeps us alive until a signal arrives.
    await handler.connect_async()
    log.info("engram.ready")

    # Block until SIGTERM / SIGINT.
    await stop_event.wait()

    log.info("engram.shutting_down")
    try:
        # close_async() tears down the WebSocket + background tasks. Wrap in a
        # timeout so a stuck socket never keeps the process from exiting.
        await asyncio.wait_for(handler.close_async(), timeout=5.0)
    except (TimeoutError, Exception) as e:
        log.warning("engram.shutdown_close_failed %s: %s", type(e).__name__, e)
    log.info("engram.shutdown_complete")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
