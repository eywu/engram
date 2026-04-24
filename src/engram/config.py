"""Engram config loading.

M1 scope: minimal config sufficient for a working DM round-trip.
Later milestones expand this (skill manifests, channel overrides, budget, etc.).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # type: ignore[import-untyped]
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
class HITLConfig:
    enabled: bool = True
    timeout_s: int = 300
    max_per_day: int = 1000

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _bool(self.enabled))
        object.__setattr__(self, "timeout_s", max(0, int(self.timeout_s)))
        object.__setattr__(self, "max_per_day", max(0, int(self.max_per_day)))

    @classmethod
    def from_mapping(cls, raw: dict | None) -> HITLConfig:
        raw = raw or {}
        return cls(
            enabled=raw.get("enabled", True),
            timeout_s=raw.get("timeout_s", 300),
            max_per_day=raw.get("max_per_day", 1000),
        )


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
        # api_key precedence: explicit YAML value (for the setup wizard's
        # write path) > GEMINI_API_KEY env var (for users who prefer env
        # management). None means semantic memory is keyword-only; FTS5
        # still works, and embeddings.py logs
        # "embeddings.disabled reason=missing_api_key" at startup.
        api_key = raw.get("api_key") or os.environ.get("GEMINI_API_KEY")
        return cls(
            enabled=raw.get("enabled", True),
            provider=raw.get("provider", "gemini"),
            model=raw.get("model", "text-embedding-004"),
            dimensions=raw.get("dimensions", 768),
            sample_rate_transcripts=raw.get("sample_rate_transcripts", 0.3),
            min_transcript_tokens=raw.get("min_transcript_tokens", 30),
            api_timeout_s=raw.get("api_timeout_s", 2.0),
            api_key=api_key,
        )


@dataclass(frozen=True)
class NightlyReportConfig:
    suppress: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "suppress", _bool(self.suppress))

    @classmethod
    def from_mapping(cls, raw: dict | None) -> NightlyReportConfig:
        raw = raw or {}
        return cls(suppress=raw.get("suppress", False))


@dataclass(frozen=True)
class NightlyConfig:
    dedup_overlap: float = 0.85
    min_evidence: int = 10
    max_tokens_per_channel: int = 100_000
    excluded_channels: tuple[str, ...] = field(default_factory=tuple)
    model: str | None = None
    daily_cost_cap_usd: float = 10.0
    report: NightlyReportConfig = field(default_factory=NightlyReportConfig)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dedup_overlap",
            min(1.0, max(0.0, float(self.dedup_overlap))),
        )
        object.__setattr__(self, "min_evidence", max(0, int(self.min_evidence)))
        object.__setattr__(
            self,
            "max_tokens_per_channel",
            max(1, int(self.max_tokens_per_channel)),
        )
        object.__setattr__(
            self,
            "excluded_channels",
            tuple(_string_list(self.excluded_channels)),
        )
        object.__setattr__(self, "model", _optional_string(self.model))
        object.__setattr__(
            self,
            "daily_cost_cap_usd",
            max(0.0, float(self.daily_cost_cap_usd)),
        )
        report = self.report
        if not isinstance(report, NightlyReportConfig):
            report = NightlyReportConfig.from_mapping(report if isinstance(report, dict) else None)
        object.__setattr__(self, "report", report)

    @classmethod
    def from_mapping(cls, raw: dict | None) -> NightlyConfig:
        raw = raw or {}
        return cls(
            dedup_overlap=raw.get("dedup_overlap", 0.85),
            min_evidence=raw.get("min_evidence", 10),
            max_tokens_per_channel=raw.get("max_tokens_per_channel", 100_000),
            excluded_channels=tuple(_string_list(raw.get("excluded_channels"))),
            model=raw.get("model"),
            daily_cost_cap_usd=raw.get("daily_cost_cap_usd", 10.0),
            report=NightlyReportConfig.from_mapping(raw.get("report")),
        )


def load_nightly_config(config_path: Path | None = None) -> NightlyConfig:
    """Load just the ``nightly`` section without requiring Slack/Claude secrets."""
    config_path = config_path or DEFAULT_CONFIG_PATH
    raw: dict = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text()) or {}
    return NightlyConfig.from_mapping(raw.get("nightly"))


@dataclass(frozen=True)
class ObservabilityConfig:
    fd_snapshots_enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "fd_snapshots_enabled",
            _bool(self.fd_snapshots_enabled),
        )

    @classmethod
    def from_mapping(cls, raw: dict | None) -> ObservabilityConfig:
        raw = raw or {}
        return cls(fd_snapshots_enabled=raw.get("fd_snapshots_enabled", True))


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
    # M4: human-in-the-loop defaults for permission prompts.
    hitl: HITLConfig = field(default_factory=HITLConfig)
    # M3b: asynchronous Gemini embeddings for semantic memory recall.
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    # M5: offline nightly memory jobs.
    nightly: NightlyConfig = field(default_factory=NightlyConfig)
    # M5b: runtime observability toggles.
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

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
            hitl=HITLConfig.from_mapping(raw.get("hitl")),
            embeddings=EmbeddingsConfig.from_mapping(raw.get("embeddings")),
            nightly=NightlyConfig.from_mapping(raw.get("nightly")),
            observability=ObservabilityConfig.from_mapping(raw.get("observability")),
        )

    def ensure_dirs(self) -> None:
        """Create state/context/log dirs if missing."""
        for p in (self.paths.state_dir, self.paths.contexts_dir, self.paths.log_dir):
            p.mkdir(parents=True, exist_ok=True)


def _load_env_files() -> None:
    """Load .env files from standard locations. Silent if absent.

    Precedence (first match wins for any given key, because override=False):
      1. $ENGRAM_ENV_FILE        — explicit override for users who keep
                                   their secrets in a non-standard location
      2. ./.env                  — project-local override
      3. ~/.engram/.env          — user-scoped default
    """
    candidates: list[Path] = []
    override = os.environ.get("ENGRAM_ENV_FILE")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path.home() / ".engram" / ".env")
    for candidate in candidates:
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


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list | tuple | set):
        values = list(value)
    else:
        values = []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        channel_id = str(item).strip()
        if channel_id and channel_id not in seen:
            normalized.append(channel_id)
            seen.add(channel_id)
    return normalized


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
