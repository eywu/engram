# Engram 🧠

> Personal AI agent for Slack — per-channel isolation, persistent memory, skill integration.

**Status:** Early development (M1 scaffold). Not yet installable.

Engram is a lightweight AI agent that lives in Slack. It gives each channel its
own Claude instance, its own context, its own memory, and its own capability
scope — so your DM with the agent is different from a team channel, and neither
bleeds into the other.

Built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
(Python). No tmux. No CLI wrapping. No browser automation. Just a single-process
bridge between Slack and Claude.

## Design Principles

- **Per-channel isolation.** Each Slack channel/DM gets its own context directory,
  identity, memory bank, and capability manifest.
- **MCP-agnostic.** Engram discovers MCP servers at setup time. Zero MCPs is a
  supported setup. One MCP is a supported setup. Many MCPs is a supported setup.
- **Soft budget guardrails.** Monthly spend is tracked and warnings fire as you
  approach the cap — but the agent keeps serving. No surprise "your agent is
  paused" moments.
- **Self-improvement by default.** Nightly, Engram reviews its own traces and
  proposes improvements to its own prompts, skills, and memory. Dry-run for a
  week before auto-apply.
- **Human-in-the-loop, cleanly.** When Engram needs your input, it asks you in
  Slack with block-kit buttons. You click. It resumes.

## Architecture (short version)

```
Slack (Socket Mode)
  │
  ▼
slack-bolt/python ──▶ Engram bridge ──▶ Claude Agent SDK ──▶ Claude
                          │                    │
                          ▼                    ▼
                   per-channel state      MCP servers
                   (CLAUDE.md, .claude/)
```

Full design: coming in later milestones.

## Status

| Milestone | Status |
|-----------|--------|
| M0 — Verify assumptions | ✅ Complete (2026-04-20) |
| M1 — Scaffold + ingress | 🏗️ In progress |
| M2 — Per-channel isolation | ⏳ |
| M3 — Budget + observability | ⏳ |
| M4 — AskUserQuestion + HITL | ⏳ |
| M5 — Self-improvement loop | ⏳ |

## License

MIT (to be added).

## Maintainer

Eric Wu ([@eywu](https://github.com/eywu)) with help from an AI collaborator
named BrownBear.
