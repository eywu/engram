"""Schema for nightly synthesis output."""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

META_CHANNEL_ID = "__meta__"


class SourcedText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    source_row_ids: list[int]


class ActionItem(SourcedText):
    owner: str | None


class CrossChannelFlag(SourcedText):
    related_channel_ids: list[str]


class NightlySynthesisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    channel_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    highlights: list[SourcedText]
    decisions: list[SourcedText]
    action_items: list[ActionItem]
    open_questions: list[SourcedText]
    cross_channel_flags: list[CrossChannelFlag]
    source_row_ids: list[int]


def synthesis_json_schema() -> dict[str, Any]:
    return NightlySynthesisOutput.model_json_schema()


def synthesis_output_format() -> dict[str, Any]:
    return {"type": "json_schema", "schema": synthesis_json_schema()}


def synthesis_schema_prompt() -> str:
    return json.dumps(synthesis_json_schema(), indent=2, sort_keys=True)
