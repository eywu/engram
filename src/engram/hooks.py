"""Claude Agent SDK hooks used for Engram audit logging."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, cast

import structlog
from claude_agent_sdk import (
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    SubagentStopHookInput,
)

from engram.costs import CostDatabase


def build_hooks(
    *,
    channel_id: str,
    cost_db: CostDatabase | None = None,
) -> dict[str, list[HookMatcher]]:
    """Build per-channel SDK hook matchers."""
    log = structlog.get_logger("engram.hooks").bind(channel_id=channel_id)

    async def pre_tool_use(
        hook_input: HookInput,
        tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        log.info(
            "hook.pre_tool_use",
            tool_name=hook_input.get("tool_name"),
            input=hook_input.get("tool_input"),
            tool_use_id=tool_use_id or hook_input.get("tool_use_id"),
            ts=_utc_ts(),
        )
        return {"continue_": True, "suppressOutput": True}

    async def post_tool_use_failure(
        hook_input: HookInput,
        tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        log.error(
            "hook.post_tool_use_failure",
            tool_name=hook_input.get("tool_name"),
            input=hook_input.get("tool_input"),
            error=hook_input.get("error"),
            tool_use_id=tool_use_id or hook_input.get("tool_use_id"),
            ts=_utc_ts(),
        )
        return {"continue_": True, "suppressOutput": True}

    async def notification(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        log.info(
            "hook.notification",
            title=hook_input.get("title"),
            message=hook_input.get("message"),
            notification_type=hook_input.get("notification_type"),
            ts=_utc_ts(),
        )
        return {"continue_": True, "suppressOutput": True}

    async def subagent_stop(
        hook_input: SubagentStopHookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        transcript_path = hook_input.get("agent_transcript_path")
        cost_usd = _extract_transcript_cost(transcript_path)
        log.info(
            "hook.subagent_stop",
            agent_id=hook_input.get("agent_id"),
            agent_type=hook_input.get("agent_type"),
            agent_transcript_path=transcript_path,
            cost_usd=cost_usd,
            ts=_utc_ts(),
        )
        if cost_db is not None and hook_input.get("agent_id"):
            cost_db.record_subagent_completion(
                channel_id=channel_id,
                session_id=str(hook_input.get("session_id") or ""),
                subagent_id=str(hook_input["agent_id"]),
                agent_type=hook_input.get("agent_type"),
                transcript_path=transcript_path,
                cost_usd=cost_usd,
        )
        return {"continue_": True, "suppressOutput": True}

    async def subagent_stop_adapter(
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        return await subagent_stop(cast(SubagentStopHookInput, hook_input), tool_use_id, context)

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])],
        "PostToolUseFailure": [
            HookMatcher(matcher=None, hooks=[post_tool_use_failure])
        ],
        "Notification": [HookMatcher(matcher=None, hooks=[notification])],
        "SubagentStop": [HookMatcher(matcher=None, hooks=[subagent_stop_adapter])],
    }


def _extract_transcript_cost(transcript_path: object) -> float | None:
    if not transcript_path:
        return None
    path = Path(str(transcript_path))
    if not path.exists():
        return None
    last_cost: float | None = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cost = _find_cost(payload)
                if cost is not None:
                    last_cost = cost
    except OSError:
        return None
    return last_cost


def _utc_ts() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _find_cost(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("total_cost_usd", "totalCostUsd", "cost_usd"):
            raw = value.get(key)
            if isinstance(raw, int | float):
                return float(raw)
        for child in value.values():
            found = _find_cost(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_cost(child)
            if found is not None:
                return found
    return None
