"""Filesystem layout helpers for Engram.

Single source of truth for where things live on disk:

    ~/.engram/
      config.yaml           # operator-edited top-level config
      project/              # project-level inheritance root (PROJECT LAYER)
        .claude/
          SOUL.md
          AGENTS.md
          skills/
          mcp.json          # (optional; created by setup_wizard / user)
      contexts/             # per-channel directories (CHANNEL LAYER)
        <channel-id>/
          .claude/
            CLAUDE.md                  # rendered from identity template
            channel-manifest.yaml      # ChannelManifest
            memory/                    # channel's persistent notes
      state/                # cost ledger, routing state, etc.
      logs/                 # structured logs
      nightly/              # nightly heartbeat / run state
"""
from __future__ import annotations

from pathlib import Path

# Package-bundled templates (read-only). Living inside src/engram/templates/
# means they ship with the wheel and are discoverable via __file__ even
# when engram is installed from PyPI.
_PKG_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = _PKG_ROOT / "templates"
TEMPLATES_PROJECT_DIR = TEMPLATES_DIR / "project"
TEMPLATES_IDENTITY_DIR = TEMPLATES_DIR / "identity"
TEMPLATES_MANIFESTS_DIR = TEMPLATES_DIR / "manifests"


def engram_home(override: Path | None = None) -> Path:
    """Root of the Engram runtime tree. `~/.engram` by default."""
    return override or (Path.home() / ".engram")


def project_root(home: Path | None = None) -> Path:
    """Project-level inheritance directory.

    Working directory used when we spawn Claude for any channel that doesn't
    override `cwd` in its manifest. The Claude SDK walks upward from `cwd`
    discovering `.claude/` — so pointing `cwd` at this directory gives every
    channel the project-level skills/MCPs/CLAUDE for free.
    """
    return engram_home(home) / "project"


def project_claude_dir(home: Path | None = None) -> Path:
    return project_root(home) / ".claude"


def contexts_dir(home: Path | None = None) -> Path:
    return engram_home(home) / "contexts"


def channel_dir(channel_id: str, home: Path | None = None) -> Path:
    """Directory for a single channel's context."""
    return contexts_dir(home) / channel_id


def channel_claude_dir(channel_id: str, home: Path | None = None) -> Path:
    return channel_dir(channel_id, home) / ".claude"


def channel_manifest_path(channel_id: str, home: Path | None = None) -> Path:
    return channel_claude_dir(channel_id, home) / "channel-manifest.yaml"


def channel_claude_md_path(channel_id: str, home: Path | None = None) -> Path:
    return channel_claude_dir(channel_id, home) / "CLAUDE.md"


def channel_memory_dir(channel_id: str, home: Path | None = None) -> Path:
    return channel_claude_dir(channel_id, home) / "memory"


def state_dir(home: Path | None = None) -> Path:
    return engram_home(home) / "state"


def new_session_requests_dir(home: Path | None = None) -> Path:
    return state_dir(home) / "new-session-requests"


def new_session_request_path(channel_id: str, home: Path | None = None) -> Path:
    return new_session_requests_dir(home) / f"{channel_id}.json"


def log_dir(home: Path | None = None) -> Path:
    return engram_home(home) / "logs"


def nightly_dir(home: Path | None = None) -> Path:
    return engram_home(home) / "nightly"


def nightly_heartbeat_path(home: Path | None = None) -> Path:
    return nightly_dir(home) / "last-run.json"
