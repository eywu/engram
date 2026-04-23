# Installation

## Troubleshooting

Run `engram doctor` first when an Engram install fails to start or behaves like
it is silently dropping events. The command checks local binaries, Python,
`~/.engram/config.yaml`, Slack tokens, Anthropic and Gemini API keys, launchd
jobs, disk space, and log writability.

Use `engram doctor --json` for scripts or issue reports. A healthy setup exits
with `0`; any failed check exits with `1`. Warnings do not fail the command, but
their hints should still be reviewed before running Engram as a background
service.
