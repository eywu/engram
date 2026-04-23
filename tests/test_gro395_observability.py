from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
)
from typer.testing import CliRunner

from engram.agent import Agent
from engram.bootstrap import provision_channel
from engram.cli import app
from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.costs import CostDatabase, TurnCost
from engram.hooks import build_hooks
from engram.manifest import IdentityTemplate
from engram.router import SessionState
from engram.telemetry import configure_logging


@dataclass
class _TextBlock:
    text: str


@dataclass
class _FakeClient:
    options: ClaudeAgentOptions
    messages: list[object] = field(default_factory=list)
    query_error: Exception | None = None
    queries: list[str] = field(default_factory=list)
    connected: bool = False
    disconnected: bool = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)
        if self.query_error is not None:
            raise self.query_error

    async def receive_response(self):
        for message in self.messages:
            yield message

    async def tag_session(self, *, session_id: str, tags: dict[str, str]) -> None:
        return None


def _cfg() -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-test"),
        max_turns_per_message=3,
    )


def _result(session_id: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=0.01,
    )


def _rate_limit(status: str, reset_at: int, session_id: str) -> RateLimitEvent:
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status=status,
            resets_at=reset_at,
            rate_limit_type="five_hour",
            utilization=0.9,
            raw={"status": status, "resetsAt": reset_at},
        ),
        uuid="rate-limit-test",
        session_id=session_id,
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "ENGRAM_SLACK_BOT_TOKEN",
        "ENGRAM_SLACK_APP_TOKEN",
        "ENGRAM_ANTHROPIC_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / ".engram"


def test_engram_status_includes_channels_and_counts(isolated_home: Path):
    provision_channel(
        "C07TEST123",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#one",
        home=isolated_home,
    )
    provision_channel(
        "C07TEST456",
        identity=IdentityTemplate.TASK_ASSISTANT,
        label="#two",
        home=isolated_home,
    )
    memory_db = isolated_home / "memory.db"
    with sqlite3.connect(memory_db) as conn:
        conn.execute("CREATE TABLE transcripts (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE summaries (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO transcripts DEFAULT VALUES", [(), (), ()])
        conn.executemany("INSERT INTO summaries DEFAULT VALUES", [(), ()])
    state_dir = isolated_home / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "status.json").write_text(
        json.dumps({"memory": {"embedding_queue": {"enabled": True}}}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert {c["channel_id"] for c in payload["channels"]} == {
        "C07TEST123",
        "C07TEST456",
    }
    assert payload["memory"]["transcripts_count"] == 3
    assert payload["memory"]["summaries_count"] == 2
    assert "embedding_queue" not in payload["memory"]
    assert all(c["rate_limit"]["status"] == "allowed" for c in payload["channels"])


def test_engram_cost_by_channel_sums_to_total(isolated_home: Path):
    db = CostDatabase(isolated_home / "cost.db")
    now = datetime.datetime.now(datetime.UTC).isoformat()
    for channel, cost in (("C07A", 0.10), ("C07A", 0.20), ("C07B", 0.30)):
        db.record_turn(
            TurnCost(
                timestamp=now,
                session_label=f"ch:{channel}",
                session_id=f"s-{channel}",
                channel_id=channel,
                is_dm=False,
                cost_usd=cost,
                duration_ms=1,
                num_turns=1,
                user_text_len=1,
                chunks_posted=1,
                is_error=False,
            )
        )

    runner = CliRunner()
    total = runner.invoke(app, ["cost", "--month"])
    by_channel = runner.invoke(app, ["cost", "--by-channel"])

    assert total.exit_code == 0
    assert by_channel.exit_code == 0
    assert "embedding cost" not in by_channel.output
    assert "gemini free-tier" not in by_channel.output
    total_amount = float(re.findall(r"\$([0-9.]+)", total.output)[0])
    amounts = [float(x) for x in re.findall(r"\$([0-9.]+)", by_channel.output)]
    assert amounts[-1] == pytest.approx(total_amount)
    assert sum(amounts[:-1]) == pytest.approx(total_amount)


def test_engram_cost_by_channel_labels_nightly_synthesis(isolated_home: Path):
    db = CostDatabase(isolated_home / "cost.db")
    db.record_turn(
        TurnCost(
            timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
            session_label="nightly",
            session_id="s-nightly",
            channel_id="__nightly__",
            is_dm=False,
            cost_usd=0.12,
            duration_ms=1,
            num_turns=1,
            user_text_len=0,
            chunks_posted=0,
            is_error=False,
        )
    )

    result = CliRunner().invoke(app, ["cost", "--by-channel"])

    assert result.exit_code == 0
    assert "[nightly-synthesis]" in result.output
    assert "__nightly__" not in result.output


@pytest.mark.asyncio
async def test_rate_limit_allowed_warning_fires_dm_and_continues(tmp_path: Path):
    session = SessionState(channel_id="C07TEST123")
    reset_at = int(datetime.datetime.now(datetime.UTC).timestamp()) + 3600
    client = _FakeClient(
        ClaudeAgentOptions(),
        messages=[
            _rate_limit("allowed_warning", reset_at, session.session_id),
            AssistantMessage(content=[_TextBlock("ok")], model="fake"),
            _result(session.session_id),
        ],
    )
    alerts: list[str] = []
    db = CostDatabase(tmp_path / "cost.db")
    agent = Agent(
        _cfg(),
        client_factory=lambda _opts: client,
        owner_alert=alerts.append,
        cost_db=db,
    )

    turn = await agent.run_turn(session, "hello")

    assert turn.text == "ok"
    assert len(client.queries) == 1
    assert alerts and "Rate limit warning" in alerts[0]
    assert db.latest_rate_limit("C07TEST123")["status"] == "allowed_warning"


@pytest.mark.asyncio
async def test_rate_limit_rejected_pauses_until_reset(tmp_path: Path):
    session = SessionState(channel_id="C07TEST123")
    reset_at = int(datetime.datetime.now(datetime.UTC).timestamp()) + 3600
    client = _FakeClient(
        ClaudeAgentOptions(),
        messages=[_rate_limit("rejected", reset_at, session.session_id)],
    )
    alerts: list[str] = []
    db = CostDatabase(tmp_path / "cost.db")
    agent = Agent(
        _cfg(),
        client_factory=lambda _opts: client,
        owner_alert=alerts.append,
        cost_db=db,
    )

    await agent.run_turn(session, "first")
    skipped = await agent.run_turn(session, "second")

    assert len(client.queries) == 1
    assert "rate limit is active" in skipped.text
    assert alerts and "rejected" in alerts[0]
    assert db.latest_rate_limit("C07TEST123")["status"] == "rejected"


@pytest.mark.asyncio
async def test_pretooluse_hook_writes_audit_log(tmp_path: Path):
    configure_logging(tmp_path / "logs", force=True)
    hook = build_hooks(channel_id="C07TEST123")["PreToolUse"][0].hooks[0]

    await hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/tmp",
            "tool_name": "Bash",
            "tool_input": {"cmd": "pwd"},
            "tool_use_id": "tool-1",
        },
        "tool-1",
        {"signal": None},
    )

    log_files = list((tmp_path / "logs").glob("engram-*.jsonl"))
    assert log_files
    records = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    record = next(r for r in records if r.get("event") == "hook.pre_tool_use")
    assert record["tool_name"] == "Bash"
    assert record["input"] == {"cmd": "pwd"}
    assert record["channel_id"] == "C07TEST123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "alerts_owner", "retries"),
    [
        (CLINotFoundError("missing"), True, False),
        (CLIConnectionError("connection failed"), True, False),
        (ProcessError("process failed", exit_code=1), False, True),
        (CLIJSONDecodeError("{bad", ValueError("bad")), False, True),
    ],
)
async def test_cli_error_taxonomy_distinct_handling(
    error: Exception,
    alerts_owner: bool,
    retries: bool,
    caplog: pytest.LogCaptureFixture,
):
    clients: list[_FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> _FakeClient:
        client = _FakeClient(
            options,
            query_error=error if not clients else None,
            messages=[
                AssistantMessage(content=[_TextBlock("recovered")], model="fake"),
                _result("s1"),
            ],
        )
        clients.append(client)
        return client

    alerts: list[str] = []
    agent = Agent(
        _cfg(),
        client_factory=factory,
        owner_alert=alerts.append,
        retry_base_delay_seconds=0,
    )

    with caplog.at_level("ERROR"):
        turn = await agent.run_turn(SessionState(channel_id="C07TEST123"), "hello")

    assert type(error).__name__ in caplog.text
    assert bool(alerts) is alerts_owner
    if retries:
        assert len(clients) == 2
        assert turn.text == "recovered"
    else:
        assert len(clients) == 1
        assert turn.is_error


def test_watchdog_script_invokes_launchctl_after_3_failures(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    engram = bin_dir / "engram"
    engram.write_text("#!/bin/sh\nexit 1\n")
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$WATCHDOG_KICKS\"\n"
    )
    engram.chmod(0o755)
    launchctl.chmod(0o755)
    kicks = tmp_path / "kicks.txt"
    env = {
        **os.environ,
        "ENGRAM_BIN": str(engram),
        "ENGRAM_STATE_DIR": str(tmp_path / "state"),
        "WATCHDOG_KICKS": str(kicks),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UID": "501",
    }
    script = Path("scripts/engram_watchdog.sh").resolve()

    subprocess.run([str(script)], env=env, check=True)
    subprocess.run([str(script)], env=env, check=True)
    assert not kicks.exists()

    subprocess.run([str(script)], env=env, check=True)
    assert "kickstart -k gui/501/com.engram.v3.bridge" in kicks.read_text()
