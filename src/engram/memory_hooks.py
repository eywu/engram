"""Claude Agent SDK hooks that ingest transcripts into memory.db."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookInput, HookJSONOutput, HookMatcher, get_session_messages

from engram.memory import (
    get_watermark,
    insert_summary,
    insert_transcript,
    open_memory_db,
    set_watermark,
)
from engram.telemetry import logger as telemetry_logger

log = telemetry_logger.getChild("memory_hooks")
pending_compactions: dict[str, dict[str, Any]] = {}

_COMPACTION_SUMMARY_PREFIXES = (
    "this session is being continued from a previous conversation",
    "this conversation is being continued from a previous conversation",
    "previous conversation summary",
    "compaction summary",
    "compact summary",
    "summary",
)


def make_memory_hooks(router: Any) -> list[HookMatcher]:
    """Return Stop and PreCompact hook matchers bound to the bridge router."""

    async def stop_hook(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> HookJSONOutput:
        try:
            _handle_stop_hook(router, hook_input)
        except Exception:
            log.error(
                "memory.stop_failed session_id=%s",
                hook_input.get("session_id"),
                exc_info=True,
            )
        return {"continue_": True, "suppressOutput": True}

    async def precompact_hook(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> HookJSONOutput:
        try:
            _handle_precompact_hook(hook_input)
        except Exception:
            log.error(
                "memory.precompact_failed session_id=%s",
                hook_input.get("session_id"),
                exc_info=True,
            )
        return {"continue_": True, "suppressOutput": True}

    return [
        HookMatcher(matcher=None, hooks=[stop_hook]),
        HookMatcher(matcher=None, hooks=[precompact_hook]),
    ]


def _handle_precompact_hook(hook_input: HookInput) -> None:
    session_id = str(hook_input.get("session_id") or "")
    if not session_id:
        log.warning("memory.precompact_missing_session")
        return

    pending_compactions[session_id] = {
        "trigger": hook_input.get("trigger"),
        "custom_instructions": hook_input.get("custom_instructions"),
        "ts": datetime.now(UTC),
    }
    log.info(
        "memory.precompact_pending session_id=%s trigger=%s",
        session_id,
        hook_input.get("trigger"),
    )


def _handle_stop_hook(router: Any, hook_input: HookInput) -> None:
    session_id = str(hook_input.get("session_id") or "")
    if not session_id:
        log.warning("memory.stop_missing_session")
        return

    channel_id = router.get_channel_by_session_id(session_id)
    if channel_id is None:
        log.warning("memory.stop_unknown_session session_id=%s", session_id)
        return

    transcript_path = hook_input.get("transcript_path")
    metadata_by_uuid = _parse_jsonl_for_metadata(str(transcript_path)) if transcript_path else {}
    conn = open_memory_db()
    try:
        watermark_uuid, _watermark_ts = get_watermark(conn, session_id)
        messages = list(
            get_session_messages(
                session_id=session_id,
                directory=_directory_from_hook_input(hook_input),
            )
        )
        new_messages = _messages_after_watermark(messages, session_id, watermark_uuid)
        rows_inserted = 0
        message_timestamps: dict[str, datetime] = {}

        for message in new_messages:
            message_uuid = str(getattr(message, "uuid", "") or "")
            if not message_uuid:
                log.warning("memory.stop_message_without_uuid session_id=%s", session_id)
                continue

            metadata = metadata_by_uuid.get(message_uuid, {})
            ts = _metadata_ts(metadata)
            message_timestamps[message_uuid] = ts
            if insert_transcript(
                conn,
                session_id=session_id,
                channel_id=channel_id,
                ts=ts,
                role=_extract_role(message),
                message_uuid=message_uuid,
                parent_uuid=_metadata_parent_uuid(metadata),
                text=_extract_text(message),
            ):
                rows_inserted += 1

        if rows_inserted and new_messages:
            last_message = new_messages[-1]
            last_uuid = str(getattr(last_message, "uuid", "") or "")
            if last_uuid:
                set_watermark(
                    conn,
                    session_id,
                    last_uuid,
                    message_timestamps.get(last_uuid, datetime.now(UTC)),
                )

        _promote_pending_compaction(
            conn,
            session_id=session_id,
            channel_id=channel_id,
            new_messages=new_messages,
        )
        log.info(
            "memory.stop_ingested session_id=%s channel_id=%s rows=%d",
            session_id,
            channel_id,
            rows_inserted,
        )
    finally:
        conn.close()


def _messages_after_watermark(
    messages: list[Any],
    session_id: str,
    watermark_uuid: str | None,
) -> list[Any]:
    if watermark_uuid is None:
        return messages

    for index, message in enumerate(messages):
        if getattr(message, "uuid", None) == watermark_uuid:
            return messages[index + 1 :]

    log.warning(
        "memory.stop_watermark_not_found session_id=%s message_uuid=%s",
        session_id,
        watermark_uuid,
    )
    return messages


def _promote_pending_compaction(
    conn: Any,
    *,
    session_id: str,
    channel_id: str,
    new_messages: list[Any],
) -> None:
    pending = pending_compactions.get(session_id)
    if pending is None:
        return

    for message in new_messages:
        text = _extract_text(message).strip()
        if text and _is_compaction_summary_message(message, text):
            insert_summary(
                conn,
                session_id=session_id,
                channel_id=channel_id,
                ts=datetime.now(UTC),
                trigger="compact",
                custom_instructions=pending.get("custom_instructions"),
                summary_text=text,
            )
            del pending_compactions[session_id]
            log.info("memory.compaction_summary_inserted session_id=%s", session_id)
            return


def _is_compaction_summary_message(message: Any, text: str) -> bool:
    if _extract_role(message).lower() == "summary":
        return True
    normalized = text.lstrip().lower()
    return any(
        normalized.startswith(prefix)
        for prefix in _COMPACTION_SUMMARY_PREFIXES
    )


def _directory_from_hook_input(hook_input: HookInput) -> str | None:
    cwd = hook_input.get("cwd")
    return str(cwd) if cwd else None


def _metadata_ts(metadata: dict[str, Any]) -> datetime:
    ts = metadata.get("ts")
    if isinstance(ts, datetime):
        return ts
    return datetime.now(UTC)


def _metadata_parent_uuid(metadata: dict[str, Any]) -> str | None:
    parent_uuid = metadata.get("parent_uuid")
    return str(parent_uuid) if parent_uuid else None


def _extract_role(message: Any) -> str:
    payload = getattr(message, "message", None)
    role: object = None
    if isinstance(payload, dict):
        role = payload.get("role") or payload.get("type")
    elif payload is not None:
        role = getattr(payload, "role", None) or getattr(payload, "type", None)
    if role is None:
        role = getattr(message, "type", None)
    return str(role or "unknown")


def _extract_text(message: Any) -> str:
    payload = getattr(message, "message", None)
    content: object
    if isinstance(payload, dict):
        content = payload.get("content", "")
    elif payload is not None and hasattr(payload, "content"):
        content = payload.content
    else:
        content = payload
    return _content_to_text(content)


def _content_to_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_text_from_block(block) for block in content)
    if isinstance(content, dict):
        return _text_from_block(content)
    text = getattr(content, "text", None)
    return text if isinstance(text, str) else str(content)


def _text_from_block(block: object) -> str:
    if isinstance(block, dict):
        block_type = block.get("type")
        text = block.get("text")
        if (block_type == "text" or block_type is None) and isinstance(text, str):
            return text
        return ""

    block_type = getattr(block, "type", None)
    text = getattr(block, "text", None)
    if (block_type == "text" or block_type is None) and isinstance(text, str):
        return text
    return ""


def _parse_jsonl_for_metadata(transcript_path: str) -> dict[str, dict[str, Any]]:
    """Return timestamp and parent UUID metadata keyed by message UUID."""
    metadata: dict[str, dict[str, Any]] = {}
    path = Path(transcript_path)
    if not path.exists():
        return metadata

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                uuid = _payload_uuid(payload)
                if not uuid:
                    continue
                metadata[uuid] = {
                    "ts": _parse_timestamp(_payload_timestamp(payload)),
                    "parent_uuid": _payload_parent_uuid(payload),
                }
    except OSError:
        log.warning("memory.jsonl_metadata_read_failed path=%s", transcript_path, exc_info=True)
    return metadata


def _payload_uuid(payload: dict[str, Any]) -> str | None:
    raw = payload.get("uuid")
    if raw is None and isinstance(payload.get("message"), dict):
        raw = payload["message"].get("uuid")
    return str(raw) if raw else None


def _payload_timestamp(payload: dict[str, Any]) -> object:
    for key in ("timestamp", "created_at", "ts"):
        if payload.get(key) is not None:
            return payload[key]
    if isinstance(payload.get("message"), dict):
        return payload["message"].get("timestamp")
    return None


def _payload_parent_uuid(payload: dict[str, Any]) -> str | None:
    raw = (
        payload.get("parentUuid")
        or payload.get("parent_uuid")
        or payload.get("parentUUID")
    )
    if raw is None and isinstance(payload.get("message"), dict):
        raw = payload["message"].get("parentUuid") or payload["message"].get("parent_uuid")
    return str(raw) if raw else None


def _parse_timestamp(raw: object) -> datetime:
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(raw, UTC)
    if isinstance(raw, str):
        value = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return datetime.now(UTC)
