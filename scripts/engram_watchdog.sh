#!/bin/sh
set -eu

STATE_DIR="${ENGRAM_STATE_DIR:-$HOME/.engram/state}"
FAILURES_FILE="$STATE_DIR/watchdog.failures"
BRIDGE_LABEL="${ENGRAM_BRIDGE_LABEL:-com.engram.v3.bridge}"
ENGRAM_BIN="${ENGRAM_BIN:-engram}"

mkdir -p "$STATE_DIR"

if "$ENGRAM_BIN" health >/dev/null 2>&1; then
  printf '0\n' > "$FAILURES_FILE"
  exit 0
fi

failures=0
if [ -f "$FAILURES_FILE" ]; then
  failures="$(cat "$FAILURES_FILE" 2>/dev/null || printf '0')"
fi
case "$failures" in
  ''|*[!0-9]*) failures=0 ;;
esac
failures=$((failures + 1))
printf '%s\n' "$failures" > "$FAILURES_FILE"

if [ "$failures" -ge 3 ]; then
  launchctl kickstart -k "gui/$UID/$BRIDGE_LABEL"
  printf '0\n' > "$FAILURES_FILE"
fi
