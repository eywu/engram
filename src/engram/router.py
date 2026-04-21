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
import logging
from dataclasses import dataclass, field
from pathlib import Path

from engram import paths
from engram.bootstrap import provision_channel
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    ManifestError,
    load_manifest,
)

log = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Per-channel state.

    M2 adds `manifest` + `cwd` derivation from the project root.
    """

    channel_id: str
    channel_name: str | None = None
    is_dm: bool = False
    cwd: Path | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_count: int = 0
    manifest: ChannelManifest | None = None

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
    ):
        self._sessions: dict[str, SessionState] = {}
        self._shared_cwd = shared_cwd
        self._home = home
        self._owner_dm_channel_id = owner_dm_channel_id
        self._create_lock = asyncio.Lock()

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

            # If a manifest already exists on disk, load it unchanged.
            manifest_path = paths.channel_manifest_path(channel_id, self._home)
            if manifest_path.exists():
                try:
                    manifest = load_manifest(manifest_path)
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

    def session_count(self) -> int:
        return len(self._sessions)
