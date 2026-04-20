"""Cost-trimming experiment — measure per-turn cost across config variations.

Runs the same prompt through several SDK configurations, records cost,
duration, tool-availability. Goal: find the cheapest config that still
preserves tool/skill access quality.

Usage:
    uv run python scripts/cost_experiment.py

Total budget: <$2, ~10 min wall time.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)

# Prompts. Each config runs all prompts, so costs are comparable.
PROMPTS = [
    # Short baseline — minimal input, minimal expected output
    ("tiny", "Reply with the single word: pong"),
    # Medium prose — typical user question
    (
        "medium",
        "In two sentences, explain what a launch agent is on macOS.",
    ),
    # Tool-requiring — forces the agent to check whether tools are available
    (
        "tool_probe",
        "List three tools you have available to you, and briefly say what each one does. "
        "Do not actually call any of them — just enumerate.",
    ),
]


@dataclass
class TurnResult:
    prompt_name: str
    cost_usd: float | None
    duration_ms: int | None
    num_turns: int | None
    response_text: str
    response_len: int
    tools_seen: list[str] = field(default_factory=list)
    system_preview: str = ""
    error: str | None = None


@dataclass
class ConfigResult:
    name: str
    setting_sources: list[str] | None
    max_turns: int
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(t.cost_usd or 0.0 for t in self.turns)

    @property
    def avg_cost(self) -> float:
        valid = [t.cost_usd for t in self.turns if t.cost_usd is not None]
        return sum(valid) / len(valid) if valid else 0.0

    @property
    def total_duration_s(self) -> float:
        return sum((t.duration_ms or 0) for t in self.turns) / 1000.0


async def run_one(prompt: str, options: ClaudeAgentOptions) -> TurnResult:
    """Run a single query; capture result + any SystemMessage metadata."""
    text_chunks: list[str] = []
    tools_seen: list[str] = []
    system_preview = ""
    result: ResultMessage | None = None
    error: str | None = None

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        text_chunks.append(text)
                    # Capture tool_use blocks if any
                    tool_name = getattr(block, "name", None)
                    if tool_name:
                        tools_seen.append(tool_name)
            elif isinstance(message, SystemMessage):
                # SystemMessage includes init info — tools, model, etc.
                data = getattr(message, "data", {}) or {}
                if data.get("subtype") == "init":
                    tools = data.get("tools", [])
                    if isinstance(tools, list):
                        tools_seen = [str(t) for t in tools]
                    # Short preview of what system prompt the SDK primed
                    sp = data.get("system_prompt") or ""
                    if isinstance(sp, str):
                        system_preview = sp[:200]
            elif isinstance(message, ResultMessage):
                result = message
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    response = "".join(text_chunks).strip()
    return TurnResult(
        prompt_name="",  # filled by caller
        cost_usd=getattr(result, "total_cost_usd", None) if result else None,
        duration_ms=getattr(result, "duration_ms", None) if result else None,
        num_turns=getattr(result, "num_turns", None) if result else None,
        response_text=response,
        response_len=len(response),
        tools_seen=tools_seen,
        system_preview=system_preview,
        error=error,
    )


async def run_config(
    name: str,
    setting_sources: list[str] | None,
    max_turns: int = 2,
) -> ConfigResult:
    print(f"\n{'='*60}\nConfig: {name}")
    print(f"  setting_sources={setting_sources}  max_turns={max_turns}")
    print(f"{'='*60}")

    cfg = ConfigResult(
        name=name, setting_sources=setting_sources, max_turns=max_turns
    )

    for prompt_name, prompt in PROMPTS:
        kwargs = {
            "max_turns": max_turns,
            "model": "claude-sonnet-4-6",  # match prod config
        }
        if setting_sources is not None:
            kwargs["setting_sources"] = setting_sources

        options = ClaudeAgentOptions(**kwargs)
        print(f"\n  [{prompt_name}] running…", end=" ", flush=True)
        start = time.time()
        result = await run_one(prompt, options)
        result.prompt_name = prompt_name
        elapsed = time.time() - start
        cfg.turns.append(result)

        if result.error:
            print(f"ERROR after {elapsed:.1f}s: {result.error}")
        else:
            cost_str = (
                f"${result.cost_usd:.4f}"
                if result.cost_usd is not None
                else "?"
            )
            print(
                f"{cost_str}  wall={elapsed:.1f}s  "
                f"sdk={result.duration_ms or 0}ms  "
                f"tools={len(result.tools_seen)}  "
                f"resp_len={result.response_len}"
            )

    return cfg


def print_report(results: list[ConfigResult]) -> None:
    print("\n\n" + "=" * 72)
    print("COST COMPARISON REPORT")
    print("=" * 72)

    print(
        f"\n{'config':<30} {'total':>9} {'avg/turn':>10} {'tools':>6} {'dur':>8}"
    )
    print("-" * 72)
    for r in results:
        # Max tools seen across all turns
        max_tools = max((len(t.tools_seen) for t in r.turns), default=0)
        print(
            f"{r.name:<30} ${r.total_cost:>7.4f} ${r.avg_cost:>8.4f} "
            f"{max_tools:>6} {r.total_duration_s:>6.1f}s"
        )

    print("\n" + "=" * 72)
    print("PER-PROMPT BREAKDOWN")
    print("=" * 72)

    # Group by prompt for side-by-side comparison
    for prompt_name, _ in PROMPTS:
        print(f"\n--- prompt: {prompt_name} ---")
        for r in results:
            t = next((x for x in r.turns if x.prompt_name == prompt_name), None)
            if not t:
                continue
            cost = f"${t.cost_usd:.4f}" if t.cost_usd is not None else "?"
            snippet = t.response_text.replace("\n", " ")[:80]
            print(
                f"  {r.name:<28} {cost:>10} "
                f"tools={len(t.tools_seen):>3} "
                f"| {snippet}"
            )

    print("\n" + "=" * 72)
    print("TOOLS AVAILABLE PER CONFIG (from SystemMessage init)")
    print("=" * 72)
    for r in results:
        # First turn's tools_seen is representative (SystemMessage once per query)
        tools = r.turns[0].tools_seen if r.turns else []
        print(f"\n{r.name} ({len(tools)} tools):")
        if tools:
            # Print in columns
            for i in range(0, len(tools), 4):
                chunk = tools[i:i + 4]
                print("  " + "  ".join(f"{t:<20}" for t in chunk))
        else:
            print("  (none)")


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    configs = [
        # Current M1 default
        ("current (user+project)", ["user", "project"], 2),
        # User settings only — no project-level config priming
        ("user-only", ["user"], 2),
        # No setting sources at all — minimal priming
        ("none", [], 2),
        # SDK default (don't pass the arg at all)
        ("sdk-default", None, 2),
    ]

    results: list[ConfigResult] = []
    for name, ss, mt in configs:
        r = await run_config(name, ss, mt)
        results.append(r)

    print_report(results)

    print("\n\nTotal experiment cost: "
          f"${sum(r.total_cost for r in results):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
