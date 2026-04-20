#!/usr/bin/env bash
# Engram install script.
#
# Installs Python 3.12+ via uv, sets up the project venv, and installs the
# `engram` CLI as a user tool.
#
# Prerequisite: you have `uv` (https://github.com/astral-sh/uv) and the
# Claude Code CLI (https://docs.claude.com/en/docs/claude-code) installed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Engram install starting in $REPO_ROOT"

# 1. Check uv.
if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv not found. Install with:"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
echo "==> uv found: $(uv --version)"

# 2. Check claude CLI (M0-F1).
if ! command -v claude >/dev/null 2>&1; then
    echo "warning: claude CLI not found on PATH."
    echo "         Install with: npm i -g @anthropic-ai/claude-code"
    echo "         Engram will fail at runtime without it."
else
    echo "==> claude CLI found: $(claude --version 2>&1 | head -1)"
fi

# 3. Install Python if needed.
uv python install 3.12 >/dev/null 2>&1 || true

# 4. Sync project deps.
echo "==> syncing project dependencies…"
uv sync --extra dev

# 5. Install the engram CLI globally for the user.
echo "==> installing engram CLI as a uv tool…"
uv tool install --from "$REPO_ROOT" engram --reinstall || uv tool install --from "$REPO_ROOT" engram

ENGRAM_PATH="$(command -v engram || true)"
if [[ -z "$ENGRAM_PATH" ]]; then
    echo "warning: engram CLI not on PATH yet. Try:"
    echo "    uv tool update-shell"
    echo "    exec \$SHELL"
else
    echo "==> engram installed at: $ENGRAM_PATH"
fi

echo ""
echo "Done."
echo ""
echo "Next steps:"
echo "    1. engram setup      # interactive config wizard"
echo "    2. engram status     # verify config"
echo "    3. engram run        # start the bridge (foreground)"
echo ""
