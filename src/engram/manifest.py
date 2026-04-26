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
import hashlib
import json
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
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from engram import paths
from engram.config import HITLConfig
from engram.permissions.authorization import classify_transition

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
        "hitl_max_per_day": 3,
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

_TIER_DEFAULT_NIGHTLY_INCLUDED: dict[PermissionTier, bool] = {
    PermissionTier.TASK_ASSISTANT: False,
    PermissionTier.OWNER_SCOPED: True,
    PermissionTier.YOLO: True,
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


@dataclass(frozen=True)
class MCPManifestChangePlan:
    """Staged manifest edit that adds new entries to ``mcp_servers.allowed``."""

    manifest_path: Path
    current_manifest: ChannelManifest
    staged_manifest: ChannelManifest
    staged_text: str
    baseline_sha256: str
    additions: list[str]


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
    nightly_included: bool = Field(
        default=True,
        description=(
            "Whether this channel may be included in the nightly cross-channel "
            "summary. Safe-tier channels are always excluded."
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

    @model_validator(mode="after")
    def _apply_nightly_included_policy(self) -> ChannelManifest:
        if "nightly_included" not in self.model_fields_set:
            self.nightly_included = _TIER_DEFAULT_NIGHTLY_INCLUDED[
                self.permission_tier
            ]
        if (
            self.permission_tier == PermissionTier.TASK_ASSISTANT
            and self.nightly_included
        ):
            self.nightly_included = False
        return self

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


def _tier_default_allow_rules(tier: PermissionTier) -> list[str]:
    return list(_TIER_DEFAULTS[tier]["allow_rules"])


def _tier_default_deny_rules(tier: PermissionTier) -> list[str]:
    return list(_TIER_DEFAULTS[tier]["deny_rules"])


def _tier_default_hitl_max_per_day(tier: PermissionTier) -> int:
    return int(_TIER_DEFAULTS[tier]["hitl_max_per_day"])


def _infer_tier_drift_source(
    data: dict,
    *,
    current_tier: PermissionTier,
) -> PermissionTier | None:
    hitl = data.get("hitl")
    if not isinstance(hitl, dict):
        return None

    max_per_day = hitl.get("max_per_day")
    current_default = _tier_default_hitl_max_per_day(current_tier)
    if max_per_day is None or max_per_day == current_default:
        return None

    permissions = data.get("permissions")
    scored_candidates: list[tuple[int, PermissionTier]] = []
    for candidate in PermissionTier:
        if candidate == current_tier:
            continue
        if max_per_day != _tier_default_hitl_max_per_day(candidate):
            continue

        score = 1
        if isinstance(permissions, dict):
            if permissions.get("allow") == _tier_default_allow_rules(candidate):
                score += 1
            if permissions.get("deny") == _tier_default_deny_rules(candidate):
                score += 1
        scored_candidates.append((score, candidate))

    if not scored_candidates:
        return None

    scored_candidates.sort(
        key=lambda item: (
            item[0],
            item[1].value,
        )
    )
    return scored_candidates[-1][1]


def _record_tier_drift_correction(
    *,
    channel_id: str | None,
    tier: PermissionTier,
    field: str,
    previous_value: object,
    updated_value: object,
) -> None:
    log.info(
        "manifest.tier_drift_corrected channel_id=%s tier=%s field=%s from=%s to=%s",
        channel_id,
        tier.value,
        field,
        previous_value,
        updated_value,
    )


def _apply_tier_defaults(
    raw: dict,
    *,
    infer_legacy_tier: bool,
    previous_tier: PermissionTier | None = None,
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

    inferred_previous_tier = (
        previous_tier
        if previous_tier is not None
        else _infer_tier_drift_source(data, current_tier=tier)
    )

    if "nightly_included" not in data:
        if "meta_eligible" in data:
            data["nightly_included"] = data.pop("meta_eligible")
        else:
            data["nightly_included"] = _TIER_DEFAULT_NIGHTLY_INCLUDED[tier]
        changed = True
    if (
        tier == PermissionTier.TASK_ASSISTANT
        and data.get("nightly_included") is not False
    ):
        data["nightly_included"] = False
        changed = True

    default_allow_rules = _tier_default_allow_rules(tier)
    default_deny_rules = _tier_default_deny_rules(tier)
    default_hitl_max_per_day = _tier_default_hitl_max_per_day(tier)
    previous_allow_rules = (
        _tier_default_allow_rules(inferred_previous_tier)
        if inferred_previous_tier is not None
        else None
    )
    previous_deny_rules = (
        _tier_default_deny_rules(inferred_previous_tier)
        if inferred_previous_tier is not None
        else None
    )
    previous_hitl_max_per_day = (
        _tier_default_hitl_max_per_day(inferred_previous_tier)
        if inferred_previous_tier is not None
        else None
    )
    log_drift_corrections = (
        previous_tier is None and inferred_previous_tier is not None
    )
    channel_id = str(data.get("channel_id") or "")

    permissions = data.get("permissions")
    if permissions is None:
        permissions = {}
        data["permissions"] = permissions
        changed = True
    if isinstance(permissions, dict):
        allow_rules = permissions.get("allow")
        if (allow_rules is None or allow_rules == []) and default_allow_rules:
            permissions["allow"] = default_allow_rules
            changed = True
        elif (
            previous_allow_rules is not None
            and allow_rules == previous_allow_rules
            and allow_rules != default_allow_rules
        ):
            permissions["allow"] = default_allow_rules
            changed = True
            if log_drift_corrections:
                _record_tier_drift_correction(
                    channel_id=channel_id,
                    tier=tier,
                    field="permissions.allow",
                    previous_value=allow_rules,
                    updated_value=default_allow_rules,
                )

        deny_rules = permissions.get("deny")
        desired_deny_rules = default_deny_rules
        if deny_rules and not (
            previous_deny_rules is not None and deny_rules == previous_deny_rules
        ):
            desired_deny_rules = _merge_rules(default_deny_rules, deny_rules)
        if permissions.get("deny") != desired_deny_rules:
            permissions["deny"] = desired_deny_rules
            changed = True
            if (
                log_drift_corrections
                and previous_deny_rules is not None
                and deny_rules == previous_deny_rules
            ):
                _record_tier_drift_correction(
                    channel_id=channel_id,
                    tier=tier,
                    field="permissions.deny",
                    previous_value=deny_rules,
                    updated_value=desired_deny_rules,
                )

    hitl = data.get("hitl")
    if hitl is None:
        hitl = {}
        data["hitl"] = hitl
        changed = True
    if isinstance(hitl, dict):
        max_per_day = hitl.get("max_per_day")
        if max_per_day is None:
            hitl["max_per_day"] = default_hitl_max_per_day
            changed = True
        elif (
            previous_hitl_max_per_day is not None
            and max_per_day == previous_hitl_max_per_day
            and max_per_day != default_hitl_max_per_day
        ):
            hitl["max_per_day"] = default_hitl_max_per_day
            changed = True
            if log_drift_corrections:
                _record_tier_drift_correction(
                    channel_id=channel_id,
                    tier=tier,
                    field="hitl.max_per_day",
                    previous_value=max_per_day,
                    updated_value=default_hitl_max_per_day,
                )

    return data, changed


def _rematerialize_manifest(
    manifest: ChannelManifest,
    *,
    update_data: dict[str, object],
    previous_tier: PermissionTier | None = None,
) -> ChannelManifest:
    staged_manifest = manifest.model_copy(update=update_data)
    rematerialized_raw, _ = _apply_tier_defaults(
        staged_manifest.model_dump(mode="json"),
        infer_legacy_tier=False,
        previous_tier=previous_tier or manifest.permission_tier,
    )
    return ChannelManifest.model_validate(rematerialized_raw)


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
    updated_manifest = _rematerialize_manifest(
        manifest,
        update_data={
            "permission_tier": effective_tier,
            "yolo_granted_at": None,
            "yolo_until": None,
            "pre_yolo_tier": None,
        },
        previous_tier=manifest.permission_tier,
    )
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

    updated_manifest = _rematerialize_manifest(
        manifest,
        update_data={
            "permission_tier": effective_tier,
            "yolo_granted_at": None,
            "yolo_until": None,
            "pre_yolo_tier": None,
        },
        previous_tier=manifest.permission_tier,
    )
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


def detect_mcp_allow_list_additions(
    tool_name: str,
    tool_input: dict[str, object],
    *,
    manifest_path: Path,
    cwd: Path | None = None,
) -> MCPManifestChangePlan | None:
    """Return a staged manifest diff when a tool adds new allowed MCP servers."""
    target_path = _tool_target_path(tool_input, cwd)
    if target_path is None or target_path != manifest_path.resolve():
        return None

    current_text = manifest_path.read_text(encoding="utf-8")
    staged_text = _apply_manifest_tool_edit(tool_name, tool_input, current_text)
    if staged_text is None:
        return None

    current_manifest = load_manifest(manifest_path)
    staged_manifest = _manifest_from_text(staged_text, manifest_path)
    additions = _mcp_allow_list_additions(current_manifest, staged_manifest)
    if not additions:
        return None

    return MCPManifestChangePlan(
        manifest_path=manifest_path,
        current_manifest=current_manifest,
        staged_manifest=staged_manifest,
        staged_text=staged_text,
        baseline_sha256=_sha256_text(current_text),
        additions=additions,
    )


def persist_approved_mcp_manifest_change(
    plan: MCPManifestChangePlan,
) -> tuple[ChannelManifest, ChannelManifest, Path]:
    """Persist an approved staged MCP allow-list change.

    If the manifest changed since the approval request was generated, merge the
    newly approved MCP names into the latest manifest instead of overwriting.
    """
    current_text = plan.manifest_path.read_text(encoding="utf-8")
    current_manifest = load_manifest(plan.manifest_path)
    if _sha256_text(current_text) == plan.baseline_sha256:
        updated_manifest = plan.staged_manifest
    else:
        merged_allowed = list(
            dict.fromkeys(
                [
                    *(current_manifest.mcp_servers.allowed or []),
                    *plan.additions,
                ]
            )
        )
        updated_manifest = current_manifest.model_copy(
            update={
                "mcp_servers": current_manifest.mcp_servers.model_copy(
                    update={"allowed": merged_allowed}
                )
            }
        )
        log.info(
            "manifest.mcp_allow_merge_after_stale_approval channel_id=%s path=%s additions=%s",
            current_manifest.channel_id,
            plan.manifest_path,
            plan.additions,
        )
    previous_manifest, persisted_manifest, persisted_path = _persist_manifest_update(
        updated_manifest,
        plan.manifest_path,
        approved_mcp_additions=plan.additions,
        audit_source="approved_addition",
    )
    return previous_manifest or current_manifest, persisted_manifest, persisted_path


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
    _audit_existing_mcp_allow_list_once(manifest, path)
    return manifest


def dump_manifest(manifest: ChannelManifest, path: Path) -> None:
    """Write a manifest to YAML, enforcing MCP allow-list approval.

    New entries in ``mcp_servers.allowed`` on an existing manifest must flow
    through ``persist_approved_mcp_manifest_change`` so the trust gate has
    already resolved and approved the additions before they reach disk.
    """
    _persist_manifest_update(manifest, path)


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


def _manifest_from_text(text: str, path: Path) -> ChannelManifest:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"YAML parse error in staged manifest {path}: {exc}") from exc
    if raw is None:
        raise ManifestError(f"Staged manifest is empty: {path}")
    if not isinstance(raw, dict):
        raise ManifestError(
            f"Staged manifest must be a YAML mapping, got {type(raw).__name__}: {path}"
        )
    normalized_raw, _migrations, _alias_changed = _normalize_manifest_tier_aliases(raw)
    hydrated_raw, _changed = _apply_tier_defaults(
        normalized_raw,
        infer_legacy_tier=True,
    )
    try:
        manifest = ChannelManifest.model_validate(hydrated_raw)
    except ValidationError as exc:
        raise ManifestError(f"Staged manifest validation failed for {path}:\n{exc}") from exc
    manifest, _ = _demote_expired_temporary_tier(manifest, path=path)
    return manifest


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


def _tool_target_path(
    tool_input: dict[str, object],
    cwd: Path | None,
) -> Path | None:
    raw_path = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        base = cwd or Path.cwd()
        candidate = (base / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return candidate


def _apply_manifest_tool_edit(
    tool_name: str,
    tool_input: dict[str, object],
    current_text: str,
) -> str | None:
    if tool_name == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            raise ManifestError("Write to channel manifest is missing string content")
        return content
    if tool_name == "Edit":
        return _apply_single_text_edit(current_text, tool_input)
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            raise ManifestError("MultiEdit for channel manifest is missing edits list")
        updated = current_text
        for edit in edits:
            if not isinstance(edit, dict):
                raise ManifestError("MultiEdit for channel manifest has non-mapping edit")
            updated = _apply_single_text_edit(updated, edit)
        return updated
    return None


def _apply_single_text_edit(current_text: str, edit: dict[str, object]) -> str:
    old_string = edit.get("old_string")
    new_string = edit.get("new_string")
    replace_all = bool(edit.get("replace_all"))
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        raise ManifestError("manifest edit requires string old_string and new_string")
    if old_string not in current_text:
        raise ManifestError("manifest edit old_string was not found in current manifest")
    if replace_all:
        return current_text.replace(old_string, new_string)
    return current_text.replace(old_string, new_string, 1)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mcp_allow_list_additions(
    current_manifest: ChannelManifest | None,
    staged_manifest: ChannelManifest,
) -> list[str]:
    current_allowed = (
        set(current_manifest.mcp_servers.allowed or [])
        if current_manifest is not None
        else set()
    )
    staged_allowed = list(staged_manifest.mcp_servers.allowed or [])
    return [name for name in staged_allowed if name not in current_allowed]


def _persist_manifest_update(
    manifest: ChannelManifest,
    path: Path,
    *,
    approved_mcp_additions: list[str] | None = None,
    audit_source: str | None = None,
) -> tuple[ChannelManifest | None, ChannelManifest, Path]:
    """Persist a manifest while enforcing the MCP allow-list trust gate.

    All public manifest writers should funnel through this helper. If an
    existing manifest gains new names in ``mcp_servers.allowed``, callers must
    provide the approved additions from the trust-gated flow; otherwise the
    write is rejected before it reaches disk.
    """
    previous_manifest = load_manifest(path) if path.exists() else None
    additions: list[str] = []
    if previous_manifest is not None:
        additions = _mcp_allow_list_additions(previous_manifest, manifest)
        approved = list(dict.fromkeys(approved_mcp_additions or []))
        unexpected = [name for name in additions if name not in approved]
        if unexpected:
            raise ManifestError(
                "Blocked manifest write: new MCP allow-list entries require "
                "trust-gated approval via persist_approved_mcp_manifest_change "
                f"(attempted additions: {unexpected})."
            )
    _write_manifest_atomic(manifest, path)
    if additions:
        _record_mcp_allow_list_audit(
            manifest,
            path,
            source=audit_source or "approved_addition",
            event_name="manifest.mcp_allow_list_addition_approved",
            additions=additions,
        )
    return previous_manifest, manifest, path


def _audit_existing_mcp_allow_list_once(
    manifest: ChannelManifest,
    path: Path,
) -> None:
    allowed = list(manifest.mcp_servers.allowed or [])
    if not allowed:
        return
    _record_mcp_allow_list_audit(
        manifest,
        path,
        source="legacy_grandfathered",
        event_name="manifest.mcp_allow_list_grandfathered",
        additions=allowed,
        only_if_absent=True,
    )


def _record_mcp_allow_list_audit(
    manifest: ChannelManifest,
    path: Path,
    *,
    source: str,
    event_name: str,
    additions: list[str],
    only_if_absent: bool = False,
) -> None:
    audit_path = _mcp_allow_audit_path(path)
    if audit_path is None:
        return
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object]
    if audit_path.exists():
        try:
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}
    audited_channels = payload.setdefault("audited_channels", {})
    if not isinstance(audited_channels, dict):
        audited_channels = {}
        payload["audited_channels"] = audited_channels
    channel_key = manifest.channel_id
    if only_if_absent and channel_key in audited_channels:
        return
    allowed = list(manifest.mcp_servers.allowed or [])
    audited_channels[channel_key] = {
        "path": str(path),
        "allowed": allowed,
        "additions": additions,
        "source": source,
        "audited_at": datetime.now(UTC).isoformat(),
    }
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    log.info(
        "%s channel_id=%s path=%s allowed=%s additions=%s source=%s",
        event_name,
        manifest.channel_id,
        path,
        allowed,
        additions,
        source,
    )


def _mcp_allow_audit_path(path: Path) -> Path | None:
    try:
        if path.name != "channel-manifest.yaml" or path.parent.name != ".claude":
            return None
        channel_dir = path.parent.parent
        contexts_dir = channel_dir.parent
        if contexts_dir.name != "contexts":
            return None
        engram_home = contexts_dir.parent
    except IndexError:
        return None
    return paths.state_dir(engram_home) / "mcp_manifest_audit.json"


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


def _assert_sticky_tier(manifest: ChannelManifest) -> None:
    """Raise ValueError if sticky allow would persist outside the trusted tier."""
    current_tier = manifest.tier_effective().value
    if (
        classify_transition(current_tier, PermissionTier.OWNER_SCOPED.value)
        != "no-op"
    ):
        raise ValueError(
            "refusing to persist sticky allow outside `trusted` tier: "
            f"channel is currently `{current_tier}`"
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
    _assert_sticky_tier(manifest)
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
    dump_manifest(updated, manifest_path)
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
    if new_tier == PermissionTier.TASK_ASSISTANT:
        update_data["nightly_included"] = False
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

    updated = _rematerialize_manifest(
        manifest,
        update_data=update_data,
        previous_tier=manifest.permission_tier,
    )
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

    dump_manifest(updated, manifest_path)
    return manifest, updated, manifest_path, normalized_duration


def set_channel_nightly_included(
    channel_id: str,
    nightly_included: bool,
    *,
    home: Path | None = None,
) -> tuple[ChannelManifest, ChannelManifest, Path]:
    """Load a channel manifest, update ``nightly_included``, and persist it."""
    manifest_path = paths.channel_manifest_path(channel_id, home)
    manifest = load_manifest(manifest_path)
    if (
        nightly_included
        and manifest.tier_effective() == PermissionTier.TASK_ASSISTANT
    ):
        raise ValueError("safe-tier channels cannot be included in nightly summary")
    if manifest.nightly_included == nightly_included:
        return manifest, manifest, manifest_path

    updated = manifest.model_copy(update={"nightly_included": nightly_included})
    dump_manifest(updated, manifest_path)
    return manifest, updated, manifest_path


def normalize_mcp_server_name(server_name: str) -> str:
    """Normalize a manifest MCP server name."""
    normalized = str(server_name or "").strip()
    if not normalized:
        raise ValueError("mcp server name cannot be empty")
    return normalized


def set_channel_mcp_server_access(
    channel_id: str,
    server_name: str,
    *,
    action: Literal["allow", "deny"],
    home: Path | None = None,
) -> tuple[ChannelManifest, ChannelManifest, Path, str]:
    """Load a channel manifest, mutate MCP access, and persist it."""
    normalized_name = normalize_mcp_server_name(server_name)
    manifest_path = paths.channel_manifest_path(channel_id, home)
    manifest = load_manifest(manifest_path)

    allowed = (
        list(manifest.mcp_servers.allowed)
        if manifest.mcp_servers.allowed is not None
        else None
    )
    disallowed = list(manifest.mcp_servers.disallowed)

    if action == "allow":
        updated_allowed = (
            None
            if allowed is None
            else list(dict.fromkeys([*allowed, normalized_name]))
        )
        updated_disallowed = [
            entry for entry in disallowed if entry != normalized_name
        ]
    else:
        updated_allowed = allowed
        updated_disallowed = list(dict.fromkeys([*disallowed, normalized_name]))

    updated_scope = manifest.mcp_servers.model_copy(
        update={
            "allowed": updated_allowed,
            "disallowed": updated_disallowed,
        }
    )
    updated = manifest.model_copy(update={"mcp_servers": updated_scope})
    if updated == manifest:
        return manifest, manifest, manifest_path, normalized_name

    additions = _mcp_allow_list_additions(manifest, updated)
    _previous, persisted, persisted_path = _persist_manifest_update(
        updated,
        manifest_path,
        approved_mcp_additions=additions,
        audit_source=f"channel_mcp_{action}",
    )
    return manifest, persisted, persisted_path, normalized_name
