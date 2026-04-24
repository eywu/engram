"""Router — maps a Slack channel/DM to a SessionState + ChannelManifest.

M1: in-memory cache, no persistence, no manifest.
M2: manifest-driven. On first sight of a channel, the router provisions a
    context directory (via `bootstrap.provision_channel`) and loads the
    resulting manifest. Team channels land as `pending` by default; the
    operator approves them via `engram channels approve`.

Backwards compatibility: calling `Router()` with no arguments still works
for tests that only exercise session caching. Manifest resolution is
off when no `home` is provided.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from engram import paths
from engram.bootstrap import apply_manifest_migrations, provision_channel
from engram.config import HITLConfig
from engram.hitl import HITLRateLimiter, HITLRegistry
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    ManifestError,
    load_manifest,
)

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient

log = logging.getLogger(__name__)

AGENT_IDLE_TIMEOUT_SECONDS = 15 * 60
AGENT_IDLE_SWEEP_INTERVAL_SECONDS = 60


def derive_session_id(channel_id: str) -> str:
    """Derive the deterministic Claude session UUID for a Slack channel."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"engram-v3/{channel_id}"))


@dataclass
class SessionState:
    """Per-channel state.

    M2 adds `manifest` + `cwd` derivation from the project root.
    M3 adds the per-channel ClaudeSDKClient and lock.
    """

    channel_id: str
    channel_name: str | None = None
    is_dm: bool = False
    cwd: Path | None = None
    session_id: str = ""
    agent_client: ClaudeSDKClient | None = None
    agent_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    agent_session_initialized: bool = False
    agent_session_tagged: bool = False
    agent_last_active_at: float = field(default_factory=time.monotonic)
    turn_count: int = 0
    manifest: ChannelManifest | None = None
    rate_limit_status: str = "allowed"
    rate_limit_reset_at: int | None = None
    rate_limit_updated_at: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = derive_session_id(self.channel_id)

    @property
    def lock(self) -> asyncio.Lock:
        """Backward-compatible alias for the per-channel agent lock."""
        return self.agent_lock

    def label(self) -> str:
        if self.channel_name:
            return f"{self.channel_name}({self.channel_id})"
        kind = "dm" if self.is_dm else "ch"
        return f"{kind}:{self.channel_id}"

    def is_active(self) -> bool:
        """Legacy (manifest-less) sessions are always active. Manifest-driven
        sessions must have ChannelStatus.ACTIVE to respond."""
        if self.manifest is None:
            return True
        return self.manifest.status == ChannelStatus.ACTIVE

    def rate_limit_state(self) -> dict[str, object]:
        return {
            "status": self.rate_limit_status,
            "reset_at": self.rate_limit_reset_at,
            "updated_at": self.rate_limit_updated_at,
        }


class Router:
    """Channel ID → SessionState.

    When `home` is provided, the router operates in manifest-driven mode:
    - First time we see a channel, provision a context directory
    - Load & cache the resulting ChannelManifest on the SessionState
    - Set `cwd` to the project root so Claude picks up project-level
      `.claude/` automatically

    When `home` is None, legacy behavior: in-memory cache, no manifest.
    """

    def __init__(
        self,
        shared_cwd: Path | None = None,
        *,
        home: Path | None = None,
        owner_dm_channel_id: str | None = None,
        template_vars: dict[str, str] | None = None,
        hitl: HITLConfig | None = None,
    ):
        self._sessions: dict[str, SessionState] = {}
        self._session_id_to_channel_id: dict[str, str] = {}
        self._shared_cwd = shared_cwd
        self._home = home
        self._owner_dm_channel_id = owner_dm_channel_id
        self._hitl_config = hitl or HITLConfig()
        # Substitutions applied when provisioning a channel's CLAUDE.md, e.g.
        # owner_display_name="Alice", slack_workspace_name="acme-corp".
        # Discovered at boot from Slack auth.test / users.info.
        self._template_vars = dict(template_vars) if template_vars else {}
        self._create_lock = asyncio.Lock()
        self._idle_sweeper_task: asyncio.Task[None] | None = None
        self.hitl = HITLRegistry()
        self.hitl_limiter = HITLRateLimiter(
            self.hitl,
            max_per_day=self._hitl_config.max_per_day,
        )

    async def get(
        self,
        channel_id: str,
        *,
        channel_name: str | None = None,
        is_dm: bool = False,
    ) -> SessionState:
        if channel_id in self._sessions:
            return self._sessions[channel_id]
        async with self._create_lock:
            if channel_id in self._sessions:
                return self._sessions[channel_id]
            session = await self._create_session(
                channel_id, channel_name=channel_name, is_dm=is_dm
            )
            self._sessions[channel_id] = session
            self._session_id_to_channel_id[session.session_id] = channel_id
            return session

    async def _create_session(
        self,
        channel_id: str,
        *,
        channel_name: str | None,
        is_dm: bool,
    ) -> SessionState:
        """Build a fresh SessionState, provisioning a manifest if applicable."""
        manifest: ChannelManifest | None = None
        cwd = self._shared_cwd

        if self._home is not None:
            # Manifest-driven mode.
            cwd = paths.project_root(self._home)

            # If a manifest already exists on disk, load it and apply any
            # idempotent bootstrap migrations.
            manifest_path = paths.channel_manifest_path(channel_id, self._home)
            if manifest_path.exists():
                try:
                    manifest = apply_manifest_migrations(
                        load_manifest(manifest_path),
                        manifest_path,
                    )
                except ManifestError:
                    log.exception(
                        "router.manifest_load_failed channel_id=%s path=%s",
                        channel_id,
                        manifest_path,
                    )
                    manifest = None
            else:
                # First time seeing this channel — auto-provision.
                identity = self._choose_identity(channel_id, is_dm=is_dm)
                label = channel_name or (
                    "DM" if is_dm else f"channel-{channel_id}"
                )
                result = provision_channel(
                    channel_id,
                    identity=identity,
                    label=label,
                    home=self._home,
                    template_vars=self._template_vars or None,
                )
                manifest = result.manifest
                log.info(
                    "router.auto_provisioned channel_id=%s identity=%s status=%s",
                    channel_id,
                    identity,
                    manifest.status,
                )

        return SessionState(
            channel_id=channel_id,
            channel_name=channel_name,
            is_dm=is_dm,
            cwd=cwd,
            manifest=manifest,
        )

    def _choose_identity(
        self, channel_id: str, *, is_dm: bool
    ) -> IdentityTemplate:
        """Pick the identity template for a brand-new channel.

        Owner DMs get full-trust identity. Everything else (including other
        DMs) gets task-assistant. DM-vs-team is the cheap heuristic; the
        operator can reclassify by editing the manifest.
        """
        if (
            is_dm
            and self._owner_dm_channel_id is not None
            and channel_id == self._owner_dm_channel_id
        ):
            return IdentityTemplate.OWNER_DM_FULL
        return IdentityTemplate.TASK_ASSISTANT

    def list_sessions(self) -> list[SessionState]:
        return list(self._sessions.values())

    @property
    def home(self) -> Path | None:
        return self._home

    @property
    def owner_dm_channel_id(self) -> str | None:
        return self._owner_dm_channel_id

    def replace_cached_manifest(self, manifest: ChannelManifest) -> None:
        session = self._sessions.get(manifest.channel_id)
        if session is not None:
            session.manifest = manifest

    def cached_manifest(self, channel_id: str) -> ChannelManifest | None:
        session = self._sessions.get(channel_id)
        return session.manifest if session is not None else None

    async def invalidate(self, channel_id: str) -> bool:
        """Drop a cached session so the next ``get`` reloads it from disk."""
        session = self._sessions.get(channel_id)
        if session is None:
            return False

        async with session.agent_lock:
            if session.agent_client is not None:
                await session.agent_client.disconnect()
                session.agent_client = None
                log.info(
                    "router.agent_client_closed_invalidate session=%s",
                    session.label(),
                )
            self._sessions.pop(channel_id, None)
            self._session_id_to_channel_id.pop(session.session_id, None)

        log.info("router.session_invalidated channel_id=%s", channel_id)
        return True

    def session_count(self) -> int:
        return len(self._sessions)

    def get_channel_by_session_id(self, session_id: str) -> str | None:
        """Return the Slack channel ID for a known Claude session ID."""
        return self._session_id_to_channel_id.get(session_id)

    def hitl_config_for_channel(
        self,
        channel_id: str,
        *,
        manifest: ChannelManifest | None = None,
    ) -> HITLConfig:
        """Return channel-specific HITL settings, falling back to global config."""
        if manifest is None:
            session = self._sessions.get(channel_id)
            manifest = session.manifest if session is not None else None

        if (
            manifest is not None
            and "hitl" in getattr(manifest, "model_fields_set", set())
        ):
            return manifest.hitl
        return self._hitl_config

    # ──────────────────────────────────────────────────────────────
    # Agent client lifecycle
    # ──────────────────────────────────────────────────────────────

    def start_idle_sweeper(
        self,
        *,
        idle_timeout_seconds: float = AGENT_IDLE_TIMEOUT_SECONDS,
        sweep_interval_seconds: float = AGENT_IDLE_SWEEP_INTERVAL_SECONDS,
    ) -> asyncio.Task[None]:
        """Start the background task that closes idle Claude clients."""
        if (
            self._idle_sweeper_task is not None
            and not self._idle_sweeper_task.done()
        ):
            return self._idle_sweeper_task

        self._idle_sweeper_task = asyncio.create_task(
            self._idle_sweeper(
                idle_timeout_seconds=idle_timeout_seconds,
                sweep_interval_seconds=sweep_interval_seconds,
            ),
            name="engram-agent-idle-sweeper",
        )
        return self._idle_sweeper_task

    async def stop_idle_sweeper(self) -> None:
        """Cancel the idle sweeper if this Router owns one."""
        if self._idle_sweeper_task is None:
            return
        self._idle_sweeper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._idle_sweeper_task
        self._idle_sweeper_task = None

    async def _idle_sweeper(
        self,
        *,
        idle_timeout_seconds: float,
        sweep_interval_seconds: float,
    ) -> None:
        while True:
            await asyncio.sleep(sweep_interval_seconds)
            await self.close_idle_agent_clients(
                idle_timeout_seconds=idle_timeout_seconds
            )

    async def close_idle_agent_clients(
        self,
        *,
        idle_timeout_seconds: float = AGENT_IDLE_TIMEOUT_SECONDS,
        now: float | None = None,
    ) -> int:
        """Close clients idle for at least `idle_timeout_seconds`.

        The per-channel agent lock is acquired before calling disconnect so an
        idle sweep cannot race an in-flight ClaudeSDKClient method call.
        """
        closed = 0
        current = time.monotonic() if now is None else now
        for session in self.list_sessions():
            if session.agent_client is None:
                continue
            if current - session.agent_last_active_at < idle_timeout_seconds:
                continue
            async with session.agent_lock:
                if session.agent_client is None:
                    continue
                check_now = time.monotonic() if now is None else now
                if (
                    check_now - session.agent_last_active_at
                    < idle_timeout_seconds
                ):
                    continue
                await session.agent_client.disconnect()
                session.agent_client = None
                closed += 1
                log.info("router.agent_client_closed_idle session=%s", session.label())
        return closed

    async def close_all_agent_clients(self) -> int:
        """Wait for in-flight turns and close every active Claude client."""
        closed = 0
        for session in self.list_sessions():
            async with session.agent_lock:
                if session.agent_client is None:
                    continue
                await session.agent_client.disconnect()
                session.agent_client = None
                closed += 1
                log.info(
                    "router.agent_client_closed_shutdown session=%s",
                    session.label(),
                )
        return closed
