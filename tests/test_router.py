"""Router tests — pure in-memory, no I/O."""
from __future__ import annotations

import asyncio

import pytest

from engram.router import Router


@pytest.mark.asyncio
async def test_creates_session_on_first_get():
    r = Router()
    s = await r.get("C1", is_dm=False)
    assert s.channel_id == "C1"
    assert s.turn_count == 0
    assert not s.is_dm


@pytest.mark.asyncio
async def test_caches_session_across_gets():
    r = Router()
    s1 = await r.get("C1")
    s2 = await r.get("C1")
    assert s1 is s2


@pytest.mark.asyncio
async def test_separates_channels():
    r = Router()
    a = await r.get("C1", is_dm=False)
    b = await r.get("D1", is_dm=True)
    assert a is not b
    assert a.is_dm is False
    assert b.is_dm is True
    assert r.session_count() == 2


@pytest.mark.asyncio
async def test_concurrent_get_no_duplicate():
    r = Router()
    results = await asyncio.gather(*(r.get("C1") for _ in range(20)))
    assert all(s is results[0] for s in results)
    assert r.session_count() == 1


@pytest.mark.asyncio
async def test_session_label():
    r = Router()
    s_named = await r.get("C1", channel_name="growth", is_dm=False)
    assert "growth" in s_named.label()
    s_dm = await r.get("D1", is_dm=True)
    assert "dm" in s_dm.label()
