from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage

from engram.budget import BudgetConfig
from engram.config import HITLConfig, NightlyConfig
from engram.manifest import (
    ChannelManifest,
    ChannelNightly,
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
    dump_manifest,
)
from engram.mcp_tools import MEMORY_SEARCH_FULL_TOOL_NAMES
from engram.nightly.schema import META_CHANNEL_ID
from engram.nightly.synthesize import (
    DEFAULT_PROMPT_TEMPLATE,
    NIGHTLY_CHANNEL_ID,
    AnthropicRuntime,
    PlannedChannel,
    SynthesisOutputError,
    build_nightly_options,
    parse_synthesis_output,
    synthesize,
)
from engram.telemetry import configure_logging


@dataclass
class _TextBlock:
    text: str


class _FakeBudget:
    def __init__(self) -> None:
        self.config = BudgetConfig(hard_cap_enabled=True)
        self.records: list[tuple[str, str | None, Any]] = []

    def record(self, channel_id: str, user_id: str | None, result_message: Any) -> None:
        self.records.append((channel_id, user_id, result_message))


class _FakeClient:
    def __init__(
        self,
        options: ClaudeAgentOptions,
        responses: list[tuple[str, ResultMessage]],
    ):
        self.options = options
        self.responses = responses
        self.connected = False
        self.disconnected = False
        self.prompts: list[str] = []
        self.session_ids: list[str] = []
        self.receive_count = 0

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.prompts.append(prompt)
        self.session_ids.append(session_id)

    async def receive_response(self):
        response_text, result = self.responses[self.receive_count]
        self.receive_count += 1
        yield AssistantMessage(content=[_TextBlock(response_text)], model="fake")
        yield result


class _BudgetAbortClient:
    def __init__(self, options: ClaudeAgentOptions):
        self.options = options
        self.disconnected = False

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        assert self.options.max_budget_usd == 5.0

    async def receive_response(self):
        raise RuntimeError("mocked max budget exceeded")
        yield


class _MultiTurnMessageClient:
    def __init__(self, options: ClaudeAgentOptions, final_response: str):
        self.options = options
        self.final_response = final_response
        self.connected = False
        self.disconnected = False
        self.prompts: list[str] = []
        self.session_ids: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.prompts.append(prompt)
        self.session_ids.append(session_id)

    async def receive_messages(self):
        yield AssistantMessage(content=[_TextBlock("")], model="fake")
        yield _result(stop_reason="tool_use", input_tokens=100)
        yield AssistantMessage(content=[_TextBlock(self.final_response)], model="fake")
        yield _result(cost=0.02, stop_reason="end_turn", input_tokens=120)


class _AssistantModelFallbackClient:
    def __init__(self, options: ClaudeAgentOptions, retry_response: str):
        self.options = options
        self.retry_response = retry_response
        self.connected = False
        self.disconnected = False
        self.receive_count = 0

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        del prompt, session_id

    async def receive_messages(self):
        if self.receive_count == 0:
            self.receive_count += 1
            yield AssistantMessage(content=[_TextBlock("not json")], model="claude-haiku-4-5")
            yield _result(model_usage={}, input_tokens=0)
            return

        self.receive_count += 1
        yield AssistantMessage(
            content=[_TextBlock(self.retry_response)],
            model="claude-sonnet-4-6",
        )
        yield _result(
            stop_reason="max_turns_exhausted",
            model_usage={},
            input_tokens=0,
        )


class _ClientFactory:
    def __init__(
        self,
        responses: list[tuple[str, ResultMessage] | list[tuple[str, ResultMessage]]],
    ) -> None:
        self.responses = responses
        self.options: list[ClaudeAgentOptions] = []
        self.clients: list[_FakeClient] = []

    def __call__(self, options: ClaudeAgentOptions) -> _FakeClient:
        self.options.append(options)
        response_spec = self.responses.pop(0)
        client_responses = response_spec if isinstance(response_spec, list) else [response_spec]
        client = _FakeClient(options, client_responses)
        self.clients.append(client)
        return client


def _result(
    cost: float = 0.01,
    *,
    cache_read: int = 0,
    input_tokens: int = 100,
    model_usage: dict[str, Any] | None = None,
    stop_reason: str | None = "end_turn",
    message_count: int | None = None,
) -> ResultMessage:
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-test",
        stop_reason=stop_reason,
        total_cost_usd=cost,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0 if cache_read else 7,
            "cache_read_input_tokens": cache_read,
        },
        model_usage=(
            {"claude-test-model": {"input_tokens": input_tokens}}
            if model_usage is None
            else model_usage
        ),
    )
    if message_count is not None:
        result.message_count = message_count
    return result


def _synthesis_json(channel_id: str, *, summary: str = "durable summary") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "date": "2026-04-22",
            "channel_id": channel_id,
            "summary": summary,
            "highlights": [],
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "cross_channel_flags": [],
            "source_row_ids": [1],
        }
    )


def _write_harvest(tmp_path: Path, channels: list[dict[str, Any]]) -> Path:
    path = tmp_path / "harvest.json"
    path.write_text(
        json.dumps(
            {
                "date": "2026-04-22",
                "channels": channels,
                "skipped_channels": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _channel(
    channel_id: str,
    *,
    row_id: int = 1,
    token_count: int = 2,
    text: str = "alpha beta",
) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "row_count": 1,
        "token_count": token_count,
        "rows": [
            {
                "kind": "transcript",
                "id": row_id,
                "channel_id": channel_id,
                "ts": "2026-04-22T01:00:00+00:00",
                "token_count": token_count,
                "text": text,
                "session_id": f"session-{channel_id}",
                "role": "assistant",
                "message_uuid": f"msg-{row_id}",
                "parent_uuid": None,
            }
        ],
    }


def _write_manifest(contexts_dir: Path, manifest: ChannelManifest) -> None:
    path = contexts_dir / manifest.channel_id / ".claude" / "channel-manifest.yaml"
    path.parent.mkdir(parents=True)
    dump_manifest(manifest, path)


@pytest.mark.asyncio
async def test_synthesize_sets_nightly_sdk_invariants_and_records_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="engram.nightly.synthesize")
    captured_mcp: dict[str, Any] = {}

    def fake_make_memory_search_server(
        caller_channel_id: str,
        memory_db_path: Path | None = None,
        embedder: object | None = None,
        *,
        excluded_channels: list[str] | None = None,
    ) -> dict[str, Any]:
        captured_mcp["caller_channel_id"] = caller_channel_id
        captured_mcp["excluded_channels"] = excluded_channels
        return {"name": "engram-memory"}

    monkeypatch.setattr(
        "engram.nightly.synthesize.make_memory_search_server",
        fake_make_memory_search_server,
    )
    contexts_dir = tmp_path / "contexts"
    _write_manifest(
        contexts_dir,
        ChannelManifest(
            channel_id="D07OWNER",
            identity=IdentityTemplate.OWNER_DM_FULL,
            status=ChannelStatus.ACTIVE,
            setting_sources=["user"],
        ),
    )
    harvest = _write_harvest(tmp_path, [_channel("D07OWNER")])
    budget = _FakeBudget()
    factory = _ClientFactory([(_synthesis_json("D07OWNER"), _result(cache_read=123))])

    result = await synthesize(
        harvest,
        output_root=tmp_path / "nightly",
        config=NightlyConfig(excluded_channels=("C07SKIP",)),
        contexts_dir=contexts_dir,
        anthropic_runtime=AnthropicRuntime(api_key="sk-test", model="global-model"),
        budget=budget,
        client_factory=factory,
    )

    assert result.output_path == tmp_path / "nightly" / "archive" / "2026-04-22" / "synthesis.json"
    assert result.payload["channels"][0]["synthesis"]["summary"] == "durable summary"
    assert result.payload["channels"][0]["prompt_cache"]["status"] == "read"
    assert budget.records[0][0] == NIGHTLY_CHANNEL_ID
    options = factory.options[0]
    assert options.cwd == str(tmp_path / "nightly" / "current")
    assert options.max_budget_usd == 5.0
    assert options.max_turns >= 5
    assert options.output_format is not None
    assert options.output_format["type"] == "json_schema"
    assert options.allowed_tools == MEMORY_SEARCH_FULL_TOOL_NAMES
    assert options.permission_mode == "dontAsk"
    assert options.skills == []
    assert options.hitl_config.enabled is False
    assert options.budget_config.hard_cap_enabled is False
    assert captured_mcp == {
        "caller_channel_id": "D07OWNER",
        "excluded_channels": ["C07SKIP"],
    }

    startup = _single_log(caplog.records, "nightly.synthesis_start")
    assert startup.hitl_disabled is True
    assert startup.hitl_config_enabled is False
    assert _single_log(caplog.records, "nightly.parse_ok").attempt == 1


def test_build_nightly_options_uses_uuid_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "engram.nightly.synthesize.make_memory_search_server",
        lambda *args, **kwargs: {"name": "engram-memory"},
    )
    options = build_nightly_options(
        plan=PlannedChannel(
            channel=_channel("C07TEST123"),
            manifest=None,
            model="sonnet",
            estimated_cost_usd=Decimal("0.01"),
        ),
        run_date="2026-04-22",
        current_dir=tmp_path,
        runtime=AnthropicRuntime(api_key=None, model="global-model"),
        hitl_config=HITLConfig(enabled=False),
        config=NightlyConfig(),
    )

    assert options.session_id is not None
    assert re.fullmatch(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        options.session_id,
    )
    assert options.max_turns >= 5


def test_default_prompt_template_includes_json_mode_instruction() -> None:
    prompt_template = DEFAULT_PROMPT_TEMPLATE.read_text(encoding="utf-8")

    assert "Respond ONLY with valid JSON matching the schema." in prompt_template
    assert "Do not call tools unless absolutely necessary." in prompt_template


def test_golden_fixture_outputs_match_schema() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "nightly"
    harvest_paths = sorted(fixture_dir.glob("harvest-*.json"))
    assert len(harvest_paths) >= 3

    for harvest_path in harvest_paths:
        harvest = json.loads(harvest_path.read_text(encoding="utf-8"))
        expected_path = fixture_dir / harvest_path.name.replace("harvest-", "expected-")
        expected = parse_synthesis_output(expected_path.read_text(encoding="utf-8"))
        channel = harvest["channels"][0]
        source_ids = {row["id"] for row in channel["rows"]}

        assert expected["date"] == harvest["date"]
        assert expected["channel_id"] == channel["channel_id"]
        assert set(expected["source_row_ids"]).issubset(source_ids)
        assert "cross_channel_flags" in expected


@pytest.mark.asyncio
async def test_mocked_sdk_malformed_then_valid_retries_and_accepts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="engram.nightly.synthesize")
    harvest = _write_harvest(tmp_path, [_channel("C07RETRY")])
    factory = _ClientFactory(
        [[("not json", _result(cost=0.01)), (_synthesis_json("C07RETRY"), _result(cost=0.02))]]
    )
    budget = _FakeBudget()

    result = await synthesize(
        harvest,
        output_root=tmp_path / "nightly",
        config=NightlyConfig(),
        contexts_dir=tmp_path / "contexts",
        anthropic_runtime=AnthropicRuntime(api_key=None, model="sonnet"),
        budget=budget,
        client_factory=factory,
    )

    channel = result.payload["channels"][0]
    assert channel["status"] == "synthesized"
    assert channel["synthesis"]["summary"] == "durable summary"
    assert channel["cost_usd"] == "0.030000"
    assert len(budget.records) == 2
    client = factory.clients[0]
    assert len(client.prompts) == 2
    assert client.prompts[1].startswith("Your previous response did not match the schema.")
    assert "Required JSON Schema" in client.prompts[1]
    assert _single_log(caplog.records, "nightly.parse_retry").channel_id == "C07RETRY"
    assert _single_log(caplog.records, "nightly.parse_ok").attempt == 2


@pytest.mark.asyncio
async def test_receive_messages_continues_past_tool_use_until_final_json(
    tmp_path: Path,
) -> None:
    harvest = _write_harvest(tmp_path, [_channel("C07TOOLUSE")])
    budget = _FakeBudget()

    def factory(options: ClaudeAgentOptions) -> _MultiTurnMessageClient:
        return _MultiTurnMessageClient(options, _synthesis_json("C07TOOLUSE"))

    result = await synthesize(
        harvest,
        output_root=tmp_path / "nightly",
        config=NightlyConfig(),
        contexts_dir=tmp_path / "contexts",
        anthropic_runtime=AnthropicRuntime(api_key=None, model="sonnet"),
        budget=budget,
        client_factory=factory,
    )

    channel = result.payload["channels"][0]
    assert channel["status"] == "synthesized"
    assert channel["synthesis"]["channel_id"] == "C07TOOLUSE"
    assert channel["cost_usd"] == "0.020000"
    assert len(budget.records) == 1


@pytest.mark.asyncio
async def test_retry_failure_logs_both_raw_outputs_and_aborts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="engram.nightly.synthesize")
    harvest = _write_harvest(tmp_path, [_channel("C07FAIL")])
    second_raw = json.dumps({"schema_version": 1, "date": "2026-04-22"})
    final_result = _result(
        stop_reason="max_turns_exhausted",
        input_tokens=321,
        model_usage={
            "claude-haiku-4-5": {"input_tokens": 111},
            "claude-sonnet-4-6": {"input_tokens": 210},
        },
        message_count=4,
    )
    factory = _ClientFactory([[("not json", _result()), (second_raw, final_result)]])

    with pytest.raises(SynthesisOutputError):
        await synthesize(
            harvest,
            output_root=tmp_path / "nightly",
            config=NightlyConfig(),
            contexts_dir=tmp_path / "contexts",
            anthropic_runtime=AnthropicRuntime(api_key=None, model="sonnet"),
            budget=_FakeBudget(),
            client_factory=factory,
        )

    assert not (tmp_path / "nightly" / "archive" / "2026-04-22" / "synthesis.json").exists()
    assert _single_log(caplog.records, "nightly.parse_retry").channel_id == "C07FAIL"
    record = _single_log(caplog.records, "nightly.parse_fail_final")
    assert record.channel_id == "C07FAIL"
    assert record.raw_outputs == ["not json", second_raw]
    assert record.raw_output_initial == "not json"
    assert record.raw_output_retry == second_raw
    assert record.stop_reason == "max_turns_exhausted"
    assert record.message_count == 4
    assert record.prompt_tokens == 321
    assert record.prompt_preview == DEFAULT_PROMPT_TEMPLATE.read_text(encoding="utf-8")[:2000]
    assert record.model_actual == "claude-haiku-4-5,claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_retry_failure_logs_assistant_model_and_observed_message_count_when_sdk_omits_them(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="engram.nightly.synthesize")
    harvest = _write_harvest(tmp_path, [_channel("C07FALLBACK")])
    second_raw = json.dumps({"schema_version": 1, "date": "2026-04-22"})

    def factory(options: ClaudeAgentOptions) -> _AssistantModelFallbackClient:
        return _AssistantModelFallbackClient(options, second_raw)

    with pytest.raises(SynthesisOutputError):
        await synthesize(
            harvest,
            output_root=tmp_path / "nightly",
            config=NightlyConfig(),
            contexts_dir=tmp_path / "contexts",
            anthropic_runtime=AnthropicRuntime(api_key=None, model="configured-model"),
            budget=_FakeBudget(),
            client_factory=factory,
        )

    record = _single_log(caplog.records, "nightly.parse_fail_final")
    assert record.channel_id == "C07FALLBACK"
    assert record.message_count == 2
    assert record.prompt_tokens == 0
    assert record.model_actual == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_retry_failure_writes_raw_outputs_to_nightly_jsonl(tmp_path: Path) -> None:
    harvest = _write_harvest(tmp_path, [_channel("C07FAILJSONL")])
    second_raw = json.dumps({"schema_version": 1, "date": "2026-04-22"})
    final_result = _result(
        stop_reason="max_turns_exhausted",
        input_tokens=432,
        model_usage={
            "claude-haiku-4-5": {"input_tokens": 200},
            "claude-sonnet-4-6": {"input_tokens": 232},
        },
        message_count=6,
    )
    factory = _ClientFactory([[("not json", _result()), (second_raw, final_result)]])
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level

    try:
        configure_logging(tmp_path / "logs", force=True, file_prefix="nightly")
        with pytest.raises(SynthesisOutputError):
            await synthesize(
                harvest,
                output_root=tmp_path / "nightly",
                config=NightlyConfig(),
                contexts_dir=tmp_path / "contexts",
                anthropic_runtime=AnthropicRuntime(api_key=None, model="sonnet"),
                budget=_FakeBudget(),
                client_factory=factory,
            )
    finally:
        for handler in root_logger.handlers:
            handler.close()
        root_logger.handlers = original_handlers
        root_logger.setLevel(original_level)

    log_files = sorted((tmp_path / "logs").glob("nightly-*.jsonl"))
    assert len(log_files) == 1
    records = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
    record = next(item for item in records if item.get("event") == "nightly.parse_fail_final")
    assert record["channel_id"] == "C07FAILJSONL"
    assert record["raw_outputs"] == ["not json", second_raw]
    assert record["raw_output_initial"] == "not json"
    assert record["raw_output_retry"] == second_raw
    assert record["error"]
    assert record["stop_reason"] == "max_turns_exhausted"
    assert record["message_count"] == 6
    assert record["prompt_tokens"] == 432
    assert record["prompt_preview"] == DEFAULT_PROMPT_TEMPLATE.read_text(encoding="utf-8")[:2000]
    assert record["model_actual"] == "claude-haiku-4-5,claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_team_manifest_nightly_model_overrides_global_default(tmp_path: Path) -> None:
    contexts_dir = tmp_path / "contexts"
    _write_manifest(
        contexts_dir,
        ChannelManifest(
            channel_id="C07TEAM",
            identity=IdentityTemplate.TASK_ASSISTANT,
            status=ChannelStatus.ACTIVE,
            nightly=ChannelNightly(model="sonnet"),
        ),
    )
    harvest = _write_harvest(tmp_path, [_channel("C07TEAM")])
    factory = _ClientFactory([(_synthesis_json("C07TEAM"), _result())])

    result = await synthesize(
        harvest,
        output_root=tmp_path / "nightly",
        config=NightlyConfig(model="opus"),
        contexts_dir=contexts_dir,
        anthropic_runtime=AnthropicRuntime(api_key=None, model="global-model"),
        budget=_FakeBudget(),
        client_factory=factory,
    )

    assert factory.options[0].model == "sonnet"
    assert result.payload["channels"][0]["model"] == "sonnet"


@pytest.mark.asyncio
async def test_weekly_meta_synthesis_excludes_ineligible_channel_from_prompt_and_memory_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts_dir = tmp_path / "contexts"
    _write_manifest(
        contexts_dir,
        ChannelManifest(
            channel_id="C07KEEP",
            identity=IdentityTemplate.TASK_ASSISTANT,
            status=ChannelStatus.ACTIVE,
            label="#keep",
            permission_tier=PermissionTier.OWNER_SCOPED,
        ),
    )
    _write_manifest(
        contexts_dir,
        ChannelManifest(
            channel_id="C07PRIVATE",
            identity=IdentityTemplate.TASK_ASSISTANT,
            status=ChannelStatus.ACTIVE,
            label="#private",
            nightly_included=False,
        ),
    )
    captured_mcp: list[dict[str, Any]] = []

    def fake_make_memory_search_server(
        caller_channel_id: str,
        memory_db_path: Path | None = None,
        embedder: object | None = None,
        *,
        excluded_channels: list[str] | None = None,
    ) -> dict[str, Any]:
        captured_mcp.append(
            {
                "caller_channel_id": caller_channel_id,
                "excluded_channels": excluded_channels,
            }
        )
        return {"name": "engram-memory"}

    monkeypatch.setattr(
        "engram.nightly.synthesize.make_memory_search_server",
        fake_make_memory_search_server,
    )
    harvest = _write_harvest(
        tmp_path,
        [
            _channel("C07KEEP", row_id=11, text="eligible keep weekly row"),
            _channel("C07PRIVATE", row_id=22, text="private canary weekly row"),
            _channel("C07OPEN", row_id=33, text="eligible open weekly row"),
        ],
    )
    factory = _ClientFactory(
        [
            (_synthesis_json("C07KEEP"), _result()),
            (_synthesis_json("C07PRIVATE", summary="private per-channel summary"), _result()),
            (_synthesis_json("C07OPEN"), _result()),
            (_synthesis_json(META_CHANNEL_ID, summary="combined keep and open"), _result()),
        ]
    )

    result = await synthesize(
        harvest,
        output_root=tmp_path / "nightly",
        config=NightlyConfig(),
        contexts_dir=contexts_dir,
        anthropic_runtime=AnthropicRuntime(api_key=None, model="sonnet"),
        budget=_FakeBudget(),
        client_factory=factory,
        weekly=True,
    )

    channel_ids = [channel["channel_id"] for channel in result.payload["channels"]]
    assert channel_ids.count(META_CHANNEL_ID) == 1
    meta_channel = result.payload["channels"][-1]
    assert meta_channel["channel_id"] == META_CHANNEL_ID
    assert meta_channel["synthesis"]["summary"] == "combined keep and open"
    assert meta_channel["synthesis"]["source_row_ids"] == [11, 33]
    assert "private canary" not in json.dumps(meta_channel)

    meta_prompt = factory.clients[-1].prompts[0]
    assert "eligible keep weekly row" in meta_prompt
    assert "eligible open weekly row" in meta_prompt
    assert "private canary weekly row" not in meta_prompt
    assert captured_mcp[-1] == {
        "caller_channel_id": META_CHANNEL_ID,
        "excluded_channels": ["C07PRIVATE"],
    }


@pytest.mark.asyncio
async def test_daily_cost_cap_skips_remaining_channels_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="engram.nightly.synthesize")
    harvest = _write_harvest(
        tmp_path,
        [
            _channel("C07FIRST", row_id=1),
            _channel("C07SECOND", row_id=2),
        ],
    )
    factory = _ClientFactory([(_synthesis_json("C07FIRST"), _result(cost=0.02))])

    result = await synthesize(
        harvest,
        output_root=tmp_path / "nightly",
        config=NightlyConfig(daily_cost_cap_usd=0.025),
        contexts_dir=tmp_path / "contexts",
        anthropic_runtime=AnthropicRuntime(api_key=None, model="sonnet"),
        budget=_FakeBudget(),
        client_factory=factory,
    )

    assert len(factory.options) == 1
    assert [channel["channel_id"] for channel in result.payload["channels"]] == ["C07FIRST"]
    assert result.payload["skipped_channels"] == [
        {
            "channel_id": "C07SECOND",
            "estimated_cost_usd": "0.010000",
            "reason": "daily_cost_cap",
        }
    ]
    record = _single_log(caplog.records, "nightly.cost_cap_hit")
    assert record.skipped_channels == ["C07SECOND"]
    assert record.daily_cost_cap_usd == "0.025000"


@pytest.mark.asyncio
async def test_mocked_sdk_budget_overrun_aborts_channel(tmp_path: Path) -> None:
    options_seen: list[ClaudeAgentOptions] = []

    def factory(options: ClaudeAgentOptions) -> _BudgetAbortClient:
        options_seen.append(options)
        return _BudgetAbortClient(options)

    harvest = _write_harvest(tmp_path, [_channel("C07BUDGET")])

    result = await synthesize(
        harvest,
        output_root=tmp_path / "nightly",
        config=NightlyConfig(),
        contexts_dir=tmp_path / "contexts",
        anthropic_runtime=AnthropicRuntime(api_key=None, model="sonnet"),
        budget=_FakeBudget(),
        client_factory=factory,
    )

    assert options_seen[0].max_budget_usd == 5.0
    assert result.payload["channels"][0]["status"] == "sdk_error"
    assert result.payload["channels"][0]["error"]["error"] == "mocked max budget exceeded"


def test_build_nightly_options_attaches_hard_cap_disabled(tmp_path: Path) -> None:
    options = build_nightly_options(
        plan=PlannedChannel(
            channel=_channel("C07TEST"),
            manifest=None,
            model="sonnet",
            estimated_cost_usd=Decimal("0.01"),
        ),
        run_date="2026-04-22",
        current_dir=tmp_path / "current",
        runtime=AnthropicRuntime(api_key=None, model="sonnet"),
        hitl_config=HITLConfig(enabled=False),
        config=NightlyConfig(),
    )

    assert options.budget_config.hard_cap_enabled is False


def _single_log(records, message: str):
    matches = [record for record in records if record.getMessage() == message]
    assert len(matches) == 1
    return matches[0]
