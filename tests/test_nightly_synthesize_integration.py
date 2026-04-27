from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeSDKClient

from engram.config import HITLConfig, NightlyConfig
from engram.nightly.synthesize import (
    DEFAULT_PROMPT_TEMPLATE,
    AnthropicRuntime,
    PlannedChannel,
    _synthesize_channel,
    parse_synthesis_output,
)

_API_KEY = os.environ.get("ENGRAM_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
_MODEL = os.environ.get("ENGRAM_MODEL") or "claude-sonnet-4-6"


@dataclass
class _BudgetProbe:
    config: object | None = None
    records: list[tuple[str, str | None, Any]] = field(default_factory=list)

    def record(self, channel_id: str, user_id: str | None, result_message: Any) -> None:
        self.records.append((channel_id, user_id, result_message))


@pytest.mark.asyncio
@pytest.mark.requires_api_key
@pytest.mark.skipif(
    not _API_KEY or bool(os.environ.get("CI")),
    reason="requires Anthropic API key and is skipped in CI",
)
async def test_synthesize_channel_fixture_returns_parseable_json(tmp_path: Path) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "nightly" / "harvest-basic.json"
    harvest = json.loads(fixture_path.read_text(encoding="utf-8"))
    channel = harvest["channels"][0]
    current_dir = tmp_path / "nightly" / "current"
    current_dir.mkdir(parents=True)
    budget = _BudgetProbe()

    result = await _synthesize_channel(
        PlannedChannel(
            channel=channel,
            manifest=None,
            model=_MODEL,
            estimated_cost_usd=Decimal("0.01"),
        ),
        run_date=harvest["date"],
        current_dir=current_dir,
        prompt_template=DEFAULT_PROMPT_TEMPLATE.read_text(encoding="utf-8"),
        weekly=False,
        config=NightlyConfig(),
        runtime=AnthropicRuntime(api_key=_API_KEY, model=_MODEL),
        hitl_config=HITLConfig(enabled=False),
        budget=budget,
        client_factory=ClaudeSDKClient,
    )

    assert result["status"] == "synthesized"
    assert result["channel_id"] == channel["channel_id"]
    assert result["synthesis"]["summary"]
    assert budget.records
    assert parse_synthesis_output(json.dumps(result["synthesis"])) == result["synthesis"]
