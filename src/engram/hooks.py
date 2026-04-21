"""Claude SDK hooks for incrementally ingesting session memory."""
from __future__ import annotations

import datetime as dt
import inspect
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import HookMatcher

from engram import memory
from engram.router import SessionState

log = logging.getLogger(__name__)


@dataclass
class PendingCompaction:
    session_id: str
    trigger: str
    custom_instructions: str | None
    ts: str

    def __getitem__(self, key: str) -> str | None:
        return getattr(self, key)

    def get(self, key: str, default: str | None = None) -> str | None:
        return getattr(self, key, default)


@dataclass
class TranscriptRecord:
    ts: str | None
    role: str
    message_uuid: str
    parent_uuid: str | None
    text: str


pending_compactions: dict[str, PendingCompaction] = {}


def make_hooks(
    session_state: SessionState,
    memory_conn: sqlite3.Connection,
) -> dict[str, list[HookMatcher]]:
    """Build Claude SDK hooks bound to one Slack channel session."""

    async def stop_hook(
        hook_input: dict[str, Any],
        _tool_use_id: str | None = None,
        _context: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        try:
            await _handle_stop(session_state, memory_conn, hook_input)
        except Exception:
            log.exception("hooks.stop_failed session=%s", session_state.label())
        return {"continue_": True}

    async def precompact_hook(
        hook_input: dict[str, Any],
        _tool_use_id: str | None = None,
        _context: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        try:
            _handle_precompact(session_state, hook_input)
        except Exception:
            log.exception("hooks.precompact_failed session=%s", session_state.label())
        return {"continue_": True}

    return {
        "Stop": [HookMatcher(hooks=[stop_hook])],
        "PreCompact": [HookMatcher(hooks=[precompact_hook])],
    }


async def _handle_stop(
    session_state: SessionState,
    memory_conn: sqlite3.Connection,
    hook_input: dict[str, Any],
) -> None:
    sid = _hook_session_id(hook_input, session_state)
    client = session_state.agent_client
    if client is None:
        log.debug("hooks.stop_no_client session=%s", session_state.label())
        return

    watermark = memory.get_watermark(memory_conn, sid)
    messages = await _fetch_session_messages(client, sid, watermark)
    newest_uuid: str | None = None
    compact_summary: TranscriptRecord | None = None

    for raw_message in messages:
        record = _extract_transcript_record(raw_message)
        if not record.message_uuid:
            log.debug("hooks.stop_skip_message_without_uuid session_id=%s", sid)
            continue

        memory.insert_transcript(
            memory_conn,
            session_id=sid,
            channel_id=session_state.channel_id,
            ts=record.ts,
            role=record.role,
            message_uuid=record.message_uuid,
            parent_uuid=record.parent_uuid,
            text=record.text,
        )
        newest_uuid = record.message_uuid
        if record.role == "summary" and compact_summary is None:
            compact_summary = record

    if newest_uuid is not None:
        memory.set_watermark(memory_conn, sid, newest_uuid)

    pending = pending_compactions.get(sid)
    if pending is not None and compact_summary is not None:
        memory.insert_summary(
            memory_conn,
            session_id=sid,
            channel_id=session_state.channel_id,
            ts=compact_summary.ts or pending.ts,
            trigger="compact",
            summary_text=compact_summary.text,
            custom_instructions=pending.custom_instructions,
            source_message_uuid=compact_summary.message_uuid,
        )
        pending_compactions.pop(sid, None)

    log.info(
        "hooks.stop_ingested session=%s messages=%d newest_uuid=%s",
        session_state.label(),
        len(messages),
        newest_uuid or "(none)",
    )


def _handle_precompact(
    session_state: SessionState,
    hook_input: dict[str, Any],
) -> None:
    sid = _hook_session_id(hook_input, session_state)
    pending_compactions[sid] = PendingCompaction(
        session_id=sid,
        trigger=str(_value(hook_input, "trigger") or ""),
        custom_instructions=_optional_str(
            _value(hook_input, "custom_instructions", "customInstructions")
        ),
        ts=_utc_now(),
    )
    log.info(
        "hooks.precompact_pending session=%s trigger=%s",
        session_state.label(),
        pending_compactions[sid].trigger or "(unknown)",
    )


async def _fetch_session_messages(
    client: Any,
    session_id: str,
    watermark: str | None,
) -> list[Any]:
    method = client.get_session_messages
    try:
        result = method(session_id=session_id, since=watermark)
    except TypeError:
        result = method(session_id=session_id)
        if inspect.isawaitable(result):
            result = await result
        return _messages_after_watermark(result or [], watermark)

    if inspect.isawaitable(result):
        result = await result
    return list(result or [])


def _messages_after_watermark(messages: Any, watermark: str | None) -> list[Any]:
    items = list(messages or [])
    if watermark is None:
        return items

    for index, message in enumerate(items):
        message_uuid = _message_uuid(message)
        if message_uuid == watermark:
            return items[index + 1 :]
    return items


def _extract_transcript_record(message: Any) -> TranscriptRecord:
    raw_message = _value(message, "message")
    role = _optional_str(
        _value(message, "role")
        or _value(raw_message, "role")
        or _value(message, "type")
    )
    return TranscriptRecord(
        ts=_optional_str(
            _value(message, "ts", "timestamp", "created_at", "createdAt")
            or _value(raw_message, "ts", "timestamp", "created_at", "createdAt")
        ),
        role=role or "unknown",
        message_uuid=_message_uuid(message),
        parent_uuid=_optional_str(
            _value(message, "parent_uuid", "parentUuid", "parent_id", "parent")
            or _value(raw_message, "parent_uuid", "parentUuid", "parent_id", "parent")
        ),
        text=_extract_text(message),
    )


def _message_uuid(message: Any) -> str:
    raw_message = _value(message, "message")
    return _optional_str(
        _value(message, "message_uuid", "messageUuid", "uuid", "id")
        or _value(raw_message, "message_uuid", "messageUuid", "uuid", "id")
    ) or ""


def _extract_text(message: Any) -> str:
    raw_message = _value(message, "message")
    for candidate in (message, raw_message):
        text = _content_to_text(_value(candidate, "text", "summary", "summary_text"))
        if text:
            return text
        text = _content_to_text(_value(candidate, "content"))
        if text:
            return text
    return _content_to_text(raw_message)


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "summary", "summary_text", "content", "message"):
            text = _content_to_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        return "\n".join(
            text for item in value if (text := _content_to_text(item))
        )
    for attr in ("text", "summary", "summary_text", "content", "message"):
        if hasattr(value, attr):
            text = _content_to_text(getattr(value, attr))
            if text:
                return text
    return str(value)


def _hook_session_id(
    hook_input: dict[str, Any],
    session_state: SessionState,
) -> str:
    return _optional_str(_value(hook_input, "session_id", "sessionId")) or (
        session_state.session_id
    )


def _value(obj: Any, *keys: str) -> Any:
    if obj is None:
        return None
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            return obj[key]
        if hasattr(obj, key):
            return getattr(obj, key)
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()
