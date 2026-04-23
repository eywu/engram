# Installation

## Uninstall

Use the Engram CLI as the primary uninstall path:

```bash
engram uninstall
```

The command unloads the user launchd bridge and nightly jobs, removes their
plist files, and then asks whether to delete local Engram data, uninstall the
CLI, and review the manual Slack app cleanup link.

Preview the uninstall plan without changing anything:

```bash
engram uninstall --dry-run
```

Example dry-run output:

```text
Dry run: no changes will be made.
This will remove Engram from your system.

  ✓ unload launchd bridge job (com.engram.bridge)
  ✓ unload launchd nightly job (com.engram.v3.nightly)
  ✓ remove launchd plist files

Optional:
  [ ] delete ~/.engram/ (config, memory DB, logs, ~230 MB)
  [ ] uninstall the `engram` CLI (uv tool uninstall engram)
  [ ] remove your Slack app (NOT automated — you'll get a link)

Commands that would run:
  launchctl bootout gui/$(id -u)/com.engram.bridge
  launchctl unload ~/Library/LaunchAgents/com.engram.bridge.plist  # fallback
  launchctl bootout gui/$(id -u)/com.engram.v3.nightly
  launchctl unload ~/Library/LaunchAgents/com.engram.v3.nightly.plist  # fallback
  rm -f ~/Library/LaunchAgents/com.engram.bridge.plist
  rm -f ~/Library/LaunchAgents/com.engram.v3.nightly.plist
  # prompt before deleting ~/.engram/
  # prompt before running: uv tool uninstall engram
  # Slack app cleanup is manual: https://api.slack.com/apps
```

Other modes:

```bash
engram uninstall --keep-data
engram uninstall --purge
```

`--keep-data` skips the `~/.engram/` delete prompt. `--purge` skips prompts,
deletes local Engram data, removes launchd jobs and plist files, and runs
`uv tool uninstall engram`.

Manual fallback:

```bash
domain="gui/$(id -u)"
launchctl bootout "$domain/com.engram.bridge" \
  || launchctl unload "$HOME/Library/LaunchAgents/com.engram.bridge.plist"
launchctl bootout "$domain/com.engram.v3.nightly" \
  || launchctl unload "$HOME/Library/LaunchAgents/com.engram.v3.nightly.plist"
rm -f "$HOME/Library/LaunchAgents/com.engram.bridge.plist"
rm -f "$HOME/Library/LaunchAgents/com.engram.v3.nightly.plist"

# Optional data and CLI cleanup:
rm -rf "$HOME/.engram"
uv tool uninstall engram
```

Slack app cleanup remains manual. Open https://api.slack.com/apps, select the
Engram app, and remove it from the workspace if desired.

## Troubleshooting

Run `engram doctor` first when an Engram install fails to start or behaves like
it is silently dropping events. The command checks local binaries, Python,
`~/.engram/config.yaml`, Slack tokens, Anthropic and Gemini API keys, launchd
jobs, disk space, and log writability.

Use `engram doctor --json` for scripts or issue reports. A healthy setup exits
with `0`; any failed check exits with `1`. Warnings do not fail the command, but
their hints should still be reviewed before running Engram as a background
service.

If Engram is running but pauses on a Slack permission card, see
[`docs/hitl.md`](hitl.md). That page explains when human-in-the-loop prompts
appear, how timeouts and daily caps work, and what to grep for in logs when a
card does not behave the way you expect.
