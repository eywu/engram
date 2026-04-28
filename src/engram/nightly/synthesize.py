"""Nightly Claude synthesis over harvested memory rows."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from string import Template
from typing import Any, Protocol, cast

import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)
from claude_agent_sdk.types import (
    McpHttpServerConfig,
    McpSdkServerConfig,
    McpSSEServerConfig,
    McpStdioServerConfig,
)
from dotenv import load_dotenv
from pydantic import ValidationError

from engram import paths
from engram.budget import Budget, BudgetConfig, load_budget_config
from engram.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_CONTEXTS_DIR,
    HITLConfig,
    NightlyConfig,
)
from engram.manifest import ChannelManifest, ManifestError, load_manifest
from engram.mcp_tools import (
    MEMORY_SEARCH_FULL_TOOL_NAMES,
    MEMORY_SEARCH_SERVER_NAME,
    make_memory_search_server,
)
from engram.nightly.schema import (
    META_CHANNEL_ID,
    NightlySynthesisOutput,
    synthesis_output_format,
    synthesis_schema_prompt,
)
from engram.options import EngramAgentOptions
from engram.telemetry import cli_stderr_logger, configure_logging, write_json

log = logging.getLogger(__name__)

NIGHTLY_CHANNEL_ID = "__nightly__"
NIGHTLY_USER_ID = "__nightly__"
MAX_TURN_BUDGET_USD = 5.00
DEFAULT_OUTPUT_ROOT = Path.home() / ".engram" / "nightly"
PromptTemplatePath = Path | Traversable
DEFAULT_PROMPT_TEMPLATE = files("engram.templates.prompts").joinpath("nightly-synthesis.md")

_USD_QUANT = Decimal("0.000001")
_MIN_CHANNEL_ESTIMATE_USD = Decimal("0.01")
_MODEL_ESTIMATE_USD_PER_1K_TOKENS = {
    "opus": Decimal("0.015"),
    "sonnet": Decimal("0.005"),
    "haiku": Decimal("0.001"),
}


@dataclass(frozen=True)
class AnthropicRuntime:
    api_key: str | None
    model: str | None


@dataclass(frozen=True)
class PlannedChannel:
    channel: dict[str, Any]
    manifest: ChannelManifest | None
    model: str
    estimated_cost_usd: Decimal
    memory_excluded_channels: tuple[str, ...] = ()

    @property
    def channel_id(self) -> str:
        return str(self.channel.get("channel_id") or "")


@dataclass(frozen=True)
class SynthesisResult:
    output_path: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class ClaudeTurnResult:
    raw_output: str
    result: ResultMessage | None
    message_count: int
    assistant_models: tuple[str, ...]


class SynthesisOutputError(ValueError):
    """Raised when Claude output cannot be parsed as the pinned synthesis schema."""

    def __init__(self, detail: str, raw_output: str):
        super().__init__(detail)
        self.detail = detail
        self.raw_output = raw_output


class BudgetRecorder(Protocol):
    config: BudgetConfig

    def record(self, channel_id: str, user_id: str | None, result_message: Any) -> None:
        ...


ClientFactory = Callable[[ClaudeAgentOptions], ClaudeSDKClient]


async def synthesize(
    harvest_json: Path,
    *,
    config_path: Path | None = None,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    prompt_template_path: PromptTemplatePath = DEFAULT_PROMPT_TEMPLATE,
    weekly: bool = False,
    config: NightlyConfig | None = None,
    contexts_dir: Path | None = None,
    anthropic_runtime: AnthropicRuntime | None = None,
    budget: BudgetRecorder | None = None,
    client_factory: ClientFactory = ClaudeSDKClient,
) -> SynthesisResult:
    """Run nightly synthesis for the harvested channels and write synthesis.json."""
    config_path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    raw_config = (
        _load_config_raw(config_path)
        if config is None or contexts_dir is None or anthropic_runtime is None
        else {}
    )
    nightly_config = config or NightlyConfig.from_mapping(raw_config.get("nightly"))
    runtime = anthropic_runtime or _load_anthropic_runtime(raw_config)
    context_root = (contexts_dir or _contexts_dir_from_raw(raw_config)).expanduser()

    harvest_path = harvest_json.expanduser()
    harvest = json.loads(harvest_path.read_text(encoding="utf-8"))
    run_date = str(harvest.get("date") or dt.datetime.now(dt.UTC).date().isoformat())
    trigger = "nightly-weekly" if weekly else "nightly"
    current_dir = output_root.expanduser() / "current"
    archive_dir = output_root.expanduser() / "archive" / run_date
    current_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Nightly synthesis never prompts a human: there is no Slack operator
    # attached to the launchd subprocess. HITL is hard-disabled here and
    # threaded into the SDK invocation at line ~376 so ``options.hitl_config``
    # carries the same truth everywhere. If this default ever needs to change,
    # hoist it to a module-level constant instead of re-enabling per-call.
    hitl_config = HITLConfig(enabled=False)

    ledger = budget or _nightly_budget(config_path)
    prompt_template = _read_prompt_template(prompt_template_path)

    log.info(
        "nightly.synthesis_start",
        extra={
            "phase": "synthesis",
            "date": run_date,
            "trigger": trigger,
            "cwd": str(current_dir),
            "hitl_disabled": not hitl_config.enabled,
            "hitl_config_enabled": hitl_config.enabled,
            "max_budget_usd": MAX_TURN_BUDGET_USD,
            "daily_cost_cap_usd": nightly_config.daily_cost_cap_usd,
        },
    )

    planned = _plan_channels(
        harvest.get("channels") or [],
        contexts_dir=context_root,
        config=nightly_config,
        global_model=runtime.model,
        weekly=weekly,
    )

    synthesized_channels: list[dict[str, Any]] = []
    skipped_channels: list[dict[str, Any]] = list(harvest.get("skipped_channels") or [])
    spent_usd = Decimal("0")
    cap_usd = _usd(nightly_config.daily_cost_cap_usd)

    index = 0
    while index < len(planned):
        current_plan = planned[index]
        remaining = planned[index:]
        projected = spent_usd + sum((plan.estimated_cost_usd for plan in remaining), Decimal("0"))
        if projected > cap_usd:
            skipped = [
                {
                    "channel_id": plan.channel_id,
                    "reason": "daily_cost_cap",
                    "estimated_cost_usd": _format_usd(plan.estimated_cost_usd),
                }
                for plan in remaining
            ]
            skipped_channels.extend(skipped)
            log.info(
                "nightly.cost_cap_hit",
                extra={
                    "phase": "synthesis",
                    "date": run_date,
                    "spent_usd": _format_usd(spent_usd),
                    "projected_usd": _format_usd(projected),
                    "daily_cost_cap_usd": _format_usd(cap_usd),
                    "skipped_channels": [item["channel_id"] for item in skipped],
                },
            )
            break

        channel_result = await _synthesize_channel(
            current_plan,
            run_date=run_date,
            current_dir=current_dir,
            prompt_template=prompt_template,
            weekly=weekly,
            config=nightly_config,
            runtime=runtime,
            hitl_config=hitl_config,
            budget=ledger,
            client_factory=client_factory,
        )
        synthesized_channels.append(channel_result)
        spent_usd += _usd(channel_result.get("cost_usd", 0))
        index += 1

    payload: dict[str, Any] = {
        "schema_version": 1,
        "date": run_date,
        "trigger": trigger,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "harvest_path": str(harvest_path),
        "cwd": str(current_dir),
        "archive_dir": str(archive_dir),
        "hitl_disabled": not hitl_config.enabled,
        "hitl_config_enabled": hitl_config.enabled,
        "max_budget_usd": MAX_TURN_BUDGET_USD,
        "daily_cost_cap_usd": nightly_config.daily_cost_cap_usd,
        "budget_channel_id": NIGHTLY_CHANNEL_ID,
        "channels": synthesized_channels,
        "skipped_channels": skipped_channels,
        "totals": {
            "channels_synthesized": len(synthesized_channels),
            "channels_skipped": len(skipped_channels),
            "cost_usd": _format_usd(spent_usd),
        },
    }
    output_path = archive_dir / ("weekly-synthesis.json" if weekly else "synthesis.json")
    write_json(output_path, payload)
    log.info(
        "nightly.synthesis_complete",
        extra={
            "phase": "synthesis",
            "date": run_date,
            "trigger": trigger,
            "output_path": str(output_path),
            "channels": len(synthesized_channels),
            "skipped_channels": len(skipped_channels),
            "cost_usd": _format_usd(spent_usd),
        },
    )
    return SynthesisResult(output_path=output_path, payload=payload)


def parse_synthesis_output(text: str) -> dict[str, Any]:
    """Parse Claude's JSON object and validate it against the nightly schema."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise SynthesisOutputError("response is not valid JSON", text) from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise SynthesisOutputError(str(exc), text) from exc

    if not isinstance(parsed, dict):
        raise SynthesisOutputError("nightly synthesis output must be a JSON object", text)

    try:
        validated = NightlySynthesisOutput.model_validate(parsed)
    except ValidationError as exc:
        raise SynthesisOutputError(exc.json(), text) from exc
    return validated.model_dump(mode="json")


def select_nightly_model(
    manifest: ChannelManifest | None,
    *,
    config: NightlyConfig,
    global_model: str | None,
) -> str:
    if manifest is not None and manifest.nightly.model:
        return manifest.nightly.model
    if config.model:
        return config.model
    if manifest is not None:
        return "opus" if manifest.is_owner_dm() else "sonnet"
    return global_model or "sonnet"


def build_nightly_options(
    *,
    plan: PlannedChannel,
    run_date: str,
    current_dir: Path,
    runtime: AnthropicRuntime,
    hitl_config: HITLConfig,
    config: NightlyConfig,
) -> ClaudeAgentOptions:
    mcp_servers = {
        MEMORY_SEARCH_SERVER_NAME: make_memory_search_server(
            plan.channel_id or NIGHTLY_CHANNEL_ID,
            excluded_channels=_merge_channel_ids(
                config.excluded_channels,
                plan.memory_excluded_channels,
            ),
        )
    }
    child_env: dict[str, str] = {}
    if runtime.api_key:
        child_env["ANTHROPIC_API_KEY"] = runtime.api_key

    budget_config = BudgetConfig(hard_cap_enabled=False)
    options = EngramAgentOptions(
        setting_sources=["project"],
        cwd=str(current_dir),
        model=plan.model,
        # GRO-564: Claude Agent SDK / Claude Code CLI rejects non-UUID
        # session_id values. Keep nightly synthesis ephemeral and use a raw
        # UUID here; adding human-readable prefixes breaks SDK validation.
        session_id=str(uuid.uuid4()),
        # GRO-565: synthesis may need tool use + a final JSON response.
        max_turns=5,
        permission_mode="dontAsk",
        allowed_tools=list(MEMORY_SEARCH_FULL_TOOL_NAMES),
        disallowed_tools=[
            "Bash",
            "BashOutput",
            "KillShell",
            "Write",
            "Edit",
            "NotebookEdit",
            "WebFetch",
            "WebSearch",
        ],
        skills=[],
        mcp_servers=cast(
            dict[
                str,
                McpSdkServerConfig
                | McpStdioServerConfig
                | McpSSEServerConfig
                | McpHttpServerConfig,
            ],
            mcp_servers,
        ),
        extra_args={"strict-mcp-config": None},
        max_budget_usd=MAX_TURN_BUDGET_USD,
        env=child_env,
        can_use_tool=None,
        hooks={},
        stderr=cli_stderr_logger(NIGHTLY_CHANNEL_ID),
        output_format=synthesis_output_format(),
        hitl_config=hitl_config,
        budget_config=budget_config,
        strict_mcp_config=True,
    )
    # Claude Agent SDK exposes output_format as CLI --json-schema. Nightly uses
    # that native structured output path first, with parse_synthesis_output as
    # defense-in-depth for SDK/model drift.
    return options


async def _synthesize_channel(
    plan: PlannedChannel,
    *,
    run_date: str,
    current_dir: Path,
    prompt_template: str,
    weekly: bool,
    config: NightlyConfig,
    runtime: AnthropicRuntime,
    hitl_config: HITLConfig,
    budget: BudgetRecorder,
    client_factory: ClientFactory,
) -> dict[str, Any]:
    prompt = _render_prompt(
        prompt_template,
        run_date=run_date,
        model=plan.model,
        channel=plan.channel,
        manifest=plan.manifest,
        excluded_channels=config.excluded_channels,
        weekly=weekly,
    )
    options = build_nightly_options(
        plan=plan,
        run_date=run_date,
        current_dir=current_dir,
        runtime=runtime,
        hitl_config=hitl_config,
        config=config,
    )
    client = client_factory(options)
    error_payload: dict[str, Any] | None = None
    raw_outputs: list[str] = []
    parsed: dict[str, Any] | None = None
    cost_usd = Decimal("0")
    prompt_cache: dict[str, object] = {
        "status": "miss",
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    budget_recorded = False

    def record_turn(result: ResultMessage | None) -> None:
        nonlocal budget_recorded, cost_usd, prompt_cache
        if result is None:
            return
        cost_usd += _usd(getattr(result, "total_cost_usd", None) or 0)
        prompt_cache = cast(dict[str, object], _prompt_cache(result))
        budget.record(NIGHTLY_CHANNEL_ID, NIGHTLY_USER_ID, result)
        budget_recorded = True

    try:
        await client.connect()
        first_turn = await _run_claude_turn(
            client,
            prompt,
            session_id=options.session_id or "default",
        )
        raw_outputs.append(first_turn.raw_output)
        record_turn(first_turn.result)
        try:
            parsed = parse_synthesis_output(first_turn.raw_output)
            _attach_plan_source_row_ids(parsed, plan=plan, weekly=weekly)
            _log_parse_ok(plan=plan, run_date=run_date, attempt=1)
        except SynthesisOutputError as exc:
            _log_parse_retry(plan=plan, run_date=run_date, error=exc.detail)
            retry_turn = await _run_claude_turn(
                client,
                _repair_prompt(exc.detail),
                session_id=options.session_id or "default",
            )
            raw_outputs.append(retry_turn.raw_output)
            record_turn(retry_turn.result)
            try:
                parsed = parse_synthesis_output(retry_turn.raw_output)
                _attach_plan_source_row_ids(parsed, plan=plan, weekly=weekly)
                _log_parse_ok(plan=plan, run_date=run_date, attempt=2)
            except SynthesisOutputError as retry_exc:
                _log_parse_fail_final(
                    plan=plan,
                    run_date=run_date,
                    error=retry_exc.detail,
                    raw_outputs=raw_outputs,
                    turn=retry_turn,
                    first_turn=first_turn,
                    prompt_template=prompt_template,
                )
                raise
    except Exception as exc:
        if isinstance(exc, SynthesisOutputError):
            raise
        error_payload = {
            "status": "sdk_error",
            "error_class": type(exc).__name__,
            "error": str(exc),
        }
    finally:
        disconnect = getattr(client, "disconnect", None)
        if disconnect is not None:
            try:
                await disconnect()
            except Exception as exc:
                log.warning(
                    "nightly.synthesis_disconnect_failed",
                    extra={
                        "phase": "synthesis",
                        "channel_id": plan.channel_id,
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                    },
                )

    status = "synthesized"
    if error_payload is not None:
        status = str(error_payload["status"])

    payload: dict[str, Any] = {
        "channel_id": plan.channel_id,
        "status": status,
        "model": plan.model,
        "estimated_cost_usd": _format_usd(plan.estimated_cost_usd),
        "cost_usd": _format_usd(cost_usd),
        "row_count": plan.channel.get("row_count", 0),
        "token_count": plan.channel.get("token_count", 0),
        "budget_recorded": budget_recorded,
        "prompt_cache": prompt_cache,
    }
    if parsed is not None:
        payload["synthesis"] = parsed
    if error_payload is not None:
        payload["error"] = error_payload
        if raw_outputs:
            payload["raw_output"] = raw_outputs[-1]

    log.info(
        "nightly.channel_synthesized",
        extra={
            "phase": "synthesis",
            "channel_id": plan.channel_id,
            "trigger": "nightly-weekly" if weekly else "nightly",
            "status": status,
            "model": plan.model,
            "cost_usd": _format_usd(cost_usd),
            "prompt_cache_status": prompt_cache["status"],
            "budget_recorded": budget_recorded,
        },
    )
    return payload


def _plan_channels(
    channels: list[dict[str, Any]],
    *,
    contexts_dir: Path,
    config: NightlyConfig,
    global_model: str | None,
    weekly: bool,
) -> list[PlannedChannel]:
    planned: list[PlannedChannel] = []
    for channel in channels:
        channel_id = str(channel.get("channel_id") or "")
        if channel_id == META_CHANNEL_ID:
            continue
        manifest = _load_channel_manifest(channel_id, contexts_dir=contexts_dir)
        model = select_nightly_model(manifest, config=config, global_model=global_model)
        planned.append(
            PlannedChannel(
                channel=channel,
                manifest=manifest,
                model=model,
                estimated_cost_usd=_estimate_channel_cost(channel, model),
            )
        )
    if weekly:
        meta_plan = _build_meta_plan(planned, config=config, global_model=global_model)
        if meta_plan is not None:
            planned.append(meta_plan)
    return planned


def _build_meta_plan(
    planned: list[PlannedChannel],
    *,
    config: NightlyConfig,
    global_model: str | None,
) -> PlannedChannel | None:
    eligible = [
        plan
        for plan in planned
        if plan.channel_id and _is_nightly_included(plan.manifest)
    ]
    if not eligible:
        return None

    rows: list[dict[str, Any]] = []
    token_count = 0
    for plan in eligible:
        channel_rows = [
            row
            for row in plan.channel.get("rows", [])
            if isinstance(row, dict)
        ]
        rows.extend(channel_rows)
        token_count += int(plan.channel.get("token_count") or 0)

    if not rows:
        return None

    model = config.model or global_model or "sonnet"
    ineligible_channel_ids = tuple(
        plan.channel_id
        for plan in planned
        if plan.channel_id and not _is_nightly_included(plan.manifest)
    )
    channel = {
        "channel_id": META_CHANNEL_ID,
        "row_count": len(rows),
        "token_count": token_count,
        "rows_before": len(rows),
        "rows_after_dedup": len(rows),
        "truncated": False,
        "source_channel_ids": [plan.channel_id for plan in eligible],
        "rows": rows,
    }
    return PlannedChannel(
        channel=channel,
        manifest=None,
        model=model,
        estimated_cost_usd=_estimate_channel_cost(channel, model),
        memory_excluded_channels=ineligible_channel_ids,
    )


def _is_nightly_included(manifest: ChannelManifest | None) -> bool:
    return True if manifest is None else manifest.nightly_included


def _load_channel_manifest(
    channel_id: str,
    *,
    contexts_dir: Path,
) -> ChannelManifest | None:
    if not channel_id:
        return None
    manifest_path = contexts_dir / channel_id / ".claude" / "channel-manifest.yaml"
    if not manifest_path.exists():
        return None
    try:
        return load_manifest(manifest_path)
    except ManifestError:
        log.warning(
            "nightly.manifest_load_failed",
            extra={"phase": "synthesis", "channel_id": channel_id, "path": str(manifest_path)},
            exc_info=True,
        )
        return None


def _render_prompt(
    prompt_template: str,
    *,
    run_date: str,
    model: str,
    channel: dict[str, Any],
    manifest: ChannelManifest | None,
    excluded_channels: tuple[str, ...],
    weekly: bool = False,
) -> str:
    manifest_json = {}
    if manifest is not None:
        manifest_json = {
            "channel_id": manifest.channel_id,
            "identity": str(manifest.identity),
            "label": manifest.label,
            "nightly_included": manifest.nightly_included,
            "nightly_model": manifest.nightly.model,
        }
    rendered = Template(prompt_template).safe_substitute(
        date=run_date,
        model=model,
        excluded_channels_json=json.dumps(list(excluded_channels), sort_keys=True),
        manifest_json=json.dumps(manifest_json, indent=2, sort_keys=True),
        channel_json=json.dumps(channel, indent=2, sort_keys=True),
    )
    if weekly and str(channel.get("channel_id") or "") == META_CHANNEL_ID:
        rendered += (
            "\n\nWeekly meta-summary mode: this is one cross-channel pass over "
            "only the eligible weekly channel rows present in channel_json. "
            f"Return channel_id {META_CHANNEL_ID!r}. Do not infer from or mention "
            "channels absent from channel_json, and include every cited input row "
            "id in top-level source_row_ids."
        )
    elif weekly:
        rendered += (
            "\n\nWeekly mode: the channel harvest contains exactly seven daily "
            "nightly summary rows ending on the run date. Synthesize across all "
            "seven rows and include all seven row id values in top-level "
            "source_row_ids."
        )
    return rendered


def _attach_plan_source_row_ids(
    parsed: dict[str, Any],
    *,
    plan: PlannedChannel,
    weekly: bool,
) -> None:
    if not weekly:
        return
    source_row_ids = [
        int(row["id"])
        for row in plan.channel.get("rows", [])
        if isinstance(row, dict) and "id" in row
    ]
    if not source_row_ids:
        return
    parsed["source_row_ids"] = source_row_ids


def _merge_channel_ids(*groups: tuple[str, ...]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            channel_id = str(raw).strip()
            if channel_id and channel_id not in seen:
                merged.append(channel_id)
                seen.add(channel_id)
    return merged


def _assistant_text(message: AssistantMessage) -> list[str]:
    chunks: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return chunks


async def _run_claude_turn(
    client: ClaudeSDKClient,
    prompt: str,
    *,
    session_id: str,
) -> ClaudeTurnResult:
    text_chunks: list[str] = []
    result: ResultMessage | None = None
    message_count = 0
    assistant_models: list[str] = []
    await client.query(prompt, session_id=session_id)
    receive_messages = getattr(client, "receive_messages", None)
    message_stream = receive_messages() if callable(receive_messages) else client.receive_response()
    async for message in message_stream:
        message_count += 1
        if isinstance(message, AssistantMessage):
            text_chunks.extend(_assistant_text(message))
            model = str(getattr(message, "model", "") or "").strip()
            if model and model not in assistant_models:
                assistant_models.append(model)
        elif isinstance(message, ResultMessage):
            result = message
            if message.stop_reason != "tool_use":
                break

    raw_output = "".join(text_chunks).strip()
    if not raw_output and result is not None and getattr(result, "structured_output", None) is not None:
        raw_output = json.dumps(result.structured_output)
    return ClaudeTurnResult(
        raw_output=raw_output,
        result=result,
        message_count=message_count,
        assistant_models=tuple(assistant_models),
    )


def _repair_prompt(error: str) -> str:
    return (
        "Your previous response did not match the schema. "
        f"The error was: `{error}`. "
        "Please reformat preserving the same content.\n\n"
        "Required JSON Schema:\n"
        "```json\n"
        f"{synthesis_schema_prompt()}\n"
        "```\n\n"
        "Return only the corrected JSON object."
    )


def _log_parse_ok(*, plan: PlannedChannel, run_date: str, attempt: int) -> None:
    log.info(
        "nightly.parse_ok",
        extra={
            "phase": "synthesis",
            "date": run_date,
            "channel_id": plan.channel_id,
            "attempt": attempt,
        },
    )


def _log_parse_retry(*, plan: PlannedChannel, run_date: str, error: str) -> None:
    log.warning(
        "nightly.parse_retry",
        extra={
            "phase": "synthesis",
            "date": run_date,
            "channel_id": plan.channel_id,
            "attempt": 1,
            "error": error,
        },
    )


def _log_parse_fail_final(
    *,
    plan: PlannedChannel,
    run_date: str,
    error: str,
    raw_outputs: list[str],
    turn: ClaudeTurnResult | None,
    first_turn: ClaudeTurnResult | None,
    prompt_template: str,
) -> None:
    result = turn.result if turn is not None else None
    if result is None:
        prompt_tokens = 0
    else:
        prompt_tokens, _ = _prompt_tokens_and_model(result)
    if result is None and first_turn is not None and first_turn is not turn:
        prompt_tokens, _ = _prompt_tokens_and_model(first_turn.result)
    message_count = getattr(result, "message_count", None) if result is not None else None
    if message_count is None and turn is not None:
        message_count = turn.message_count
    model_actual = _observed_model_actual(turn)
    first_attempt_stop_reason = None
    if first_turn is not None and first_turn is not turn and first_turn.result is not None:
        first_attempt_stop_reason = getattr(first_turn.result, "stop_reason", None)
    log.error(
        "nightly.parse_fail_final",
        extra={
            "phase": "synthesis",
            "date": run_date,
            "channel_id": plan.channel_id,
            "attempts": len(raw_outputs),
            "error": error,
            "raw_outputs": raw_outputs,
            "raw_output_initial": raw_outputs[0] if raw_outputs else "",
            "raw_output_retry": raw_outputs[1] if len(raw_outputs) > 1 else "",
            "stop_reason": getattr(result, "stop_reason", None) if result is not None else None,
            "first_attempt_stop_reason": first_attempt_stop_reason,
            "message_count": message_count,
            "prompt_tokens": prompt_tokens,
            "prompt_preview": prompt_template[:2000],
            "model_actual": model_actual,
            "model_configured": plan.model,
        },
    )


def _observed_model_actual(turn: ClaudeTurnResult | None) -> str | None:
    if turn is None:
        return None
    _, model_actual = _prompt_tokens_and_model(turn.result)
    if model_actual is None and turn.assistant_models:
        model_actual = ",".join(turn.assistant_models)
    return model_actual


def _prompt_tokens_and_model(result: ResultMessage | None) -> tuple[int, str | None]:
    if result is None:
        return 0, None

    usage = getattr(result, "usage", None) or {}
    prompt_tokens = _int_token(usage.get("input_tokens"))
    model_usage = getattr(result, "model_usage", None) or {}
    model_actual: str | None = None

    if isinstance(model_usage, dict) and model_usage:
        model_keys = [str(key) for key in model_usage if key]
        if model_keys:
            model_actual = ",".join(model_keys)

        if prompt_tokens == 0:
            for value in model_usage.values():
                if not isinstance(value, dict):
                    continue
                prompt_tokens += _int_token(value.get("input_tokens"))

    return prompt_tokens, model_actual


def _prompt_cache(result: ResultMessage) -> dict[str, int | str]:
    usage = getattr(result, "usage", None) or {}
    creation = _int_token(usage.get("cache_creation_input_tokens"))
    read = _int_token(usage.get("cache_read_input_tokens"))
    if creation == 0 and read == 0:
        for model_usage in (getattr(result, "model_usage", None) or {}).values():
            if not isinstance(model_usage, dict):
                continue
            creation += _int_token(model_usage.get("cache_creation_input_tokens"))
            read += _int_token(model_usage.get("cache_read_input_tokens"))
    if read > 0:
        status = "read"
    elif creation > 0:
        status = "created"
    else:
        status = "miss"
    return {
        "status": status,
        "cache_creation_input_tokens": creation,
        "cache_read_input_tokens": read,
    }


def _estimate_channel_cost(channel: dict[str, Any], model: str) -> Decimal:
    token_count = max(0, int(channel.get("token_count") or 0))
    rate = _estimate_rate_for_model(model)
    estimated = (Decimal(token_count) / Decimal(1000)) * rate
    return max(_MIN_CHANNEL_ESTIMATE_USD, estimated).quantize(_USD_QUANT)


def _estimate_rate_for_model(model: str) -> Decimal:
    normalized = model.lower()
    for alias, rate in _MODEL_ESTIMATE_USD_PER_1K_TOKENS.items():
        if alias in normalized:
            return rate
    return _MODEL_ESTIMATE_USD_PER_1K_TOKENS["sonnet"]


def _read_prompt_template(prompt_template_path: PromptTemplatePath) -> str:
    if isinstance(prompt_template_path, Path):
        return prompt_template_path.expanduser().read_text(encoding="utf-8")
    return prompt_template_path.read_text(encoding="utf-8")


def _load_config_raw(config_path: Path) -> dict[str, Any]:
    _load_env_files()
    if not config_path.exists():
        return {}
    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def _load_anthropic_runtime(raw_config: dict[str, Any]) -> AnthropicRuntime:
    anthropic = raw_config.get("anthropic") or {}
    return AnthropicRuntime(
        api_key=(
            os.environ.get("ENGRAM_ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or anthropic.get("api_key")
        ),
        model=(os.environ.get("ENGRAM_MODEL") or anthropic.get("model")),
    )


def _contexts_dir_from_raw(raw_config: dict[str, Any]) -> Path:
    raw_paths = raw_config.get("paths") or {}
    return Path(raw_paths.get("contexts_dir") or DEFAULT_CONTEXTS_DIR)


def _nightly_budget(config_path: Path) -> Budget:
    loaded = load_budget_config(config_path)
    budget_config = BudgetConfig(
        monthly_cap_usd=loaded.monthly_cap_usd,
        hard_cap_enabled=False,
        warn_thresholds=loaded.warn_thresholds,
        timezone=loaded.timezone,
    )
    return Budget(budget_config)


def _load_env_files() -> None:
    # Mirrors engram.config._load_env_files; see its docstring for precedence.
    candidates: list[Path] = []
    override = os.environ.get("ENGRAM_ENV_FILE")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path.cwd() / ".env")
    candidates.append(paths.engram_home() / ".env")
    for candidate in candidates:
        if candidate.exists():
            load_dotenv(candidate, override=False)


def _usd(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_USD_QUANT)


def _format_usd(value: Decimal) -> str:
    return str(value.quantize(_USD_QUANT, rounding=ROUND_HALF_UP))


def _int_token(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthesize a nightly harvest.json into synthesis.json."
    )
    parser.add_argument("harvest_json", type=Path, help="Path to harvest.json.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.yaml. Defaults to ~/.engram/config.yaml.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Nightly root. Outputs go to <root>/archive/<date>/synthesis.json.",
    )
    parser.add_argument(
        "--prompt-template",
        type=Path,
        default=DEFAULT_PROMPT_TEMPLATE,
        help="Prompt template path.",
    )
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="Synthesize weekly daily-summary harvest rows.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(file_prefix="nightly")
    try:
        result = asyncio.run(
            synthesize(
                args.harvest_json,
                config_path=args.config,
                output_root=args.output_root,
                prompt_template_path=args.prompt_template,
                weekly=args.weekly,
            )
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"synthesis failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"synthesis": str(result.output_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
