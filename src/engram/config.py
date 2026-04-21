"""Engram config loading.

M1 scope: minimal config sufficient for a working DM round-trip.
Later milestones expand this (skill manifests, channel overrides, budget, etc.).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from engram.budget import BudgetConfig

DEFAULT_CONFIG_PATH = Path.home() / ".engram" / "config.yaml"
DEFAULT_STATE_DIR = Path.home() / ".engram" / "state"
DEFAULT_CONTEXTS_DIR = Path.home() / ".engram" / "contexts"
DEFAULT_LOG_DIR = Path.home() / ".engram" / "logs"


@dataclass
class SlackConfig:
    bot_token: str
    app_token: str
    signing_secret: str | None = None  # not required for Socket Mode


@dataclass
class AnthropicConfig:
    api_key: str
    model: str = "claude-sonnet-4-6"  # per M0 findings; updatable in config.yaml


@dataclass
class PathsConfig:
    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR)
    contexts_dir: Path = field(default_factory=lambda: DEFAULT_CONTEXTS_DIR)
    log_dir: Path = field(default_factory=lambda: DEFAULT_LOG_DIR)


@dataclass(frozen=True)
class EmbeddingsConfig:
    enabled: bool = True
    provider: str = "gemini"
    model: str = "text-embedding-004"
    dimensions: int = 768
    sample_rate_transcripts: float = 0.3
    min_transcript_tokens: int = 30
    api_timeout_s: float = 2.0
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _bool(self.enabled))
        object.__setattr__(self, "provider", str(self.provider or "gemini").lower())
        object.__setattr__(self, "model", str(self.model or "text-embedding-004"))
        object.__setattr__(self, "dimensions", max(1, int(self.dimensions or 768)))
        object.__setattr__(
            self,
            "sample_rate_transcripts",
            min(1.0, max(0.0, float(self.sample_rate_transcripts))),
        )
        object.__setattr__(
            self,
            "min_transcript_tokens",
            max(0, int(self.min_transcript_tokens or 0)),
        )
        object.__setattr__(self, "api_timeout_s", max(0.01, float(self.api_timeout_s)))

    @classmethod
    def from_mapping(cls, raw: dict | None) -> EmbeddingsConfig:
        raw = raw or {}
        return cls(
            enabled=raw.get("enabled", True),
            provider=raw.get("provider", "gemini"),
            model=raw.get("model", "text-embedding-004"),
            dimensions=raw.get("dimensions", 768),
            sample_rate_transcripts=raw.get("sample_rate_transcripts", 0.3),
            min_transcript_tokens=raw.get("min_transcript_tokens", 30),
            api_timeout_s=raw.get("api_timeout_s", 2.0),
            api_key=os.environ.get("GEMINI_API_KEY"),
        )


@dataclass
class EngramConfig:
    slack: SlackConfig
    anthropic: AnthropicConfig
    paths: PathsConfig = field(default_factory=PathsConfig)
    # M1 test surface: one channel we're allowed to respond in, plus DMs.
    # M2 keeps this for legacy/fallback mode but prefers manifest-driven
    # gating when available.
    allowed_channels: list[str] = field(default_factory=list)
    # Soft limits — M3 tightens these. For M1: just a safety cap.
    max_turns_per_message: int = 8
    # M2: DM channel that gets the owner-DM identity template on auto-
    # provision. Other DMs get task-assistant. Optional; if unset, every
    # DM is treated as task-assistant (safer default for first-run).
    owner_dm_channel_id: str | None = None
    # M3: monthly budget tracking / warning ladder.
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    # M3b: asynchronous Gemini embeddings for semantic memory recall.
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)

    @classmethod
    def load(cls, config_path: Path | None = None) -> EngramConfig:
        """Load config from YAML + environment variables.

        Precedence (low → high):
          1. config.yaml defaults
          2. explicit values in config.yaml
          3. environment variables (ENGRAM_* / SLACK_* / ANTHROPIC_API_KEY)
        """
        config_path = config_path or DEFAULT_CONFIG_PATH
        raw: dict = {}
        if config_path.exists():
            raw = yaml.safe_load(config_path.read_text()) or {}

        # Load .env files if present
        _load_env_files()

        slack_raw = raw.get("slack", {})
        slack = SlackConfig(
            bot_token=_resolve(
                slack_raw.get("bot_token"),
                "ENGRAM_SLACK_BOT_TOKEN",
                "SLACK_BOT_TOKEN",
            ),
            app_token=_resolve(
                slack_raw.get("app_token"),
                "ENGRAM_SLACK_APP_TOKEN",
                "SLACK_APP_TOKEN",
            ),
            signing_secret=_resolve_optional(
                slack_raw.get("signing_secret"),
                "ENGRAM_SLACK_SIGNING_SECRET",
                "SLACK_SIGNING_SECRET",
            ),
        )

        anth_raw = raw.get("anthropic", {})
        anthropic = AnthropicConfig(
            api_key=_resolve(
                anth_raw.get("api_key"),
                "ENGRAM_ANTHROPIC_API_KEY",
                "ANTHROPIC_API_KEY",
            ),
            model=anth_raw.get("model")
            or os.environ.get("ENGRAM_MODEL")
            or "claude-sonnet-4-6",
        )

        paths_raw = raw.get("paths", {})
        paths = PathsConfig(
            state_dir=Path(paths_raw.get("state_dir") or DEFAULT_STATE_DIR).expanduser(),
            contexts_dir=Path(paths_raw.get("contexts_dir") or DEFAULT_CONTEXTS_DIR).expanduser(),
            log_dir=Path(paths_raw.get("log_dir") or DEFAULT_LOG_DIR).expanduser(),
        )

        return cls(
            slack=slack,
            anthropic=anthropic,
            paths=paths,
            allowed_channels=list(raw.get("allowed_channels", [])),
            max_turns_per_message=int(raw.get("max_turns_per_message", 8)),
            owner_dm_channel_id=(
                raw.get("owner_dm_channel_id")
                or os.environ.get("ENGRAM_OWNER_DM_CHANNEL_ID")
            ),
            budget=BudgetConfig.from_mapping(raw.get("budget")),
            embeddings=EmbeddingsConfig.from_mapping(raw.get("embeddings")),
        )

    def ensure_dirs(self) -> None:
        """Create state/context/log dirs if missing."""
        for p in (self.paths.state_dir, self.paths.contexts_dir, self.paths.log_dir):
            p.mkdir(parents=True, exist_ok=True)


def _load_env_files() -> None:
    """Load .env files from standard locations. Silent if absent."""
    for candidate in (
        Path.cwd() / ".env",
        Path.home() / ".engram" / ".env",
        Path.home() / "code" / "_secret" / ".env",
    ):
        if candidate.exists():
            load_dotenv(candidate, override=False)


def _resolve(value: str | None, *env_keys: str) -> str:
    """Return value, or first non-empty env var. Raise if all empty."""
    if value:
        return value
    for key in env_keys:
        if val := os.environ.get(key):
            return val
    raise RuntimeError(
        f"Missing required config value. Set one of: {', '.join(env_keys)}"
    )


def _resolve_optional(value: str | None, *env_keys: str) -> str | None:
    """Like _resolve but returns None if all empty."""
    if value:
        return value
    for key in env_keys:
        if val := os.environ.get(key):
            return val
    return None


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
