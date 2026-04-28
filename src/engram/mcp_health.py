"""MCP health primitives for runtime turn-level circuit breaking.

GRO-555: When an MCP server fails repeatedly during a turn, Engram needs to
disable it to break the reconnect-loop amplification that previously caused
egress truncation (see GRO-555 investigation comment for details).

This module provides two primitives the Agent uses inside ``_run_sdk_turn_once``:

* ``disable_failed_mcps_pre_turn`` — at the start of a turn, snapshot
  ``client.get_mcp_status()`` and disable any server that is already in
  ``failed`` / ``needs-auth`` state, so the model never tries to call its
  tools during the turn. Disabled servers are recorded in
  ``session.disabled_mcp_servers`` so the runtime status snapshot's
  reconnect helper (``runtime._reconnect_failed_mcp_servers``) won't keep
  retrying them either.

* ``McpHealthWatchdog`` — runs as a background task during the turn,
  polling ``client.get_mcp_status()`` on a fixed cadence. If the same
  server reports ``failed`` / ``needs-auth`` for ``threshold`` consecutive
  polls, it disables the server via ``client.toggle_mcp_server`` and
  enqueues a user-visible warning string the agent splices into
  ``text_chunks``. Polling (rather than sniffing SDK system messages) is
  used because the CLI's MCP-error subtype names are not part of the SDK
  contract and have shifted between releases.

The deliberate non-goals are documented in GRO-555:

* We do NOT call ``client.disconnect()`` between turns — session continuity
  is required for memory.
* We do NOT replace the SDK's subprocess management; we use its public
  control primitives (``get_mcp_status``, ``toggle_mcp_server``).
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Default: after 3 reconnect failures of the same MCP server in a single
# turn, circuit-break it. Picked to be tolerant of a brief transient (1-2
# failures during normal MCP startup) while still firing well before the
# 16-second-cadence reconnect storm in GRO-538 ran for 9 minutes.
DEFAULT_RECONNECT_FAIL_THRESHOLD = 3
# Default poll cadence for the watchdog. 5s is fast enough to catch the
# 16s-cadence reconnect storm seen in GRO-538 within ~15s, and slow enough
# to add negligible load to the SDK control channel.
DEFAULT_WATCHDOG_POLL_INTERVAL_S = 5.0


class _SupportsMcpControl(Protocol):
    """Subset of ``ClaudeSDKClient`` we depend on. Lets tests pass fakes."""

    async def get_mcp_status(self) -> Any: ...
    async def toggle_mcp_server(self, server_name: str, enabled: bool) -> None: ...


_FAILED_STATUSES = frozenset({"failed", "needs-auth"})


def _extract_servers(mcp_status: Any) -> list[dict[str, Any]]:
    """Tolerate the SDK's two response shapes.

    The SDK returns a ``McpStatusResponse`` TypedDict-shaped value with key
    ``mcpServers``. In test fakes a plain ``dict`` is common. In some SDK
    paths it's an object with ``model_dump()``. Be defensive.
    """
    raw = mcp_status
    if raw is None:
        return []
    if hasattr(raw, "model_dump"):
        try:
            raw = raw.model_dump(mode="python")
        except TypeError:  # pragma: no cover — defensive only
            raw = raw.model_dump()
    if not isinstance(raw, dict):
        return []
    servers = raw.get("mcpServers") or raw.get("mcp_servers") or []
    if not isinstance(servers, list):
        return []
    out: list[dict[str, Any]] = []
    for s in servers or []:
        if isinstance(s, dict):
            out.append(s)
    return out


@dataclass
class PreTurnDisableOutcome:
    """What ``disable_failed_mcps_pre_turn`` did, for telemetry / tests."""

    disabled: list[str]
    skipped_already_disabled: list[str]
    healthy: list[str]
    error: str | None = None


async def disable_failed_mcps_pre_turn(
    client: _SupportsMcpControl,
    *,
    session_label: str,
    already_disabled: set[str],
) -> PreTurnDisableOutcome:
    """Inspect MCP status and disable any failed server before the turn runs.

    Mutates ``already_disabled`` by adding newly-disabled server names. The
    caller (Agent) holds the per-channel ``agent_lock`` so the set is safe
    to mutate without further locking.
    """
    try:
        mcp_status = await client.get_mcp_status()
    except Exception as exc:
        # Non-fatal: log and proceed. The watchdog will catch in-turn issues.
        log.warning(
            "mcp_health.pre_turn_status_failed session=%s error_class=%s",
            session_label,
            type(exc).__name__,
            exc_info=True,
        )
        return PreTurnDisableOutcome(
            disabled=[],
            skipped_already_disabled=[],
            healthy=[],
            error=f"{type(exc).__name__}: {exc}",
        )

    disabled_now: list[str] = []
    skipped: list[str] = []
    healthy: list[str] = []

    for server in _extract_servers(mcp_status):
        name = server.get("name")
        status = server.get("status")
        if not isinstance(name, str) or not name:
            continue
        if status in _FAILED_STATUSES:
            if name in already_disabled:
                skipped.append(name)
                continue
            try:
                await client.toggle_mcp_server(name, False)
            except Exception:
                log.warning(
                    "mcp_health.pre_turn_disable_failed session=%s server=%s",
                    session_label,
                    name,
                    exc_info=True,
                )
                continue
            already_disabled.add(name)
            disabled_now.append(name)
            log.info(
                "runtime.mcp_pre_turn_disabled session=%s server=%s status=%s",
                session_label,
                name,
                status,
            )
        elif status == "disabled":
            # Server was disabled out-of-band (e.g. owner toggled it off).
            # Track it so we don't try to re-enable it implicitly.
            if name not in already_disabled:
                already_disabled.add(name)
                skipped.append(name)
        else:
            healthy.append(name)

    return PreTurnDisableOutcome(
        disabled=disabled_now,
        skipped_already_disabled=skipped,
        healthy=healthy,
    )


@dataclass
class WatchdogTrip:
    """Records a single circuit-breaker trip for telemetry."""

    server: str
    consecutive_failures: int


class McpHealthWatchdog:
    """Per-turn circuit breaker for MCP servers stuck in failure loops.

    Runs as a background asyncio task during the turn. Polls
    ``client.get_mcp_status()`` every ``poll_interval_s`` seconds. For each
    server, tracks consecutive polls that report ``failed`` or
    ``needs-auth``. When a server hits ``threshold`` consecutive failed
    polls, the watchdog disables it via ``toggle_mcp_server`` and queues a
    user-visible warning string for the agent to splice into
    ``text_chunks`` once the turn completes.

    A successful poll (status ``connected``, ``pending``, etc.) resets the
    failure counter for that server, so brief transients during normal
    startup don't trip the breaker.
    """

    def __init__(
        self,
        client: _SupportsMcpControl,
        *,
        session_label: str,
        already_disabled: set[str],
        threshold: int = DEFAULT_RECONNECT_FAIL_THRESHOLD,
        poll_interval_s: float = DEFAULT_WATCHDOG_POLL_INTERVAL_S,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be > 0")
        self._client = client
        self._session_label = session_label
        self._already_disabled = already_disabled
        self._threshold = threshold
        self._poll_interval_s = poll_interval_s
        self._consecutive_failures: Counter[str] = Counter()
        self.trips: list[WatchdogTrip] = []
        self.warnings: list[str] = []

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def poll_interval_s(self) -> float:
        return self._poll_interval_s

    def failures_for(self, server: str) -> int:
        return self._consecutive_failures[server]

    async def run(self, sleep: Any = None) -> None:
        """Run the polling loop until cancelled.

        ``sleep`` lets tests inject a deterministic clock; defaults to
        ``asyncio.sleep``. The loop swallows per-poll exceptions so a
        transient SDK glitch never tears down the watchdog.
        """
        if sleep is None:
            import asyncio as _asyncio
            sleep = _asyncio.sleep
        try:
            while True:
                await sleep(self._poll_interval_s)
                await self._poll_once()
        except Exception:  # pragma: no cover — should be CancelledError only
            log.debug(
                "mcp_health.watchdog_loop_exited session=%s",
                self._session_label,
                exc_info=True,
            )
            raise

    async def _poll_once(self) -> None:
        try:
            mcp_status = await self._client.get_mcp_status()
        except Exception:
            log.debug(
                "mcp_health.watchdog_poll_failed session=%s",
                self._session_label,
                exc_info=True,
            )
            return

        seen: set[str] = set()
        for server in _extract_servers(mcp_status):
            name = server.get("name")
            status = server.get("status")
            if not isinstance(name, str) or not name:
                continue
            seen.add(name)
            if name in self._already_disabled:
                continue
            if status in _FAILED_STATUSES:
                self._consecutive_failures[name] += 1
                count = self._consecutive_failures[name]
                if count >= self._threshold:
                    await self._trip(name, count)
            else:
                # Healthy / pending / connected → reset counter.
                if name in self._consecutive_failures:
                    del self._consecutive_failures[name]

        # Servers that vanished from the status list (rare; happens if
        # toggle_mcp_server removes them) shouldn't keep their counters.
        stale = [s for s in self._consecutive_failures if s not in seen]
        for s in stale:
            del self._consecutive_failures[s]

    async def _trip(self, server: str, count: int) -> None:
        try:
            await self._client.toggle_mcp_server(server, False)
        except Exception:
            log.warning(
                "mcp_health.watchdog_disable_failed session=%s server=%s "
                "consecutive_failures=%d",
                self._session_label,
                server,
                count,
                exc_info=True,
            )
            return

        self._already_disabled.add(server)
        self.trips.append(
            WatchdogTrip(server=server, consecutive_failures=count)
        )
        warning = (
            f"\n\n⚠️ Tool from `{server}` MCP is failing repeatedly — "
            f"disabled for the rest of this turn. Run `engram doctor` for "
            f"diagnostics."
        )
        self.warnings.append(warning)
        log.warning(
            "runtime.mcp_watchdog_disabled session=%s server=%s "
            "consecutive_failures=%d",
            self._session_label,
            server,
            count,
        )


def warning_chunk_for_pre_turn(disabled: Iterable[str]) -> str | None:
    """User-visible warning when pre-turn disabled one or more MCPs.

    Kept narrow: only emits text the *first* time a server is disabled in a
    given turn. Repeated disables across turns are silent (the model just
    doesn't see the tool).
    """
    names = sorted({n for n in disabled if n})
    if not names:
        return None
    if len(names) == 1:
        return (
            f"\n\n⚠️ MCP `{names[0]}` is unhealthy — its tools are unavailable "
            f"for this turn. Run `engram doctor` for diagnostics."
        )
    pretty = ", ".join(f"`{n}`" for n in names)
    return (
        f"\n\n⚠️ MCPs {pretty} are unhealthy — their tools are unavailable "
        f"for this turn. Run `engram doctor` for diagnostics."
    )
