# Engram 🧠

> Personal AI agent for Slack — per-channel isolation, persistent memory, skill integration.

**Status:** Beta — M1 through M5 shipped. Installable, self-hosted, mac-only.

Engram is a lightweight AI agent that lives in Slack. It gives each channel its
own Claude instance, its own context, its own memory, and its own capability
scope — so your DM with the agent is different from a team channel, and neither
bleeds into the other.

Built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
(Python). No tmux. No CLI wrapping. No browser automation. Just a single-process
bridge between Slack and Claude.

## Quickstart

Assumes macOS + a Slack workspace where you can create apps. Full walkthrough
with screenshots, prerequisites, and troubleshooting is in
[docs/INSTALL.md](docs/INSTALL.md).

```bash
# 1. Install prereqs (if you don't have them)
curl -LsSf https://astral.sh/uv/install.sh | sh    # uv (Python tooling)
npm install -g @anthropic-ai/claude-code           # claude CLI

# 2. Clone + install Engram
git clone https://github.com/eywu/engram.git && cd engram
./scripts/install.sh

# 3. Create the Slack app — see docs/slack-app-setup.md
#    Grab xoxb-… (bot token) and xapp-… (app-level token)

# 4. Configure — wizard collects tokens, API keys, writes ~/.engram/config.yaml
engram setup

# 5. Run — foreground to verify
engram run

# 6. Daemonize under launchd (optional, recommended)
./scripts/install_launchd.sh
./scripts/install_launchd.sh --install-nightly      # optional: nightly memory synthesis
```

Then DM the Engram bot in your Slack workspace. First response typically
arrives within ~30s.

## Design Principles

- **Per-channel isolation.** Each Slack channel/DM gets its own context
  directory, identity, memory bank, and capability manifest. Team-channel
  scope leaks are prevented at the manifest layer, not via convention.
- **MCP-agnostic.** Engram discovers MCP servers at setup time. Zero MCPs,
  one MCP, or many MCPs are all supported setups.
- **Soft budget guardrails.** Monthly spend is tracked and warnings fire as
  you approach the cap — but the agent keeps serving. No surprise
  "your agent is paused" moments.
- **Self-improvement by default.** Nightly, Engram reviews its own
  transcripts, synthesizes per-channel summaries, and writes them back to a
  shared memory layer. Opus-synthesized, validator-gated, HITL-disabled.
- **Human-in-the-loop, cleanly.** When Claude needs your permission for a
  tool call, Engram posts a Block Kit question in the same channel, waits
  for your click or threaded reply, and only then resumes. The gate is a
  real `can_use_tool` callback — execution blocks until you answer. See
  [docs/hitl.md](docs/hitl.md).

## Architecture (short version)

```
Slack (Socket Mode)
  │
  ▼
slack-bolt/python ──▶ Engram bridge ──▶ Claude Agent SDK ──▶ Claude
                          │                    │
                          ▼                    ▼
                   per-channel state      MCP servers
                   (CLAUDE.md, .claude/)         │
                          │                     ▼
                          ▼               (your configured
                    ~/.engram/              MCP tools)
                      ├── config.yaml
                      ├── memory.db       ← FTS5 + embeddings
                      ├── channels/<id>/  ← per-channel manifests
                      └── logs/           ← structured JSONL
```

## Status — Milestone Summary

| Milestone | Status | What it added |
|-----------|--------|---------------|
| M0 — Verify assumptions | ✅ Shipped | SDK validation, auth model, repo setup |
| M1 — Scaffold + ingress | ✅ Shipped | Bolt bridge, first live turn, cost ledger |
| M2 — Per-channel isolation | ✅ Shipped | Manifest templates, scope denies, provisioning flow |
| M3 — Memory + budget | ✅ Shipped | SQLite memory.db + FTS5 + Gemini embeddings, budget warnings |
| M4 — Human-in-the-loop | ✅ Shipped | Block Kit permission cards, `can_use_tool` gate |
| M5 — Self-improvement | ✅ Shipped | Nightly harvest → synthesize → validate → write back |

Post-M5 cleanup (Apr 2026): 336 tests, ruff clean, live under launchd.

## What Running This Costs

Engram calls Claude via your own Anthropic API key — this is billed separately
from any Claude subscription you have. Embeddings use Gemini (optional, free
tier is plenty).

- **Per-turn cost** varies with model + config + tool use: typical range
  ~\$0.005–\$0.06. Single tool-heavy turns can occasionally spike higher.
- **Monthly cost** depends entirely on how much you use it. Light DM use is
  dollars/month; heavy daily use in multiple channels can run \$100+.
- **Budget guardrails** are soft: warnings fire as monthly spend approaches
  your configured cap; the agent keeps serving.

Check your actual spend any time:

```bash
engram cost --month              # month-to-date
engram cost --today              # today only
engram cost --by-channel         # break down per channel
```

Cost data is stored in `~/.engram/logs/costs.jsonl` (one line per turn) and
aggregated in the SQLite ledger at `~/.engram/costs.db`.

## Prerequisites

- **macOS** (Linux may work; not tested)
- **Python 3.12+** (installed automatically by `install.sh` via `uv`)
- **Node.js + npm** (for the `claude` CLI)
- **Anthropic API key** — https://console.anthropic.com/settings/keys
- **Gemini API key** (optional but recommended — enables semantic memory
  search) — https://aistudio.google.com/app/apikey
- **Slack workspace** where you have admin rights to create apps

## Commands

```bash
engram setup                    # first-time configuration wizard
engram run                      # start the bridge (foreground)
engram status                   # show bridge health, channels, memory counts
engram cost                     # query the cost ledger
engram logs                     # tail the most recent structured log
engram health                   # health check for launchd watchdogs
engram channels list            # list provisioned channels
engram channels show <id>       # inspect a channel's manifest + CLAUDE.md
engram channels approve <id>    # approve a pending channel
engram scope                    # audit per-channel scope and memory eligibility
engram nightly                  # run nightly synthesis manually
```

See `engram <command> --help` for options.

## Config

Engram reads `~/.engram/config.yaml` (mode `600`). Secrets live here. You can
also keep them in an env file — see [docs/INSTALL.md](docs/INSTALL.md) for
the `ENGRAM_ENV_FILE` override.

## License

MIT (to be added).

## Maintainer

Eric Wu ([@eywu](https://github.com/eywu)) with help from an AI collaborator
named BrownBear.
