# Test environment

Run the suite with:

```sh
uv run pytest
```

Tests that exercise Claude MCP inventory behavior should create their own
temporary `.claude.json` and set `HOME` to that temporary directory with
`monkeypatch`. The suite must not depend on the developer or CI runner's real
`~/.claude.json`.

Tests that exercise Node-backed MCP launchd behavior should provide fake `npx`
or shell discovery through their fixtures. The suite must not depend on whether
the host machine or CI image happens to have `npx` on `/usr/bin`,
`/usr/local/bin`, or Homebrew paths.

## `ENGRAM_BRIDGE_PATH_OVERRIDE` (test-only)

`scripts/install_launchd.sh` reads `ENGRAM_BRIDGE_PATH_OVERRIDE` to replace
its hardcoded `DEFAULT_BRIDGE_PATH`. This is a test-only escape hatch so the
install script can be exercised against a hermetic, controlled bridge path
rather than whatever the host filesystem happens to provide.

`tests/test_smoketest.py::_bridge_install_fixture` sets this to an empty
temp directory, which means:

- Tests that need `npx` reachable on the bridge path supply node via
  `PATH` manipulation or `NVM_DIR` (going through the script's shell-based
  resolution path), not by relying on `/usr/bin/npx` existing on the host.
- Tests that assert `npx` is unreachable (e.g.,
  `test_install_launchd_bridge_fails_when_npx_mcp_has_no_reachable_node_runtime`)
  are isolated from the runner's pre-installed Node — important on
  GitHub-hosted Linux runners which ship `npx` at `/usr/bin/npx`.

**Real users should never set `ENGRAM_BRIDGE_PATH_OVERRIDE`.** It exists
solely to make the test fixture independent of host state.
