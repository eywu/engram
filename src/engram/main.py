"""Engram bridge entrypoint.

Starts a Bolt AsyncApp in Socket Mode, wires router + agent, blocks forever.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from engram import __version__, paths
from engram.agent import Agent
from engram.bootstrap import ensure_project_root
from engram.budget import Budget
from engram.config import EngramConfig
from engram.costs import CostLedger
from engram.egress import post_question, update_question_timeout
from engram.embeddings import EmbeddingQueue, GeminiEmbedder
from engram.hitl import PendingQuestion
from engram.ingress import register_listeners
from engram.router import Router
from engram.runtime import (
    fd_usage_snapshot,
    prune_fd_snapshot_files,
    write_fd_snapshot,
    write_runtime_snapshot,
)
from engram.telemetry import configure_logging

READY_LOG_LINE = "engram.ready"  # stable string for health probes / test harnesses
_TIMEOUT_UPDATE_TASKS: set[asyncio.Task[None]] = set()


def _configure_logging() -> None:
    configure_logging()


def _validate_owner_approval_config(config: EngramConfig, log: logging.Logger) -> None:
    if not config.owner_dm_channel_id:
        log.warning("engram.owner_approval_config_missing field=owner_dm_channel_id")
    elif not config.owner_dm_channel_id.startswith("D"):
        log.warning(
            "engram.owner_approval_config_invalid field=owner_dm_channel_id value=%s",
            config.owner_dm_channel_id,
        )

    if not config.owner_user_id:
        log.warning("engram.owner_approval_config_missing field=owner_user_id")
    elif not config.owner_user_id.startswith("U"):
        log.warning(
            "engram.owner_approval_config_invalid field=owner_user_id value=%s",
            config.owner_user_id,
        )


@dataclass
class _FdAlertTierState:
    consecutive: int = 0
    active: bool = False


@dataclass
class _RuntimeSnapshotLoopState:
    warn: _FdAlertTierState = field(default_factory=_FdAlertTierState)
    critical: _FdAlertTierState = field(default_factory=_FdAlertTierState)
    next_fd_snapshot_at: float = 0.0
    fd_high_water: dict[str, Any] | None = None


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
            channel_info = info.get("channel")
            user_id = channel_info.get("user") if isinstance(channel_info, dict) else None
            if user_id:
                u = await app.client.users_info(user=user_id)
                user_info = u.get("user")
                profile = user_info if isinstance(user_info, dict) else {}
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
    _validate_owner_approval_config(config, log)

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
    if config.observability.fd_snapshots_enabled:
        try:
            removed = prune_fd_snapshot_files(log_dir=config.paths.log_dir)
        except Exception:
            log.warning("engram.fd_snapshot_prune_failed", exc_info=True)
        else:
            log.info("engram.fd_snapshot_pruned removed=%s", removed)

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
        hitl=config.hitl,
    )
    budget = Budget(config.budget, db_path=engram_home / "cost.db")
    cost_ledger = CostLedger(
        config.paths.log_dir / "costs.jsonl",
        db_path=budget.db_path,
    )
    embedder = GeminiEmbedder(config.embeddings)
    embedding_queue = EmbeddingQueue(
        embedder,
        db_path=engram_home / "memory.db",
    )
    embedding_worker_task = (
        asyncio.create_task(
            embedding_queue.run(),
            name="engram-embedding-queue",
        )
        if embedder.enabled
        else None
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
        router=router,
        embedder=embedder,
        embedding_queue=embedding_queue,
    )

    async def on_new_question_for_channel(q: PendingQuestion) -> None:
        try:
            channel_ts, thread_ts = await post_question(q, app.client)
            q.slack_channel_ts = channel_ts
            q.slack_thread_ts = thread_ts
            _schedule_timeout_update(q, app.client)
        except Exception as e:
            log.exception("Failed to post HITL question: %s", e)
            raise

    agent._on_new_question = on_new_question_for_channel  # type: ignore[attr-defined]
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
            owner_alert=_owner_alert,
            log_dir=config.paths.log_dir,
            fd_snapshots_enabled=config.observability.fd_snapshots_enabled,
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
        if embedding_worker_task is not None:
            await embedding_queue.drain()
            embedding_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await idle_sweeper_task
        with contextlib.suppress(asyncio.CancelledError):
            await runtime_snapshot_task
        if embedding_worker_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await embedding_worker_task

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
    owner_alert: Callable[[str], Awaitable[None]] | None = None,
    log_dir: Path | None = None,
    fd_snapshots_enabled: bool = True,
    fd_snapshot_interval_seconds: float = 3600.0,
    fd_high_water_sample_interval_seconds: float = 0.25,
    fd_usage_reader: Callable[[], dict[str, int | None] | None] = fd_usage_snapshot,
) -> None:
    loop_log = logging.getLogger("engram.main")
    state = _RuntimeSnapshotLoopState(
        next_fd_snapshot_at=asyncio.get_running_loop().time()
    )
    while True:
        await _sample_fd_high_water_for_interval(
            state,
            duration_seconds=interval_seconds,
            sample_interval_seconds=fd_high_water_sample_interval_seconds,
            usage_reader=fd_usage_reader,
        )
        try:
            current_fd_usage = fd_usage_reader()
            fd_high_water = _consume_fd_high_water(state, current_fd_usage)
            snapshot = await write_runtime_snapshot(
                state_dir=state_dir,
                router=router,
                cost_db=cost_db,
                fd_usage=current_fd_usage,
                fd_high_water=fd_high_water,
            )
            bridge = snapshot.get("bridge", {})
            fd_usage = bridge.get("fds")
            if isinstance(fd_usage, dict):
                await _maybe_alert_on_fd_pressure(
                    fd_usage,
                    pid=bridge.get("pid"),
                    owner_alert=owner_alert,
                    state=state,
                )
            if fd_snapshots_enabled and log_dir is not None:
                now = asyncio.get_running_loop().time()
                if now >= state.next_fd_snapshot_at:
                    try:
                        # `lsof` can stall under FD pressure; keep the bridge loop responsive.
                        await asyncio.to_thread(write_fd_snapshot, log_dir=log_dir)
                    except Exception:
                        loop_log.warning("engram.fd_snapshot_write_failed", exc_info=True)
                    finally:
                        state.next_fd_snapshot_at = now + fd_snapshot_interval_seconds
        except Exception:
            loop_log.warning(
                "engram.runtime_snapshot_failed",
                exc_info=True,
            )


async def _sample_fd_high_water_for_interval(
    state: _RuntimeSnapshotLoopState,
    *,
    duration_seconds: float,
    sample_interval_seconds: float,
    usage_reader: Callable[[], dict[str, int | None] | None],
) -> None:
    if duration_seconds <= 0:
        return
    remaining = duration_seconds
    while remaining > 0:
        delay = min(sample_interval_seconds, remaining) if sample_interval_seconds > 0 else remaining
        await asyncio.sleep(delay)
        remaining = max(0.0, remaining - delay)
        _observe_fd_high_water(state, usage_reader())


def _observe_fd_high_water(
    state: _RuntimeSnapshotLoopState,
    fd_usage: dict[str, int | None] | None,
) -> None:
    if fd_usage is None:
        return
    in_use = fd_usage.get("in_use")
    if in_use is None:
        return
    observed_at = datetime.datetime.now(datetime.UTC).isoformat()
    current = state.fd_high_water
    if current is None:
        state.fd_high_water = {
            "in_use": in_use,
            "soft_limit": fd_usage.get("soft_limit"),
            "hard_limit": fd_usage.get("hard_limit"),
            "window_started_at": observed_at,
            "observed_at": observed_at,
        }
        return
    if current.get("window_started_at") is None:
        current["window_started_at"] = observed_at
    if current.get("in_use") is None or int(in_use) >= int(current["in_use"]):
        current["in_use"] = in_use
        current["soft_limit"] = fd_usage.get("soft_limit")
        current["hard_limit"] = fd_usage.get("hard_limit")
        current["observed_at"] = observed_at


def _consume_fd_high_water(
    state: _RuntimeSnapshotLoopState,
    current_fd_usage: dict[str, int | None] | None,
) -> dict[str, Any] | None:
    _observe_fd_high_water(state, current_fd_usage)
    high_water = state.fd_high_water
    state.fd_high_water = None
    return dict(high_water) if high_water is not None else None


async def _maybe_alert_on_fd_pressure(
    fd_usage: dict[str, int | None],
    *,
    pid: object,
    owner_alert: Callable[[str], Awaitable[None]] | None,
    state: _RuntimeSnapshotLoopState,
) -> None:
    in_use = fd_usage.get("in_use")
    if in_use is None:
        return

    warn_threshold, critical_threshold = _fd_pressure_thresholds(fd_usage.get("soft_limit"))
    warn_ready = _update_fd_alert_tier(state.warn, in_use >= warn_threshold)
    critical_ready = _update_fd_alert_tier(state.critical, in_use >= critical_threshold)
    if critical_ready:
        state.warn.active = True
        state.warn.consecutive = max(state.warn.consecutive, 2)
        message = _critical_fd_pressure_alert(in_use, fd_usage.get("soft_limit"))
    elif warn_ready:
        message = _warn_fd_pressure_alert(in_use, fd_usage.get("soft_limit"), pid)
    else:
        return

    if owner_alert is None:
        return
    try:
        await owner_alert(message)
    except Exception:
        logging.getLogger("engram.main").warning(
            "engram.fd_pressure_alert_failed",
            exc_info=True,
        )


def _fd_pressure_thresholds(soft_limit: int | None) -> tuple[int, int]:
    # Keep the default 150/200 thresholds for normal bridge deployments,
    # including launchd contexts with NumberOfFiles=4096. GRO-481's leak grew to
    # 245 FDs before EMFILE; percentage-based thresholds at 4096 would alert far
    # too late to catch that class of leak. Only scale down for genuinely small
    # per-process limits where 150/200 would already exceed available headroom.
    if soft_limit is not None and 0 < soft_limit < 256:
        return max(1, (soft_limit * 3 + 4) // 5), max(1, (soft_limit * 4 + 4) // 5)
    return 150, 200


def _update_fd_alert_tier(state: _FdAlertTierState, over_threshold: bool) -> bool:
    if not over_threshold:
        state.consecutive = 0
        state.active = False
        return False
    state.consecutive += 1
    if state.consecutive >= 2 and not state.active:
        state.active = True
        return True
    return False


def _warn_fd_pressure_alert(in_use: int, soft_limit: int | None, pid: object) -> str:
    limit = soft_limit if soft_limit is not None else "unknown"
    pid_text = pid if pid is not None else "PID"
    return (
        f"⚠️ Engram FD pressure: {in_use} / {limit} in use. "
        "Monotonic growth may indicate a resource leak.\n"
        f"Run `lsof -p {pid_text}` to inspect. "
        "See GRO-481 for prior incident pattern."
    )


def _critical_fd_pressure_alert(in_use: int, soft_limit: int | None) -> str:
    limit = soft_limit if soft_limit is not None else "unknown"
    return (
        f"🚨 Engram FD pressure CRITICAL: {in_use} / {limit} in use. "
        "Bridge may soon fail to bootstrap new channels or reconnect to Slack.\n"
        "Recommended: `launchctl kickstart -k gui/$(id -u)/com.engram.bridge` "
        "to recover, then investigate."
    )


def _schedule_timeout_update(q: PendingQuestion, slack_client) -> None:
    """Update the Slack question if asyncio.wait_for cancels it on timeout."""

    def update_if_timed_out(future: asyncio.Future[Any]) -> None:
        if not future.cancelled():
            return
        task = asyncio.create_task(update_question_timeout(q, slack_client))
        _TIMEOUT_UPDATE_TASKS.add(task)
        task.add_done_callback(_TIMEOUT_UPDATE_TASKS.discard)

    q.future.add_done_callback(update_if_timed_out)


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
