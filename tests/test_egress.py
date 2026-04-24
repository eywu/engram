"""Egress tests — chunking + post shape, no real Slack calls."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
from slack_sdk.errors import SlackApiError

from engram.agent import AgentTurn
from engram.egress import (
    SLACK_MAX_TEXT_LEN,
    _chunk_text,
    _notification_fallback,
    _suggestion_label,
    post_question,
    post_reply,
    update_question_resolved,
    update_question_timeout,
)
from engram.footguns import match_footgun
from engram.hitl import PendingQuestion
from engram.manifest import ChannelManifest, IdentityTemplate, PermissionTier


class FakeSlackClient:
    def __init__(self) -> None:
        self.post_calls = []
        self.update_calls = []
        self.chat_postMessage = self.chat_post_message

    async def chat_post_message(self, **kwargs):
        self.post_calls.append(kwargs)
        return {"ts": "1713800000.000100"}

    async def chat_update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"ok": True}


def make_question(
    *,
    suggestions=None,
    tool_name: str = "Bash",
    channel_manifest=None,
    tool_input=None,
    footgun_match=None,
) -> PendingQuestion:
    return PendingQuestion(
        permission_request_id="prq-1",
        channel_id="C07TEST123",
        session_id="session-1",
        turn_id="turn-1",
        tool_name=tool_name,
        tool_input=dict(tool_input or {"cmd": "pytest", "timeout": 30}),
        suggestions=list(suggestions or []),
        who_can_answer=None,
        posted_at=datetime(2026, 4, 22, tzinfo=UTC),
        timeout_s=300,
        slack_channel_ts="1713800000.000100",
        slack_thread_ts="1713800000.000100",
        channel_manifest=channel_manifest,
        footgun_match=footgun_match,
    )


def owner_dm_manifest() -> ChannelManifest:
    return ChannelManifest(
        channel_id="D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        permission_tier=PermissionTier.OWNER_SCOPED,
    )


def team_manifest() -> ChannelManifest:
    return ChannelManifest(
        channel_id="C07TEAM",
        identity=IdentityTemplate.TASK_ASSISTANT,
    )


def test_chunk_short_text_single():
    assert _chunk_text("hi", 100) == ["hi"]


def test_chunk_splits_on_blank_line():
    first = "a" * 50
    second = "b" * 50
    text = f"{first}\n\n{second}"
    out = _chunk_text(text, 60)
    assert len(out) == 2
    assert out[0].endswith("a")
    assert out[1].startswith("b")


def test_chunk_splits_on_newline_if_no_blank():
    text = "line1\n" + "b" * 200 + "\nline3"
    out = _chunk_text(text, 100)
    assert len(out) >= 2
    # No chunk exceeds the limit
    assert all(len(c) <= 100 for c in out)


def test_chunk_hard_split_if_no_newlines():
    text = "a" * 250
    out = _chunk_text(text, 100)
    assert len(out) == 3
    assert "".join(out) == text


@pytest.mark.asyncio
async def test_reply_renders_markdown_block():
    slack = FakeSlackClient()
    turn = AgentTurn(
        text="hello **bold** there",
        cost_usd=0.001,
        duration_ms=100,
        num_turns=1,
        is_error=False,
    )
    res = await post_reply(
        slack,
        "C07TEST123",
        turn,
        thread_ts="T1",
        session_label="ch:C1",
    )
    assert res.chunks_posted == 1
    assert res.posted_message_ts == "1713800000.000100"
    assert len(slack.post_calls) == 1
    call = slack.post_calls[0]
    assert call["channel"] == "C07TEST123"
    assert call["thread_ts"] == "T1"
    assert call["blocks"] == [
        {
            "type": "markdown",
            "text": "hello **bold** there\n\ncost: $0.0010 · 100ms",
        }
    ]
    assert call["text"] == "hello bold there"


@pytest.mark.asyncio
async def test_post_reply_chunks_long_text():
    slack = FakeSlackClient()
    long_text = "a" * (SLACK_MAX_TEXT_LEN + 100)
    turn = AgentTurn(
        text=long_text,
        cost_usd=None,
        duration_ms=None,
        num_turns=None,
        is_error=False,
    )
    res = await post_reply(
        slack,
        "C07TEST123",
        turn,
        thread_ts=None,
        session_label="ch:C1",
    )
    assert res.chunks_posted == 2
    assert len(slack.post_calls) == 2


@pytest.mark.asyncio
async def test_reply_chunks_within_slack_markdown_block_limit():
    """Slack's markdown block caps text at 12,000 chars; chunker must respect that."""
    slack = FakeSlackClient()
    long_text = "a" * 20_000
    turn = AgentTurn(
        text=long_text,
        cost_usd=None,
        duration_ms=None,
        num_turns=1,
        is_error=False,
    )

    await post_reply(
        slack,
        "C07TEST123",
        turn,
        thread_ts=None,
        session_label="ch:C1",
    )

    for call in slack.post_calls:
        md_text = call["blocks"][0]["text"]
        assert len(md_text) <= SLACK_MAX_TEXT_LEN


@pytest.mark.asyncio
async def test_reply_preserves_thread_ts():
    slack = FakeSlackClient()
    turn = AgentTurn(
        text="threaded reply",
        cost_usd=None,
        duration_ms=None,
        num_turns=1,
        is_error=False,
    )

    await post_reply(slack, "C07TEST123", turn, thread_ts="1713800000.000200")

    assert slack.post_calls[0]["thread_ts"] == "1713800000.000200"


@pytest.mark.asyncio
async def test_post_reply_logs_chunk_failure_and_reraises(
    caplog: pytest.LogCaptureFixture,
):
    slack = FakeSlackClient()
    error = SlackApiError("chunk rejected", {"ok": False, "error": "invalid_blocks_format"})

    async def raise_slack_error(**kwargs):
        raise error

    slack.chat_postMessage = raise_slack_error
    turn = AgentTurn(
        text="a" * (SLACK_MAX_TEXT_LEN + 1),
        cost_usd=None,
        duration_ms=None,
        num_turns=1,
        is_error=False,
    )

    with (
        caplog.at_level(logging.ERROR, logger="engram.egress"),
        pytest.raises(SlackApiError),
    ):
        await post_reply(
            slack,
            "C07TEST123",
            turn,
            thread_ts="1713800000.000200",
            session_label="ch:C07TEST123",
        )

    record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("egress.chunk_failed")
    )
    assert (
        record.getMessage()
        == "egress.chunk_failed session=ch:C07TEST123 chunk=1/2 error_type=SlackApiError"
    )
    assert record.exc_info is not None


def test_notification_fallback_strips_markdown():
    body = "**bold** with [example](https://example.com), `code`, and ~~strike~~"

    assert _notification_fallback(body) == "bold with example, code, and strike"

    long = "# " + ("a" * 200)
    fallback = _notification_fallback(long)
    assert fallback.endswith("…")
    assert len(fallback) == 120


def test_notification_fallback_empty_input():
    assert _notification_fallback("") == ""


@pytest.mark.asyncio
async def test_reply_with_code_block():
    slack = FakeSlackClient()
    body = "```python\nprint('hi')\n```"
    turn = AgentTurn(
        text=body,
        cost_usd=None,
        duration_ms=None,
        num_turns=1,
        is_error=False,
    )

    await post_reply(slack, "C07TEST123", turn)

    assert slack.post_calls[0]["blocks"] == [{"type": "markdown", "text": body}]
    assert slack.post_calls[0]["text"] == "print('hi')"


@pytest.mark.asyncio
async def test_reply_with_link():
    slack = FakeSlackClient()
    body = "[example](https://example.com)"
    turn = AgentTurn(
        text=body,
        cost_usd=None,
        duration_ms=None,
        num_turns=1,
        is_error=False,
    )

    await post_reply(slack, "C07TEST123", turn)

    assert slack.post_calls[0]["blocks"] == [{"type": "markdown", "text": body}]
    assert slack.post_calls[0]["text"] == "example"


@pytest.mark.asyncio
async def test_post_question_block_kit_shape():
    slack = FakeSlackClient()
    q = make_question(suggestions=[{"name": "Allow"}])

    channel_ts, thread_ts = await post_question(q, slack)

    assert (channel_ts, thread_ts) == ("1713800000.000100", "1713800000.000100")
    assert len(slack.post_calls) == 1
    call = slack.post_calls[0]
    blocks = call["blocks"]
    assert call["channel"] == "C07TEST123"
    assert call["text"] == "🤔 Can I proceed with `Bash`?"
    assert blocks[0]["text"]["text"] == "🤔 Can I proceed with `Bash`?"
    assert blocks[1]["text"]["text"].startswith("```")
    assert '"cmd": "pytest"' in blocks[1]["text"]["text"]
    assert blocks[1]["text"]["text"].endswith("```")
    assert blocks[2]["type"] == "actions"
    assert blocks[2]["block_id"] == "hitl_actions"
    assert blocks[3]["type"] == "context"
    assert "reply in this thread" in blocks[3]["elements"][0]["text"]


@pytest.mark.asyncio
async def test_post_question_suggestion_buttons():
    slack = FakeSlackClient()
    q = make_question(suggestions=[{"name": "A"}, {"name": "B"}, {"name": "C"}])

    await post_question(q, slack)

    elements = slack.post_calls[0]["blocks"][2]["elements"]
    assert len(elements) == 4
    assert [element["text"]["text"] for element in elements] == ["A", "B", "C", "Deny"]
    assert [element["value"] for element in elements] == [
        "prq-1|0",
        "prq-1|1",
        "prq-1|2",
        "prq-1|deny",
    ]


@pytest.mark.asyncio
async def test_post_question_suggestions_truncated_at_5():
    slack = FakeSlackClient()
    q = make_question(suggestions=[{"name": f"Choice {i}"} for i in range(10)])

    await post_question(q, slack)

    elements = slack.post_calls[0]["blocks"][2]["elements"]
    assert len(elements) == 6
    assert [element["text"]["text"] for element in elements] == [
        "Choice 0",
        "Choice 1",
        "Choice 2",
        "Choice 3",
        "Choice 4",
        "Deny",
    ]


@pytest.mark.asyncio
async def test_post_question_deny_button_has_danger_style():
    slack = FakeSlackClient()
    q = make_question()

    await post_question(q, slack)

    deny = slack.post_calls[0]["blocks"][2]["elements"][-1]
    assert deny["action_id"] == "hitl_choice_deny"
    assert deny["style"] == "danger"


@pytest.mark.asyncio
async def test_post_question_renders_sticky_button_for_owner_dm_webfetch():
    slack = FakeSlackClient()
    q = make_question(
        suggestions=[],
        tool_name="WebFetch",
        channel_manifest=owner_dm_manifest(),
    )

    await post_question(q, slack)

    elements = slack.post_calls[0]["blocks"][2]["elements"]
    assert [element["text"]["text"] for element in elements] == [
        "Allow fetch",
        "Always allow fetch",
        "Deny",
    ]
    assert elements[0]["style"] == "primary"
    assert "style" not in elements[1]
    assert elements[1]["action_id"] == "hitl_choice_always_0"
    assert elements[1]["value"] == "prq-1|always|WebFetch"
    assert elements[2]["style"] == "danger"


@pytest.mark.asyncio
async def test_post_question_bash_keeps_two_button_layout():
    slack = FakeSlackClient()
    q = make_question(
        suggestions=[],
        tool_name="Bash",
        channel_manifest=owner_dm_manifest(),
    )

    await post_question(q, slack)

    elements = slack.post_calls[0]["blocks"][2]["elements"]
    assert [element["text"]["text"] for element in elements] == [
        "Allow shell command",
        "Deny",
    ]


@pytest.mark.asyncio
async def test_post_question_team_channel_webfetch_keeps_two_button_layout():
    slack = FakeSlackClient()
    q = make_question(
        suggestions=[],
        tool_name="WebFetch",
        channel_manifest=team_manifest(),
    )

    await post_question(q, slack)

    elements = slack.post_calls[0]["blocks"][2]["elements"]
    assert [element["text"]["text"] for element in elements] == [
        "Allow fetch",
        "Deny",
    ]


@pytest.mark.asyncio
async def test_post_question_renders_footgun_confirmation_card():
    slack = FakeSlackClient()
    command = "rm -rf /tmp/demo"
    q = make_question(
        suggestions=[],
        tool_name="Bash",
        channel_manifest=owner_dm_manifest(),
        tool_input={"cmd": command},
        footgun_match=match_footgun("Bash", {"cmd": command}),
    )

    await post_question(q, slack)

    call = slack.post_calls[0]
    blocks = call["blocks"]
    assert call["text"] == "⚠️ Destructive action confirmation required"
    assert blocks[0]["text"]["text"] == "⚠️ Destructive action confirmation required"
    assert blocks[1]["text"]["text"] == "*Matched rule:* recursive rm command"
    assert blocks[2]["text"]["text"] == f"```{command}```"
    assert blocks[3]["block_id"] == "footgun_actions"
    assert [element["text"]["text"] for element in blocks[3]["elements"]] == [
        "Confirm..."
    ]
    assert blocks[3]["elements"][0]["action_id"] == "footgun_confirm_open"
    assert "Always allow" not in json.dumps(blocks)


@pytest.mark.asyncio
async def test_post_question_derives_footgun_confirmation_when_metadata_missing():
    slack = FakeSlackClient()
    command = "rm -rf /tmp/demo"
    q = make_question(
        suggestions=[],
        tool_name="Bash",
        channel_manifest=owner_dm_manifest(),
        tool_input={"cmd": command},
        footgun_match=None,
    )

    await post_question(q, slack)

    call = slack.post_calls[0]
    assert call["text"] == "⚠️ Destructive action confirmation required"
    assert q.footgun_match is not None
    assert q.footgun_match.command == command
    assert "Always allow" not in json.dumps(call["blocks"])


@pytest.mark.asyncio
async def test_update_question_resolved_strips_buttons():
    slack = FakeSlackClient()
    q = make_question()

    await update_question_resolved(q, "approved", slack)

    assert len(slack.update_calls) == 1
    call = slack.update_calls[0]
    assert call["channel"] == "C07TEST123"
    assert call["ts"] == "1713800000.000100"
    assert call["text"] == "Answered: approved"
    assert all(block["type"] != "actions" for block in call["blocks"])
    assert call["blocks"][0]["text"]["text"] == "✅ Answered: approved"


@pytest.mark.asyncio
async def test_update_question_timeout_has_clock_emoji():
    slack = FakeSlackClient()
    q = make_question()

    await update_question_timeout(q, slack)

    call = slack.update_calls[0]
    assert call["text"] == "Timed out"
    assert "⏱️ Question timed out" in call["blocks"][0]["text"]["text"]


@pytest.mark.asyncio
async def test_update_footgun_question_timeout_denies_command():
    slack = FakeSlackClient()
    q = make_question(
        tool_input={"cmd": "rm -rf /tmp/demo"},
        footgun_match=match_footgun("Bash", {"cmd": "rm -rf /tmp/demo"}),
    )

    await update_question_timeout(q, slack)

    call = slack.update_calls[0]
    assert call["text"] == "Timed out"
    assert "command denied" in call["blocks"][0]["text"]["text"]


@pytest.mark.asyncio
async def test_update_question_timeout_derives_footgun_metadata():
    slack = FakeSlackClient()
    q = make_question(
        tool_input={"cmd": "rm -rf /tmp/demo"},
        footgun_match=None,
    )

    await update_question_timeout(q, slack)

    call = slack.update_calls[0]
    assert call["text"] == "Timed out"
    assert "command denied" in call["blocks"][0]["text"]["text"]
    assert q.footgun_match is not None


def test_suggestion_label_extraction():
    # 1. Explicit override in a dict (internal flows like OQ31)
    assert _suggestion_label({"name": "Allow once"}) == "Allow once"
    assert _suggestion_label({"label": "Allow with label"}) == "Allow with label"
    # 2. Dict with no name/label falls through to the semantic default,
    #    NOT the old placeholder "choice"
    assert _suggestion_label({"unknown": "value"}) == "Allow"
    assert _suggestion_label({"unknown": "value"}, tool_name="WebFetch") == "Allow fetch"
    # 3. Universal fallback for bare / unknown-shape suggestions (e.g. None)
    assert _suggestion_label(None) == "Allow"
    assert _suggestion_label(None, tool_name="Bash") == "Allow shell command"
    assert _suggestion_label(None, tool_name="UnknownTool") == "Allow"
    # 4. 40-char cap still applies for long explicit overrides
    assert _suggestion_label({"name": "x" * 50}) == "x" * 40


def test_suggestion_label_from_sdk_permission_update():
    """The SDK emits PermissionUpdate dataclasses — derive labels from .type."""
    from claude_agent_sdk.types import PermissionUpdate

    add_rules = PermissionUpdate(type="addRules")
    assert _suggestion_label(add_rules) == "Always allow"

    set_mode = PermissionUpdate(type="setMode", mode="acceptEdits")
    assert _suggestion_label(set_mode) == "Set mode: acceptEdits"

    add_dirs = PermissionUpdate(type="addDirectories")
    assert _suggestion_label(add_dirs) == "Add to allowed dirs"


@pytest.mark.asyncio
async def test_post_question_renders_allow_button_even_with_no_suggestions():
    """Regression: historically we only posted buttons for suggestions in the
    SDK context, so an empty list left the user with only a 'Deny' button."""
    slack = FakeSlackClient()
    q = make_question(suggestions=[])
    q.tool_name = "WebFetch"

    await post_question(q, slack)

    call = slack.post_calls[0]
    action_block = next(b for b in call["blocks"] if b["type"] == "actions")
    labels = [el["text"]["text"] for el in action_block["elements"]]
    # Must include at least one allow-style button AND the Deny button
    assert labels[0] == "Allow fetch"
    assert "Deny" in labels
    # Primary button should have the "primary" style to stand out
    assert action_block["elements"][0].get("style") == "primary"
    assert "choice" not in labels  # regression for the old placeholder bug


@pytest.mark.asyncio
async def test_post_question_never_renders_choice_placeholder():
    """Regression: a suggestion dict with neither name nor label previously
    produced a button labeled 'choice'. Must now render a semantic label."""
    slack = FakeSlackClient()
    q = make_question(suggestions=[{"unknown": "payload"}])
    q.tool_name = "Bash"

    await post_question(q, slack)

    call = slack.post_calls[0]
    action_block = next(b for b in call["blocks"] if b["type"] == "actions")
    labels = [el["text"]["text"] for el in action_block["elements"]]
    assert "choice" not in labels
    assert "Allow shell command" in labels
