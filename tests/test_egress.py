"""Egress tests — chunking + post shape, no real Slack calls."""
from __future__ import annotations

import pytest

from engram.agent import AgentTurn
from engram.egress import SLACK_MAX_TEXT_LEN, _chunk_text, post_reply


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
async def test_post_reply_calls_say_once_for_short_text():
    calls = []

    async def say(*, text, thread_ts):
        calls.append((text, thread_ts))
        return {"ts": "123.456"}

    turn = AgentTurn(
        text="hello there", cost_usd=0.001, duration_ms=100, num_turns=1, is_error=False
    )
    res = await post_reply(say, turn, thread_ts="T1", session_label="ch:C1")
    assert res.chunks_posted == 1
    assert res.posted_message_ts == "123.456"
    assert len(calls) == 1
    text_sent, thread = calls[0]
    assert "hello there" in text_sent
    assert "cost: $0.0010" in text_sent  # cost footer on last chunk
    assert thread == "T1"


@pytest.mark.asyncio
async def test_post_reply_chunks_long_text():
    calls = []

    async def say(*, text, thread_ts):
        calls.append(text)
        return {"ts": "x"}

    long_text = "a" * (SLACK_MAX_TEXT_LEN + 100)
    turn = AgentTurn(text=long_text, cost_usd=None, duration_ms=None, num_turns=None, is_error=False)
    res = await post_reply(say, turn, thread_ts=None, session_label="ch:C1")
    assert res.chunks_posted == 2
    assert len(calls) == 2
