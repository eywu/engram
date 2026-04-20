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

from engram import __version__
from engram.agent import Agent
from engram.config import EngramConfig
from engram.ingress import register_listeners
from engram.router import Router


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

    app = AsyncApp(token=config.slack.bot_token)
    router = Router(shared_cwd=config.paths.state_dir)
    agent = Agent(config)
    register_listeners(app, config, router, agent)

    handler = AsyncSocketModeHandler(app, config.slack.app_token)

    stop_event = asyncio.Event()

    def _graceful(*_):
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
    await handler.start_async()

    # Wait for signal.
    await stop_event.wait()

    log.info("engram.shutting_down")
    await handler.close_async()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
