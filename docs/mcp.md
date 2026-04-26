# Per-Channel MCP Access

Engram uses a hybrid policy for MCP access:

- Owner DMs inherit the operator's user-level MCP inventory from `~/.claude.json`.
- New team channels stay restrictive by default.
- The shared-channel baseline still starts with `engram-memory` only.
- Per-channel exceptions are managed through first-class CLI and Slack commands, not hand-edited YAML.

This records the `GRO-531` decision: option **C**.

## Why the default stays restrictive

Team channels use `setting_sources: [project]` plus strict MCP config on
purpose. Registering a new MCP globally should not silently expand tool reach
in shared Slack rooms.

The escape hatch is explicit per-channel allow/deny management:

- `engram channels mcp allow <channel-id> <server>`
- `engram channels mcp deny <channel-id> <server>`
- `engram channels mcp list <channel-id>`
- `/engram mcp allow <server>`
- `/engram mcp deny <server>`
- `/engram mcp list`

## CLI

Use the CLI on the host running the bridge:

```bash
engram channels mcp list C07TEAM123
engram channels mcp allow C07TEAM123 camoufox
engram channels mcp deny C07TEAM123 camoufox
```

`list` shows:

- the channel tier
- whether the manifest is in `inherit-all` or `allow-list` mode
- declared `allowed` and `disallowed` values
- the effective MCP servers after both lists are applied
- any allow-listed servers that are still missing from `~/.claude.json`

## Slack

Use the slash command in the target channel:

```text
/engram mcp list
/engram mcp allow camoufox
/engram mcp deny camoufox
```

`allow` mirrors the `/engram upgrade` safety posture:

- granting MCP access is owner-only
- denying MCP access is allowed for anyone, because it only reduces access
- `list` is read-only and available to anyone in the channel

## Notes

- These commands update `mcp_servers.allowed` and `mcp_servers.disallowed`
  in the channel manifest. They do not change the channel tier.
- Safe channels can remain `safe` while gaining one specific MCP server.
- If a server is missing from `~/.claude.json`, allowing it updates the
  manifest but it will not become effective until the shared Claude inventory
  contains that server.
