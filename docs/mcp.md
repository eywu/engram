# MCP Onboarding

Engram reuses Claude Code's MCP inventory, but there are multiple layers
between "I ran `claude mcp add ...`" and "this MCP is available in Slack."

## The Three Layers

### 1. Registration: Claude Code user inventory

Register the MCP in `~/.claude.json` under `mcpServers`.

- This is the inventory Claude Code itself uses.
- `claude mcp add ...` writes here.
- `~/.claude/mcp.json` is a deprecated path. Engram will migrate it if it
  still exists, but new installs should treat it as dead.

Example:

```json
{
  "mcpServers": {
    "camoufox": {
      "command": "uvx",
      "args": ["camoufox-browser[mcp]==0.1.1"]
    }
  }
}
```

### 2. Channel allow-list: Engram team manifests

Owner DMs use `setting_sources: [user]`, so they auto-discover registered
user MCPs from `~/.claude.json`.

Team channels are different. Engram starts them with strict MCP config, so
the channel manifest must explicitly allow each server:

`~/.engram/contexts/<channel-id>/.claude/channel-manifest.yaml`

Example:

```yaml
mcp_servers:
  allowed:
    - engram-memory
    - camoufox
```

If the server is registered in `~/.claude.json` but missing here, the MCP
will not appear in that team channel.

### 3. Tier and trust policy

Allow-listing a server answers "is this MCP available in this channel?"
Tier and trust policy answer "is Engram allowed to make that change?"

- `official` MCPs can be approved silently.
- `community-trusted` MCPs can be approved with notification.
- `unknown` MCPs require owner approval before Engram persists a new
  allow-list entry.

This applies when Engram itself is asked to modify a channel manifest.

## Why Didn’t My MCP Show Up?

Work through these in order:

1. Check `~/.claude.json`. If the server is not under `mcpServers`, Engram
   cannot discover it.
2. Run `engram doctor`. The MCP coverage check warns when a registered user
   MCP is allowed in no team channel manifests and points at the manifest
   path to edit.
3. If the missing MCP is only failing in a team channel, inspect that
   channel's manifest and confirm the server name appears under
   `mcp_servers.allowed`.
4. If Engram already started once, check the structured logs for
   `mcp.excluded_by_manifest`. That means the MCP was present in the user
   inventory but filtered out by the strict channel manifest.

Helpful commands:

```bash
engram doctor
engram channels list
engram channels show C07TEAM123
```

## Current Behavior

- Owner DMs auto-discover user MCPs from `~/.claude.json`.
- Team channels require explicit allow-list entries.
- `engram setup` shows the shared user inventory and warns when registered
  MCPs are not yet allowed in any existing team channel manifest.
