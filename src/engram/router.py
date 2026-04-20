"""Router — maps a Slack channel/DM to a SessionState.

M1: in-memory only. No persistence. Each channel gets a fresh SessionState
on process start; lost on restart.

M2 replaces this with manifest-driven SessionState creation and adds
persistence via `routing-state.json`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SessionState:
    """Per-channel state. M1 keeps it minimal."""

    channel_id: str
    channel_name: str | None = None
    # Whether this is a DM (True) or a team/group channel (False).
    is_dm: bool = False
    # Working directory for this session's Claude invocations. In M1,
    # a single shared workspace; in M2, per-channel directories.
    cwd: Path | None = None
    # Lock to serialize turns within a single channel so rapid-fire
    # messages don't race to launch concurrent SDK queries.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Running message-turn counter (useful for logs, not persisted in M1).
    turn_count: int = 0

    def label(self) -> str:
        """Short human-readable label for logs."""
        if self.channel_name:
            return f"{self.channel_name}({self.channel_id})"
        kind = "dm" if self.is_dm else "ch"
        return f"{kind}:{self.channel_id}"


class Router:
    """Channel ID → SessionState.

    M1: resolves on demand, caches in memory. Not persistent.
    """

    def __init__(self, shared_cwd: Path | None = None):
        self._sessions: dict[str, SessionState] = {}
        self._shared_cwd = shared_cwd
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
            # Re-check after acquiring lock
            if channel_id in self._sessions:
                return self._sessions[channel_id]
            session = SessionState(
                channel_id=channel_id,
                channel_name=channel_name,
                is_dm=is_dm,
                cwd=self._shared_cwd,
            )
            self._sessions[channel_id] = session
            return session

    def list_sessions(self) -> list[SessionState]:
        return list(self._sessions.values())

    def session_count(self) -> int:
        return len(self._sessions)
