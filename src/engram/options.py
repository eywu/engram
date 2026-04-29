"""Engram-specific Claude Agent SDK option extensions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions

if TYPE_CHECKING:
    from engram.budget import BudgetConfig
    from engram.config import HITLConfig


@dataclass
class EngramAgentOptions(ClaudeAgentOptions):
    """Claude Agent options plus Engram runtime metadata."""

    strict_mcp_config: bool = False
    hitl_config: HITLConfig | None = None
    budget_config: BudgetConfig | None = None
