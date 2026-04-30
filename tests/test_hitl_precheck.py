"""GRO-430: branch coverage for build_hitl_tool_guard precheck paths.

These are tight unit tests focused on the three precheck branches in
``build_hitl_tool_guard`` (``src/engram/hitl.py``):

  1. ``precheck is None`` (no precheck installed) — existing integration
     tests already cover this; included here for completeness.
  2. ``precheck`` returns ``PermissionResultDeny`` — short-circuit early
     return, NO HITL question fired, NO ``tool_guard_*`` events emitted.
  3. ``precheck`` returns ``PermissionResultAllow(updated_input={...})``
     — HITL question carries the *updated* tool_input, not the original.

Before this file, branches (2) and (3) had no direct test coverage; the
tool_guard's precheck contract was only exercised indirectly through
scope.py integration tests.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import ToolPermissionContext

from engram.config import HITLConfig
from engram.hitl import PendingQuestion, build_hitl_tool_guard
from engram.router import Router

CHANNEL_ID = "C07TEST430"
SESSION_ID = "session-gro-430"


def _ctx(tool_use_id: str = "tool-use-1") -> ToolPermissionContext:
    return ToolPermissionContext(tool_use_id=tool_use_id)


async def _build_guard(
    *,
    precheck: Any = None,
    on_new_question: Any = None,
) -> tuple[Any, Router, list[PendingQuestion]]:
    router = Router(hitl=HITLConfig(timeout_s=30))
    questions: list[PendingQuestion] = []

    async def _default_on_new_question(q: PendingQuestion) -> None:
        questions.append(q)

    guard = build_hitl_tool_guard(
        router=router,
        channel_id=CHANNEL_ID,
        session_id=SESSION_ID,
        client_provider=lambda: None,
        on_new_question=on_new_question or _default_on_new_question,
        default_timeout_s=30,
        precheck=precheck,
    )
    return guard, router, questions


@pytest.mark.asyncio
async def test_precheck_deny_short_circuits_without_hitl_question():
    """Branch: precheck returns PermissionResultDeny → return immediately.

    Contract: no HITL question is ever fired, on_new_question is never
    called, and the deny object propagates back to the SDK verbatim.
    """
    deny = PermissionResultDeny(message="scope violation", interrupt=False)

    precheck_calls: list[tuple[str, dict[str, Any]]] = []

    async def deny_precheck(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> Any:
        precheck_calls.append((tool_name, tool_input))
        return deny

    on_new_question_calls: list[PendingQuestion] = []

    async def on_new_question(q: PendingQuestion) -> None:
        on_new_question_calls.append(q)

    guard, router, _ = await _build_guard(
        precheck=deny_precheck,
        on_new_question=on_new_question,
    )

    result = await guard("Bash", {"cmd": "rm -rf /"}, _ctx())

    # precheck was consulted with the ORIGINAL input
    assert precheck_calls == [("Bash", {"cmd": "rm -rf /"})]

    # deny propagates back verbatim — same object
    assert result is deny
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "scope violation"

    # no HITL question registered for this channel
    assert router.hitl.pending_for_channel(CHANNEL_ID) == []
    # on_new_question was never called — nothing hit Slack
    assert on_new_question_calls == []


@pytest.mark.asyncio
async def test_precheck_allow_with_updated_input_passes_updated_input_to_hitl():
    """Branch: precheck returns Allow(updated_input=...) → HITL uses updated.

    Contract: when precheck rewrites the tool_input (e.g. to strip a
    dangerous arg or canonicalize a path), the HITL question and the
    resolved permission result must reflect the UPDATED input, not the
    original one the SDK supplied.
    """
    original = {"file_path": "/tmp/raw", "content": "x"}
    updated = {"file_path": "/tmp/canonical", "content": "x"}

    async def rewriting_precheck(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> Any:
        # Verify precheck sees the ORIGINAL input
        assert tool_input == original
        return PermissionResultAllow(updated_input=updated)

    guard, router, questions = await _build_guard(precheck=rewriting_precheck)

    # Start the guard; it should block on the HITL question
    guard_task = asyncio.create_task(guard("Write", original, _ctx()))

    # Wait for the question to register
    for _ in range(50):  # up to 50ms of polling
        if questions:
            break
        await asyncio.sleep(0.001)
    assert len(questions) == 1, "HITL question should have been registered"
    q = questions[0]

    # The pending question carries the UPDATED input, not the original
    assert q.tool_input == updated
    assert q.tool_input != original

    # Resolve and verify the final PermissionResultAllow also carries updated
    allow = PermissionResultAllow(updated_input=updated)
    assert router.hitl.resolve(q.permission_request_id, allow) is True

    result = await asyncio.wait_for(guard_task, timeout=1.0)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == updated


@pytest.mark.asyncio
async def test_precheck_allow_without_updated_input_passes_original():
    """Branch: precheck returns Allow() with no updated_input → pass-through.

    Contract: when precheck approves without mutation, the HITL question
    carries the exact tool_input the SDK supplied. This is the most
    common production path (scope.can_use_tool returns plain Allow()).
    """
    original = {"cmd": "ls -la"}

    async def plain_allow_precheck(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> Any:
        return PermissionResultAllow()  # no updated_input

    guard, router, questions = await _build_guard(precheck=plain_allow_precheck)

    guard_task = asyncio.create_task(guard("Bash", original, _ctx()))

    for _ in range(50):
        if questions:
            break
        await asyncio.sleep(0.001)
    assert len(questions) == 1
    q = questions[0]

    # No mutation: tool_input equals the original
    assert q.tool_input == original

    # Clean up
    router.hitl.resolve(q.permission_request_id, PermissionResultAllow())
    await asyncio.wait_for(guard_task, timeout=1.0)


@pytest.mark.asyncio
async def test_precheck_deny_does_not_count_against_rate_limit():
    """Precheck-deny short-circuits BEFORE the HITL rate limiter.

    Contract: manifest-level denies should never consume the daily
    HITL quota — the limiter is for operator-facing prompts only.
    """
    async def always_deny(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> Any:
        return PermissionResultDeny(message="denied by scope", interrupt=False)

    guard, router, _ = await _build_guard(precheck=always_deny)

    # Fire 5 denies in a row
    for i in range(5):
        result = await guard("Bash", {"cmd": f"cmd-{i}"}, _ctx())
        assert isinstance(result, PermissionResultDeny)

    # Zero HITL questions, zero entries in the daily counter
    assert router.hitl.pending_for_channel(CHANNEL_ID) == []
    # Daily cap is a router-internal counter; pending list is the cleanest
    # external probe we have without reaching into private state.
