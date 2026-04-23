#!/usr/bin/env bash
# launchd wrapper for the scheduled Engram nightly job.

set -euo pipefail

REPO_ROOT="${ENGRAM_REPO_ROOT:?ENGRAM_REPO_ROOT is required}"
UV_BIN="${ENGRAM_UV_BIN:?ENGRAM_UV_BIN is required}"
LOG_DIR="${ENGRAM_LOG_DIR:-$HOME/.engram/logs}"

mkdir -p "$LOG_DIR"
STDIO_LOG="$LOG_DIR/nightly-stdio-$(date +%F).log"
exec >> "$STDIO_LOG" 2>&1

args=(
    "$UV_BIN"
    "run"
    "--project"
    "$REPO_ROOT"
    "engram"
    "nightly"
    "--verbose"
)

if [[ "${ENGRAM_NIGHTLY_DRY_RUN:-}" == "1" ]]; then
    args+=("--dry-run")
fi

if [[ "${ENGRAM_NIGHTLY_FORCE_WEEKLY:-}" == "1" || "$(date +%u)" == "1" ]]; then
    args+=("--weekly")
fi

exec "${args[@]}"
