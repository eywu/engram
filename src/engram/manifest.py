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

import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from claude_agent_sdk import PermissionMode, SettingSource
from pydantic import BaseModel, Field, ValidationError, field_validator

from engram.config import HITLConfig

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

    # ── Helpers ──────────────────────────────────────────────────
    def is_owner_dm(self) -> bool:
        """True iff this manifest uses the owner-DM identity."""
        return self.identity == IdentityTemplate.OWNER_DM_FULL


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
        return ChannelManifest.model_validate(raw)
    except ValidationError as e:
        raise ManifestError(
            f"Manifest validation failed for {path}:\n{e}"
        ) from e


def dump_manifest(manifest: ChannelManifest, path: Path) -> None:
    """Write a manifest to YAML. Parent dir must exist."""
    data = manifest.model_dump(mode="json", exclude_none=False)
    # Pretty: stable key order, no flow style for nested mappings.
    path.write_text(
        yaml.safe_dump(
            data,
            sort_keys=False,
            default_flow_style=False,
            indent=2,
        )
    )
