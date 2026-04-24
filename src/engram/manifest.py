"""Channel manifest schema and loader.

A channel manifest declares per-channel scope: which tools, MCPs, and skills
are excluded from the project-level inheritance, plus identity, behavior,
and budget settings.

Inheritance model (decided 2026-04-20):
- Every channel inherits ALL project-level skills/tools/MCPs by default.
- Channel manifests express EXCLUSIONS via `disallowed` lists.
- The `allowed` field exists as an escape hatch for least-privilege channels
  but is not the recommended default. When `allowed` is set, it overrides
  inheritance entirely (only those entries are exposed).
- Owner-DM manifests are typically empty: full inheritance, no exclusions.

Schema is the FULL §05 spec, but only `tools`, `mcp_servers`, `skills`, and
`setting_sources` are enforced in M2. The rest (`subagents`,
`ask_user_question`, `cost_budget`, `behavior`, `hitl`) are validated and
stored for M3/M4 to consume without revisiting the schema.
"""
from __future__ import annotations

import contextlib
import copy
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from claude_agent_sdk import PermissionMode, SettingSource
from pydantic import BaseModel, Field, ValidationError, field_validator

from engram import paths
from engram.config import HITLConfig

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────
# Identity & status
# ──────────────────────────────────────────────────────────────────────────


class IdentityTemplate(StrEnum):
    """Built-in identity templates. See `templates/identity/*.md`."""

    OWNER_DM_FULL = "owner-dm-full"
    TASK_ASSISTANT = "task-assistant"


class ChannelStatus(StrEnum):
    """Lifecycle state of a channel.

    `pending` = bot has been mentioned, owner hasn't approved yet.
    `active` = owner approved; bot responds normally.
    `denied` = owner explicitly denied; bot stays silent (records mentions).
    """

    PENDING = "pending"
    ACTIVE = "active"
    DENIED = "denied"


class PermissionTier(StrEnum):
    """Channel trust tier used for default permissions and HITL posture."""

    TASK_ASSISTANT = "safe"
    OWNER_SCOPED = "trusted"
    YOLO = "yolo"

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        return _PERMISSION_TIER_ALIASES.get(normalized)


_PERMISSION_TIER_ALIASES: dict[str, PermissionTier] = {
    "safe": PermissionTier.TASK_ASSISTANT,
    "task-assistant": PermissionTier.TASK_ASSISTANT,
    "trusted": PermissionTier.OWNER_SCOPED,
    "owner-scoped": PermissionTier.OWNER_SCOPED,
    "yolo": PermissionTier.YOLO,
}
_DEPRECATED_PERMISSION_TIER_NAMES: dict[str, str] = {
    "task-assistant": PermissionTier.TASK_ASSISTANT.value,
    "owner-scoped": PermissionTier.OWNER_SCOPED.value,
}


def parse_permission_tier(
    value: PermissionTier | str,
) -> tuple[PermissionTier, str | None]:
    """Parse a tier name and return the canonical tier plus any deprecated alias."""
    if isinstance(value, PermissionTier):
        return value, None
    normalized = str(value or "").strip().lower()
    tier = PermissionTier(normalized)
    deprecated = (
        normalized if normalized in _DEPRECATED_PERMISSION_TIER_NAMES else None
    )
    return tier, deprecated


def permission_tier_choices_text() -> str:
    """Return the canonical tier list for help/usage text."""
    return "safe|trusted|yolo"


ABSOLUTE_DENY_RULES: tuple[str, ...] = (
    "Read(~/.ssh/**)",
    "Read(~/.aws/**)",
    "Read(~/.gnupg/**)",
    "Read(**/.env*)",
)

_TASK_ASSISTANT_DENY_RULES: tuple[str, ...] = (
    *ABSOLUTE_DENY_RULES,
    "Read(~/.config/**)",
    "Read(~/.zsh_history)",
    "Read(~/.bash_history)",
    "Read(~/Library/Keychains/**)",
    "Grep(~/.ssh/**)",
    "Grep(~/.aws/**)",
    "Grep(**/.env*)",
    "Glob(~/.ssh/**)",
    "Glob(~/.aws/**)",
    "Glob(**/.env*)",
)

_TIER_DEFAULTS: dict[PermissionTier, dict[str, tuple[str, ...] | int]] = {
    PermissionTier.TASK_ASSISTANT: {
        "allow_rules": (),
        "deny_rules": _TASK_ASSISTANT_DENY_RULES,
        "hitl_max_per_day": 1000,
    },
    PermissionTier.OWNER_SCOPED: {
        "allow_rules": (
            "Read",
            "Grep",
            "Glob",
            "WebFetch",
            "WebSearch",
            "TodoWrite",
        ),
        "deny_rules": ABSOLUTE_DENY_RULES,
        "hitl_max_per_day": 1000,
    },
    PermissionTier.YOLO: {
        "allow_rules": (),
        "deny_rules": ABSOLUTE_DENY_RULES,
        "hitl_max_per_day": 1000,
    },
}

OWNER_DM_DEFAULT_PERMISSION_ALLOW_RULES: tuple[str, ...] = tuple(
    _TIER_DEFAULTS[PermissionTier.OWNER_SCOPED]["allow_rules"]
)

YOLO_MIN_DURATION = timedelta(hours=6)
YOLO_DEFAULT_DURATION = timedelta(hours=24)
YOLO_MAX_DURATION = timedelta(hours=72)
YOLO_DEFAULT_DURATION_TEXT = "24h"
YOLO_DURATION_CHOICES = frozenset({"6h", "24h", "72h"})


@dataclass(frozen=True)
class YoloDemotion:
    channel_id: str
    previous_tier: PermissionTier
    effective_tier: PermissionTier
    pre_yolo_tier: PermissionTier
    expired_at: datetime | None
    duration_used: timedelta | None
    trigger: Literal["lazy", "sweep"]
    manifest: ChannelManifest


# ──────────────────────────────────────────────────────────────────────────
# Scope sub-blocks (exclusion-first model)
# ──────────────────────────────────────────────────────────────────────────


class ScopeList(BaseModel):
    """A scope block with optional allow-list (escape hatch) + disallow-list.

    Default behavior: inherit everything from project-level config; deny
    nothing. To restrict: add entries to `disallowed`. To replace
    inheritance entirely (least-privilege channel): set `allowed`.

    `allowed` and `disallowed` together is unusual but legal: `allowed`
    defines the universe, `disallowed` further filters within it.
    """

    allowed: list[str] | None = Field(
        default=None,
        description=(
            "Escape hatch: when set, ONLY these entries are exposed; "
            "project-level inheritance is disabled for this category. "
            "Leave None for inherit-all (default and recommended)."
        ),
    )
    disallowed: list[str] = Field(
        default_factory=list,
        description=(
            "Entries to exclude from inheritance. Most common use: "
            "team channels excluding Bash/Write/Edit etc."
        ),
    )

    def is_unrestricted(self) -> bool:
        """True iff this scope inherits everything with no exclusions."""
        return self.allowed is None and not self.disallowed


# ──────────────────────────────────────────────────────────────────────────
# Behavior, budget, ask-user (M2 stores; M3/M4 enforce)
# ──────────────────────────────────────────────────────────────────────────


class Behavior(BaseModel):
    """Agent runtime behavior overrides. M2 stores; agent.py reads."""

    max_turns: int | None = Field(
        default=None,
        description="Override config.max_turns_per_message for this channel.",
    )
    style: Literal["concise", "balanced", "thorough"] = "balanced"
    permission_mode: PermissionMode = "default"

    @field_validator("max_turns")
    @classmethod
    def _positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("max_turns must be >= 1")
        return v


class CostBudget(BaseModel):
    """Per-channel cost ceilings. M2 stores; M3 enforces."""

    daily_usd: float | None = Field(
        default=None, description="Max spend per UTC day. None = no limit."
    )
    monthly_usd: float | None = Field(
        default=None, description="Max spend per calendar month."
    )
    warn_at_percent: int = Field(
        default=80,
        ge=1,
        le=100,
        description="Warn owner via DM when cumulative spend hits this %.",
    )

    @field_validator("daily_usd", "monthly_usd")
    @classmethod
    def _non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("budget values must be >= 0")
        return v


class AskUserQuestion(BaseModel):
    """AskUserQuestion routing. M2 stores; M4 implements stream-watch."""

    enabled: bool = True
    fallback: Literal["ignore", "answer-as-self", "escalate-to-owner"] = (
        "escalate-to-owner"
    )


class MemoryScope(BaseModel):
    """Memory search visibility controls for this channel."""

    excluded_channels: list[str] = Field(
        default_factory=list,
        description=(
            "Slack channel IDs that memory_search must never return, even "
            "when the caller requests scope='all_channels'."
        ),
    )

    @field_validator("excluded_channels")
    @classmethod
    def _normalize_excluded_channels(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            channel_id = raw.strip()
            if not channel_id:
                raise ValueError("excluded channel IDs cannot be empty")
            if channel_id not in seen:
                normalized.append(channel_id)
                seen.add(channel_id)
        return normalized


class ChannelNightly(BaseModel):
    """Per-channel nightly synthesis overrides."""

    model: str | None = Field(
        default=None,
        description=(
            "Claude model alias or full model ID for nightly synthesis. "
            "When unset, the synthesizer falls back to global nightly.model, "
            "then owner-DM/team defaults."
        ),
    )

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


# ──────────────────────────────────────────────────────────────────────────
# Permission rules (native Claude Code `Tool(specifier)` syntax)
# ──────────────────────────────────────────────────────────────────────────

# A permission rule is either a bare tool name ("Bash", "Read") or a
# tool with a specifier ("Read(~/.ssh/**)", "Bash(git commit*)").
# We validate shape at load time to catch typos early (e.g. missing paren)
# rather than silently forwarding garbage to the CLI.
#
# Note: we do NOT evaluate the glob ourselves. That's the CLI's job, and
# doing it twice would risk semantic drift. We just sanity-check syntax.
_RULE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\([^)]*\))?$")


def _validate_rule(rule: str) -> str:
    rule = rule.strip()
    if not rule:
        raise ValueError("permission rule cannot be empty")
    if not _RULE_RE.match(rule):
        raise ValueError(
            f"invalid permission rule {rule!r}: expected "
            f"'ToolName' or 'ToolName(specifier)' "
            f"(e.g. 'Bash', 'Read(~/.ssh/**)', 'Bash(git status*)')"
        )
    # Commas inside rules would break the SDK's comma-joined CLI flag.
    # Real file paths and common bash patterns don't contain commas, so
    # this is a safe guardrail.
    if "," in rule:
        raise ValueError(
            f"invalid permission rule {rule!r}: commas are not "
            f"supported (the SDK comma-joins rules into a single CLI flag)"
        )
    return rule


class PermissionsRules(BaseModel):
    """Native Claude Code permission rules, flowed into SDK options.

    Uses the same `Tool(specifier)` syntax as Claude Code's
    `~/.claude/settings.json` — operators can copy-paste rules directly.

    Evaluation order (enforced by the CLI, not us): deny → allow.
    Deny always wins. We intentionally do NOT expose `ask` — Slack has
    no interactive prompt surface, so asking would just stall.

    Globs: `*` = single level, `**` = recursive. Path prefixes:
    - `./rel` or `rel`   → relative to cwd
    - `/rel`              → relative to project root
    - `~/path`            → operator's home
    - `//abs/path`        → absolute (note the double slash)

    Examples:
        deny:  ["Read(~/.ssh/**)", "Read(**/.env*)", "Bash(curl *)"]
        allow: ["Edit(./src/**)", "Bash(npm *)"]
    """

    deny: list[str] = Field(
        default_factory=list,
        description=(
            "Rules that block matching tool calls. Always wins over allow."
        ),
    )
    allow: list[str] = Field(
        default_factory=list,
        description=(
            "Rules that auto-approve matching tool calls. Optional; when "
            "empty, the default permission mode applies."
        ),
    )

    @field_validator("deny", "allow")
    @classmethod
    def _validate_rules(cls, rules: list[str]) -> list[str]:
        return [_validate_rule(r) for r in rules]

    def is_empty(self) -> bool:
        return not self.deny and not self.allow


# ──────────────────────────────────────────────────────────────────────────
# Top-level manifest
# ──────────────────────────────────────────────────────────────────────────


class ChannelManifest(BaseModel):
    """Full §05 channel manifest schema.

    Loaded from `contexts/<channel-id>/.claude/channel-manifest.yaml`.
    """

    # ── Identity ─────────────────────────────────────────────────
    channel_id: str = Field(description="Slack channel ID (e.g. C07ABC123).")
    identity: IdentityTemplate = Field(
        description="Which identity template renders this channel's CLAUDE.md."
    )
    label: str | None = Field(
        default=None,
        description="Human-readable name (e.g. '#growth-team'). Optional.",
    )
    status: ChannelStatus = ChannelStatus.PENDING
    acknowledged_pending: bool = Field(
        default=False,
        description=(
            "Whether Engram has already posted the one-time pending-channel "
            "acknowledgement in Slack for this channel."
        ),
    )
    meta_eligible: bool = Field(
        default=True,
        description=(
            "Whether this channel may be included in the weekly cross-channel "
            "nightly meta-summary. Defaults true per OQ31 opt-in."
        ),
    )
    permission_tier: PermissionTier = Field(
        default=PermissionTier.TASK_ASSISTANT,
        description=(
            "Trust tier controlling default permission rules and HITL limits."
        ),
    )
    yolo_until: datetime | None = Field(
        default=None,
        description=(
            "Optional expiry for a time-boxed tier upgrade. Must be timezone-aware."
        ),
    )
    yolo_granted_at: datetime | None = Field(
        default=None,
        description=(
            "Timestamp when the active YOLO window started. Used for expiry "
            "notifications and duration accounting."
        ),
    )
    pre_yolo_tier: PermissionTier | None = Field(
        default=None,
        description="Tier to restore when a temporary upgrade window expires.",
    )

    # ── Scope (exclusion-first; M2-enforced) ─────────────────────
    setting_sources: list[SettingSource] = Field(
        default_factory=lambda: ["project"],
        description=(
            "Which settings layers Claude SDK loads. Owner-DM uses "
            "['user'] for full personal MCP access; team channels use "
            "['project'] to avoid leaking personal config."
        ),
    )
    tools: ScopeList = Field(default_factory=ScopeList)
    mcp_servers: ScopeList = Field(default_factory=ScopeList)
    skills: ScopeList = Field(default_factory=ScopeList)
    permissions: PermissionsRules = Field(
        default_factory=PermissionsRules,
        description=(
            "Native Claude Code permission rules (Tool(specifier) syntax). "
            "Deny rules always win; allow rules auto-approve. Passed through "
            "to SDK's allowed_tools/disallowed_tools."
        ),
    )

    # ── Behavior, budget, ask-user (stored, not enforced in M2) ──
    behavior: Behavior = Field(default_factory=Behavior)
    cost_budget: CostBudget = Field(default_factory=CostBudget)
    ask_user_question: AskUserQuestion = Field(default_factory=AskUserQuestion)
    hitl: HITLConfig = Field(default_factory=HITLConfig)
    memory: MemoryScope = Field(default_factory=MemoryScope)
    nightly: ChannelNightly = Field(default_factory=ChannelNightly)

    # ── Sub-agents (stored only — M-future) ──────────────────────
    subagents: list[str] = Field(
        default_factory=list,
        description=(
            "Named sub-agents this channel can invoke. Stored only; "
            "wiring deferred to a future milestone."
        ),
    )

    # ── Validation ───────────────────────────────────────────────
    @field_validator("channel_id")
    @classmethod
    def _channel_id_shape(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("channel_id cannot be empty")
        # Slack channel IDs are alphanumeric, typically uppercase, prefixed
        # with C/D/G. We're lenient — workspace conventions may vary.
        return v.strip()

    @field_validator("setting_sources")
    @classmethod
    def _setting_sources_nonempty(
        cls, v: list[str]
    ) -> list[str]:
        if not v:
            raise ValueError(
                "setting_sources must have at least one entry; use ['project'] "
                "if you want minimal priming."
            )
        return v

    @field_validator("yolo_until", "yolo_granted_at")
    @classmethod
    def _yolo_timestamps_must_be_timezone_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("yolo timestamps must be timezone-aware")
        return value

    # ── Helpers ──────────────────────────────────────────────────
    def is_owner_dm(self) -> bool:
        """Deprecated thin wrapper. Prefer `tier_effective()` for policy."""
        return self.identity == IdentityTemplate.OWNER_DM_FULL

    def tier_effective(
        self, *, now: datetime | None = None
    ) -> PermissionTier:
        """Return the effective tier, lazily demoting expired temporary state."""
        if self.yolo_until is None:
            return self.permission_tier

        current_time = now or datetime.now(UTC)
        if self.yolo_until <= current_time:
            return self.pre_yolo_tier or PermissionTier.TASK_ASSISTANT
        return self.permission_tier


# ──────────────────────────────────────────────────────────────────────────
# Tier defaults + legacy migrations
# ──────────────────────────────────────────────────────────────────────────


def _default_permission_tier(
    identity: IdentityTemplate | str | None,
) -> PermissionTier:
    if identity == IdentityTemplate.OWNER_DM_FULL:
        return PermissionTier.OWNER_SCOPED
    if identity == IdentityTemplate.OWNER_DM_FULL.value:
        return PermissionTier.OWNER_SCOPED
    return PermissionTier.TASK_ASSISTANT


def _merge_rules(*groups: list[str] | tuple[str, ...]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for rule in group:
            text = str(rule).strip()
            if not text or text in seen:
                continue
            merged.append(text)
            seen.add(text)
    return merged


def _normalize_permission_tier_name(
    value: object,
) -> tuple[object, str | None, bool]:
    if value in (None, ""):
        return value, None, False
    if isinstance(value, PermissionTier):
        return value.value, None, False
    if not isinstance(value, str):
        return value, None, False

    normalized = value.strip().lower()
    try:
        tier = PermissionTier(normalized)
    except ValueError:
        return value, None, False

    deprecated = (
        normalized if normalized in _DEPRECATED_PERMISSION_TIER_NAMES else None
    )
    return tier.value, deprecated, value != tier.value


def _normalize_manifest_tier_aliases(
    raw: dict,
) -> tuple[dict, list[tuple[str, str, str]], bool]:
    data = copy.deepcopy(raw)
    migrations: list[tuple[str, str, str]] = []
    changed = False

    for field_name in ("permission_tier", "pre_yolo_tier"):
        normalized, deprecated, field_changed = _normalize_permission_tier_name(
            data.get(field_name)
        )
        if field_changed:
            data[field_name] = normalized
            changed = True
        if deprecated is not None:
            migrations.append(
                (
                    field_name,
                    deprecated,
                    str(normalized),
                )
            )

    return data, migrations, changed


def _apply_tier_defaults(
    raw: dict,
    *,
    infer_legacy_tier: bool,
) -> tuple[dict, bool]:
    data = copy.deepcopy(raw)
    changed = False

    permission_tier = data.get("permission_tier")
    if permission_tier in (None, ""):
        resolved_tier = (
            _default_permission_tier(data.get("identity"))
            if infer_legacy_tier
            else PermissionTier.TASK_ASSISTANT
        )
        data["permission_tier"] = resolved_tier.value
        permission_tier = resolved_tier.value
        changed = True

    try:
        tier = PermissionTier(permission_tier)
    except (TypeError, ValueError):
        return data, changed

    defaults = _TIER_DEFAULTS[tier]

    permissions = data.get("permissions")
    if permissions is None:
        permissions = {}
        data["permissions"] = permissions
        changed = True
    if isinstance(permissions, dict):
        allow_rules = permissions.get("allow")
        default_allow_rules = list(defaults["allow_rules"])
        if (allow_rules is None or allow_rules == []) and default_allow_rules:
            permissions["allow"] = default_allow_rules
            changed = True

        deny_rules = permissions.get("deny")
        desired_deny_rules = (
            list(defaults["deny_rules"])
            if not deny_rules
            else _merge_rules(defaults["deny_rules"], deny_rules)
        )
        if permissions.get("deny") != desired_deny_rules:
            permissions["deny"] = desired_deny_rules
            changed = True

    hitl = data.get("hitl")
    if hitl is None:
        hitl = {}
        data["hitl"] = hitl
        changed = True
    if isinstance(hitl, dict) and hitl.get("max_per_day") is None:
        hitl["max_per_day"] = int(defaults["hitl_max_per_day"])
        changed = True

    return data, changed


def _demote_expired_temporary_tier(
    manifest: ChannelManifest,
    *,
    path: Path | None = None,
) -> tuple[ChannelManifest, bool]:
    if manifest.permission_tier == PermissionTier.YOLO:
        return manifest, False
    effective_tier = manifest.tier_effective()
    if manifest.yolo_until is None:
        return manifest, False
    if effective_tier == manifest.permission_tier:
        return manifest, False

    expired_at = manifest.yolo_until
    rematerialized_raw, _ = _apply_tier_defaults(
        {
            **manifest.model_dump(mode="json"),
            "permission_tier": effective_tier.value,
            "yolo_granted_at": None,
            "yolo_until": None,
            "pre_yolo_tier": None,
        },
        infer_legacy_tier=False,
    )
    updated_manifest = ChannelManifest.model_validate(rematerialized_raw)
    log.info(
        "channel.yolo_expired channel_id=%s path=%s expired_at=%s restored_tier=%s",
        updated_manifest.channel_id,
        path,
        expired_at.isoformat() if expired_at is not None else None,
        effective_tier,
    )
    return updated_manifest, True


def expired_yolo_demotion(
    manifest: ChannelManifest,
    *,
    now: datetime | None = None,
    trigger: Literal["lazy", "sweep"],
    path: Path | None = None,
) -> YoloDemotion | None:
    current_time = now or datetime.now(UTC)
    effective_tier = manifest.tier_effective(now=current_time)
    if manifest.permission_tier != PermissionTier.YOLO:
        return None
    if effective_tier == PermissionTier.YOLO:
        return None

    expired_at = manifest.yolo_until
    pre_yolo_tier = manifest.pre_yolo_tier or PermissionTier.TASK_ASSISTANT
    duration_used: timedelta | None = None
    if expired_at is not None and manifest.yolo_granted_at is not None:
        duration_used = max(expired_at - manifest.yolo_granted_at, timedelta())

    rematerialized_raw, _ = _apply_tier_defaults(
        {
            **manifest.model_dump(mode="json"),
            "permission_tier": effective_tier.value,
            "yolo_granted_at": None,
            "yolo_until": None,
            "pre_yolo_tier": None,
        },
        infer_legacy_tier=False,
    )
    updated_manifest = ChannelManifest.model_validate(rematerialized_raw)
    log.info(
        "channel.yolo_expired channel_id=%s path=%s expired_at=%s pre_yolo_tier=%s duration_used_s=%s trigger=%s",
        manifest.channel_id,
        path,
        expired_at.isoformat() if expired_at is not None else None,
        pre_yolo_tier,
        int(duration_used.total_seconds()) if duration_used is not None else None,
        trigger,
    )
    return YoloDemotion(
        channel_id=manifest.channel_id,
        previous_tier=manifest.permission_tier,
        effective_tier=effective_tier,
        pre_yolo_tier=pre_yolo_tier,
        expired_at=expired_at,
        duration_used=duration_used,
        trigger=trigger,
        manifest=updated_manifest,
    )


# ──────────────────────────────────────────────────────────────────────────
# I/O
# ──────────────────────────────────────────────────────────────────────────


class ManifestError(Exception):
    """Raised when a manifest file is missing, malformed, or invalid."""


def load_manifest(path: Path) -> ChannelManifest:
    """Load and validate a channel manifest from YAML.

    Raises ManifestError with a friendly message on any failure.
    """
    if not path.exists():
        raise ManifestError(f"Manifest not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ManifestError(f"YAML parse error in {path}: {e}") from e

    if raw is None:
        raise ManifestError(f"Manifest is empty: {path}")
    if not isinstance(raw, dict):
        raise ManifestError(
            f"Manifest must be a YAML mapping, got {type(raw).__name__}: {path}"
        )

    try:
        normalized_raw, alias_migrations, alias_changed = (
            _normalize_manifest_tier_aliases(raw)
        )
        hydrated_raw, changed = _apply_tier_defaults(
            normalized_raw,
            infer_legacy_tier=True,
        )
        manifest = ChannelManifest.model_validate(hydrated_raw)
        manifest, yolo_changed = _demote_expired_temporary_tier(
            manifest,
            path=path,
        )
    except ValidationError as e:
        raise ManifestError(
            f"Manifest validation failed for {path}:\n{e}"
        ) from e

    if alias_migrations:
        migration_summary = ", ".join(
            f"{field_name}: {old_name} -> {new_name}"
            for field_name, old_name, new_name in alias_migrations
        )
        log.info(
            "channel.permission_tier_migrated channel_id=%s path=%s migrations=%s",
            manifest.channel_id,
            path,
            migration_summary,
        )

    if alias_changed or changed or yolo_changed:
        _write_manifest_atomic(manifest, path)
    return manifest


def dump_manifest(manifest: ChannelManifest, path: Path) -> None:
    """Write a manifest to YAML. Parent dir must exist."""
    path.write_text(_manifest_yaml(manifest))


def persist_yolo_demotion(
    path: Path,
    *,
    now: datetime | None = None,
    trigger: Literal["lazy", "sweep"],
) -> YoloDemotion | None:
    manifest = load_manifest(path)
    demotion = expired_yolo_demotion(
        manifest,
        now=now,
        trigger=trigger,
        path=path,
    )
    if demotion is None:
        return None
    dump_manifest(demotion.manifest, path)
    return demotion


def _manifest_yaml(manifest: ChannelManifest) -> str:
    """Render a manifest to stable YAML."""
    data = manifest.model_dump(mode="json", exclude_none=False)
    return yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
    )


def set_channel_status(
    channel_id: str,
    new_status: ChannelStatus,
    *,
    home: Path | None = None,
) -> tuple[ChannelManifest, ChannelManifest, Path]:
    """Load a channel manifest, update ``status``, and persist it."""
    manifest_path = paths.channel_manifest_path(channel_id, home)
    manifest = load_manifest(manifest_path)
    if manifest.status == new_status:
        return manifest, manifest, manifest_path

    updated = manifest.model_copy(update={"status": new_status})
    dump_manifest(updated, manifest_path)
    return manifest, updated, manifest_path


def _write_manifest_atomic(manifest: ChannelManifest, path: Path) -> None:
    """Atomically replace a manifest file via temp-file + rename."""
    text = _manifest_yaml(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


# Tools that must NEVER receive sticky channel-scoped allow rules, even if
# the caller (UI, webhook, bulk-import) claims they are eligible. This is the
# authoritative defense-in-depth layer: UI filters what's shown, but this set
# filters what's actually persisted. See GRO-478 review for rationale.
_STICKY_INELIGIBLE_TOOLS = frozenset({
    "Bash",
    "BashOutput",
    "KillShell",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Task",
    "SlashCommand",
})


def _assert_sticky_eligible(tool_name: str) -> None:
    """Raise ValueError if tool_name must never receive a sticky allow rule."""
    base = tool_name.split("(", 1)[0].strip()
    if base in _STICKY_INELIGIBLE_TOOLS:
        raise ValueError(
            f"refusing to persist sticky allow for ineligible tool {tool_name!r}: "
            f"{base} is in _STICKY_INELIGIBLE_TOOLS (mutating/high-risk)"
        )
    if base.startswith("mcp__"):
        raise ValueError(
            f"refusing to persist sticky allow for mcp tool {tool_name!r}: "
            f"mcp tools require per-server opt-in, not blanket sticky"
        )


def add_allow_rule(
    channel_id: str,
    tool_name: str,
    *,
    home: Path | None = None,
) -> tuple[ChannelManifest, ChannelManifest, Path]:
    """Persist a deduped ``permissions.allow`` entry for a channel manifest.

    Enforces sticky-eligibility at the persistence layer as defense-in-depth:
    if a caller somehow bypasses the UI's eligibility filter (tampered payload,
    stale button, new code path, etc.), mutating/high-risk tools are rejected
    here before they reach disk. See GRO-478 review.
    """
    _assert_sticky_eligible(tool_name)
    manifest_path = paths.channel_manifest_path(channel_id, home)
    manifest = load_manifest(manifest_path)
    normalized_rule = _validate_rule(tool_name)
    allow_rules = list(
        dict.fromkeys([*manifest.permissions.allow, normalized_rule])
    )
    if allow_rules == manifest.permissions.allow:
        return manifest, manifest, manifest_path

    updated = manifest.model_copy(
        update={
            "permissions": manifest.permissions.model_copy(
                update={"allow": allow_rules}
            )
        }
    )
    _write_manifest_atomic(updated, manifest_path)
    return manifest, updated, manifest_path


UPGRADE_DURATION_DELTAS: dict[str, timedelta] = {
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "72h": timedelta(hours=72),
    "30d": timedelta(days=30),
}
UPGRADE_DURATION_CHOICES: tuple[str, ...] = (
    "6h",
    "24h",
    "72h",
    "30d",
    "permanent",
)


def validate_upgrade_duration(duration: str) -> str:
    normalized = str(duration or "").strip().lower()
    if normalized not in UPGRADE_DURATION_CHOICES:
        raise ValueError(
            f"unsupported upgrade duration {duration!r}; "
            f"expected one of {', '.join(UPGRADE_DURATION_CHOICES)}"
        )
    return normalized


def upgrade_expires_at(
    duration: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    normalized = validate_upgrade_duration(duration)
    if normalized == "permanent":
        return None
    current_time = now or datetime.now(UTC)
    return current_time + UPGRADE_DURATION_DELTAS[normalized]


def set_channel_permission_tier(
    channel_id: str,
    new_tier: PermissionTier,
    *,
    duration: str = "permanent",
    home: Path | None = None,
    now: datetime | None = None,
) -> tuple[ChannelManifest, ChannelManifest, Path, str]:
    """Load a channel manifest, update ``permission_tier``, and persist it."""
    normalized_duration = validate_upgrade_duration(duration)
    if new_tier == PermissionTier.YOLO and normalized_duration not in YOLO_DURATION_CHOICES:
        raise ValueError(
            "yolo upgrades must use a bounded duration of 6h, 24h, or 72h"
        )
    current_time = now or datetime.now(UTC)
    manifest_path = paths.channel_manifest_path(channel_id, home)
    manifest = load_manifest(manifest_path)

    expires_at = upgrade_expires_at(normalized_duration, now=current_time)
    is_active_yolo_extension = (
        new_tier == PermissionTier.YOLO
        and manifest.permission_tier == PermissionTier.YOLO
        and manifest.yolo_until is not None
        and manifest.yolo_until > current_time
    )
    restore_tier = (
        manifest.pre_yolo_tier
        if (
            manifest.yolo_until is not None
            and manifest.yolo_until > current_time
            and manifest.permission_tier == new_tier
            and manifest.pre_yolo_tier is not None
        )
        else manifest.tier_effective(now=current_time)
    )

    update_data: dict[str, object] = {"permission_tier": new_tier}
    if expires_at is None:
        update_data["yolo_until"] = None
        update_data["yolo_granted_at"] = None
        update_data["pre_yolo_tier"] = None
    elif is_active_yolo_extension:
        update_data["yolo_until"] = manifest.yolo_until + UPGRADE_DURATION_DELTAS[normalized_duration]
        update_data["yolo_granted_at"] = manifest.yolo_granted_at or current_time
        update_data["pre_yolo_tier"] = (
            manifest.pre_yolo_tier or PermissionTier.TASK_ASSISTANT
        )
    else:
        update_data["yolo_until"] = expires_at
        update_data["yolo_granted_at"] = (
            current_time if new_tier == PermissionTier.YOLO else None
        )
        update_data["pre_yolo_tier"] = restore_tier

    updated = manifest.model_copy(update=update_data)
    if updated == manifest:
        return manifest, manifest, manifest_path, normalized_duration

    if new_tier == PermissionTier.YOLO:
        log.info(
            "channel.yolo_granted channel_id=%s duration_s=%s pre_yolo_tier=%s yolo_until=%s",
            updated.channel_id,
            int(UPGRADE_DURATION_DELTAS[normalized_duration].total_seconds()),
            updated.pre_yolo_tier,
            updated.yolo_until.isoformat() if updated.yolo_until is not None else None,
        )

    _write_manifest_atomic(updated, manifest_path)
    return manifest, updated, manifest_path, normalized_duration
