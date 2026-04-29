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
