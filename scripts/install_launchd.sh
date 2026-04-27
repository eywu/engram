#!/usr/bin/env bash
# Install & load the Engram user-launchd service.
#
# Idempotent: safe to re-run after upgrades.
#
# What it does:
#   1. Resolves absolute paths to this repo + the uv binary
#   2. Renders the canonical launchd/com.engram.bridge.plist template into
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
DEFAULT_BRIDGE_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

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

resolve_absolute_path() {
    local path="$1"

    if [[ "$path" == "~/"* ]]; then
        path="$HOME/${path#"~/"}"
    elif [[ "$path" == "~" ]]; then
        path="$HOME"
    fi

    if [[ "$path" != /* ]]; then
        path="$PWD/$path"
    fi

    local dir
    dir="$(cd "$(dirname "$path")" && pwd)"
    printf '%s/%s\n' "$dir" "$(basename "$path")"
}

resolve_bridge_env_file() {
    local candidate

    if [[ -n "${ENGRAM_ENV_FILE:-}" ]]; then
        candidate="$(resolve_absolute_path "$ENGRAM_ENV_FILE")"
        if [[ ! -f "$candidate" ]]; then
            echo "error: ENGRAM_ENV_FILE is set but does not exist: $candidate" >&2
            echo "Set ENGRAM_ENV_FILE to a readable .env file or run 'engram setup'." >&2
            exit 1
        fi
        printf '%s\n' "$candidate"
        return
    fi

    for candidate in "$HOME/.engram/.env" "$REPO_ROOT/.env"; do
        if [[ -f "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done

    echo "error: could not find an Engram env file for launchd." >&2
    echo "Set ENGRAM_ENV_FILE to your secrets file, or run 'engram setup' to create ~/.engram/.env before installing launchd." >&2
    exit 1
}

list_node_dependent_mcps() {
    python3 - "$HOME/.claude.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
if not path.exists():
    raise SystemExit(0)

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

servers = payload.get("mcpServers")
if not isinstance(servers, dict):
    raise SystemExit(0)

for name, config in servers.items():
    if not isinstance(name, str) or not isinstance(config, dict):
        continue
    command = str(config.get("command") or "").strip()
    if command in {"npx", "node"}:
        print(f"{name}\t{command}")
PY
}

resolve_nvm_node_bin_dir() {
    local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
    local nvm_sh="$nvm_dir/nvm.sh"
    local current=""
    local candidate=""

    if [[ ! -s "$nvm_sh" ]]; then
        return 0
    fi

    current="$(
        NVM_DIR="$nvm_dir" bash -lc '
            if [[ -s "$NVM_DIR/nvm.sh" ]]; then
                . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
                nvm current 2>/dev/null
            fi
        ' 2>/dev/null | tail -n 1 | tr -d '\r'
    )"
    if [[ -z "$current" || "$current" == "none" || "$current" == "system" ]]; then
        return 0
    fi

    candidate="$nvm_dir/versions/node/$current/bin"
    if [[ -x "$candidate/npx" || -x "$candidate/node" ]]; then
        printf '%s\n' "$candidate"
    fi
}

resolve_login_shell_node_bin_dir() {
    local shell_bin="${SHELL:-/bin/zsh}"
    local resolved=""

    resolved="$("$shell_bin" -lic 'command -v npx || command -v node' 2>/dev/null | head -n 1 || true)"
    if [[ -n "$resolved" ]]; then
        dirname "$resolved"
    fi
}

resolve_node_bin_dir() {
    local detected=""

    detected="$(resolve_nvm_node_bin_dir)"
    if [[ -n "$detected" ]]; then
        printf '%s\n' "$detected"
        return
    fi

    detected="$(resolve_login_shell_node_bin_dir)"
    if [[ -n "$detected" ]]; then
        printf '%s\n' "$detected"
    fi
}

path_contains_dir() {
    local path_value="$1"
    local candidate="$2"
    [[ ":$path_value:" == *":$candidate:"* ]]
}

build_bridge_path() {
    local node_bin_dir="$1"

    if [[ -n "$node_bin_dir" ]] && ! path_contains_dir "$DEFAULT_BRIDGE_PATH" "$node_bin_dir"; then
        printf '%s:%s\n' "$node_bin_dir" "$DEFAULT_BRIDGE_PATH"
        return
    fi

    printf '%s\n' "$DEFAULT_BRIDGE_PATH"
}

command_resolves_in_path() {
    local path_value="$1"
    local command_name="$2"
    local dir=""
    local -a parts=()

    IFS=':' read -r -a parts <<< "$path_value"
    for dir in "${parts[@]}"; do
        if [[ -x "$dir/$command_name" ]]; then
            return 0
        fi
    done
    return 1
}

require_node_for_mcps_if_needed() {
    local bridge_path="$1"
    local found_configured=0
    local name=""
    local command_name=""

    while IFS=$'\t' read -r name command_name; do
        found_configured=1
        if command_resolves_in_path "$bridge_path" "$command_name"; then
            continue
        fi

        echo "error: Found ${command_name}-based MCP \`$name\` in ~/.claude.json but ${command_name} is not on a stable path. Install Node via Homebrew (\`brew install node\`) or run \`nvm use --lts\` and rerun this installer." >&2
        exit 1
    done < <(list_node_dependent_mcps)

    if [[ "$found_configured" -eq 0 ]]; then
        return
    fi
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

BRIDGE_ENV_FILE="$(resolve_bridge_env_file)"
NODE_BIN_DIR="$(resolve_node_bin_dir)"
BRIDGE_PATH="$(build_bridge_path "${NODE_BIN_DIR:-}")"
require_node_for_mcps_if_needed "$BRIDGE_PATH"

echo "==> env file:  $BRIDGE_ENV_FILE"
if [[ -n "${NODE_BIN_DIR:-}" && "$BRIDGE_PATH" != "$DEFAULT_BRIDGE_PATH" ]]; then
    echo "==> node bin:  $NODE_BIN_DIR"
fi

# 2. Render the plist
mkdir -p "$HOME/Library/LaunchAgents"
sed \
    -e "s|/REPLACE/WITH/ABSOLUTE/PATH/TO/uv|$(escape_sed_replacement "$UV_BIN")|g" \
    -e "s|/REPLACE/WITH/ABSOLUTE/PATH/TO/engram-repo|$(escape_sed_replacement "$REPO_ROOT")|g" \
    -e "s|/REPLACE/WITH/HOME|$(escape_sed_replacement "$HOME")|g" \
    -e "s|/REPLACE/WITH/ABSOLUTE/PATH/TO/engram.env|$(escape_sed_replacement "$BRIDGE_ENV_FILE")|g" \
    -e "s|/REPLACE/WITH/OPTIONAL/NODE/PATH/PREFIX/|$(escape_sed_replacement "${BRIDGE_PATH%"$DEFAULT_BRIDGE_PATH"}")|g" \
    "$PLIST_SRC" > "$PLIST_DST"
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
