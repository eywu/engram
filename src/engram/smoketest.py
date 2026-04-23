"""Standalone launchd smoke test for ClaudeSDKClient.

This module intentionally bypasses the Slack bridge. It verifies the minimal
runtime assumptions a future nightly launchd job will depend on: project
settings discovery from the owner-DM context, Claude CLI resolution, explicit
HITL disablement, prompt cache accounting, and budget ledger recording.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import platform
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import claude_agent_sdk
import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)
from dotenv import load_dotenv

from engram import paths
from engram.budget import Budget, load_budget_config
from engram.config import HITLConfig

SMOKE_CHANNEL_ID = "__smoke__"
SMOKE_USER_ID = "__launchd_smoke__"
SMOKE_PROMPT = "reply with 'smoke-test-ok'"
SMOKE_EXPECTED_TEXT = "smoke-test-ok"


@dataclass(frozen=True)
class CliResolution:
    resolved: bool
    cli_path: str | None
    source: str | None
    path_cli: str | None


@dataclass(frozen=True)
class AnthropicRuntime:
    api_key: str | None
    model: str | None


class JsonlLogger:
    def __init__(self, path: Path):
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, name: str, **fields: Any) -> None:
        payload = {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "event": name,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True, default=_json_default))
            fh.write("\n")


def default_log_path() -> Path:
    today = dt.datetime.now().date().isoformat()
    return paths.log_dir() / f"smoketest-{today}.jsonl"


def default_owner_dm_cwd() -> Path:
    return paths.contexts_dir() / "owner-dm"


def resolve_cli_path(path_env: str | None = None) -> CliResolution:
    """Mirror the SDK's CLI lookup order enough to log the resolved binary."""
    path_cli = shutil.which("claude", path=path_env)
    bundled = _bundled_cli_path()
    if bundled is not None:
        return CliResolution(
            resolved=True,
            cli_path=str(bundled),
            source="bundled",
            path_cli=path_cli,
        )
    if path_cli:
        return CliResolution(
            resolved=True,
            cli_path=path_cli,
            source="path",
            path_cli=path_cli,
        )
    return CliResolution(
        resolved=False,
        cli_path=None,
        source=None,
        path_cli=path_cli,
    )


def load_anthropic_runtime(config_path: Path | None = None) -> AnthropicRuntime:
    _load_env_files()
    raw: dict[str, Any] = {}
    path = config_path or (paths.engram_home() / "config.yaml")
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    anthropic = raw.get("anthropic") or {}
    return AnthropicRuntime(
        api_key=(
            os.environ.get("ENGRAM_ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or anthropic.get("api_key")
        ),
        model=(
            os.environ.get("ENGRAM_MODEL")
            or anthropic.get("model")
        ),
    )


async def run_smoke(
    *,
    cwd: Path | None = None,
    log_path: Path | None = None,
    client_factory: Callable[[ClaudeAgentOptions], ClaudeSDKClient] = ClaudeSDKClient,
    budget: Budget | None = None,
    cli_resolver: Callable[[str | None], CliResolution] = resolve_cli_path,
    anthropic_loader: Callable[[], AnthropicRuntime] = load_anthropic_runtime,
) -> int:
    smoke_cwd = (cwd or default_owner_dm_cwd()).expanduser()
    logger = JsonlLogger(log_path or default_log_path())
    hitl_config = HITLConfig(enabled=False)
    hitl_disabled = not hitl_config.enabled
    project_found = (smoke_cwd / ".claude").is_dir()
    cli_resolution = cli_resolver(os.environ.get("PATH"))
    runtime = anthropic_loader()

    logger.event(
        "smoketest.start",
        cwd=str(smoke_cwd),
        process_cwd=str(Path.cwd()),
        path=os.environ.get("PATH", ""),
        home=os.environ.get("HOME", ""),
        hitl_disabled=hitl_disabled,
        project_found=project_found,
        cli_resolved=cli_resolution.resolved,
        cli_source=cli_resolution.source,
        cli_path=cli_resolution.cli_path,
        path_cli=cli_resolution.path_cli,
        path_cli_resolved=cli_resolution.path_cli is not None,
        anthropic_api_key_configured=runtime.api_key is not None,
        model=runtime.model,
    )

    if not project_found:
        logger.event(
            "smoketest.failure",
            reason="project_not_found",
            cwd=str(smoke_cwd),
            project_found=False,
        )
        return 1
    if not cli_resolution.resolved:
        logger.event(
            "smoketest.failure",
            reason="cli_not_resolved",
            cli_resolved=False,
            path=os.environ.get("PATH", ""),
        )
        return 1

    child_env: dict[str, str] = {}
    if runtime.api_key:
        child_env["ANTHROPIC_API_KEY"] = runtime.api_key

    options = ClaudeAgentOptions(
        setting_sources=["project"],
        cwd=smoke_cwd,
        model=runtime.model,
        session_id=f"engram-smoketest-{uuid.uuid4().hex}",
        max_turns=1,
        env=child_env,
        can_use_tool=None,
        hooks={},
    )
    # ``permission_request_hook_wired`` is a diagnostic kept after GRO-432:
    # Since our ``build_permission_request_hook`` factory was removed, this
    # flag should always log False in production. If it ever reports True,
    # someone re-wired the fire-and-forget PermissionRequest hook (which
    # does NOT block tool execution, see GRO-426). The smoketest JSONL
    # flag makes that regression visible early.
    logger.event(
        "smoketest.options",
        setting_sources=options.setting_sources,
        cwd=str(options.cwd),
        hitl_disabled=hitl_disabled,
        can_use_tool_wired=options.can_use_tool is not None,
        permission_request_hook_wired=bool(
            options.hooks and options.hooks.get("PermissionRequest")
        ),
    )

    client = client_factory(options)
    result: ResultMessage | None = None
    text_chunks: list[str] = []
    budget_recorded = False
    try:
        await client.connect()
        logger.event("smoketest.client_connected")
        await client.query(SMOKE_PROMPT, session_id=options.session_id)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                text_chunks.extend(_assistant_text(message))
            elif isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        logger.event(
            "smoketest.failure",
            reason="sdk_error",
            error_class=type(exc).__name__,
            error=str(exc),
        )
        return 1
    finally:
        disconnect = getattr(client, "disconnect", None)
        if disconnect is not None:
            try:
                await disconnect()
            except Exception as exc:
                logger.event(
                    "smoketest.disconnect_failed",
                    error_class=type(exc).__name__,
                    error=str(exc),
                )

    if result is None:
        logger.event("smoketest.failure", reason="missing_result")
        return 1

    response_text = "".join(text_chunks).strip()
    prompt_cache = _prompt_cache(result)
    if SMOKE_EXPECTED_TEXT not in response_text:
        logger.event(
            "smoketest.failure",
            reason="unexpected_response",
            response_text=response_text,
            prompt_cache_status=prompt_cache["status"],
        )
        return 1

    try:
        ledger = budget or Budget(load_budget_config())
        ledger.record(SMOKE_CHANNEL_ID, SMOKE_USER_ID, result)
        budget_recorded = True
    except Exception as exc:
        logger.event(
            "smoketest.failure",
            reason="budget_record_failed",
            error_class=type(exc).__name__,
            error=str(exc),
        )
        return 1

    logger.event(
        "smoketest.success",
        hitl_disabled=hitl_disabled,
        cli_resolved=cli_resolution.resolved,
        cli_source=cli_resolution.source,
        project_found=project_found,
        budget_recorded=budget_recorded,
        budget_channel_id=SMOKE_CHANNEL_ID,
        response_text=response_text,
        total_cost_usd=getattr(result, "total_cost_usd", None),
        prompt_cache_status=prompt_cache["status"],
        prompt_cache_creation_input_tokens=prompt_cache["cache_creation_input_tokens"],
        prompt_cache_read_input_tokens=prompt_cache["cache_read_input_tokens"],
        hitl_guard_invocations=0,
        write_edit_hitl_guard_fired=False,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Engram launchd smoke test.")
    parser.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="Owner-DM context cwd. Defaults to ~/.engram/contexts/owner-dm.",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=None,
        help="JSONL log path. Defaults to ~/.engram/logs/smoketest-<date>.jsonl.",
    )
    args = parser.parse_args(argv)
    code = asyncio.run(run_smoke(cwd=args.cwd, log_path=args.log_path))
    print(f"engram smoketest exit={code} log={args.log_path or default_log_path()}")
    return code


def _assistant_text(message: AssistantMessage) -> list[str]:
    chunks: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return chunks


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


def _int_token(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bundled_cli_path() -> Path | None:
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    candidate = Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / cli_name
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def _load_env_files() -> None:
    for candidate in (
        Path.cwd() / ".env",
        paths.engram_home() / ".env",
        Path.home() / "code" / "_secret" / ".env",
    ):
        if candidate.exists():
            load_dotenv(candidate, override=False)


def _json_default(value: Any) -> str | int | float | bool | None:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
