# Launchd Smoke Test

`launchd/com.engram.v3.smoketest.plist` runs `python -m engram.smoketest` as a
manual one-shot probe. Install it into `~/Library/LaunchAgents/`, edit the
placeholder paths, then run:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.engram.v3.smoketest.plist
launchctl kickstart gui/$(id -u)/com.engram.v3.smoketest
```

The smoke script writes structured JSONL to
`~/.engram/logs/smoketest-<date>.jsonl` and records the turn in the budget
ledger under the disposable `__smoke__` channel.

Doc note for GRO-436: nightly plist inherits this env pattern + HITL-disabled
config.
