"""Engram bridge entrypoint.

Starts a Bolt AsyncApp in Socket Mode, wires router + agent, blocks forever.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from engram import __version__, paths
from engram.agent import Agent
from engram.bootstrap import ensure_project_root
from engram.budget import Budget
from engram.config import EngramConfig
from engram.costs import CostLedger
from engram.ingress import register_listeners
from engram.router import Router
from engram.runtime import write_runtime_snapshot
from engram.telemetry import configure_logging

READY_LOG_LINE = "engram.ready"  # stable string for health probes / test harnesses


def _configure_logging() -> None:
    configure_logging()


async def _discover_template_vars(
    app: AsyncApp,
    owner_dm_channel_id: str | None,
    log: logging.Logger,
) -> dict[str, str]:
    """Query Slack for workspace + owner display name at boot.

    Returns a dict suitable for bootstrap's `_render_identity_md` — missing
    keys fall back to the generic defaults ("the operator", "this workspace").
    Best-effort: any Slack API error is logged and swallowed.
    """
    vars_: dict[str, str] = {}
    try:
        auth = await app.client.auth_test()
        team_name = auth.get("team")
        if team_name:
            vars_["slack_workspace_name"] = team_name
            log.info("engram.discovered slack_workspace_name=%s", team_name)
    except Exception as e:
        log.warning(
            "engram.discover_workspace_failed %s: %s", type(e).__name__, e
        )

    if owner_dm_channel_id:
        try:
            info = await app.client.conversations_info(
                channel=owner_dm_channel_id
            )
            user_id = info.get("channel", {}).get("user")
            if user_id:
                u = await app.client.users_info(user=user_id)
                profile = u.get("user", {})
                display = (
                    profile.get("real_name")
                    or profile.get("name")
                )
                if display:
                    vars_["owner_display_name"] = display
                    log.info(
                        "engram.discovered owner_display_name=%s user_id=%s",
                        display,
                        user_id,
                    )
        except Exception as e:
            log.warning(
                "engram.discover_owner_failed %s: %s", type(e).__name__, e
            )

    return vars_


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
    configure_logging(config.paths.log_dir, force=True)
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

    # Discover workspace + owner display name from Slack so CLAUDE.md
    # templates render with real identity (name / workspace) instead of the
    # generic fallbacks ("the operator" / "this workspace"). Best-effort:
    # any Slack API failure falls back to the defaults in
    # bootstrap._render_identity_md.
    template_vars = await _discover_template_vars(
        app, config.owner_dm_channel_id, log
    )

    router = Router(
        shared_cwd=project_root_path,
        home=engram_home,
        owner_dm_channel_id=config.owner_dm_channel_id,
        template_vars=template_vars,
    )
    budget = Budget(config.budget, db_path=engram_home / "cost.db")
    cost_ledger = CostLedger(
        config.paths.log_dir / "costs.jsonl",
        db_path=budget.db_path,
    )

    async def _owner_alert(text: str) -> None:
        if not config.owner_dm_channel_id:
            log.warning("engram.owner_alert_dropped reason=no_owner_dm text=%s", text)
            return
        try:
            await app.client.chat_postMessage(
                channel=config.owner_dm_channel_id,
                text=text,
            )
        except Exception:
            log.warning("engram.owner_alert_failed", exc_info=True)

    agent = Agent(
        config,
        budget=budget,
        owner_alert=_owner_alert,
        cost_db=cost_ledger.db,
    )
    register_listeners(app, config, router, agent, cost_ledger=cost_ledger)
    idle_sweeper_task = router.start_idle_sweeper()

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
    await write_runtime_snapshot(
        state_dir=config.paths.state_dir,
        router=router,
        cost_db=cost_ledger.db,
    )
    runtime_snapshot_task = asyncio.create_task(
        _runtime_snapshot_loop(
            state_dir=config.paths.state_dir,
            router=router,
            cost_db=cost_ledger.db,
        ),
        name="engram-runtime-snapshot",
    )

    # Block until SIGTERM / SIGINT.
    await stop_event.wait()

    log.info("engram.shutting_down")
    try:
        # close_async() tears down the WebSocket + background tasks. Wrap in a
        # timeout so a stuck socket never keeps the process from exiting.
        await asyncio.wait_for(handler.close_async(), timeout=5.0)
    except (TimeoutError, Exception) as e:
        log.warning("engram.shutdown_close_failed %s: %s", type(e).__name__, e)
    finally:
        idle_sweeper_task.cancel()
        runtime_snapshot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await idle_sweeper_task
        with contextlib.suppress(asyncio.CancelledError):
            await runtime_snapshot_task

        try:
            await router.close_all_agent_clients()
        except (TimeoutError, Exception) as e:
            log.warning(
                "engram.shutdown_agent_close_failed %s: %s",
                type(e).__name__,
                e,
            )
    log.info("engram.shutdown_complete")
    return 0


async def _runtime_snapshot_loop(
    *,
    state_dir,
    router: Router,
    cost_db,
    interval_seconds: float = 15.0,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await write_runtime_snapshot(
                state_dir=state_dir,
                router=router,
                cost_db=cost_db,
            )
        except Exception:
            logging.getLogger("engram.main").warning(
                "engram.runtime_snapshot_failed",
                exc_info=True,
            )


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
