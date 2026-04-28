# Engram Install Guide

A single walkthrough, from zero to a running Slack bot. Follow it top to
bottom and you'll end up with Engram running under `launchd` with a nightly
memory synthesis job.

Estimated time: **25–40 minutes** the first time, most of which is Slack
app configuration.

> **Platform:** macOS only (tested on Apple Silicon). Linux should mostly
> work but isn't part of the supported install path.

---

## Prerequisites

You need all six of these before starting. It's easiest to set them up in
this order.

### 1. `uv` — Python package manager

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verify: `uv --version` (should print `uv 0.4+`).

### 2. Node.js + npm

If you don't already have it:

```bash
brew install node
```

Verify: `node --version` (any LTS is fine) and `npm --version`.

### 3. Claude CLI

Engram delegates turns to the Claude Agent SDK, which subprocesses the
`claude` CLI. You need this installed globally:

```bash
npm install -g @anthropic-ai/claude-code
```

Verify: `claude --version` (should print `@anthropic-ai/claude-code X.Y.Z`).

### 4. Anthropic API key

Engram uses a **separate** API key, not your Claude Code OAuth session.
This is billed independently from any Claude subscription.

1. Go to https://console.anthropic.com/settings/keys
2. Create a key (any name — e.g. `engram-bot`)
3. Copy it (`sk-ant-…`) — you'll paste it into the wizard in a moment

### 5. Gemini API key (optional, recommended)

Engram uses Gemini `text-embedding-004` for semantic memory search. The
free tier is more than enough for personal use.

- **With a key:** semantic + keyword (FTS5) memory search
- **Without a key:** keyword-only memory — still works, just less accurate
  for paraphrase and conceptual recall

1. Go to https://aistudio.google.com/app/apikey
2. Create an API key
3. Copy it (`AIzaSy…`) — paste it into the wizard in a moment

You can skip this step and add a key later by editing
`~/.engram/config.yaml` (`embeddings.api_key`) or exporting
`GEMINI_API_KEY`.

### 6. Slack workspace

You need admin rights to create apps in the workspace where Engram should
live. Personal workspaces work. Company workspaces typically require
approval from a workspace admin.

---

## Step 1 — Clone and install Engram

```bash
git clone https://github.com/eywu/engram.git
cd engram
./scripts/install.sh
```

What this does:
- Verifies `uv` and `claude` are on your `PATH`
- Installs Python 3.12 via `uv` (if not already present)
- Syncs project dependencies
- Installs the `engram` CLI as a `uv tool` (so `engram` is on your `PATH`)

Verify:

```bash
engram version
```

If `engram` isn't found:

```bash
uv tool update-shell
exec $SHELL
```

---

## Step 2 — Create the Slack app

Engram needs its own Slack app with Socket Mode enabled. Full details with
the app manifest are in [slack-app-setup.md](slack-app-setup.md).

**Short version:**

1. Go to https://api.slack.com/apps → **Create New App** → **From an app manifest**
2. Pick your workspace
3. Paste the manifest from `docs/slack-app-setup.md` (or run
   `engram setup` — it also writes the manifest to
   `/tmp/engram-slack-manifest.yaml` for convenience)
4. Click **Create**
5. **Install to Workspace** → **Allow**
6. Under **OAuth & Permissions**, copy the **Bot User OAuth Token** (`xoxb-…`)
7. Under **Basic Information** → **App-Level Tokens**, generate a token
   with scope `connections:write` and copy it (`xapp-…`)
8. **Verify slash commands work:** After installing the app, type `/engram`
   in any channel where Engram is present. Slack should autocomplete it. If
   it doesn't, the manifest wasn't applied — re-paste the manifest at
   [api.slack.com](http://api.slack.com) and reinstall.

Keep both tokens handy for the next step.

### Upgrading an existing install

If you installed Engram before slash commands were added to the manifest: go
to [api.slack.com/apps](http://api.slack.com/apps) → your Engram app →
**App Manifest**, replace the contents with the new manifest from
`docs/slack-app-setup.md`, click **Save Changes**, then **Install App** to
reinstall. This is a no-downtime change.

---

## Step 3 — Run the setup wizard

```bash
engram setup
```

The wizard walks you through six steps:

1. **Claude CLI check** — confirms `claude` is installed
2. **Slack tokens** — paste the two tokens from Step 2
3. **Anthropic API key** — paste or confirm the env var
4. **Gemini API key** (optional) — paste or skip
5. **MCP inventory** — discovers any MCP servers in `~/.claude.json`
   (`mcpServers`), the single user inventory `claude mcp add` updates. If
   a legacy `~/.claude/mcp.json` exists, Engram migrates it with a backup.
   For existing team channels, the wizard can also add discovered MCPs to
   per-channel allow-lists interactively. See
   [mcp.md](mcp.md).
6. **Write config** — saves to `~/.engram/config.yaml` with mode `600`

If anything was wrong, re-run `engram setup` to overwrite.

---

## Step 4 — Verify

```bash
engram status
```

You should see:
- Bridge version and bridge status (not running yet, that's expected)
- Memory counts (all zero the first time)
- Channels table (empty the first time)

If tokens are invalid or the config is malformed, `engram status` will
tell you. Fix and re-run `engram setup`.

---

## Step 5 — Run it (foreground first)

```bash
engram run
```

You should see:
- `engram.starting` and `engram.ready` log lines
- A connection confirmation from Socket Mode

**DM the Engram bot in Slack.** Any message works — try "Hello". You should
get a Claude response within ~30 seconds.

When you `Ctrl-C` to stop, the bridge shuts down gracefully.

---

## Step 6 — Daemonize under launchd (recommended)

```bash
./scripts/install_launchd.sh
```

This:
1. Writes `~/Library/LaunchAgents/com.engram.bridge.plist`
2. Loads the service
3. Polls for `engram.ready` to confirm the bot actually connected

After this, Engram runs in the background and auto-restarts on crash. Logs
go to `/tmp/engram.bridge.out.log` and `/tmp/engram.bridge.err.log`.
The installed bridge plist also raises launchd's soft open-file limit to
`4096` so the service has headroom above macOS's low default of `256`.

During install, the script resolves one secrets file for the bridge and
writes its absolute path into the plist as `ENGRAM_ENV_FILE`. Resolution
order is:

1. `$ENGRAM_ENV_FILE` from the shell running the installer
2. `~/.engram/.env`
3. `<repo>/.env`

That pinned `ENGRAM_ENV_FILE` is how the launchd-managed bridge receives
`ANTHROPIC_API_KEY` and other secrets after `launchctl kickstart -k`,
reboots, or crashes. If none of those files exists, the installer stops
and tells you to either export `ENGRAM_ENV_FILE` or run `engram setup`.

The installer also checks for Node-based MCP runtimes. If your
`~/.claude.json` includes an MCP whose `command` is `npx` or `node`, the
installer tries to detect the active Node bin directory and prepends it to
the bridge plist `PATH` when needed. If detection fails, the install stops
with an explicit fix: either install Node on a stable path with
`brew install node`, or run `nvm use --lts` and rerun
`./scripts/install_launchd.sh`.

### Nightly memory synthesis (also recommended)

```bash
./scripts/install_launchd.sh --install-nightly
```

This registers a second launchd job (`com.engram.v3.nightly`) that runs
every night at 02:00 local time. It harvests recent transcripts, synthesizes
per-channel summaries with Opus, validates the output, and writes it back
to the memory DB. Weekly meta-summaries run Monday nights.

You don't need this on day one. Add it once you're using Engram enough
that a week's worth of transcripts is worth summarizing.

---

## Managing Engram without slash commands

If you can't register slash commands in your Slack workspace, all Engram
management works via CLI on the host running the bridge. The CLI is fully
equivalent to the Slack slash-command surface, so an admin-less user can
manage per-channel MCP access, tiers, YOLO mode, nightly-summary inclusion,
and the channel dashboard without needing Slack app-manifest changes.

| Slack surface | CLI equivalent | Notes |
| --- | --- | --- |
| `/engram mcp list` | `engram channels mcp list <channel-id>` | Shows the effective MCPs for one channel plus declared allow/deny state. |
| `/engram mcp allow <server>` | `engram channels mcp allow <channel-id> <server>` | First-class replacement for hand-editing `mcp_servers.allowed`. |
| `/engram mcp deny <server>` | `engram channels mcp deny <channel-id> <server>` | Adds to `mcp_servers.disallowed`; safe for immediate lock-downs. |
| `/engram new` | `engram channels new <channel-id> [--yes]` | Starts a fresh SDK conversation for the channel. Memory, tier, and YOLO grants are preserved; MCP and project config reload on the next message. |
| `/engram upgrade <safe\|trusted\|yolo>` | `engram channels upgrade <channel-id> <tier> [--until 24h\|30d\|permanent]` | Upgrade or downgrade a channel tier directly from the bridge host. |
| `/engram yolo extend <channel> <duration>` | `engram yolo extend --channel <channel-id> <6h\|24h\|72h>` | Omitting `--channel` auto-targets the only active YOLO grant. |
| `/engram yolo list` | `engram yolo list` | Lists every active YOLO grant with remaining time and restore tier. |
| `/engram yolo off <channel>` | `engram yolo off --channel <channel-id>` | Omitting `--channel` auto-targets the only active YOLO grant. |
| `/engram exclude` | `engram channels exclude <channel-id>` | Excludes one channel from the nightly cross-channel summary. |
| `/engram include` | `engram channels include <channel-id>` | Re-includes a channel unless it is still `safe`. |
| `/engram channels` | `engram channels list` | Shows the channel dashboard in tabular form. |
| Slack-only parity gap: scripting | `engram channels list --json` | Stable versioned JSON for shell and `jq` pipelines. |

Examples:

```bash
engram channels mcp list C07TEAM123
engram channels mcp allow C07TEAM123 camoufox
engram channels mcp deny C07TEAM123 camoufox
engram channels new C07TEAM123 --yes
engram channels upgrade C07TEAM123 trusted
engram channels exclude C07TEAM123
engram channels include C07TEAM123
engram yolo extend --channel D07OWNER123 24h
engram yolo off --channel C07TEAM123
engram channels list --json | jq '.channels[] | {channel_id, tier, nightly}'
```

For tier behavior, defaults, and alias migration details, see
[permission-tiers.md](permission-tiers.md).
For per-channel MCP policy and troubleshooting, see [mcp.md](mcp.md).

---

## Optional: personal secret-file conventions

Engram's `.env` lookup order is:

1. `$ENGRAM_ENV_FILE` (if set)
2. `./.env` (project-local)
3. `~/.engram/.env` (user-scoped)

If you keep secrets in a non-standard location (e.g. a shared `~/secrets/`
directory), point Engram at it with:

```bash
# Add to ~/.zshrc or ~/.bash_profile
export ENGRAM_ENV_FILE=~/secrets/engram.env
```

Foreground CLI runs use the lookup order above. For `launchd`, do not rely
on the shell that happened to run the installer. Instead, export
`ENGRAM_ENV_FILE` before running `./scripts/install_launchd.sh`; the installer
will resolve it and write the absolute path into
`~/Library/LaunchAgents/com.engram.bridge.plist`:

```xml
<key>ENGRAM_ENV_FILE</key>
<string>/Users/YOU/secrets/engram.env</string>
```

If you do edit the plist manually, reload the job with:

```bash
launchctl bootout gui/$(id -u)/com.engram.bridge
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.engram.bridge.plist
```

---

## Verifying everything works

```bash
# Service registered?
launchctl list | grep engram

# Bridge healthy?
engram status

# Recent turn costs?
engram cost --month

# Tail the structured log?
engram logs

# Memory populated?
engram channels list
```

---

## Uninstall

The primary uninstall path is the built-in CLI command:

```bash
engram uninstall
```

It unloads the launchd bridge + nightly jobs, removes their plist files,
and then prompts separately for each optional action: delete `~/.engram/`,
uninstall the `engram` CLI, and review the Slack-app cleanup link.

Preview without changing anything:

```bash
engram uninstall --dry-run
```

Other modes:

- `engram uninstall --keep-data` — skip the `~/.engram/` delete prompt
- `engram uninstall --purge` — skip all prompts, delete everything
  (bridge jobs, plists, `~/.engram/`, CLI tool). Use with care.

### Manual fallback

If `engram uninstall` can't run (e.g. the CLI is already uninstalled):

```bash
domain="gui/$(id -u)"
launchctl bootout "$domain/com.engram.bridge" \
  || launchctl unload "$HOME/Library/LaunchAgents/com.engram.bridge.plist"
launchctl bootout "$domain/com.engram.v3.nightly" \
  || launchctl unload "$HOME/Library/LaunchAgents/com.engram.v3.nightly.plist"
rm -f "$HOME/Library/LaunchAgents/com.engram.bridge.plist"
rm -f "$HOME/Library/LaunchAgents/com.engram.v3.nightly.plist"

# Optional data and CLI cleanup
rm -rf "$HOME/.engram"
uv tool uninstall engram
```

The Slack app you created stays in your workspace until you delete it at
https://api.slack.com/apps.

---

## Troubleshooting

### First stop: `engram doctor`

Before manual debugging, run:

```bash
engram doctor
```

It runs 19 pre-flight checks — `uv`, `claude` CLI, Python version, config
file + permissions, Slack tokens (live `auth.test`), slash-command coverage
(recent bridge-log probe), Anthropic + Gemini API keys (live validation),
launchd jobs, MCP channel coverage, disk space, log directory — and prints
a Rich table with actionable hints for every `❌`. Exits `0` on a healthy
setup or only-warnings, `1` on any failure.

```bash
engram doctor --json       # machine-readable output for scripts / issue reports
```

If Engram is running but pauses on a Slack permission card, see
[`hitl.md`](hitl.md) — that page explains when human-in-the-loop prompts
fire, how timeouts and daily caps behave, and what to grep for in logs when
a card does not behave as expected.

If a newly added MCP is visible in Claude Code but not in Slack, see
[mcp.md](mcp.md) for the registration vs. channel-manifest model and the
`mcp.excluded_by_manifest` troubleshooting path. If you edited MCP or
project config and need the running channel to reload it without bouncing the
bridge, run `/engram new` in that channel or
`engram channels new <channel-id> --yes` on the host.

### `engram setup` reports tokens with the wrong prefix

Double-check which token you copied from where. Bot token starts with
`xoxb-`, app-level token starts with `xapp-`. The wizard warns but still
saves — fix by re-running `engram setup`.

### Bridge starts but never responds to DMs

Most common cause: the Slack app isn't installed to your workspace, or
interactivity/Socket Mode isn't enabled. Re-check
[slack-app-setup.md](slack-app-setup.md) — the manifest should handle
this automatically, but manual app configuration sometimes drifts.

Run `tail -f /tmp/engram.bridge.err.log` while DMing the bot. Socket Mode
auth errors show up immediately.

### Turn takes longer than 30s

Normal for the first turn after a cold start (cache warmup).
Subsequent turns in the same session typically land in 3–10s. If every
turn is slow, check `engram cost --today` — high per-turn cost often
means the agent is doing a lot of tool work.

### "semantic memory disabled — missing Gemini key"

You skipped the Gemini step in setup. Either:
- Edit `~/.engram/config.yaml` and add `embeddings.api_key: AIzaSy…` under
  `embeddings:`
- Or export `GEMINI_API_KEY=AIzaSy…` before starting the bridge

Restart with `launchctl kickstart gui/$(id -u)/com.engram.bridge`.

### Cost looks unexpectedly high

Check `engram cost --by-channel` to see which channel is spending. Each
channel has its own identity template and permission manifest — tool-heavy
templates (e.g. `owner-dm-full`) cost more per turn than restricted ones
(e.g. `safe`).

### MCP works in owner DM but not in a team channel

That is the expected default. Team channels stay restrictive even after you
register an MCP globally in `~/.claude.json`; new channels only start with
`engram-memory`.

Allow the server explicitly for that channel:

```bash
engram channels mcp allow C07TEAM123 camoufox
engram channels mcp list C07TEAM123
```

Or in Slack, from the target channel:

```text
/engram mcp allow camoufox
/engram mcp list
```

If `list` shows `Missing from ~/.claude.json`, the channel manifest is ready
but the server is not registered in the shared Claude inventory yet.

---

## Next steps

- Read [slack-app-setup.md](slack-app-setup.md) for the full Slack app
  manifest and token guide.
- Read [mcp.md](mcp.md) for the per-channel MCP policy, CLI, and Slack
  command reference.
- Read [memory-search-scoping.md](memory-search-scoping.md) to understand
  how per-channel memory isolation works.
- Read [m4-report.md](m4-report.md) for the HITL (human-in-the-loop)
  walkthrough.
