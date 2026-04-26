from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import ToolPermissionContext

from engram.agent import Agent
from engram.config import AnthropicConfig, EngramConfig, HITLConfig, SlackConfig
from engram.egress import post_question
from engram.ingress import handle_block_action
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    ManifestError,
    PermissionTier,
    ScopeList,
    dump_manifest,
    load_manifest,
)
from engram.mcp_trust import MCPTrustDecision, MCPTrustTier, resolve_mcp_server_trust
from engram.paths import channel_manifest_path, state_dir
from engram.router import Router


class FakeSlackClient:
    def __init__(self) -> None:
        self.post_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.chat_postMessage = self._chat_post_message

    async def _chat_post_message(self, **kwargs):
        self.post_calls.append(kwargs)
        return {"ts": "1713800000.000100"}

    async def chat_update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"ok": True}


def _cfg() -> EngramConfig:
    cfg = EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-test"),
    )
    cfg.owner_dm_channel_id = "D07OWNER"
    cfg.owner_user_id = "U07OWNER"
    return cfg


def _render_manifest(manifest: ChannelManifest) -> str:
    return yaml.safe_dump(
        manifest.model_dump(mode="json", exclude_none=False),
        sort_keys=False,
        default_flow_style=False,
        indent=2,
    )


async def _wait_until(predicate) -> None:
    deadline = asyncio.get_running_loop().time() + 1.0
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition was not met before timeout")
        await asyncio.sleep(0)


def _manifest(
    channel_id: str = "C07TEAM",
    *,
    allowed: list[str] | None = None,
    hitl_enabled: bool = True,
) -> ChannelManifest:
    return ChannelManifest(
        channel_id=channel_id,
        identity=IdentityTemplate.TASK_ASSISTANT,
        status=ChannelStatus.ACTIVE,
        permission_tier=PermissionTier.OWNER_SCOPED,
        tools=ScopeList(allowed=["Write", "Edit", "MultiEdit"]),
        mcp_servers=ScopeList(allowed=allowed),
        setting_sources=["project"],
        label="#growth",
        hitl=HITLConfig(enabled=hitl_enabled),
    )


def _block_action_payload(value: str, *, user_id: str = "U07OWNER") -> dict:
    choice = value.split("|", 2)[1]
    action_id = f"hitl_choice_{choice}"
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id, "block_id": "hitl_actions", "value": value}],
        "user": {"id": user_id},
    }


def _tampered_block_action_payload(
    permission_request_id: str,
    choice_key: str,
    *,
    action_id: str = "hitl_choice_0",
    user_id: str = "U07OWNER",
) -> dict:
    return {
        "type": "block_actions",
        "actions": [
            {
                "action_id": action_id,
                "block_id": "hitl_actions",
                "value": f"{permission_request_id}|{choice_key}",
            }
        ],
        "user": {"id": user_id},
    }


@pytest.mark.asyncio
async def test_resolve_mcp_server_trust_official_npm(tmp_path: Path) -> None:
    home = tmp_path / ".engram"
    calls: list[str] = []

    async def fetch_json(url: str, *, headers=None):
        calls.append(url)
        if "registry.npmjs.org" in url:
            return {
                "dist-tags": {"latest": "1.2.3"},
                "time": {
                    "created": "2024-01-01T00:00:00+00:00",
                    "1.2.3": "2026-04-01T00:00:00+00:00",
                    "modified": "2026-04-05T00:00:00+00:00",
                },
                "versions": {
                    "1.2.3": {
                        "publisher": {"name": "modelcontextprotocol"},
                        "repository": {"url": "https://github.com/modelcontextprotocol/server-github"},
                    }
                },
            }
        if "api.npmjs.org" in url:
            return {"downloads": 25000}
        if "api.github.com/repos" in url and "contributors" not in url:
            return {"stargazers_count": 500, "pushed_at": "2026-04-10T00:00:00+00:00"}
        if "contributors" in url:
            return [{"login": f"user-{i}"} for i in range(10)]
        raise AssertionError(f"unexpected URL {url}")

    decision = await resolve_mcp_server_trust(
        "github",
        {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github@1.2.3"]},
        home=home,
        fetch_json=fetch_json,
        now=datetime(2026, 4, 25, tzinfo=UTC),
    )

    assert decision.tier == MCPTrustTier.OFFICIAL
    assert decision.publisher == "modelcontextprotocol"
    assert decision.package_name == "@modelcontextprotocol/server-github"
    assert calls


@pytest.mark.asyncio
async def test_resolve_mcp_server_trust_community_trusted_cache_hit_and_miss(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    calls: list[str] = []

    async def fetch_json(url: str, *, headers=None):
        calls.append(url)
        if "registry.npmjs.org" in url:
            return {
                "dist-tags": {"latest": "4.0.0"},
                "time": {
                    "created": "2024-01-01T00:00:00+00:00",
                    "4.0.0": "2026-04-01T00:00:00+00:00",
                    "modified": "2026-04-05T00:00:00+00:00",
                },
                "versions": {
                    "4.0.0": {
                        "publisher": {"name": "community-owner"},
                        "repository": {"url": "https://github.com/community/mcp-server"},
                    }
                },
            }
        if "api.npmjs.org" in url:
            return {"downloads": 20000}
        if "api.github.com/repos" in url and "contributors" not in url:
            return {"stargazers_count": 120, "pushed_at": "2026-04-20T00:00:00+00:00"}
        if "contributors" in url:
            return [{"login": f"user-{i}"} for i in range(8)]
        raise AssertionError(f"unexpected URL {url}")

    kwargs = {
        "server_name": "community",
        "server_config": {"command": "npx", "args": ["-y", "community-mcp@4.0.0"]},
        "home": home,
        "fetch_json": fetch_json,
        "now": datetime(2026, 4, 25, tzinfo=UTC),
    }
    first = await resolve_mcp_server_trust(**kwargs)
    call_count = len(calls)
    second = await resolve_mcp_server_trust(**kwargs)

    assert first.tier == MCPTrustTier.COMMUNITY_TRUSTED
    assert second.tier == MCPTrustTier.COMMUNITY_TRUSTED
    assert len(calls) == call_count

    cache_path = state_dir(home) / "mcp_trust_cache.json"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    entries = payload["entries"]
    entry = next(iter(entries.values()))
    entry["fetched_at"] = (datetime(2026, 4, 20, tzinfo=UTC)).isoformat()
    cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    refreshed = await resolve_mcp_server_trust(**kwargs)

    assert refreshed.tier == MCPTrustTier.COMMUNITY_TRUSTED
    assert len(calls) > call_count


@pytest.mark.asyncio
async def test_resolve_mcp_server_trust_fetch_failure_is_unknown(tmp_path: Path) -> None:
    decision = await resolve_mcp_server_trust(
        "broken",
        {"command": "uvx", "args": ["broken-mcp==0.1.0"]},
        home=tmp_path / ".engram",
        fetch_json=_raising_fetch,
        now=datetime(2026, 4, 25, tzinfo=UTC),
    )

    assert decision.tier == MCPTrustTier.UNKNOWN
    assert decision.registry == "pypi"
    assert decision.package_name == "broken-mcp"
    assert decision.version == "0.1.0"
    assert decision.reason == "metadata lookup failed"


async def _raising_fetch(url: str, *, headers=None):
    raise RuntimeError(f"boom: {url}")


def test_load_manifest_grandfathers_existing_allow_list_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / ".engram"
    manifest_path = channel_manifest_path("C07TEAM", home)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    dump_manifest(_manifest(allowed=["linear"]), manifest_path)

    with caplog.at_level("INFO", logger="engram.manifest"):
        first = load_manifest(manifest_path)
        second = load_manifest(manifest_path)

    assert first.mcp_servers.allowed == ["linear"]
    assert second.mcp_servers.allowed == ["linear"]
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("manifest.mcp_allow_list_grandfathered")
    ]
    assert len(records) == 1
    assert (state_dir(home) / "mcp_manifest_audit.json").exists()


def test_dump_manifest_blocks_ungated_mcp_allow_list_addition(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".engram"
    manifest_path = channel_manifest_path("C07TEAM", home)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    dump_manifest(_manifest(), manifest_path)

    with pytest.raises(
        ManifestError,
        match="persist_approved_mcp_manifest_change",
    ):
        dump_manifest(_manifest(allowed=["camoufox"]), manifest_path)

    assert load_manifest(manifest_path).mcp_servers.allowed is None


@pytest.mark.asyncio
async def test_official_manifest_mcp_addition_allows_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".engram"
    monkeypatch.setenv("HOME", str(tmp_path))
    github_config = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github@1.2.3"],
    }
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"github": github_config}}),
        encoding="utf-8",
    )
    manifest_path = channel_manifest_path("C07TEAM", home)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    dump_manifest(_manifest(hitl_enabled=False), manifest_path)

    router = Router(
        home=home,
        owner_dm_channel_id="D07OWNER",
        hitl=HITLConfig(enabled=False),
    )
    session = await router.get("C07TEAM")
    alerts: list[str] = []
    agent = Agent(_cfg(), router=router, owner_alert=alerts.append)
    opts = agent._build_options(session)

    staged = session.manifest.model_copy(update={"mcp_servers": ScopeList(allowed=["github"])})
    captured_config: dict[str, object] = {}

    async def fake_resolve_mcp_server_trust(server_name, server_config, *, home):
        captured_config["server_name"] = server_name
        captured_config["server_config"] = server_config
        return MCPTrustDecision(
            server_name="github",
            tier=MCPTrustTier.OFFICIAL,
            registry="npm",
            package_name="@modelcontextprotocol/server-github",
            version="1.2.3",
            publisher="modelcontextprotocol",
            publishers=["modelcontextprotocol"],
            trust_summary="official server",
            reason="test fixture",
        )

    monkeypatch.setattr(
        "engram.agent.resolve_mcp_server_trust",
        fake_resolve_mcp_server_trust,
    )

    result = await opts.can_use_tool(
        "Write",
        {"file_path": str(manifest_path), "content": _render_manifest(staged)},
        ToolPermissionContext(tool_use_id="tool-1"),
    )

    assert isinstance(result, PermissionResultAllow)
    assert captured_config == {
        "server_name": "github",
        "server_config": github_config,
    }
    assert alerts == []
    assert router.hitl.pending_for_channel("C07TEAM") == []


@pytest.mark.asyncio
async def test_community_trusted_manifest_mcp_addition_notifies_owner_and_allows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".engram"
    manifest_path = channel_manifest_path("C07TEAM", home)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    dump_manifest(_manifest(hitl_enabled=False), manifest_path)

    router = Router(
        home=home,
        owner_dm_channel_id="D07OWNER",
        hitl=HITLConfig(enabled=False),
    )
    session = await router.get("C07TEAM")
    alerts: list[str] = []
    agent = Agent(_cfg(), router=router, owner_alert=alerts.append)
    opts = agent._build_options(session)

    staged = session.manifest.model_copy(update={"mcp_servers": ScopeList(allowed=["community"])})
    monkeypatch.setattr(
        "engram.agent.resolve_mcp_server_trust",
        _constant_trust_decision(
            MCPTrustTier.COMMUNITY_TRUSTED,
            server_name="community",
            publisher="community-owner",
            summary="age=500d, downloads=5000/week, contributors=8, repo_active=yes",
        ),
    )

    result = await opts.can_use_tool(
        "Write",
        {"file_path": str(manifest_path), "content": _render_manifest(staged)},
        ToolPermissionContext(tool_use_id="tool-1"),
    )

    assert isinstance(result, PermissionResultAllow)
    assert len(alerts) == 1
    assert "Community-trusted MCP addition auto-approved." in alerts[0]
    assert router.hitl.pending_for_channel("C07TEAM") == []


@pytest.mark.asyncio
async def test_unknown_manifest_mcp_addition_requires_owner_dm_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / ".engram"
    monkeypatch.setenv("HOME", str(tmp_path))
    (Path.home() / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "camoufox": {
                        "command": "uvx",
                        "args": ["camoufox-browser[mcp]==0.1.1"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    manifest_path = channel_manifest_path("C07TEAM", home)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    dump_manifest(_manifest(), manifest_path)

    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    session = await router.get("C07TEAM")
    slack = FakeSlackClient()
    agent = Agent(_cfg(), router=router)

    async def on_new_question(q) -> None:
        channel_ts, thread_ts = await post_question(q, slack)
        q.slack_channel_ts = channel_ts
        q.slack_thread_ts = thread_ts

    agent._on_new_question = on_new_question
    opts = agent._build_options(session)
    staged = session.manifest.model_copy(update={"mcp_servers": ScopeList(allowed=["camoufox"])})

    monkeypatch.setattr(
        "engram.agent.resolve_mcp_server_trust",
        _constant_trust_decision(
            MCPTrustTier.UNKNOWN,
            server_name="camoufox",
            package_name="camoufox-browser",
            version="0.1.1",
            publisher="camoufox-labs",
            summary="age=14d, downloads=25/week, contributors=1, repo_active=no",
        ),
    )

    result = await opts.can_use_tool(
        "Write",
        {"file_path": str(manifest_path), "content": _render_manifest(staged)},
        ToolPermissionContext(tool_use_id="tool-1"),
    )

    assert isinstance(result, PermissionResultDeny)
    assert "owner DM" in result.message
    assert load_manifest(manifest_path).mcp_servers.allowed is None
    assert len(slack.post_calls) == 1
    post = slack.post_calls[0]
    assert post["channel"] == "D07OWNER"
    assert post["blocks"][0]["text"]["text"] == "🔐 Owner approval required for MCP addition"
    assert [element["text"]["text"] for element in post["blocks"][2]["elements"]] == [
        "Approve once",
        "Approve + add publisher to trust list",
        "Reject",
    ]

    pending = router.hitl.pending_for_channel("C07TEAM")
    assert len(pending) == 1
    q = pending[0]
    with caplog.at_level("INFO", logger="engram.manifest"):
        ack = await handle_block_action(
            _block_action_payload(f"{q.permission_request_id}|1"),
            router,
            slack,
        )

    assert ack == {"ok": True}
    await _wait_until(
        lambda: load_manifest(manifest_path).mcp_servers.allowed == ["camoufox"]
        and bool(slack.update_calls)
    )
    assert load_manifest(manifest_path).mcp_servers.allowed == ["camoufox"]
    trusted_overlay = state_dir(home) / "trusted_publishers.yaml"
    assert trusted_overlay.exists()
    assert "camoufox-labs" in trusted_overlay.read_text(encoding="utf-8")
    assert slack.update_calls[-1]["channel"] == "D07OWNER"
    audit_payload = json.loads(
        (state_dir(home) / "mcp_manifest_audit.json").read_text(encoding="utf-8")
    )
    assert audit_payload["audited_channels"]["C07TEAM"]["source"] == "approved_addition"
    assert not [
        record
        for record in caplog.records
        if record.getMessage().startswith("manifest.mcp_allow_list_grandfathered")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice_key", ["99", "garbage"])
async def test_unknown_manifest_mcp_addition_malformed_choice_key_denies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    choice_key: str,
) -> None:
    home = tmp_path / ".engram"
    monkeypatch.setenv("HOME", str(tmp_path))
    (Path.home() / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "camoufox": {
                        "command": "uvx",
                        "args": ["camoufox-browser[mcp]==0.1.1"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    manifest_path = channel_manifest_path("C07TEAM", home)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    dump_manifest(_manifest(), manifest_path)

    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    session = await router.get("C07TEAM")
    slack = FakeSlackClient()
    agent = Agent(_cfg(), router=router)

    async def on_new_question(q) -> None:
        channel_ts, thread_ts = await post_question(q, slack)
        q.slack_channel_ts = channel_ts
        q.slack_thread_ts = thread_ts

    agent._on_new_question = on_new_question
    opts = agent._build_options(session)
    staged = session.manifest.model_copy(update={"mcp_servers": ScopeList(allowed=["camoufox"])})

    monkeypatch.setattr(
        "engram.agent.resolve_mcp_server_trust",
        _constant_trust_decision(
            MCPTrustTier.UNKNOWN,
            server_name="camoufox",
            package_name="camoufox-browser",
            version="0.1.1",
            publisher="camoufox-labs",
            summary="age=14d, downloads=25/week, contributors=1, repo_active=no",
        ),
    )

    result = await opts.can_use_tool(
        "Write",
        {"file_path": str(manifest_path), "content": _render_manifest(staged)},
        ToolPermissionContext(tool_use_id="tool-1"),
    )
    assert isinstance(result, PermissionResultDeny)

    pending = router.hitl.pending_for_channel("C07TEAM")
    assert len(pending) == 1
    q = pending[0]

    with caplog.at_level("WARNING", logger="engram.ingress"):
        ack = await handle_block_action(
            _tampered_block_action_payload(q.permission_request_id, choice_key),
            router,
            slack,
        )

    assert ack == {"ok": True}
    await _wait_until(lambda: q.future.done() and bool(slack.update_calls))
    resolution = q.future.result()
    assert isinstance(resolution, PermissionResultDeny)
    assert load_manifest(manifest_path).mcp_servers.allowed is None
    assert "invalid payload" in slack.update_calls[-1]["text"]
    assert any(
        record.getMessage().startswith("hitl.invalid_choice_payload")
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_unknown_manifest_mcp_addition_apply_failure_updates_dm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".engram"
    monkeypatch.setenv("HOME", str(tmp_path))
    (Path.home() / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "camoufox": {
                        "command": "uvx",
                        "args": ["camoufox-browser[mcp]==0.1.1"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    manifest_path = channel_manifest_path("C07TEAM", home)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    dump_manifest(_manifest(), manifest_path)

    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    session = await router.get("C07TEAM")
    slack = FakeSlackClient()
    agent = Agent(_cfg(), router=router)

    async def on_new_question(q) -> None:
        channel_ts, thread_ts = await post_question(q, slack)
        q.slack_channel_ts = channel_ts
        q.slack_thread_ts = thread_ts

    agent._on_new_question = on_new_question
    opts = agent._build_options(session)
    staged = session.manifest.model_copy(update={"mcp_servers": ScopeList(allowed=["camoufox"])})

    monkeypatch.setattr(
        "engram.agent.resolve_mcp_server_trust",
        _constant_trust_decision(
            MCPTrustTier.UNKNOWN,
            server_name="camoufox",
            package_name="camoufox-browser",
            version="0.1.1",
            publisher="camoufox-labs",
            summary="age=14d, downloads=25/week, contributors=1, repo_active=no",
        ),
    )

    result = await opts.can_use_tool(
        "Write",
        {"file_path": str(manifest_path), "content": _render_manifest(staged)},
        ToolPermissionContext(tool_use_id="tool-1"),
    )
    assert isinstance(result, PermissionResultDeny)

    pending = router.hitl.pending_for_channel("C07TEAM")
    assert len(pending) == 1
    q = pending[0]

    manifest_dir = manifest_path.parent
    original_mode = manifest_dir.stat().st_mode
    manifest_dir.chmod(0o500)
    try:
        ack = await handle_block_action(
            _block_action_payload(f"{q.permission_request_id}|0"),
            router,
            slack,
        )
        assert ack == {"ok": True}
        await _wait_until(lambda: q.future.done() and bool(slack.update_calls))
    finally:
        manifest_dir.chmod(original_mode)

    assert load_manifest(manifest_path).mcp_servers.allowed is None
    assert "Approval failed to apply:" in slack.update_calls[-1]["text"]
    assert slack.update_calls[-1]["blocks"][0]["text"]["text"].startswith(
        "❌ Answered: Approval failed to apply:"
    )


def _constant_trust_decision(
    tier: MCPTrustTier,
    *,
    server_name: str,
    package_name: str | None = None,
    version: str | None = None,
    publisher: str | None = None,
    summary: str = "age=unknown, downloads=unknown, contributors=unknown, repo_active=no",
):
    async def _resolver(*args, **kwargs):
        return MCPTrustDecision(
            server_name=server_name,
            tier=tier,
            registry="pypi",
            package_name=package_name or server_name,
            version=version,
            publisher=publisher,
            publishers=[publisher] if publisher else [],
            trust_summary=summary,
            reason="test fixture",
        )

    return _resolver
