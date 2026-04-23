#!/usr/bin/env bash
# Install & load the Engram user-launchd service.
#
# Idempotent: safe to re-run after upgrades.
#
# What it does:
#   1. Resolves absolute paths to this repo + the uv binary
#   2. Renders launchd/com.engram.bridge.plist template into
#      ~/Library/LaunchAgents/com.engram.bridge.plist
#   3. Unloads any existing copy, then loads + starts the service
#   4. Waits briefly and confirms `engram.ready` appeared in the log
#
# Logs land at /tmp/engram.bridge.{out,err}.log

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO_ROOT/launchd/com.engram.bridge.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.engram.bridge.plist"
SERVICE_LABEL="com.engram.bridge"
READY_LOG="/tmp/engram.bridge.out.log"
NIGHTLY_PLIST_SRC="$REPO_ROOT/launchd/com.engram.v3.nightly.plist"
NIGHTLY_PLIST_DST="$HOME/Library/LaunchAgents/com.engram.v3.nightly.plist"
NIGHTLY_SERVICE_LABEL="com.engram.v3.nightly"

MODE="bridge"
case "${1:-}" in
    --install-nightly)
        MODE="nightly"
        shift
        ;;
    --install-bridge|"")
        [[ "${1:-}" == "--install-bridge" ]] && shift
        ;;
    --help|-h)
        echo "usage: $0 [--install-bridge|--install-nightly]"
        exit 0
        ;;
    *)
        echo "usage: $0 [--install-bridge|--install-nightly]" >&2
        exit 2
        ;;
esac

if [[ $# -gt 0 ]]; then
    echo "usage: $0 [--install-bridge|--install-nightly]" >&2
    exit 2
fi

# 1. Required binaries
UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
    echo "error: uv not found on PATH. Install with:"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

escape_sed_replacement() {
    printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

install_nightly() {
    local wrapper="$REPO_ROOT/scripts/engram_nightly_launchd.sh"
    local domain="gui/$(id -u)"

    if [[ ! -x "$wrapper" ]]; then
        echo "error: nightly wrapper is not executable: $wrapper" >&2
        exit 1
    fi

    echo "==> repo:      $REPO_ROOT"
    echo "==> uv:        $UV_BIN"
    echo "==> plist dst: $NIGHTLY_PLIST_DST"

    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.engram/logs"
    sed \
        -e "s|/REPLACE/WITH/ABSOLUTE/PATH/TO/uv|$(escape_sed_replacement "$UV_BIN")|g" \
        -e "s|/REPLACE/WITH/ABSOLUTE/PATH/TO/engram-repo|$(escape_sed_replacement "$REPO_ROOT")|g" \
        -e "s|/REPLACE/WITH/ABSOLUTE/PATH/TO/engram/scripts/engram_nightly_launchd.sh|$(escape_sed_replacement "$wrapper")|g" \
        -e "s|/REPLACE/WITH/HOME|$(escape_sed_replacement "$HOME")|g" \
        "$NIGHTLY_PLIST_SRC" > "$NIGHTLY_PLIST_DST"
    echo "==> wrote $NIGHTLY_PLIST_DST"

    if launchctl list | grep -q "$NIGHTLY_SERVICE_LABEL"; then
        echo "==> unloading existing nightly job…"
        launchctl bootout "$domain/$NIGHTLY_SERVICE_LABEL" 2>/dev/null \
            || launchctl bootout "$domain" "$NIGHTLY_PLIST_DST" 2>/dev/null \
            || launchctl unload "$NIGHTLY_PLIST_DST" 2>/dev/null \
            || true
        sleep 1
    fi

    echo "==> loading nightly job…"
    launchctl bootstrap "$domain" "$NIGHTLY_PLIST_DST" 2>/dev/null \
        || launchctl load "$NIGHTLY_PLIST_DST"
    launchctl enable "$domain/$NIGHTLY_SERVICE_LABEL" 2>/dev/null || true

    if ! launchctl list | grep -q "$NIGHTLY_SERVICE_LABEL"; then
        echo "error: nightly job did not register with launchctl" >&2
        exit 1
    fi

    echo ""
    echo "✓ nightly job loaded: $NIGHTLY_SERVICE_LABEL"
    echo ""
    echo "Schedule: daily at 02:00 local; wrapper adds --weekly on Mondays."
    echo "Manual run:"
    echo "    launchctl kickstart $domain/$NIGHTLY_SERVICE_LABEL"
    echo "Logs:"
    echo "    tail -f $HOME/.engram/logs/nightly-stdio-\$(date +%F).log"
}

if [[ "$MODE" == "nightly" ]]; then
    install_nightly
    exit 0
fi

echo "==> repo:      $REPO_ROOT"
echo "==> uv:        $UV_BIN"
echo "==> plist dst: $PLIST_DST"

# 2. Render the plist
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_DST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$SERVICE_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string>
        <string>run</string>
        <string>--project</string>
        <string>$REPO_ROOT</string>
        <string>python</string>
        <string>-m</string>
        <string>engram.main</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>/tmp/engram.bridge.out.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/engram.bridge.err.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST
echo "==> wrote $PLIST_DST"

# 3. Reload the service
if launchctl list | grep -q "$SERVICE_LABEL"; then
    echo "==> unloading existing service…"
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    sleep 1
fi

echo "==> loading service…"
launchctl load "$PLIST_DST"

# 4. Confirm it's running. Poll — launchctl list can lag by a second or two.
PID=""
for i in $(seq 1 10); do
    sleep 0.5
    LINE="$(launchctl list | grep "$SERVICE_LABEL" | head -1 || true)"
    if [[ -n "$LINE" ]]; then
        PID="$(echo "$LINE" | awk '{print $1}')"
        if [[ "$PID" != "-" && -n "$PID" ]]; then
            break
        fi
    fi
done

if [[ -z "$LINE" ]]; then
    echo "error: service did not register with launchctl"
    exit 1
fi
if [[ "$PID" == "-" || -z "$PID" ]]; then
    echo "error: service registered but has no PID. Check /tmp/engram.bridge.err.log"
    tail -20 /tmp/engram.bridge.err.log 2>/dev/null || true
    exit 1
fi

echo "==> service pid: $PID"

# Wait up to 20s for engram.ready
echo "==> waiting for engram.ready…"
for i in $(seq 1 40); do
    if grep -q "engram.ready" "$READY_LOG" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

if ! grep -q "engram.ready" "$READY_LOG" 2>/dev/null; then
    echo "warning: engram.ready not seen in $READY_LOG after 20s"
    echo "--- last 20 lines of out.log ---"
    tail -20 "$READY_LOG" 2>/dev/null || echo "(no out.log yet)"
    echo "--- last 20 lines of err.log ---"
    tail -20 /tmp/engram.bridge.err.log 2>/dev/null || echo "(no err.log)"
    exit 1
fi

echo ""
echo "✓ service up and ready. pid=$PID"
echo ""
echo "Logs:"
echo "    tail -f /tmp/engram.bridge.out.log"
echo "    tail -f /tmp/engram.bridge.err.log"
echo ""
echo "To stop:       launchctl unload $PLIST_DST"
echo "To uninstall:  launchctl unload $PLIST_DST && rm $PLIST_DST"
