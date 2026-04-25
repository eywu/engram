"""Tests for /engram new — session reset slash command and button action."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engram.agent import AgentTurn, _claude_cli_jsonl_for
from engram.bootstrap import provision_channel
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.hitl import HITLRateLimiter
from engram.ingress import (
    SESSION_NEW_ACTION_PATTERN,
    handle_new_command,
    handle_new_confirm_action,
    register_listeners,
)
from engram.manifest import (
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
    load_manifest,
)
from engram.paths import channel_manifest_path
from engram.router import Router, derive_session_id

# ── Fixtures / helpers ───────────────────────────────────────────────────────


class DecoratorApp:
    def __init__(self) -> None:
        self.actions: list[Any] = []
        self.commands: list[Any] = []
        self.events: list[Any] = []
        self.views: list[Any] = []
        self.view_closed_handlers: list[Any] = []

    def action(self, pattern):
        def decorator(func):
            self.actions.append((pattern, func))
            return func
        return decorator

    def command(self, command_name):
        def decorator(func):
            self.commands.append((command_name, func))
            return func
        return decorator

    def event(self, event_name):
        def decorator(func):
            self.events.append((event_name, func))
            return func
        return decorator

    def view(self, callback_id):
        def decorator(func):
            self.views.append((callback_id, func))
            return func
        return decorator

    def view_closed(self, callback_id):
        def decorator(func):
            self.view_closed_handlers.append((callback_id, func))
            return func
        return decorator


class FakeSlackClient:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.ephemeral_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.chat_postMessage = self._chat_post_message
        self.chat_postEphemeral = self._chat_post_ephemeral

    async def _chat_post_message(self, **kwargs):
        ts = f"1713800000.{len(self.post_calls) + 200:06d}"
        self.post_calls.append({**kwargs, "_ts": ts})
        return {"ok": True, "ts": ts}

    async def _chat_post_ephemeral(self, **kwargs):
        self.ephemeral_calls.append(kwargs)
        return {"ok": True}

    async def chat_update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"ok": True}

    async def conversations_info(self, *, channel):
        return {"ok": True, "channel": {"name": "test-channel"}}


class FakeAgent:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []

    async def run_turn(self, session, text, *, user_id=None):
        self.turns.append((session.channel_id, text))
        return AgentTurn(
            text="agent response",
            cost_usd=None,
            duration_ms=1,
            num_turns=1,
            is_error=False,
        )


def _config(
    owner_user_id: str = "U07OWNER",
    owner_dm_channel_id: str = "D07OWNER",
) -> EngramConfig:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )
    cfg.owner_user_id = owner_user_id
    cfg.owner_dm_channel_id = owner_dm_channel_id
    return cfg


def _activate_channel(channel_id: str, home: Path) -> None:
    provision_channel(
        channel_id,
        identity=IdentityTemplate.TASK_ASSISTANT,
        label=f"#{channel_id[-3:].lower()}",
        status=ChannelStatus.ACTIVE,
        home=home,
    )


def _make_confirm_payload(channel_id: str, invoker_user_id: str) -> dict[str, Any]:
    action_id = f"engram_session_new_confirm:{channel_id}"
    return {
        "actions": [
            {
                "action_id": action_id,
                "value": f"{channel_id}|{invoker_user_id}",
            }
        ],
        "user": {"id": invoker_user_id},
        "channel": {"id": channel_id},
    }


def _make_cancel_payload(channel_id: str, invoker_user_id: str) -> dict[str, Any]:
    action_id = f"engram_session_new_cancel:{channel_id}"
    return {
        "actions": [
            {
                "action_id": action_id,
                "value": f"{channel_id}|{invoker_user_id}",
            }
        ],
        "user": {"id": invoker_user_id},
        "channel": {"id": channel_id},
    }


# ── Tests: handle_new_command ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_new_command_rejects_non_owner(tmp_path: Path) -> None:
    """Non-owner caller receives an error and no ephemeral confirm."""
    home = tmp_path / ".engram"
    _activate_channel("C07TEST123", home)
    router = Router(home=home)
    slack = FakeSlackClient()
    config = _config(owner_user_id="U07OWNER")

    result = await handle_new_command(
        router=router,
        config=config,
        slack_client=slack,
        source_channel_id="C07TEST123",
        source_channel_name="test-channel",
        user_id="U07STRANGER",
    )

    assert result["ok"] is False
    assert result["error"] == "not owner"
    assert len(slack.ephemeral_calls) == 1
    assert "Owner-only" in slack.ephemeral_calls[0]["text"]


@pytest.mark.asyncio
async def test_handle_new_command_posts_ephemeral_confirm(tmp_path: Path) -> None:
    """Owner receives an ephemeral confirm with [Start fresh] and [Cancel] buttons."""
    home = tmp_path / ".engram"
    _activate_channel("C07TEST123", home)
    router = Router(home=home)
    slack = FakeSlackClient()
    config = _config(owner_user_id="U07OWNER")

    result = await handle_new_command(
        router=router,
        config=config,
        slack_client=slack,
        source_channel_id="C07TEST123",
        source_channel_name="test-channel",
        user_id="U07OWNER",
    )

    assert result["ok"] is True
    assert result.get("pending_confirm") is True
    assert len(slack.ephemeral_calls) == 1
    # Check that blocks contain both buttons
    blocks = slack.ephemeral_calls[0].get("blocks") or []
    all_action_ids: list[str] = []
    for block in blocks:
        for element in block.get("elements", []):
            if isinstance(element, dict):
                action_id = element.get("action_id")
                if isinstance(action_id, str):
                    all_action_ids.append(action_id)
    assert any("confirm" in aid for aid in all_action_ids), all_action_ids
    assert any("cancel" in aid for aid in all_action_ids), all_action_ids
    # All action IDs match the registered pattern
    for aid in all_action_ids:
        assert SESSION_NEW_ACTION_PATTERN.match(aid), f"action_id {aid!r} not matched"


# ── Tests: handle_new_confirm_action ────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_cancel_does_not_reset_session(tmp_path: Path) -> None:
    """Clicking [Cancel] returns ok=True, cancelled=True; session is untouched."""
    home = tmp_path / ".engram"
    _activate_channel("C07TEST123", home)
    router = Router(home=home)
    slack = FakeSlackClient()
    config = _config(owner_user_id="U07OWNER")

    payload = _make_cancel_payload("C07TEST123", "U07OWNER")
    result = await handle_new_confirm_action(
        payload=payload,
        router=router,
        config=config,
        slack_client=slack,
    )

    assert result["ok"] is True
    assert result.get("cancelled") is True
    # No public message posted
    assert len(slack.post_calls) == 0


@pytest.mark.asyncio
async def test_confirm_rejects_identity_mismatch(tmp_path: Path) -> None:
    """Button clicked by a different user than the invoker is rejected."""
    home = tmp_path / ".engram"
    _activate_channel("C07TEST123", home)
    router = Router(home=home)
    slack = FakeSlackClient()
    config = _config(owner_user_id="U07OWNER")

    payload = _make_confirm_payload("C07TEST123", "U07OWNER")
    # Simulate a different user clicking the button
    payload["user"] = {"id": "U07STRANGER"}

    result = await handle_new_confirm_action(
        payload=payload,
        router=router,
        config=config,
        slack_client=slack,
    )

    assert result["ok"] is False
    assert result["error"] == "identity mismatch"
    assert "response" in result


@pytest.mark.asyncio
async def test_confirm_resets_session_state(tmp_path: Path, monkeypatch) -> None:
    """Confirm action resets session flags and posts a public follow-up."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    home = tmp_path / ".engram"
    _activate_channel("C07TEST123", home)
    router = Router(home=home)
    slack = FakeSlackClient()
    config = _config(owner_user_id="U07OWNER")

    # Prime the session first so we know its cwd
    session = await router.get("C07TEST123", channel_name="test-channel", is_dm=False)
    session.agent_session_initialized = True

    # Pre-seed a JSONL file at the path the session actually uses
    session_id = derive_session_id("C07TEST123")
    jsonl_path = _claude_cli_jsonl_for(session_id, cwd=session.cwd)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text('{"type":"test"}\n', encoding="utf-8")

    payload = _make_confirm_payload("C07TEST123", "U07OWNER")
    result = await handle_new_confirm_action(
        payload=payload,
        router=router,
        config=config,
        slack_client=slack,
    )

    assert result["ok"] is True
    assert result.get("reset") is True
    # Ephemeral replacement payload present
    assert "response" in result
    assert result["response"]["replace_original"] is True

    # Session flags correctly reset
    session = await router.get("C07TEST123")
    assert session.agent_session_initialized is False
    assert session.session_just_started is True

    # JSONL archived
    assert not jsonl_path.exists()
    archived_files = list(jsonl_path.parent.glob(f"{session_id}.jsonl.archived-*"))
    assert len(archived_files) == 1

    # Public follow-up posted
    assert len(slack.post_calls) == 1
    follow_up = slack.post_calls[0]["text"]
    assert "🔄" in follow_up


@pytest.mark.asyncio
async def test_greeting_appears_on_first_message_only(tmp_path: Path) -> None:
    """session_just_started → greeting on first message, not on second."""
    home = tmp_path / ".engram"
    _activate_channel("C07TEST123", home)
    app = DecoratorApp()
    agent = FakeAgent()
    router = Router(home=home)
    slack = FakeSlackClient()
    config = _config()
    register_listeners(app, config, router, agent)
    handler = next(handler for event, handler in app.events if event == "message")

    # Mark session as just-started
    session = await router.get("C07TEST123", channel_name="test-channel", is_dm=False)
    session.session_just_started = True

    say_calls: list[dict] = []

    async def say(*, text, thread_ts=None):
        say_calls.append({"text": text})
        return {"ok": True, "ts": "1713800001.000001"}

    # First message — should include greeting
    await handler(
        event={
            "channel": "C07TEST123",
            "channel_type": "channel",
            "user": "U07OWNER",
            "text": "hello",
            "ts": "1713800001.000001",
        },
        say=say,
        client=slack,
    )

    assert len(slack.post_calls) >= 1
    first_post_text = slack.post_calls[0].get("text") or ""
    first_post_blocks = slack.post_calls[0].get("blocks") or []
    first_post_body = first_post_text
    if first_post_blocks:
        first_post_body = " ".join(
            b.get("text", "") if isinstance(b.get("text"), str)
            else str(b.get("text", {}).get("text", ""))
            for b in first_post_blocks
        )
    assert "👋" in first_post_body or any(
        "👋" in str(b) for b in first_post_blocks
    ), f"greeting missing: blocks={first_post_blocks!r}"

    # session_just_started should be cleared now
    assert session.session_just_started is False

    # Second message — no greeting
    n_posts_before = len(slack.post_calls)
    await handler(
        event={
            "channel": "C07TEST123",
            "channel_type": "channel",
            "user": "U07OWNER",
            "text": "second message",
            "ts": "1713800002.000001",
        },
        say=say,
        client=slack,
    )

    second_post_blocks = slack.post_calls[n_posts_before].get("blocks") or []
    second_post_text = slack.post_calls[n_posts_before].get("text") or ""
    assert "👋" not in second_post_text
    assert all("👋" not in str(b) for b in second_post_blocks)


# ── Regression: YOLO grants persist across /engram new ──────────────────────


@pytest.mark.asyncio
async def test_yolo_grant_persists_after_new(tmp_path: Path, monkeypatch) -> None:
    """A YOLO grant is preserved in the manifest after /engram new confirm."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    home = tmp_path / ".engram"
    _activate_channel("C07TEST123", home)

    # Grant YOLO in manifest
    manifest_path = channel_manifest_path("C07TEST123", home)
    manifest = load_manifest(manifest_path)
    yolo_until = datetime.now(UTC) + timedelta(hours=24)
    updated = manifest.model_copy(update={
        "permission_tier": PermissionTier.YOLO,
        "yolo_until": yolo_until,
    })
    from engram.manifest import dump_manifest
    dump_manifest(updated, manifest_path)

    router = Router(home=home)
    slack = FakeSlackClient()
    config = _config(owner_user_id="U07OWNER")

    payload = _make_confirm_payload("C07TEST123", "U07OWNER")
    result = await handle_new_confirm_action(
        payload=payload,
        router=router,
        config=config,
        slack_client=slack,
    )
    assert result["ok"] is True

    # Reload manifest and verify YOLO still there
    manifest_after = load_manifest(manifest_path)
    assert manifest_after.permission_tier == PermissionTier.YOLO
    assert manifest_after.yolo_until is not None


# ── Regression: HITL daily count NOT reset ──────────────────────────────────


@pytest.mark.asyncio
async def test_hitl_daily_count_not_reset_by_new(tmp_path: Path, monkeypatch) -> None:
    """Calling /engram new does not reset the HITL rate-limiter daily counter."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    home = tmp_path / ".engram"
    _activate_channel("C07TEST123", home)
    router = Router(home=home)

    # Manually bump the HITL limiter counter via its reserve() API
    limiter: HITLRateLimiter = router.hitl_limiter
    limiter.reserve("C07TEST123")
    # Verify the counter was bumped (can still check with check())
    _allowed_before, _ = limiter.check("C07TEST123")
    # Get raw count by inspecting internal state
    from datetime import date as _date
    _today = _date.today()
    _day, count_before = limiter._daily_counts.get("C07TEST123", (_today, 0))
    assert count_before > 0

    slack = FakeSlackClient()
    config = _config(owner_user_id="U07OWNER")
    payload = _make_confirm_payload("C07TEST123", "U07OWNER")
    await handle_new_confirm_action(
        payload=payload,
        router=router,
        config=config,
        slack_client=slack,
    )

    _day2, count_after = limiter._daily_counts.get("C07TEST123", (_today, 0))
    assert count_after == count_before, "HITL daily count must not change after /engram new"


# ── Regression: memory entries remain searchable after reset ─────────────────


@pytest.mark.asyncio
async def test_memory_entries_still_searchable_after_new(
    tmp_path: Path, monkeypatch
) -> None:
    """Memory transcript rows for a channel survive /engram new."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    home = tmp_path / ".engram"
    _activate_channel("C07TEST123", home)

    # Seed a transcript row in the memory DB
    from engram.memory import open_memory_db
    db_path = home / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_memory_db(db_path)
    conn.execute(
        "INSERT INTO transcripts (session_id, channel_id, ts, role, message_uuid, text) "
        "VALUES (?, ?, datetime('now'), 'user', 'uuid-test-1', 'before-reset message')",
        (derive_session_id("C07TEST123"), "C07TEST123"),
    )
    conn.commit()

    router = Router(home=home)
    slack = FakeSlackClient()
    config = _config(owner_user_id="U07OWNER")
    payload = _make_confirm_payload("C07TEST123", "U07OWNER")
    await handle_new_confirm_action(
        payload=payload,
        router=router,
        config=config,
        slack_client=slack,
    )

    # Memory row still present
    row = conn.execute(
        "SELECT count(*) FROM transcripts WHERE channel_id = ? AND text = ?",
        ("C07TEST123", "before-reset message"),
    ).fetchone()
    assert row[0] == 1, "memory transcript row must survive /engram new"
    conn.close()
