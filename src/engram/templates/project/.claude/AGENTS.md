# AGENTS.md — Project-Level Rules

*Universal rules every Engram channel follows. Channel manifests can add
constraints on top of this but cannot relax these rules.*

---

## Tool Use

- **Prefer reading over writing.** When a task can be answered by reading
  files or searching, do that before modifying anything.
- **Don't run destructive commands without explicit user intent.** Deletes,
  force-pushes, dropping tables, revoking access — ask first if in doubt.
- **Check before you install.** Verify package names and versions before
  running `pip install` / `npm install`. Bad installs can pull unexpected
  dependencies.

## Tool Results

- **Quote sources.** When you cite a file, include the path. When you cite a
  URL, include it.
- **Don't invent output.** If a tool call fails or returns nothing useful,
  say so.

## Data Handling

- **Don't echo credentials.** API keys, tokens, passwords — treat as
  radioactive. If you need to reference a credential, describe where it
  lives (e.g. "the value in `~/.engram/config.yaml`"), never the value.
- **Scope MCP calls appropriately.** Only call MCPs with data the channel's
  operator has consented to.

## Memory

- Every channel has its own persistent memory. Write to it when you learn
  something worth remembering.
- Don't assume the operator remembers prior context from your side. If
  something matters, surface it.

## Error Handling

- When a tool call fails, read the error, explain it plainly, and propose a
  next step.
- Don't retry blindly. If a call failed three times with the same error,
  stop and report.

---

*The operator maintains this file. Channel manifests in `contexts/*/.claude/`
can layer additional rules specific to a channel's purpose.*
