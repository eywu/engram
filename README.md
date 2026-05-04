# Engram 🧠

> Personal AI agent that lives in your Slack — with persistent memory, per-channel isolation, and human-in-the-loop safety.

**Status:** Beta. Installable, self-hosted, **macOS-only**.

## What it does

You install Engram on your Mac. It logs into your Slack workspace as a bot.
You DM it, and it answers as Claude — with memory of your past conversations.
Drop it into a team channel, and that channel gets its own isolated identity,
memory, and capabilities. Your DM and the team channel never bleed into each
other.

It's a single-process bridge between Slack and the
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).
No tmux, no CLI wrapping, no browser automation.

## Before you start

> **You need all of these before installing.** Engram won't work without them.

- 🍎 **A Mac.** Linux may work; not tested. Windows: no.
- 💬 **A Slack workspace where YOU have admin rights.**
  Most company workspaces require admin approval to create apps — if yours
  does, [create a free personal workspace](https://slack.com/create) for
  testing instead. Don't try to install Engram in your company Slack on day
  one.
- 🔑 **An Anthropic account with billing enabled.**
  Engram calls Claude via your own API key, separate from any Claude.ai
  subscription. Typical cost is **\$5–\$50/month** depending on use; you can
  set a soft monthly cap to get warned as you approach it. Sign up at
  [console.anthropic.com](https://console.anthropic.com/settings/keys).
- 🍺 **Homebrew + Node.** Install Homebrew from
  [brew.sh](https://brew.sh), then `brew install node`. Engram uses the
  `claude` CLI which ships via npm.
- ✨ **(Optional) A Gemini API key.** Free tier is plenty. This enables
  semantic memory search — without it, memory still works but uses keyword
  search only. Get one at
  [aistudio.google.com](https://aistudio.google.com/app/apikey).

If you don't have all of those yet, **stop here and gather them first**.
The install assumes everything above is already in place.

## Quickstart

Full walkthrough with troubleshooting is in
[docs/INSTALL.md](docs/INSTALL.md). The short version:

```bash
# 1. Install Python tooling and the Claude CLI
curl -LsSf https://astral.sh/uv/install.sh | sh    # uv (Python tooling)
npm install -g @anthropic-ai/claude-code           # claude CLI

# 2. Clone + install Engram
git clone https://github.com/eywu/engram.git && cd engram
./scripts/install.sh

# 3. Create the Slack app — see docs/slack-app-setup.md
#    You'll generate two tokens: xoxb-… (bot) and xapp-… (app-level)

# 4. Configure — wizard collects tokens, writes ~/.engram/config.yaml
engram setup

# 5. Run it in the foreground to verify everything works
engram run

# 6. (Recommended) Daemonize under launchd so it runs on boot
./scripts/install_launchd.sh
./scripts/install_launchd.sh --install-nightly      # optional: nightly memory synthesis
```

Then DM the Engram bot in your Slack workspace. First response typically
arrives within ~30 seconds.

## Uninstalling

To see what Engram would remove, run a dry run first, then run the
interactive uninstall when you're ready. Use `--keep-data` for an
upgrade-style uninstall that leaves `~/.engram/` in place, or `--purge` for
non-interactive removal.

```bash
uv run engram uninstall --dry-run   # see what would happen
uv run engram uninstall              # interactive
```

## What things cost

Engram calls Claude via your own Anthropic API key — billed separately from
any Claude.ai subscription.

- **Per-turn cost** typically ranges **\$0.005–\$0.06**. Tool-heavy turns can
  occasionally spike higher.
- **Monthly cost** depends on how much you use it.
  - Light personal-DM use: a few dollars a month.
  - Heavy daily use across multiple channels: \$50–\$100+.
- **You can set a soft cap.** Add `monthly_budget_usd: 20` to your
  `~/.engram/config.yaml` and Engram warns you as you approach it; the agent
  keeps serving so you never get a "your bot is paused" surprise.

Check your spend any time:

```bash
engram cost --month              # month-to-date
engram cost --today              # today only
engram cost --by-channel         # break down per channel
```

## Safety model

Engram isolates each Slack room at three layers: separate workspace files,
separate memory state, and a separate capability boundary. Each channel
carries its own manifest, so approval status, permission tier, deny rules,
and nightly eligibility stay local to that room.

- **Sensitive tools go through human-in-the-loop.** Engram posts a Slack
  approval card, waits for your answer, and only then proceeds. See
  [docs/hitl.md](docs/hitl.md).
- **Destructive shell commands have a second barrier.** A type-to-confirm
  modal requires the literal word `CONFIRM`, even in YOLO mode. See
  [docs/footguns.md](docs/footguns.md).
- **Three permission tiers** (`safe` / `trusted` / `yolo`) let you tune how
  much Engram can do without asking. See
  [docs/permission-tiers.md](docs/permission-tiers.md).

## Commands

### CLI

| Command | What it does |
| --- | --- |
| `engram setup` | First-time configuration wizard |
| `engram run` | Start the bridge in the foreground |
| `engram doctor` | Run 19 health checks with actionable fixes |
| `engram status` | Show bridge health, channels, and memory counts |
| `engram cost` | Query the cost ledger |
| `engram logs` | Tail recent structured logs |
| `engram health` | Launchd watchdog health check (machine-readable) |
| `engram channels list` | List provisioned channels |
| `engram channels show <id>` | Inspect a channel manifest and `CLAUDE.md` |
| `engram channels approve <id>` | Approve a pending channel |
| `engram channels upgrade <channel-id> <tier> [--until 24h\|30d\|permanent]` | Change a channel's permission tier |
| `engram channels tier <channel-id>` | Show the effective tier, YOLO status, and expiry |
| `engram yolo list` | List channels with active YOLO grants |
| `engram yolo off <channel-id>` | Revoke an active YOLO grant immediately |
| `engram yolo extend <channel-id> <6h\|24h\|72h>` | Extend an active YOLO grant |
| `engram scope` | Audit per-channel scope and memory eligibility |
| `engram nightly` | Run nightly synthesis manually |

### Slack slash commands

| Slash command | What it does |
| --- | --- |
| `/engram upgrade <tier> [reason...]` | Request a permission-tier upgrade. Team channels require owner-DM approval. Tiers: `safe`, `trusted`, `yolo`. |
| `/engram yolo <list\|off\|extend> ...` | Manage time-boxed YOLO sessions. |
| `/exclude-from-nightly [#channel]` | Exclude a channel from the nightly cross-channel meta-summary. |
| `/include-in-nightly [#channel]` | Re-include a channel in the nightly meta-summary. |

See `engram <command> --help` for options on any command.

## Configuration

Engram reads `~/.engram/config.yaml` (mode `600`). Secrets live here. You can
also keep them in an env file — see
[docs/INSTALL.md](docs/INSTALL.md) for the `ENGRAM_ENV_FILE` override.

If something feels off, **always run `engram doctor` first**. It checks 19
things (Slack tokens, API keys, launchd job, MCP coverage, config validity)
and tells you exactly how to fix anything that's wrong.

---

<details>
<summary><b>For developers / under the hood</b></summary>

### Design Principles

- **Per-channel isolation.** Each Slack channel/DM gets its own context
  directory, identity, memory bank, and capability manifest. Team-channel
  scope leaks are prevented at the manifest layer, not via convention.
- **MCP-agnostic.** Engram uses `~/.claude.json` as the single user MCP
  inventory, matching what `claude mcp add` updates. Zero, one, or many
  MCPs are all supported.
- **Soft budget guardrails.** Monthly spend is tracked and warnings fire as
  you approach the cap — but the agent keeps serving. No surprise paused
  states.
- **Self-improvement by default.** Nightly, Engram reviews its own
  transcripts, synthesizes per-channel summaries, and writes them back to a
  shared memory layer. Opus-synthesized, validator-gated, HITL-disabled.
- **Human-in-the-loop, cleanly.** When Claude needs your permission for a
  tool call, Engram posts a Block Kit question in the same channel, waits
  for your click or threaded reply, and only then resumes. The gate is a
  real `can_use_tool` callback — execution blocks until you answer.

### Architecture

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

</details>

## License

[MIT](https://opensource.org/license/mit). See [LICENSE](LICENSE).
